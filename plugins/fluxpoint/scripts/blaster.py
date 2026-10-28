#!/usr/bin/env python3
"""Optional, solver-trusted Blaster checks with fresh receipts and scalar replay.

The Lean bridge queries the backend API directly. Human diagnostics and a
successful compiler exit cannot stand in for a complete structured result.
Receipts are local artifacts; independent gate attestation still owns execution
authority. See references/blaster.md for the trust and coverage boundaries.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

from process_tree import ProcessTree

MANIFEST = '.fluxpoint-blaster.json'
STORE = '.claude/fluxpoint/blaster'
BRIDGE = Path(__file__).resolve().parent.parent / 'templates/blaster/Bridge.lean'
NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_\']*(?:\.[A-Za-z_][A-Za-z0-9_\']*)*$')
ID = re.compile(r'^[a-z][a-z0-9-]*$')
SHA = re.compile(r'^[0-9a-f]{40}$')
STATUS = {'valid', 'falsified', 'undetermined'}


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def loads(text):
    return json.loads(text, object_pairs_hook=unique)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def shape(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError('missing or unknown fields: expected ' + ', '.join(required))


def bounded_int(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be an integer in [{low}, {high}]')


def relative(root, name):
    if not isinstance(name, str) or not name:
        raise ValueError('nonempty repository-relative path required')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('repository-relative path required: ' + str(name))
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('path escapes repository: ' + str(name))
    return target


def bindings_valid(values):
    if not isinstance(values, list) or not values or len(values) > 128:
        raise ValueError('concrete replay requires a nonempty scalar binding list')
    seen = set()
    for binding in values:
        shape(binding, ('name', 'type', 'value'))
        name, kind, value = binding['name'], binding['type'], binding['value']
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in seen:
            raise ValueError('duplicate or unsupported binding name')
        seen.add(name)
        valid = ((kind == 'Nat' and type(value) is int and value >= 0)
                 or (kind == 'Int' and type(value) is int)
                 or (kind == 'Bool' and type(value) is bool)
                 or (kind == 'String' and isinstance(value, str) and len(value) <= 4096))
        if not valid or (type(value) is int and value.bit_length() > 4096):
            raise ValueError('unsupported replay binding type or value')


def load(root):
    doc = loads((root / MANIFEST).read_text(encoding='utf-8'))
    shape(doc, ('version', 'toolchain', 'timeoutSeconds', 'buildTimeoutSeconds', 'checks', 'regressions'))
    if type(doc['version']) is not int or doc['version'] != 1:
        raise ValueError('Blaster manifest version must be 1')
    shape(doc['toolchain'], ('lean', 'z3', 'blaster'))
    for tool in ('lean', 'z3'):
        if not isinstance(doc['toolchain'][tool], str) or not re.fullmatch(r'\d+\.\d+\.\d+', doc['toolchain'][tool]):
            raise ValueError('exact Lean/Z3 version required')
    if not isinstance(doc['toolchain']['blaster'], str) or not SHA.fullmatch(doc['toolchain']['blaster']):
        raise ValueError('exact Blaster commit required')
    bounded_int(doc['timeoutSeconds'], 1, 600, 'timeoutSeconds')
    bounded_int(doc['buildTimeoutSeconds'], 1, 3600, 'buildTimeoutSeconds')
    if not isinstance(doc['checks'], list) or not doc['checks']:
        raise ValueError('at least one Blaster check required')
    ids = set()
    for check in doc['checks']:
        shape(check, ('id', 'module', 'declaration', 'witness', 'expected', 'trust', 'seed', 'scope', 'assumptions'))
        if not isinstance(check['id'], str) or not ID.fullmatch(check['id']) or check['id'] in ids:
            raise ValueError('duplicate or invalid check id')
        ids.add(check['id'])
        for name in ('module', 'declaration', 'witness'):
            if not isinstance(check[name], str) or not NAME.fullmatch(check[name]):
                raise ValueError('qualified Lean identifier required: ' + name)
        if check['expected'] not in ('valid', 'falsified'):
            raise ValueError('expected must be valid or falsified; unknown cannot satisfy a check')
        if check['trust'] != 'solver':
            raise ValueError('Blaster has no proof reconstruction: only explicit solver trust is supported, never kernel')
        bounded_int(check['seed'], 0, 2**31 - 1, 'seed')
        if not isinstance(check['scope'], str) or not check['scope'].strip():
            raise ValueError('nonempty check scope required')
        if not isinstance(check['assumptions'], list) or not check['assumptions'] or not all(
                isinstance(s, str) and s.strip() for s in check['assumptions']):
            raise ValueError('explicit solver and model assumptions required')
        relative(root, check['module'].replace('.', '/') + '.lean')
    if not isinstance(doc['regressions'], list):
        raise ValueError('regressions must be a list')
    regressions = set()
    for case in doc['regressions']:
        shape(case, ('cexId', 'check', 'bindings'))
        if not isinstance(case['cexId'], str) or not re.fullmatch(r'cex_[0-9a-f]{12}', case['cexId']) or case['cexId'] in regressions:
            raise ValueError('duplicate or invalid regression counterexample id')
        regressions.add(case['cexId'])
        check = next((c for c in doc['checks'] if c['id'] == case['check']), None)
        if check is None or check['expected'] != 'valid':
            raise ValueError('regression must name a safety check expecting valid')
        bindings_valid(case['bindings'])
    return doc


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('git operation failed: ' + result.stderr.decode('utf-8', 'replace')[:300])
    decoded = result.stdout.decode('utf-8')
    return decoded if args[:1] == ('ls-files',) else decoded.strip()


def snapshot(root):
    paths = git(root, 'ls-files', '-z').split('\0')
    # Counterexample history is output from this run, not a proof subject.
    paths = {p for p in paths if p and p != '.fluxpoint-cex.jsonl'}
    for needed in (MANIFEST, 'lean-toolchain', 'lake-manifest.json'):
        paths.add(needed)
    if not any(p in paths for p in ('lakefile.lean', 'lakefile.toml')):
        raise ValueError('tracked Lake configuration required')
    inputs = {p: file_hash(relative(root, p)) for p in sorted(paths)}
    inputs['@bridge'] = file_hash(BRIDGE)
    inputs['@adapter'] = file_hash(Path(__file__))
    return {'head': git(root, 'rev-parse', 'HEAD'), 'inputs': inputs,
            'sha256': digest(canonical(inputs).encode())}


def dependencies(root, doc):
    lake = loads((root / 'lake-manifest.json').read_text(encoding='utf-8'))
    if not isinstance(lake, dict):
        raise ValueError('Lake lock must be a JSON object')
    packages = lake.get('packages')
    if not isinstance(packages, list) or not packages:
        raise ValueError('locked Lake git dependencies required')
    packages_dir = relative(root, lake.get('packagesDir', '.lake/packages'))
    revisions = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError('invalid Lake dependency entry')
        name, rev = package.get('name'), package.get('rev')
        if package.get('type') != 'git' or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in revisions:
            raise ValueError('unique pinned git packages required; path dependencies are unsupported')
        if not isinstance(rev, str) or not SHA.fullmatch(rev):
            raise ValueError('full dependency commit required')
        path = packages_dir / name
        if not path.resolve().is_relative_to(root.resolve()) or git(path, 'rev-parse', 'HEAD') != rev:
            raise ValueError('installed dependency differs from Lake lock: ' + name)
        if git(path, 'status', '--porcelain=v1', '--untracked-files=all'):
            raise ValueError('installed dependency has local changes: ' + name)
        revisions[name] = rev
    if revisions.get('Blaster') != doc['toolchain']['blaster']:
        raise ValueError('Blaster dependency differs from configured commit')
    return revisions


def tools(root, doc):
    actual = {}
    for name, argv in [('lean', ['lake', 'env', 'lean', '--version']), ('z3', ['z3', '--version'])]:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=30)
        version = re.search(r'\b(\d+\.\d+\.\d+)\b', result.stdout)
        if result.returncode or version is None or version[1] != doc['toolchain'][name]:
            raise ValueError('wrong or missing ' + name + ' version: ' + (result.stdout + result.stderr)[:200])
        actual[name] = version[1]
        actual[name + 'VersionOutput'] = result.stdout.strip()
    for name in ('lake', 'z3'):
        executable = shutil.which(name)
        if not executable:
            raise ValueError('missing executable: ' + name)
        actual[name + 'ExecutableSha256'] = file_hash(executable)
    actual['dependencies'] = dependencies(root, doc)
    selectors = {name: os.environ.get(name) for name in (
        'PATH', 'LEAN_PATH', 'LEAN_SRC_PATH', 'LEAN_SYSROOT', 'ELAN_TOOLCHAIN', 'LAKE_HOME')}
    actual['environmentSha256'] = digest(canonical(selectors).encode())
    return actual


def supervised(root, argv, log, timeout):
    with log.open('wb') as stream:
        tree = ProcessTree(argv, str(root), stream)
        try:
            tree.resume()
            try:
                code = tree.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('Blaster command timed out') from exc
        finally:
            try:
                tree.terminate()
                deadline = time.monotonic() + 5
                while tree.running():
                    if time.monotonic() >= deadline:
                        raise ValueError('Blaster command descendants did not terminate')
                    time.sleep(0.02)
                tree.proc.wait(timeout=5)
            finally:
                tree.close()
    if code:
        raise ValueError(f'Blaster command exited {code}; see {log}')


def requests(doc):
    result = []
    for check in doc['checks']:
        for role, declaration in [('witness', check['witness']), ('query', check['declaration'])]:
            result.append({'key': check['id'] + ':' + role, 'declaration': declaration,
                           'seed': check['seed'], 'timeout': doc['timeoutSeconds'], 'bindings': []})
    for case in doc['regressions']:
        check = next(c for c in doc['checks'] if c['id'] == case['check'])
        result.append({'key': case['cexId'], 'declaration': check['declaration'],
                       'seed': check['seed'], 'timeout': doc['timeoutSeconds'], 'bindings': case['bindings']})
    return result


def probe(doc, nonce):
    reqs = requests(doc)
    imports = ''.join('import ' + module + '\n' for module in sorted({c['module'] for c in doc['checks']}))
    source = imports + BRIDGE.read_text(encoding='utf-8') + '\n'
    source += '-- FPL_REQUEST ' + canonical({'nonce': nonce, 'requests': reqs}) + '\n'
    for request in reqs:
        source += '#fluxpoint_blaster ' + ' '.join(json.dumps(value, ensure_ascii=True) for value in (
            nonce, request['key'], request['declaration'], canonical(request['bindings'])))
        source += f" {request['seed']} {request['timeout']}\n"
    return source


def events(text, nonce, wanted):
    result = {}
    for line in text.splitlines():
        if not line.startswith('FLUXPOINT_BLASTER '):
            continue
        row = loads(line[len('FLUXPOINT_BLASTER '):])
        shape(row, ('nonce', 'key', 'status', 'model', 'variables', 'axioms', 'query'))
        if not all(isinstance(row[k], str) for k in ('nonce', 'key', 'status')):
            raise ValueError('malformed backend result identity/status')
        if row['nonce'] != nonce or row['key'] not in wanted or row['key'] in result:
            raise ValueError('unexpected, duplicate or wrong-nonce backend result')
        if row['status'] not in STATUS:
            raise ValueError('unsupported backend result')
        for key in ('model', 'axioms'):
            if not isinstance(row[key], list) or not all(isinstance(v, str) for v in row[key]):
                raise ValueError('malformed backend ' + key)
        if not isinstance(row['variables'], list) or not isinstance(row['query'], str):
            raise ValueError('malformed backend variable/query data')
        for variable in row['variables']:
            shape(variable, ('name', 'type'))
            if not all(isinstance(v, str) for v in variable.values()):
                raise ValueError('malformed backend variable')
        result[row['key']] = row
    if set(result) != set(wanted):
        raise ValueError('missing backend results')
    return result


def parse_model(model, variables):
    """Reify only total, unique scalar assignments; never evaluate SMT text."""
    try:
        if not variables or len(model) != len(variables):
            return None
        values = {}
        for line in model:
            name, separator, value = line.strip().partition(': ')
            if not separator or name in values:
                return None
            values[name] = value.strip()
        result = []
        for variable in variables:
            name, kind = variable['name'], variable['type']
            raw = values[name]
            if kind in ('Nat', 'Int') and re.fullmatch(r'-?\d{1,1200}', raw):
                value = int(raw)
            elif kind == 'Int' and re.fullmatch(r'\(- \d{1,1200}\)', raw):
                value = -int(raw[3:-1])
            elif kind == 'Bool' and raw in ('true', 'false'):
                value = raw == 'true'
            elif kind == 'String' and re.fullmatch(r'"(?:[^"\\\r\n]|"")*"', raw):
                value = raw[1:-1].replace('""', '"')
            else:
                return None
            result.append({'name': name, 'type': kind, 'value': value})
        bindings_valid(result)
        return result
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    os.replace(temp, path)


def run(root):
    store = relative(root, STORE)
    token = secrets.token_hex(16)
    directory = store / 'runs' / token
    directory.mkdir(parents=True)
    receipt = {'schema': 'BlasterVerificationV1', 'status': 'error', 'complete': False,
               'runId': token, 'checks': [], 'regressions': [], 'logs': {}}
    try:
        doc = load(root)
        before = snapshot(root)
        for check in doc['checks']:
            source = check['module'].replace('.', '/') + '.lean'
            if source not in before['inputs']:
                raise ValueError('checked module must be tracked: ' + source)
        toolchain = tools(root, doc)
        receipt.update({'snapshot': before, 'toolchain': toolchain, 'configSha256': digest(canonical(doc).encode())})
        build_log = directory / 'build.log'
        supervised(root, ['lake', 'build', 'Blaster', *sorted({c['module'] for c in doc['checks']})], build_log, doc['buildTimeoutSeconds'])
        source = directory / 'Probe.lean'
        source.write_text(probe(doc, token), encoding='utf-8')
        query_log = directory / 'query.log'
        supervised(root, ['lake', 'env', 'lean', str(source)], query_log,
                   (doc['timeoutSeconds'] + 10) * len(requests(doc)))
        if query_log.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('backend log exceeds supported size')
        found = events(query_log.read_text(encoding='utf-8'), token, {r['key'] for r in requests(doc)})
        for check in doc['checks']:
            query, witness = found[check['id'] + ':query'], found[check['id'] + ':witness']
            result = dict(check, observed=query['status'], witnessObserved=witness['status'],
                          model=query['model'], bindings=parse_model(query['model'], query['variables']),
                          variables=query['variables'],
                          axioms=sorted(set(query['axioms'] + witness['axioms'])), query=query['query'])
            result['status'] = 'passed' if query['status'] == check['expected'] and witness['status'] == 'valid' else 'failed'
            receipt['checks'].append(result)
        for case in doc['regressions']:
            observed = found[case['cexId']]['status']
            parent = next(c for c in receipt['checks'] if c['id'] == case['check'])
            receipt['regressions'].append(dict(case, observed=observed,
                status='passed' if observed == 'valid' and parent['witnessObserved'] == 'valid' else 'failed'))
        if snapshot(root) != before or tools(root, doc) != toolchain:
            raise ValueError('project inputs or tools changed during verification')
        receipt['complete'] = True
        receipt['status'] = 'passed' if all(r['status'] == 'passed' for r in receipt['checks'] + receipt['regressions']) else 'failed'
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt['error'] = str(exc)
    finally:
        for name in ('build.log', 'query.log'):
            log = directory / name
            if log.exists():
                receipt['logs'][str(log.relative_to(root)).replace(os.sep, '/')] = file_hash(log)
        atomic(directory / 'receipt.json', receipt)
        atomic(store / 'latest.json', receipt)
    if receipt['complete']:
        import cex
        cex.ingest(str(root), json.dumps(receipt), 0 if receipt['status'] == 'passed' else 1, 'blaster')
    print('blaster:', receipt['status'], '-', directory / 'receipt.json')
    if receipt.get('error'):
        print('blaster:', receipt['error'], file=sys.stderr)
    return 0 if receipt['status'] == 'passed' else 1


def current(root):
    doc = load(root)
    receipt = loads(relative(root, STORE + '/latest.json').read_text(encoding='utf-8'))
    if receipt.get('schema') != 'BlasterVerificationV1' or receipt.get('complete') is not True or receipt.get('status') != 'passed':
        raise ValueError('no complete passing Blaster receipt')
    if receipt.get('configSha256') != digest(canonical(doc).encode()) or receipt.get('snapshot') != snapshot(root) or receipt.get('toolchain') != tools(root, doc):
        raise ValueError('stale Blaster receipt: project, configuration or toolchain changed')
    if not receipt.get('logs') or any(file_hash(relative(root, path)) != value for path, value in receipt['logs'].items()):
        raise ValueError('missing or changed Blaster evidence logs')
    checks, regressions = receipt.get('checks'), receipt.get('regressions')
    if not isinstance(checks, list) or not isinstance(regressions, list) or len(checks) != len(doc['checks']) or len(regressions) != len(doc['regressions']):
        raise ValueError('incomplete receipt coverage')
    for declared, result in zip(doc['checks'], checks):
        if not isinstance(result, dict) or any(result.get(k) != v for k, v in declared.items()) or result.get('observed') != declared['expected'] or result.get('witnessObserved') != 'valid' or result.get('status') != 'passed':
            raise ValueError('receipt check does not satisfy its declaration')
    for declared, result in zip(doc['regressions'], regressions):
        if not isinstance(result, dict) or any(result.get(k) != v for k, v in declared.items()) or result.get('observed') != 'valid' or result.get('status') != 'passed':
            raise ValueError('receipt regression does not satisfy its declaration')
    return doc, receipt


def counterexamples(root, receipt):
    if receipt.get('schema') != 'BlasterVerificationV1' or receipt.get('complete') is not True or not isinstance(receipt.get('checks'), list):
        raise ValueError('not a complete Blaster receipt')
    rows = []
    for result in receipt['checks']:
        if result.get('expected') != 'valid' or result.get('observed') != 'falsified' or result.get('witnessObserved') != 'valid':
            continue
        model, bindings = result['model'], result['bindings']
        key = canonical([result['id'], result['module'], result['declaration'], bindings if bindings is not None else model])
        rows.append({'cexId': 'cex_' + digest(key.encode())[:12], 'dedupeKey': 'blaster|' + key,
                     'tool': 'blaster', 'toolVersion': receipt['toolchain']['lean'] + '/Z3-' + receipt['toolchain']['z3'],
                     'module': result['module'].replace('.', '/') + '.lean', 'title': result['declaration'],
                     'selector': result['id'], 'kind': 'model', 'input': canonical(model), 'bindings': bindings,
                     'inputForm': 'blaster-scalars' if bindings else 'opaque-smt-model', 'assertion': result['scope'],
                     'containment': canonical(bindings) if bindings else '', 'signature': 'falsified',
                     'seed': result['seed'], 'status': 'open', 'firstSeen': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                     'headSha': receipt['snapshot']['head'], 'runId': receipt['runId']})
    return rows


def regression(row):
    if not row.get('bindings'):
        raise ValueError('opaque model has no supported concrete replay; counterexample remains open')
    return {'cexId': row['cexId'], 'check': row['selector'], 'bindings': row['bindings']}


def verify_pin(root, row, rel, name):
    try:
        if rel != MANIFEST or name != row['cexId']:
            raise ValueError('Blaster pins name the manifest regression and exact cexId')
        expected = regression(row)
        doc, receipt = current(Path(root))
        if expected not in doc['regressions']:
            raise ValueError('exact recorded bindings missing from tracked regression')
        check = next(c for c in doc['checks'] if c['id'] == row['selector'])
        if check['module'].replace('.', '/') + '.lean' != row['module'] or check['declaration'] != row['title']:
            raise ValueError('regression refers to a different original property')
        if not any(all(r.get(k) == v for k, v in expected.items()) and r.get('status') == 'passed' for r in receipt['regressions']):
            raise ValueError('original property was not successfully replayed with the exact bindings')
        return True, ''
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        return False, str(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--run', action='store_true')
    action.add_argument('--check', action='store_true')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.run:
            return run(root)
        current(root)
        print('blaster: current solver-trusted checks and concrete regressions passed')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('blaster: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
