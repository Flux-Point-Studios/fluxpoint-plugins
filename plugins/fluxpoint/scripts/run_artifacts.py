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


# ---- where a run's earlier records can be -----------------------------------
#
# A record is accepted only when the graph and the Evidence file really sit
# under its root. Whichever root an earlier record of the same file was given,
# its store is therefore under a directory above that file, and reading every
# such store finds it. The readers below only add to what is read, and every
# decision made from them only refuses or lowers, so reading one store more
# can never file a run that reading one store fewer refused.

def store(root):
    return os.path.join(str(root), '.claude', 'fluxpoint', 'runs')


def stores(root, *files, shared=None):
    """Every runs directory that can hold an earlier record of a run about these files.

    The store under `root`, the store of every directory above each file
    once its links are followed, and `shared`: the clone-wide store when the
    caller has one. That last one is extra. It moves with FPL_MEMORY_HOME,
    so nothing is decided on its being there.
    """
    found = [store(root)]
    for named in files:
        if not named:
            continue
        current = os.path.dirname(os.path.realpath(str(named)))
        while True:
            found.append(store(current))
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    found += [path for path in (shared or ()) if path]
    unique = []
    for path in found:
        real = os.path.realpath(path)
        if real not in unique:
            unique.append(real)
    return unique


def _safe(run_id):
    try:
        return _name(run_id)
    except ValueError:
        return None


def records(run_stores, run_id):
    """Each file an attempt of this run id may be in: per store, the latest and its history."""
    paths = []
    if not _safe(run_id):
        return paths
    for runs in run_stores:
        paths.append(os.path.join(runs, run_id + '.json'))
        history = os.path.join(runs, 'history', run_id)
        try:
            names = sorted(os.listdir(history))
        except OSError:
            continue
        paths += [os.path.join(history, name) for name in names if name.endswith('.json')]
    return paths


def absent(error):
    """An OSError that says nothing is there: no file, or a file where a directory was expected."""
    return isinstance(error, (FileNotFoundError, NotADirectoryError))


def lineage(run_stores, run_id, source_id=None):
    """Run ids joined to this one by a resume, this one first.

    A resumed run that gets an id of its own names the run it was resumed
    from, so the runs of one campaign form a tree. The links are read from
    every attempt on file, the latest and the history: a later attempt
    under a run id may name no source. Only ids that are safe file names
    are followed.
    """
    links = {}

    def join(child, source):
        if _safe(child) and _safe(source) and child != source:
            links.setdefault(child, set()).add(source)
            links.setdefault(source, set()).add(child)

    def read(path, fallback):
        try:
            with open(path, 'rb') as handle:
                raw = handle.read()
            if b'"resumeSource"' not in raw:
                return
            row = json.loads(raw.decode('utf-8'))
        except (OSError, ValueError, RecursionError):
            return
        summary = row.get('summary') if isinstance(row, dict) else None
        source = summary.get('resumeSource') if isinstance(summary, dict) else None
        join(row.get('runId') if isinstance(row.get('runId'), str) else fallback,
             source.get('runId') if isinstance(source, dict) else None)

    for runs in run_stores:
        try:
            names = sorted(os.listdir(runs))
        except OSError:
            continue
        for name in names:
            if name.endswith('.json'):
                read(os.path.join(runs, name), name[:-5])
        history = os.path.join(runs, 'history')
        try:
            ids = sorted(os.listdir(history))
        except OSError:
            continue
        for held in ids:
            try:
                snapshots = sorted(os.listdir(os.path.join(history, held)))
            except OSError:
                continue
            for name in snapshots:
                if name.endswith('.json'):
                    read(os.path.join(history, held, name), held)
    join(run_id, source_id)
    seen, queue = [run_id], [run_id]
    while queue:
        for other in sorted(links.get(queue.pop(0), ())):
            if other not in seen:
                seen.append(other)
                queue.append(other)
    return seen
