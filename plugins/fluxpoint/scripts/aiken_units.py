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


def summary_counts(summary, total, units, properties, label):
    if (not isinstance(summary, dict)
            or any(type(summary.get(key)) is not int for key in ('total', 'passed', 'failed'))
            or summary['total'] != total or summary['passed'] != total or summary['failed'] != 0
            or not isinstance(summary.get('kind'), dict)
            or type(summary['kind'].get('unit')) is not int or summary['kind']['unit'] != units
            or type(summary['kind'].get('property')) is not int or summary['kind']['property'] != properties):
        raise ValueError(f'{label}: malformed, failed or incomplete Aiken test summary')


def parse(output):
    doc = json.loads(output) if isinstance(output, str) else output
    if not isinstance(doc, dict) or not isinstance(doc.get('modules'), list):
        raise ValueError('Aiken costs require modules[].tests[] JSON from non-TTY stdout')
    costs, excluded, seen_modules, seen_tests = {}, [], set(), set()
    for module in doc['modules']:
        if (not isinstance(module, dict) or not isinstance(module.get('name'), str) or not module['name']
                or not isinstance(module.get('tests'), list) or module['name'] in seen_modules):
            raise ValueError('malformed or duplicate Aiken module')
        seen_modules.add(module['name'])
        units_before, properties_before = len(costs), len(excluded)
        for test in module['tests']:
            if not isinstance(test, dict) or not isinstance(test.get('title'), str) or not test['title']:
                raise ValueError('malformed Aiken test')
            key = module['name'] + '.' + test['title']
            if key in seen_tests:
                raise ValueError(f'duplicate Aiken test {key}')
            seen_tests.add(key)
            if test.get('status') != 'pass':
                raise ValueError(f'{key}: Aiken test did not pass')
            if 'iterations' in test:
                if type(test['iterations']) is not int or test['iterations'] < 0:
                    raise ValueError(f'{key}: malformed property iteration count')
                excluded.append(key)
                continue
            units = test.get('execution_units')
            if not isinstance(units, dict):
                raise ValueError(f'{key}: missing passing unit-test execution_units')
            if any(type(units.get(k)) is not int or units[k] < 0 for k in ('mem', 'cpu')):
                raise ValueError(f'{key}: mem/cpu must be nonnegative integers')
            costs[key] = {'mem': units['mem'], 'cpu': units['cpu']}
        summary_counts(module.get('summary'), len(module['tests']), len(costs) - units_before,
                       len(excluded) - properties_before, module['name'])
    summary_counts(doc.get('summary'), len(costs) + len(excluded), len(costs), len(excluded), 'Aiken')
    return costs, excluded


def extract(output, selector):
    if not isinstance(selector, dict) or set(selector) != {'test', 'unit'} or selector['unit'] not in ('mem', 'cpu'):
        raise ValueError('extract.aiken needs test="module.test" and unit="mem" or "cpu"')
    costs, _ = parse(output)
    if selector['test'] not in costs:
        raise ValueError(f'Aiken unit cost missing (or property excluded): {selector["test"]}')
    return costs[selector['test']][selector['unit']]
