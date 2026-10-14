#!/usr/bin/env python3
"""Record a declared deterministic metric; exit 10 when its best value plateaus."""
import argparse
import importlib.util
import json
import os
import subprocess
import sys

spec = importlib.util.spec_from_file_location('metric_guard', os.path.join(os.path.dirname(__file__), 'metric-guard.py'))
metric = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metric)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default='.')
    ap.add_argument('--window', type=int, default=3)
    ap.add_argument('--minimum', type=int, default=1)
    ap.add_argument('--metric')
    ap.add_argument('--reset', action='store_true')
    args = ap.parse_args()
    try:
        if args.window < 2 or args.minimum < 0:
            raise ValueError('PLATEAU_WINDOW must be >= 2 and PLATEAU_MIN >= 0')
        definitions = metric.manifest(args.root)
        if not definitions:
            return 0
        name = args.metric or definitions[0]['name']
        chosen = next((m for m in definitions if m['name'] == name), None)
        if chosen is None:
            raise ValueError(f'plateau metric {name!r} is not declared')
        value = metric.measure(args.root, chosen)
        path = os.path.join(args.root, '.claude', 'fluxpoint', 'loop-metrics.json')
        history = []
        if not args.reset and os.path.isfile(path):
            with open(path, encoding='utf-8') as f:
                previous = json.load(f)
            if previous.get('definition') == metric.definition(chosen):
                history = previous['values']
        history.append(value)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'metric': name, 'direction': chosen['direction'],
                       'definition': metric.definition(chosen), 'values': history}, f)
            f.write('\n')
        print(f'loop: metric {name} = {value}')
        if len(history) <= args.window:
            return 0
        # Compare best-so-far at the start/end of the last W iterations.
        # One worse sample cannot erase progress or manufacture a stop.
        best = min if chosen['direction'] == 'down' else max
        old, new = best(history[:-args.window]), best(history)
        improvement = old - new if chosen['direction'] == 'down' else new - old
        if improvement < args.minimum:
            print(f'loop: plateau on {name}: improved {improvement} over '
                  f'{args.window} iterations (minimum {args.minimum})')
            return 10
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e:
        print(f'loop: metric unavailable; plateau not evaluated: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
