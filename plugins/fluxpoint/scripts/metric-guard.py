#!/usr/bin/env python3
"""Deterministic integer metrics: explicit commands, repeated measurements, ratchets.

.fluxpoint-metrics.json contains {"version":1,"metrics":[
 {"name":"bundle.bytes","command":"wc -c < dist/app.js","direction":"down"},
 {"name":"queries","command":["python","count.py"],"extract":{"regex":"queries: ([0-9]+)"},"direction":"down"}
]}. Extract is omitted for a plain integer, or {"regex": one capture group},
or {"pointer":"/JSON/pointer"}. Commands are trusted repo code, like the harness.
--baseline repeats every command (default twice) and atomically arms only identical
integers. --check fails closed on missing baselines, noisy commands and regressions.
--tighten requires a clean tree and a green full harness and only improves limits.
--report prints each current value against its ceiling/floor; --json is machine readable.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

MANIFEST = '.fluxpoint-metrics.json'
BASELINE = '.fluxpoint-metrics-baseline.json'


def manifest(root):
    path = os.path.join(root, MANIFEST)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get('version', 1) != 1 or not isinstance(doc.get('metrics'), list):
        raise ValueError(f'{MANIFEST}: expected version 1 and metrics list')
    seen = set()
    for m in doc['metrics']:
        if not isinstance(m, dict) or not isinstance(m.get('name'), str) or not m['name'].strip():
            raise ValueError('each metric needs a nonempty name')
        if m['name'] in seen:
            raise ValueError(f'duplicate metric {m["name"]}')
        seen.add(m['name'])
        cmd = m.get('command')
        if not ((isinstance(cmd, str) and cmd.strip()) or
                (isinstance(cmd, list) and cmd and all(isinstance(v, str) and v for v in cmd))):
            raise ValueError(f'{m["name"]}: command must be a string or nonempty argv list')
        if m.get('direction') not in ('down', 'up'):
            raise ValueError(f'{m["name"]}: direction must be down or up')
        runs = m.get('runs', 2)
        if type(runs) is not int or runs < 2:
            raise ValueError(f'{m["name"]}: runs must be an integer >= 2')
        timeout = m.get('timeout', 120)
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError(f'{m["name"]}: timeout must be positive')
        ex = m.get('extract')
        if ex is not None and (not isinstance(ex, dict) or len(ex) != 1 or not
                               ({'regex', 'pointer'} & set(ex))):
            raise ValueError(f'{m["name"]}: extract must contain only regex or pointer')
    return doc['metrics']


def definition(m):
    return hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()


def extract(output, spec):
    if spec and 'regex' in spec:
        regex = re.compile(spec['regex'], re.MULTILINE)
        if regex.groups != 1:
            raise ValueError('extract.regex needs exactly one capture group')
        hits = regex.findall(output)
        if len(hits) != 1:
            raise ValueError(f'extract.regex produced {len(hits)} values; expected one')
        value = hits[0]
    elif spec and 'pointer' in spec:
        value = json.loads(output)
        pointer = spec['pointer']
        if not isinstance(pointer, str) or (pointer and not pointer.startswith('/')):
            raise ValueError('extract.pointer must be a JSON pointer')
        for part in pointer.split('/')[1:]:
            key = part.replace('~1', '/').replace('~0', '~')
            value = value[int(key)] if isinstance(value, list) else value[key]
    else:
        value = output.strip()
    if type(value) is int:
        return value
    if not isinstance(value, str) or not re.fullmatch(r'-?\d+', value.strip()):
        raise ValueError(f'extractor must produce exactly one integer, got {value!r}')
    return int(value)


def measure(root, m):
    values = []
    for _ in range(m.get('runs', 2)):
        r = subprocess.run(m['command'], shell=isinstance(m['command'], str), cwd=root,
                           capture_output=True, text=True, timeout=m.get('timeout', 120))
        if r.returncode:
            raise ValueError(f'{m["name"]}: command exited {r.returncode}: {r.stderr[-600:]}')
        values.append(extract(r.stdout, m.get('extract')))
    if len(set(values)) != 1:
        raise ValueError(f'{m["name"]}: NOT ARMED: nondeterministic repeated values {values}')
    return values[0]


def load_baseline(root):
    p = os.path.join(root, BASELINE)
    if not os.path.exists(p):
        return {}
    with open(p, encoding='utf-8') as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get('metrics'), dict):
        raise ValueError(f'{BASELINE}: expected metrics object')
    return doc['metrics']


def collect(root):
    base = load_baseline(root)
    rows = []
    for m in manifest(root):
        value = measure(root, m)
        prior = base.get(m['name'])
        armed = (isinstance(prior, dict) and prior.get('definition') == definition(m)
                 and type(prior.get('value')) is int)
        limit = prior.get('value') if armed else None
        good = armed and (value <= limit if m['direction'] == 'down' else value >= limit)
        rows.append({'name': m['name'], 'value': value, 'direction': m['direction'],
                     'limit': limit, 'armed': armed, 'ok': good, 'definition': definition(m)})
    return rows


def write(root, base):
    p = os.path.join(root, BASELINE)
    with open(p + '.tmp', 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'version': 1, 'metrics': base}, f, indent=2, sort_keys=True)
        f.write('\n')
    os.replace(p + '.tmp', p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default='.')
    ap.add_argument('--json', action='store_true')
    mode = ap.add_mutually_exclusive_group(required=True)
    for flag in ('baseline', 'check', 'tighten', 'report'):
        mode.add_argument('--' + flag, action='store_true')
    a = ap.parse_args()
    try:
        root = os.path.abspath(a.root)
        if not manifest(root):
            if a.json:
                print(json.dumps({'metrics': [], 'dormant': True}))
            return 0
        if a.tighten:
            from ratchet import green
            green(root)
        rows = collect(root)
        if a.baseline:
            write(root, {r['name']: {k: r[k] for k in ('value', 'direction', 'definition')} for r in rows})
        elif a.tighten:
            if any(not r['ok'] for r in rows):
                raise ValueError('--tighten refused: unarmed metric or worse measurement')
            from ratchet import clean
            clean(root)
            base = load_baseline(root)
            changed = False
            for r in rows:
                if r['value'] != r['limit']:
                    print(f'metric-guard: {r["name"]}: {r["limit"]} -> {r["value"]}')
                    base[r['name']]['value'] = r['value']
                    changed = True
            if changed:
                write(root, base)
        if a.json:
            print(json.dumps({'metrics': rows, 'dormant': False}, sort_keys=True))
        else:
            for r in rows:
                bound = 'ceiling' if r['direction'] == 'down' else 'floor'
                state = 'NOT ARMED' if not r['armed'] else ('green' if r['ok'] else 'RED')
                print(f'metric-guard: {r["name"]}: {r["value"]} / {bound} {r["limit"]} ({state})')
        return int(a.check and any(not r['ok'] for r in rows))
    except (ValueError, OSError, KeyError, IndexError, TypeError, subprocess.SubprocessError) as e:
        print(f'metric-guard: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
