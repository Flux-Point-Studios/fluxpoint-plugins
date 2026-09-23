#!/usr/bin/env python3
"""Validate, freeze and execute a requirement packet before implementation."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import decision

SPEC = '.fluxpoint-spec.json'
LOCK = '.fluxpoint-spec-lock.json'
BASELINE = '.fluxpoint-proof-baseline.json'
ID = re.compile(r'^[a-z][a-z0-9-]*$')

if os.name == 'nt':
    import ctypes as C
    from ctypes import wintypes as W

    class JobLimits(C.Structure):
        _fields_ = [('PerProcessUserTimeLimit', C.c_int64), ('PerJobUserTimeLimit', C.c_int64),
                    ('LimitFlags', W.DWORD), ('MinimumWorkingSetSize', C.c_size_t),
                    ('MaximumWorkingSetSize', C.c_size_t), ('ActiveProcessLimit', W.DWORD),
                    ('Affinity', C.c_size_t), ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]

    class ExtendedJobLimits(C.Structure):
        _fields_ = [('BasicLimitInformation', JobLimits), ('IoInfo', C.c_uint64 * 6),
                    ('ProcessMemoryLimit', C.c_size_t), ('JobMemoryLimit', C.c_size_t),
                    ('PeakProcessMemoryUsed', C.c_size_t), ('PeakJobMemoryUsed', C.c_size_t)]

    class JobAccounting(C.Structure):
        _fields_ = [('TotalUserTime', C.c_int64), ('TotalKernelTime', C.c_int64),
                    ('ThisPeriodTotalUserTime', C.c_int64), ('ThisPeriodTotalKernelTime', C.c_int64),
                    ('TotalPageFaultCount', W.DWORD), ('TotalProcesses', W.DWORD),
                    ('ActiveProcesses', W.DWORD), ('TotalTerminatedProcesses', W.DWORD)]

    kernel = C.WinDLL('kernel32', use_last_error=True)
    for name, params, returns in [
        ('CreateJobObjectW', [C.c_void_p, W.LPCWSTR], W.HANDLE),
        ('SetInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        ('AssignProcessToJobObject', [W.HANDLE, W.HANDLE], W.BOOL),
        ('QueryInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        ('TerminateJobObject', [W.HANDLE, W.UINT], W.BOOL),
        ('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        ('CloseHandle', [W.HANDLE], W.BOOL),
    ]:
        function = getattr(kernel, name)
        function.argtypes, function.restype = params, returns

    def win_checked(value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    # The command cannot spawn until its trusted launcher belongs to the job.
    CHECK_LAUNCHER = ('import json,subprocess,sys; go=sys.stdin.buffer.read(1); '
                      'sys.exit(subprocess.call(json.loads(sys.argv[1]),stdin=subprocess.DEVNULL) '
                      'if go==b"1" else 125)')


def strings(value, nonempty=True):
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(v, str) and v.strip() for v in value))


def validate(doc):
    errors = []

    def shape(value, fields, where, optional=()):
        if not isinstance(value, dict):
            errors.append(f'{where}: expected an object')
            return False
        if set(value) - set(fields) - set(optional) or set(fields) - set(value):
            errors.append(f'{where}: missing or unknown fields')
        return True

    def text_fields(value, fields, where):
        for key in fields:
            if not isinstance(value.get(key), str) or not value[key].strip():
                errors.append(f'{where}.{key}: nonempty text required')

    def entries(key):
        values = doc.get(key)
        if not isinstance(values, list) or not values:
            errors.append(f'{key}: at least one entry required')
            return []
        if any(not isinstance(v, dict) for v in values):
            errors.append(f'{key}: entries must be objects')
            return []
        ids = [v.get('id') for v in values]
        if any(not isinstance(v, str) or not ID.fullmatch(v) for v in ids):
            errors.append(f'{key}: ids must be lowercase kebab-case')
        elif len(set(ids)) != len(ids):
            errors.append(f'{key}: duplicate ids')
        return values

    if not shape(doc, ('version', 'goal', 'asset', 'scope', 'outOfScope',
                       'decisions', 'requirements', 'checks', 'challenges'), 'spec'):
        return errors
    if type(doc.get('version')) is not int or doc['version'] != 1:
        errors.append('spec: version must be 1')
    text_fields(doc, ('goal', 'asset'), 'spec')
    for key in ('scope', 'outOfScope'):
        if not strings(doc.get(key)):
            errors.append(f'{key}: nonempty text list required')
    decisions = entries('decisions')
    requirements = entries('requirements')
    checks = entries('checks')
    dids = {d['id'] for d in decisions if isinstance(d.get('id'), str)}
    cids = {c['id'] for c in checks if isinstance(c.get('id'), str)}
    schema = decision.load_schema(Path(__file__).resolve().parent.parent)
    dependencies = {}
    for d in decisions:
        shape(d, ('id', 'status', 'dependsOn', 'record', 'costs'), 'decision')
        if d.get('status') not in ('defaulted', 'user-confirmed'):
            errors.append(f"decision {d.get('id')}: unresolved or invalid status")
        deps = d.get('dependsOn')
        if not strings(deps, nonempty=False) or any(v not in dids for v in deps):
            errors.append('decision: unknown or invalid dependencies')
        elif isinstance(d.get('id'), str):
            dependencies[d['id']] = deps
        record = d.get('record')
        errors.extend(decision.validate(record, schema))
        if not isinstance(record, dict):
            continue
        if not strings(record.get('evidence')):
            errors.append('decision: evidence is required, including the basis for an estimate')
        options = record.get('options', [])
        names = [o.get('option') for o in options if isinstance(o, dict)] if isinstance(options, list) else []
        if not strings(names) or len(set(names)) != len(names):
            errors.append('decision: distinct named options required')
        costs = d.get('costs')
        if not isinstance(costs, list) or any(not isinstance(c, dict) for c in costs):
            errors.append('decision: costs must be objects')
            continue
        cost_names = [c.get('option') for c in costs]
        if sorted(map(str, cost_names)) != sorted(map(str, names)):
            errors.append('decision: exactly one cost entry per option required')
        for c in costs:
            shape(c, ('option', 'cost', 'basis', 'evidence'), 'cost')
            text_fields(c, ('option', 'cost'), 'cost')
            if c.get('basis') not in ('measured', 'estimate', 'unknown') or not strings(c.get('evidence')):
                errors.append('cost: name measured/estimate/unknown and its evidence')
    pending = dict(dependencies)
    while pending:
        ready = [k for k, deps in pending.items() if not any(v in pending for v in deps)]
        if not ready:
            errors.append('decision: dependency cycle')
            break
        for k in ready:
            del pending[k]
    used_checks = set()
    for r in requirements:
        shape(r, ('id', 'statement', 'decisions', 'checks', 'counterexample'), 'requirement')
        text_fields(r, ('statement', 'counterexample'), 'requirement')
        for key, known in (('decisions', dids), ('checks', cids)):
            if not strings(r.get(key)) or any(v not in known for v in r[key]):
                errors.append(f'requirement: {key} must reference declared ids')
        if strings(r.get('checks')):
            used_checks.update(r['checks'])
    if cids - used_checks:
        errors.append('checks: every check must cover a requirement')
    for c in checks:
        shape(c, ('id', 'kind', 'argv', 'scope', 'assumptions', 'timeoutSeconds'), 'check', ('witness', 'obligations'))
        text_fields(c, ('scope',), 'check')
        if c.get('kind') not in ('test', 'property', 'model', 'proof', 'runtime'):
            errors.append('check: unknown assurance kind')
        if not strings(c.get('argv')) or any('\0' in v for v in c.get('argv', []) if isinstance(v, str)):
            errors.append('check: argv must be a nonempty argument list')
        if not strings(c.get('assumptions'), nonempty=False):
            errors.append('check: assumptions must be a text list')
        if type(c.get('timeoutSeconds')) is not int or not 1 <= c['timeoutSeconds'] <= 3600:
            errors.append('check: timeoutSeconds must be an integer from 1 to 3600')
        if (c.get('kind') in ('model', 'proof') or 'witness' in c) and not strings(c.get('witness')):
            errors.append('check: model/proof requires an executable non-vacuity witness')
        if (c.get('kind') in ('model', 'proof') or 'obligations' in c) and not strings(c.get('obligations')):
            errors.append('check: model/proof requires named obligations from spec-guard.py --scan')
    challenges = doc.get('challenges')
    if not isinstance(challenges, list) or not challenges:
        errors.append('challenges: record at least one resolved challenge to the design')
    else:
        for c in challenges:
            if shape(c, ('claim', 'resolution'), 'challenge'):
                text_fields(c, ('claim', 'resolution'), 'challenge')
    return errors


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() if path.exists() else None


def unique_keys(pairs):
    doc = {}
    for key, value in pairs:
        if key in doc:
            raise ValueError(f'duplicate JSON key: {key}')
        doc[key] = value
    return doc


# A graph file declares its packet and its contracts in its header. Only
# lines outside fenced blocks count, so an IR string or a quoted example
# can never redirect the compiler.
HEADER = re.compile(r'^(SPEC|CONTRACTS):[ \t]*(\S[^\r\n]*?)[ \t]*\r?$', re.M)
FENCE = re.compile(r'^```.*?^```[ \t]*\r?$', re.M | re.S)


def headers(text):
    """The SPEC: and CONTRACTS: declarations of a graph file.

    Two different values for one key is an error rather than first-wins:
    which packet a campaign is held to must not depend on reading order.
    """
    out = {}
    for key, value in HEADER.findall(FENCE.sub('', text)):
        if key in out and out[key] != value:
            raise ValueError(f'{key}: declared twice ({out[key]!r} and {value!r})')
        out[key] = value
    return out


def graph_headers(path):
    """headers() of a graph file, or {} when there is no such file."""
    path = Path(path)
    return headers(path.read_text(encoding='utf-8')) if path.is_file() else {}


def in_repo(rel, what):
    """A header path, checked: relative, inside the repository."""
    p = Path(rel)
    if p.is_absolute() or rel.startswith(('/', '\\')) or '..' in p.parts:
        raise ValueError(f'{what} {rel!r} must be a path inside the repository, '
                         'relative to its root')
    return p


def packet_paths(root, spec=None):
    """(packet, lock) for a packet path; the default pair when spec is None.

    The lock sits beside its packet and is named after it, so a campaign
    that keeps its packet at a path of its own gets a lock of its own too:
    `.fluxpoint-spec.json` locks into `.fluxpoint-spec-lock.json`, and
    `.fluxpoint-spec.rollout.json` into `.fluxpoint-spec.rollout-lock.json`.
    """
    root = Path(root)
    if spec is None or spec == SPEC:
        return root / SPEC, root / LOCK
    rel = in_repo(spec, 'SPEC:')
    if rel.suffix != '.json':
        raise ValueError(f'SPEC: {spec!r} must name a .json packet')
    return root / rel, root / rel.with_name(rel.name[:-len('.json')] + '-lock.json')


def required(root):
    root = Path(root)
    work = root / 'WORK.md'
    return ((root / SPEC).exists() or (root / LOCK).exists()
            or (work.exists() and re.search(r'^SPEC:[ \t]*\.fluxpoint-spec\.json[ \t]*$',
                                           work.read_text(encoding='utf-8'), re.M)))


def load(root, locked=True, spec=None):
    root = Path(root)
    packet_file, lock_file = packet_paths(root, spec)
    snapshot = packet_file.read_bytes().replace(b'\r\n', b'\n')
    doc = json.loads(snapshot.decode('utf-8'), object_pairs_hook=unique_keys)
    errors = validate(doc)
    identity = {'version': 1, 'sha256': hashlib.sha256(snapshot).hexdigest(),
                'proofBaselineSha256': digest(root / BASELINE)}
    if not errors:
        ids = {oid for check in doc['checks'] for oid in check.get('obligations', [])}
        if ids:
            module = importlib.util.spec_from_file_location('spec_guard', Path(__file__).with_name('spec-guard.py'))
            guard = importlib.util.module_from_spec(module)
            module.loader.exec_module(guard)
            observed, _ = guard.scan(str(root))
            for oid in sorted(ids - observed.keys()):
                errors.append('unknown or unsupported obligation: ' + oid)
            identity['obligations'] = {oid: observed[oid]['statementSha'] for oid in sorted(ids & observed.keys())}
    if locked:
        lock = json.loads(lock_file.read_text(encoding='utf-8'), object_pairs_hook=unique_keys)
        if lock != identity:
            errors.append('spec, obligation or proof baseline changed since lock; review the change and re-lock')
    if errors:
        raise ValueError('; '.join(errors))
    return doc, identity


def run_checks(root, doc, stack):
    failed = False
    checks = doc['checks']
    if any(c['kind'] in ('model', 'proof') for c in checks):
        checks = [{'id': 'spec-proof-ratchet', 'timeoutSeconds': 60,
                   'argv': [sys.executable, str(Path(__file__).with_name('proof-guard.py')),
                            '--check', '--require-armed']}] + checks
    for check in checks:
        for key in ('witness', 'argv'):
            if key not in check:
                continue
            argv = [sys.executable if v == '{python}' else v for v in check[key]]
            print(f"specification: {check['id']} {key}: {json.dumps(argv)}", flush=True)
            process, job = None, None
            try:
                try:
                    env = dict(os.environ, PYTHONIOENCODING='utf-8', FPL_SPEC_STACK=json.dumps(stack))
                    if os.name == 'nt':
                        job = win_checked(kernel.CreateJobObjectW(None, None))
                        limits = ExtendedJobLimits()
                        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
                        win_checked(kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
                        process = subprocess.Popen(
                            [sys.executable, '-I', '-S', '-c', CHECK_LAUNCHER, json.dumps(argv)],
                            cwd=root, env=env, stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
                        handle = win_checked(kernel.OpenProcess(0x0101, False, process.pid))
                        try:
                            win_checked(kernel.AssignProcessToJobObject(job, handle))
                        finally:
                            win_checked(kernel.CloseHandle(handle))
                        process.stdin.write(b'1')
                        process.stdin.close()
                    else:
                        process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                                   start_new_session=True)
                    code = process.wait(timeout=check['timeoutSeconds'])
                finally:
                    pending_error = sys.exc_info()[1]
                    try:
                        try:
                            if job is not None:
                                win_checked(kernel.TerminateJobObject(job, 125))
                                info = JobAccounting()
                                deadline = time.monotonic() + 5
                                while True:
                                    win_checked(kernel.QueryInformationJobObject(job, 1, C.byref(info), C.sizeof(info), None))
                                    if info.ActiveProcesses == 0:
                                        break
                                    if time.monotonic() >= deadline:
                                        raise OSError('check job did not drain within five seconds')
                                    time.sleep(0.01)
                            elif process is not None and os.name != 'nt':
                                try:
                                    os.killpg(process.pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    # An already empty group has nothing left to terminate.
                                    pass
                        finally:
                            try:
                                if job is not None:
                                    win_checked(kernel.CloseHandle(job))
                            finally:
                                if process is not None:
                                    try:
                                        if process.stdin is not None and not process.stdin.closed:
                                            process.stdin.close()
                                    finally:
                                        if process.poll() is None:
                                            process.kill()
                                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired) as e:
                        prior = f'{pending_error}; ' if pending_error else ''
                        raise ValueError(f'{prior}check cleanup failed; stopping remaining checks: {e}') from e
            except (OSError, subprocess.TimeoutExpired) as e:
                print(f"specification: {check['id']} could not complete: {e}", file=sys.stderr)
                failed = True
                continue
            print(f"specification: {check['id']} {key} exit {code}", flush=True)
            failed |= code != 0
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--if-present', action='store_true')
    parser.add_argument('--spec', help='packet path relative to --root (default ' + SPEC + ')')
    parser.add_argument('--graph', help="a graph file whose SPEC: header names the packet")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--lock', action='store_true')
    action.add_argument('--check', action='store_true')
    action.add_argument('--run', action='store_true')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        spec = args.spec
        if args.graph:
            declared = graph_headers(root / args.graph).get('SPEC')
            if spec and declared and spec != declared:
                raise ValueError(f'--spec {spec} contradicts {args.graph}, which declares SPEC: {declared}')
            spec = spec or declared
        if args.if_present and not args.run:
            raise ValueError('--if-present is only for the legacy harness runner')
        if args.if_present and not spec and not required(root):
            print('specification: legacy loop without a spec; no requirements checked')
            return 0
        packet_file, lock_path = packet_paths(root, spec)
        doc, identity = load(root, locked=not args.lock, spec=spec)
        name = packet_file.relative_to(root).as_posix()
        if args.lock:
            with lock_path.open('w', encoding='utf-8', newline='\n') as lock_file:
                lock_file.write(json.dumps(identity, indent=2) + '\n')
            print(f"specification: locked {name} {identity['sha256']}; checks have not run")
            return 0
        print(f"specification: validated {name} {identity['sha256']}", flush=True)
        if args.run:
            stack = json.loads(os.environ.get('FPL_SPEC_STACK', '[]'))
            if not strings(stack, nonempty=False) or str(root) in stack:
                raise ValueError('recursive specification check or invalid check stack')
            result = run_checks(root, doc, stack + [str(root)])
            _, after = load(root, spec=spec)
            if after != identity:
                raise ValueError('spec, obligation or proof baseline changed during checks')
            return result
        return 0
    except (OSError, ValueError, TypeError) as e:
        print(f'specification: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
