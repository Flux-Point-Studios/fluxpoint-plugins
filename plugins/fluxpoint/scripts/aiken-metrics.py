#!/usr/bin/env python3
"""Initialize per-unit-test mem/CPU metric ceilings from real pinned Aiken runs.

--baseline runs Aiken v1.1.23 twice with seed 1 and refuses unequal costs before
writing. Property tests are named and excluded. Existing custom metrics remain
in the manifest and are remeasured before either file is written.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
from aiken_units import COMMAND, VERSION, check_version, parse

spec = importlib.util.spec_from_file_location('metric_guard', os.path.join(os.path.dirname(__file__), 'metric-guard.py'))
metric = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metric)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default='.')
    ap.add_argument('--baseline', action='store_true', required=True)
    a = ap.parse_args()
    try:
        check_version(a.root)
        samples = []
        excluded = []
        for _ in range(2):
            result = subprocess.run(COMMAND, cwd=a.root, capture_output=True, text=True, check=True)
            costs, excluded = parse(result.stdout)
            samples.append(costs)
        if samples[0] != samples[1]:
            raise ValueError('Aiken unit costs are nondeterministic; no baseline written')
        if not samples[0]:
            raise ValueError('no unit-test costs found; property statistics cannot be baselined')
        existing = metric.manifest(a.root)
        metrics = [m for m in existing if 'aiken' not in (m.get('extract') or {})]
        baseline = {}
        outputs = {}
        for m in metrics:
            baseline[m['name']] = {'value': metric.measure(a.root, m, outputs),
                                   'direction': m['direction'], 'definition': metric.definition(m)}
        for test, units in sorted(samples[0].items()):
            for unit in ('mem', 'cpu'):
                m = {'name': f'aiken.{test}.{unit}', 'command': COMMAND, 'direction': 'down',
                     'extract': {'aiken': {'test': test, 'unit': unit}}}
                metrics.append(m)
                baseline[m['name']] = {'value': units[unit], 'direction': 'down', 'definition': metric.definition(m)}
        if len({m['name'] for m in metrics}) != len(metrics):
            raise ValueError('generated Aiken metric name conflicts with an existing custom metric')
        path = os.path.join(a.root, metric.MANIFEST)
        doc = {}
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                doc = json.load(f)
        doc.update(version=1, metrics=metrics)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write('\n')
        metric.write(a.root, baseline)
        print(f'aiken-metrics: pinned {VERSION}, identical costs on two real runs; '
              f'armed {len(samples[0])} unit test(s) / {2*len(samples[0])} ceilings')
        for test in excluded:
            print(f'aiken-metrics: excluded property statistics: {test}')
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as e:
        print(f'aiken-metrics: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
