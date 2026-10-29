"""Canonical latest run records with immutable, full-hash attempt snapshots."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile


def _name(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', value)
            or value.endswith('.') or value.split('.')[0].upper() in
            ({'CON', 'PRN', 'AUX', 'NUL'} | {'COM' + str(i) for i in range(1, 10)}
             | {'LPT' + str(i) for i in range(1, 10)})):
        raise ValueError('invalid run id')
    return value


def _path(root, run_id, digest=None):
    base = Path(root).resolve() / '.claude/fluxpoint/runs'
    path = base / (_name(run_id) + '.json')
    if digest is not None:
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('invalid run artifact SHA-256')
        path = base / 'history' / run_id / (digest + '.json')
    # Windows resolve(strict=False) may leave a dangling symlink unresolved.
    # Artifact entries are regular files/directories, so reject links below the
    # canonical store before reading, snapshotting or creating anything there.
    # lstat also identifies Windows junctions through their reparse attribute.
    current = base
    for component in path.relative_to(base).parts:
        current = current / component
        try:
            entry = current.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(entry.st_mode) or
                getattr(entry, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('run artifact escapes canonical run store through a link')
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('run artifact escapes canonical run store')
    return path


def read(root, run_id, artifact_sha256=None):
    path = _path(root, run_id, artifact_sha256)
    if artifact_sha256 and not path.exists():
        # Existing records from before history support are eligible only while
        # their latest pointer still contains precisely these bytes.
        path = _path(root, run_id)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if artifact_sha256 is not None and digest != artifact_sha256:
        raise ValueError('run artifact SHA-256 mismatch: ' + run_id)
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate JSON key in source artifact: ' + key)
            out[key] = value
        return out
    def finite(text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError('non-finite JSON number in source artifact')
        return number
    row = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_float=finite, parse_constant=finite)
    if not isinstance(row, dict) or row.get('runId') != run_id or not isinstance(row.get('summary'), dict):
        raise ValueError('source artifact has no matching run id and summary')
    return row, digest


def _snapshot(root, run_id, data):
    digest = hashlib.sha256(data).hexdigest()
    path = _path(root, run_id, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('immutable run artifact changed: ' + str(path))
        return digest
    fd, temporary = tempfile.mkstemp(prefix='.attempt-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError('immutable run artifact changed: ' + str(path))
    finally:
        os.unlink(temporary)
    return digest


def write(root, run_id, row):
    latest = _path(root, run_id)
    if not isinstance(row, dict) or row.get('runId') != run_id or not isinstance(row.get('summary'), dict):
        raise ValueError('run artifact must contain matching run id and summary')
    latest.parent.mkdir(parents=True, exist_ok=True)
    if latest.exists():
        _snapshot(root, run_id, latest.read_bytes())
    data = (json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8', 'backslashreplace')
    digest = _snapshot(root, run_id, data)
    fd, temporary = tempfile.mkstemp(prefix='.latest-', dir=str(latest.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        os.replace(temporary, latest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(latest), digest
