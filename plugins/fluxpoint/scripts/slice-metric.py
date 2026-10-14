#!/usr/bin/env python3
"""Record a slice's prediction before work and its measured gain afterwards.

  slice-metric.py --start --slice index-lookup --role implementer \
      --metric latency.ms --predicted-delta 20
  slice-metric.py --end --slice index-lookup

A predicted delta is a positive improvement in the metric's own units.
Measured gain is before-after for a down metric, after-before for an up
metric, and negative for a regression. Both endpoints use metric-guard's
repeated deterministic measurements. Changing the metric definition between
endpoints refuses calibration instead of comparing different quantities.
Artifacts live in ignored .claude/fluxpoint/slices/<slice>.json. Predictions
cannot be overwritten; use a new slice id for another attempt.
"""
import argparse
from contextlib import contextmanager
import datetime
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SLICES = Path('.claude/fluxpoint/slices')


def guard():
    spec = importlib.util.spec_from_file_location('metric_guard_slice', Path(__file__).with_name('metric-guard.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def head(root):
    try:
        p = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None
    except OSError:
        return None


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    return value


def selected(root, name):
    module = guard()
    metric = next((m for m in module.manifest(root) if m['name'] == name), None)
    if metric is None:
        raise ValueError(f'target metric {name!r} is not declared in {module.MANIFEST}')
    return dict(name=name, value=module.measure(root, metric),
                direction=metric['direction'], definition=module.definition(metric))


def write(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.slice-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(data, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write('\n')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def lock(path):
    # One slice's endpoint measurement cannot race another writer. A killed
    # measurement leaves an explicit lock, never a silently replaced forecast.
    marker = path.with_suffix('.lock')
    try:
        marker.mkdir()
    except FileExistsError:
        raise ValueError(f'{marker}: slice is being measured; if its process ended, remove the stale lock and retry') from None
    try:
        yield
    finally:
        marker.rmdir()


def start(root, sid, role, name, predicted, campaign=None):
    if not isinstance(role, str) or not role.strip():
        raise ValueError('--role is required at slice start')
    predicted = number(predicted, '--predicted-delta')
    if predicted <= 0:
        raise ValueError('--predicted-delta must be a positive predicted improvement')
    path = artifact_path(root, sid)
    with lock(path):
        if path.exists():
            raise ValueError(f'{sid}: prediction already recorded; use a new slice id')
        measurement = selected(root, name)
        data = dict(version=1, sliceId=sid, role=role.strip(), metric=name,
                    campaign=campaign or '<loop>', predictedDelta=predicted,
                    before=measurement['value'], direction=measurement['direction'],
                    definition=measurement['definition'], startedAt=now(),
                    startHead=head(root), status='STARTED')
        write(path, data)
    return data


def finish(root, sid):
    path = artifact_path(root, sid)
    with lock(path):
        if not path.exists():
            raise ValueError(f'{sid}: no prediction recorded; start the slice before changing code')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('sliceId') != sid or data.get('status') not in ('STARTED', 'COMPLETE'):
            raise ValueError(f'{sid}: malformed slice artifact')
        predicted = number(data.get('predictedDelta'), 'recorded prediction')
        if (predicted <= 0 or type(data.get('before')) is not int
                or data.get('direction') not in ('down', 'up')
                or not isinstance(data.get('role'), str) or not data['role'].strip()
                or not isinstance(data.get('metric'), str) or not data['metric']):
            raise ValueError(f'{sid}: malformed starting measurement')
        if data['status'] == 'COMPLETE':
            if type(data.get('after')) is not int:
                raise ValueError(f'{sid}: malformed ending measurement')
            delta = data['before'] - data['after'] if data['direction'] == 'down' else data['after'] - data['before']
            if (type(data.get('measuredDelta')) is not int or data['measuredDelta'] != delta
                    or number(data.get('ratio'), 'recorded ratio') != delta / predicted):
                raise ValueError(f'{sid}: completed calibration disagrees with its endpoints')
            return data  # replaying completion cannot remeasure or double-count
        measurement = selected(root, data.get('metric'))
        if (measurement['definition'] != data.get('definition')
                or measurement['direction'] != data.get('direction')):
            raise ValueError(f'{sid}: metric definition changed since the prediction; calibration refused')
        after = measurement['value']
        delta = data['before'] - after if data['direction'] == 'down' else after - data['before']
        data.update(after=after, measuredDelta=delta, ratio=delta / predicted,
                    endedAt=now(), endHead=head(root), status='COMPLETE')
        write(path, data)
    return data


def artifact_path(root, sid):
    if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', sid):
        raise ValueError('--slice must be 1-100 letters, digits, underscores or hyphens')
    path = Path(root) / SLICES
    path.mkdir(parents=True, exist_ok=True)
    try:
        with (path.parent / '.gitignore').open('x', encoding='utf-8') as fh:
            fh.write('*\n')
    except FileExistsError:
        pass
    return path / (sid + '.json')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--start', action='store_true')
    mode.add_argument('--end', action='store_true')
    ap.add_argument('--root', default='.')
    ap.add_argument('--slice', required=True, dest='sid')
    ap.add_argument('--role')
    ap.add_argument('--metric')
    ap.add_argument('--predicted-delta', type=float)
    ap.add_argument('--campaign')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    try:
        if a.start:
            if a.metric is None or a.predicted_delta is None:
                raise ValueError('--metric and --predicted-delta are required at slice start')
            data = start(a.root, a.sid, a.role, a.metric, a.predicted_delta, a.campaign)
        else:
            if any(v is not None for v in (a.role, a.metric, a.predicted_delta, a.campaign)):
                raise ValueError('completion uses the recorded role, metric and prediction; do not supply them again')
            data = finish(a.root, a.sid)
        if a.json:
            print(json.dumps(data, sort_keys=True, allow_nan=False))
        elif data['status'] == 'STARTED':
            print(f'slice-metric: {a.sid} ({data["role"]}) predicted +{data["predictedDelta"]:g} '
                  f'{data["metric"]}, before {data["before"]}')
        else:
            print(f'slice-metric: {a.sid} ({data["role"]}) measured {data["measuredDelta"]:+g} '
                  f'{data["metric"]} vs predicted +{data["predictedDelta"]:g}; '
                  f'measured/predicted {data["ratio"]:.2f}x')
        return 0
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError) as e:
        print(f'slice-metric: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
