"""Aiken v1.1.23 non-TTY JSON unit-test costs, verified against the real binary.

Property tests have iterations and report no single execution cost. Always
exclude them, even if a future producer adds an execution_units field.
"""
import json
import re
import subprocess

VERSION = 'v1.1.23'
COMMAND = ['aiken', 'check', '--seed', '1']


def check_version(root):
    result = subprocess.run(['aiken', '--version'], cwd=root, capture_output=True, text=True, check=True)
    if not re.fullmatch(r'aiken v1\.1\.23(?:\+[A-Za-z0-9]+)?\s*', result.stdout):
        raise ValueError(f'Aiken execution metrics pin {VERSION}; found {result.stdout.strip()!r}')


def parse(output):
    doc = json.loads(output) if isinstance(output, str) else output
    if not isinstance(doc, dict) or not isinstance(doc.get('modules'), list):
        raise ValueError('Aiken costs require modules[].tests[] JSON from non-TTY stdout')
    if doc.get('summary', {}).get('failed', 0):
        raise ValueError('Aiken reported failed tests; costs cannot arm a green baseline')
    costs, excluded = {}, []
    for module in doc['modules']:
        if not isinstance(module, dict) or not isinstance(module.get('name'), str) or not isinstance(module.get('tests'), list):
            raise ValueError('malformed Aiken module')
        for test in module['tests']:
            if not isinstance(test, dict) or not isinstance(test.get('title'), str):
                raise ValueError('malformed Aiken test')
            key = module['name'] + '.' + test['title']
            if 'iterations' in test:
                excluded.append(key)
                continue
            units = test.get('execution_units')
            if test.get('status') != 'pass' or not isinstance(units, dict):
                raise ValueError(f'{key}: missing passing unit-test execution_units')
            if any(type(units.get(k)) is not int or units[k] < 0 for k in ('mem', 'cpu')):
                raise ValueError(f'{key}: mem/cpu must be nonnegative integers')
            if key in costs:
                raise ValueError(f'duplicate Aiken unit test {key}')
            costs[key] = {'mem': units['mem'], 'cpu': units['cpu']}
    return costs, excluded


def extract(output, selector):
    if not isinstance(selector, dict) or set(selector) != {'test', 'unit'} or selector['unit'] not in ('mem', 'cpu'):
        raise ValueError('extract.aiken needs test="module.test" and unit="mem" or "cpu"')
    costs, _ = parse(output)
    if selector['test'] not in costs:
        raise ValueError(f'Aiken unit cost missing (or property excluded): {selector["test"]}')
    return costs[selector['test']][selector['unit']]
