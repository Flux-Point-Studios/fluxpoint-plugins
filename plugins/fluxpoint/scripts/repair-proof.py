#!/usr/bin/env python3
"""Fixed-command witness and fresh base-archive snapshots for repair review.

The Workflow sandbox cannot read files. Its independent sentinel invokes this
helper, returns raw bytes, and the compiled JS derives the digest and hunks.
The recorder later re-reads the actual file; neither model's digest is trusted.
"""
import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from repair_diff import manifest


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True).stdout


def read(root, base, patch, item, round_):
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', base):
        raise ValueError('base must be a resolved launch commit')
    if git(root, 'rev-parse', '--verify', base + '^{commit}').decode().strip() != base:
        raise ValueError('base identity mismatch')
    path = Path(patch)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('patch must be a live regular absolute file')
    data = path.read_bytes()
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('repair patch exceeds the 16 MiB review witness limit')
    # Validate encoding/hunk syntax here as well; JS independently repeats it.
    manifest(data)
    return dict(status='READ', base=base, patch=str(path), item=item, round=round_,
                rawPatch=base64.b64encode(data).decode('ascii'))


def materialize(root, base, patch, digest, item, round_):
    witness = read(root, base, patch, item, round_)
    data = base64.b64decode(witness['rawPatch'])
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('patch changed since the harness read')
    package = git(root, 'archive', base)
    parent = Path(tempfile.mkdtemp(prefix='fluxpoint-repair-proof-'))
    snapshot = parent / 'tree'
    snapshot.mkdir()
    try:
        with tarfile.open(fileobj=io.BytesIO(package)) as archive:
            for entry in archive.getmembers():
                name, target = Path(entry.name), Path(entry.linkname)
                if name.is_absolute() or '..' in name.parts or entry.isdev():
                    raise ValueError('unsafe base archive entry')
                if (entry.issym() or entry.islnk()) and (target.is_absolute() or '..' in target.parts):
                    raise ValueError('escaping base archive link')
            archive.extractall(snapshot)
        # Apply the bytes already read, not a mutable path observed twice.
        applied = subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], cwd=snapshot,
                                 input=data, capture_output=True)
        if applied.returncode:
            raise ValueError('patch cannot apply to fresh launch-base archive: ' + applied.stderr.decode(errors='replace'))
        token = secrets.token_hex(32)
        info = dict(status='READY', snapshot=str(snapshot), token=token, base=base,
                    patchSha256=digest, item=item, round=round_)
        (parent / 'witness.json').write_text(json.dumps(info), encoding='utf-8')
        return info
    except BaseException:
        shutil.rmtree(parent)
        raise


def snapshot_info(snapshot, token):
    path = Path(snapshot)
    if (not path.is_absolute() or path.name != 'tree' or path.is_symlink() or path.parent.is_symlink()
            or not path.parent.name.startswith('fluxpoint-repair-proof-')
            or path.parent.parent.resolve() != Path(tempfile.gettempdir()).resolve()):
        raise ValueError('snapshot is not an owned repair archive')
    info = json.loads((path.parent / 'witness.json').read_text(encoding='utf-8'))
    if not isinstance(token, str) or not secrets.compare_digest(info.get('token', ''), token) or info.get('snapshot') != str(path):
        raise ValueError('snapshot ownership token mismatch')
    return path, info


def cleanup(snapshot, token):
    path, _ = snapshot_info(snapshot, token)
    if (path.parent / 'active-command').exists():
        raise ValueError('snapshot has an undrained or unconfirmed command tree; preserve it')
    log = path.parent / 'observations.jsonl'
    observations = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []
    shutil.rmtree(path.parent)
    if path.parent.exists():
        raise ValueError('snapshot cleanup could not be confirmed')
    return dict(status='CLEANED', snapshot=snapshot, token=token, observations=observations)


def execute(snapshot, token, control, attack, timeout, series=None):
    path, info = snapshot_info(snapshot, token)
    spec = importlib.util.spec_from_file_location('repair_replay', Path(__file__).with_name('repair-replay.py'))
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    marker = path.parent / 'active-command'
    fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    drained = True
    previous_series = os.environ.get('FPL_PRODUCTION_SERIES')
    try:
        series_data = None
        if series:
            series_path = Path(series)
            if not series_path.is_absolute():
                raise ValueError('production series path must be absolute')
            series_data = series_path.read_bytes()
            pinned_series = path.parent / 'production-series.json'
            pinned_series.write_bytes(series_data)
            os.environ['FPL_PRODUCTION_SERIES'] = str(pinned_series)
        start = time.monotonic()
        rc, control_result = replay.execute(control, path, timeout)
        control_ms = (time.monotonic() - start) * 1000
        if rc:
            raise ValueError('valid control failed: ' + control_result)
        start = time.monotonic()
        rc, attack_result = replay.execute(attack, path, timeout)
        attack_ms = (time.monotonic() - start) * 1000
        if rc not in (0, 1):
            raise ValueError('attack did not complete: ' + attack_result)
        result = dict(base=info['base'], patchSha256=info['patchSha256'],
                      control=dict(command=control, result=control_result, elapsedMs=control_ms),
                      attack=dict(command=attack, result=attack_result, elapsedMs=attack_ms), reproduces=rc == 1)
        if series:
            if series_path.read_bytes() != series_data:
                raise ValueError('production series changed during the experiment')
            result['seriesPath'] = str(series_path)
            result['seriesSha256'] = hashlib.sha256(series_data).hexdigest()
        try:
            metrics = json.loads(attack_result.split(': ', 1)[1])
            if isinstance(metrics, dict):
                result['metrics'] = metrics
        except (ValueError, IndexError):
            pass
        with (path.parent / 'observations.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps(result) + '\n')
    except replay.CleanupPending:
        drained = False
        raise
    finally:
        if previous_series is None:
            os.environ.pop('FPL_PRODUCTION_SERIES', None)
        else:
            os.environ['FPL_PRODUCTION_SERIES'] = previous_series
        if drained:
            marker.unlink()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    for mode in ('read', 'materialize'):
        p = sub.add_parser(mode)
        for name in ('root', 'base', 'patch', 'item'):
            p.add_argument('--' + name, required=True)
        p.add_argument('--round', type=int, required=True)
        if mode == 'materialize':
            p.add_argument('--sha256', required=True)
    for mode in ('cleanup', 'execute'):
        p = sub.add_parser(mode)
        p.add_argument('--snapshot', required=True)
        p.add_argument('--token', required=True)
        if mode == 'execute':
            p.add_argument('--control', required=True)
            p.add_argument('--attack', required=True)
            p.add_argument('--timeout', type=float, default=120)
            p.add_argument('--series', help='absolute recorded production-series artifact to bind to this experiment')
    args = parser.parse_args()
    try:
        if args.mode == 'read':
            result = read(args.root, args.base, args.patch, args.item, args.round)
        elif args.mode == 'materialize':
            result = materialize(args.root, args.base, args.patch, args.sha256, args.item, args.round)
        elif args.mode == 'cleanup':
            result = cleanup(args.snapshot, args.token)
        else:
            if not 0 < args.timeout < 86400:
                raise ValueError('timeout must be finite and between zero and one day')
            result = execute(args.snapshot, args.token, args.control, args.attack, args.timeout, args.series)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(status='INCOMPLETE', issue=str(exc))))
        return 1


if __name__ == '__main__':
    sys.exit(main())
