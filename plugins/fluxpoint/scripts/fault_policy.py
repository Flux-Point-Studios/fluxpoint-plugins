"""Pinned, deterministic crash policy. No failure is excused by an agent label."""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

import bounded_regex

MARKERS = ('Windows fatal exception:', 'Fatal Python error:')
REFERENCE_FIELDS = {'image', 'run'}
FAULT_FIELDS = {'id', 'gates', 'host', 'exit', 'logMust', 'logMustNot'}
UNCLASSIFIABLE_EXITS = {124, 130, 137, 143, -2, -9, -15, 3221225786, -1073741510}
CANCELLATION_EXITS = UNCLASSIFIABLE_EXITS - {124}


def manifest_sha(root):
    try:
        with open(os.path.join(root, '.fluxpoint-gates.json'), 'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def policy(doc, gates):
    """Return reference, signatures and strict configuration findings."""
    problems = []
    reference, faults = doc.get('reference'), doc.get('faults', [])
    if reference is not None:
        if (not isinstance(reference, dict) or set(reference) != REFERENCE_FIELDS
                or not isinstance(reference.get('image'), str)
                or not re.fullmatch(r'[^\s@{}$`]+@sha256:[0-9a-f]{64}', reference['image'])
                or not isinstance(reference.get('run'), str)):
            problems.append('reference requires exactly image (immutable @sha256 digest) and run')
        else:
            try:
                reference_argv(reference, '/snapshot', 'true')
            except ValueError as exc:
                problems.append('reference.run: ' + str(exc))
    if not isinstance(faults, list) or len(faults) > 32:
        return reference, [], problems + ['faults must be an array of at most 32 crash signatures']
    if faults and reference is None:
        problems.append('faults requires one digest-pinned reference runtime')
    ids = set()
    for fault in faults:
        if not isinstance(fault, dict) or set(fault) != FAULT_FIELDS:
            problems.append('each fault needs exactly id, gates, host, exit, logMust and logMustNot')
            continue
        ident = fault['id']
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', ident) or ident in ids:
            problems.append('fault.id must be unique lowercase kebab-case')
        else:
            ids.add(ident)
        names = fault['gates']
        if (not isinstance(names, list) or not names or any(not isinstance(x, str) or x not in gates for x in names)
                or len(set(names)) != len(names)):
            problems.append('fault.gates must name unique declared local gates')
        if not isinstance(fault['host'], str) or not re.fullmatch(r'\d+\.\d+\.\d+ [A-Za-z][A-Za-z0-9_ -]*', fault['host']):
            problems.append('fault.host must pin an interpreter version and platform, e.g. 3.11.0 Windows')
        exits = fault['exit']
        if not isinstance(exits, list) or not exits or any(type(x) is not int or x == 0 or x in UNCLASSIFIABLE_EXITS for x in exits):
            problems.append('fault.exit must contain nonzero integer crash exit codes, never timeout/cancellation codes')
        for key in ('logMust', 'logMustNot'):
            patterns = fault[key]
            if (not isinstance(patterns, list) or len(patterns) > 16 or key == 'logMust' and not patterns
                    or any(not isinstance(x, str) or not x or len(x) > 4096 for x in patterns)):
                problems.append('fault.{} must contain at most 16 nonempty bounded regexes'.format(key))
                continue
            for pattern in patterns:
                try:
                    bounded_regex.search_lines(pattern, [], 2)
                except ValueError as exc:
                    problems.append('fault.{} invalid regex: {}'.format(key, exc))
        if isinstance(fault['logMust'], list) and not any(isinstance(p, str) and any(m in p.replace('\\:', ':') for m in MARKERS) for p in fault['logMust']):
            problems.append('fault.logMust must include a faulthandler crash marker')
    return reference, faults, problems


def reference_argv(reference, tree, command):
    """Expand argv without evaluating a host shell. {command} runs IN the image."""
    try:
        words = shlex.split(reference['run'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('invalid runtime argv') from exc
    if any(words.count(token) != 1 for token in ('{image}', '{command}')) or sum(word.count('{tree}') for word in words) != 1:
        raise ValueError('requires one {tree} operand and whole {image} and {command} arguments')
    if any(re.search(r'[{}]', word.replace('{tree}', '').replace('{image}', '').replace('{command}', '')) for word in words):
        raise ValueError('unknown runtime template placeholder')
    if any(word in (';', '|', '&&', '||', '&', '>', '<') for word in words):
        raise ValueError('runtime is literal argv, not a host shell program')
    argv = []
    for word in words:
        if word == '{command}':
            argv.extend(['sh', '-c', command])
        else:
            argv.append(word.replace('{tree}', tree).replace('{image}', reference['image']))
    return argv


def host_for(at, tree, gates, gate):
    """Probe a literal direct/deployed interpreter; wrappers remain unknown."""
    deployed = any(lane['gate'] == gate for lane in getattr(gates, 'deployed', {}).values())
    probe = 'import json,platform,sys; print(json.dumps(dict(version=platform.python_version(),platform=platform.system(),executable=sys.executable)))'
    if deployed:
        argv, cwd, flags = at.deployed_invocation(tree, gates, gate)
        command = [argv[0], *flags, '-c', probe]
    else:
        cwd = os.path.abspath(at._resolve(tree, getattr(gates, 'cds', {}).get(gate, '')))
        try:
            first = shlex.split(gates[gate])[0]
        except (IndexError, ValueError):
            first = 'python'
        if not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', first.replace('\\', '/').rsplit('/', 1)[-1], re.I):
            return None  # A shell/pytest wrapper may select a different interpreter.
        try:
            _, _, flags = at.deployed_invocation(tree, gates, gate)
        except (ValueError, IndexError):
            return None  # A compound shell gate has no one known interpreter.
        interpreter = first
        command = [at._bash(), '-c', ' '.join(shlex.quote(x) for x in [interpreter, *flags, '-c', probe])]
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=15)
        host = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(host, dict) or set(host) != {'version', 'platform', 'executable'} or not all(isinstance(v, str) and v for v in host.values()):
            return None
        return host
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _under_tree(path, tree, windows):
    path, tree = path.replace('\\', '/'), tree.replace('\\', '/').rstrip('/')
    if windows:
        path, tree = path.lower(), tree.lower()
    return path.startswith(tree + '/') or path == tree or not re.match(r'^(?:[A-Za-z]:)?/', path)


def floor(log, tree, host):
    """Plugin-fixed exclusions cannot be removed by a repository signature."""
    lines = log.splitlines()
    if not any(marker in line for marker in MARKERS for line in lines) or 'AssertionError' in log:
        return False
    current, frames = False, 0
    for line in lines:
        if re.search(r'(^|\s)(FAILED|ERROR)(\s|:|$)', line):
            if not re.search(r"worker ['\"]gw\d+['\"] crashed", line):
                return False
        if re.search(r'^\s*Current thread\b', line):
            current = True
            continue
        if current:
            match = re.search(r'\bFile "([^"]+)"', line)
            if match:
                frames += 1
            test_frame = match and re.search(r'(?:^|/)(?:tests?/|test_[^/]*\.py$|[^/]*_test\.py$)', match.group(1).replace('\\', '/'), re.I)
            if match and (test_frame or _under_tree(match.group(1), tree, host.get('platform') == 'Windows')):
                return False
            if not line.strip() or re.match(r'^\s*(?:Thread\b|Extension modules:)', line):
                current = False
    # Without an identified faulting-thread block, absence of repo frames is
    # not evidence about the crash. Wrapper-only error messages stay red.
    return frames > 0


def _search(pattern, lines, deadline=None):
    deadline = min(deadline, time.monotonic() + 3) if deadline is not None else time.monotonic() + 3
    batch, size = [], 0
    for line in lines:
        if len(line) > 1024 * 1024:
            raise ValueError('crash log line exceeds bounded matcher limit')
        if size + len(line) > 512 * 1024 and batch:
            hit = bounded_regex.search_lines(pattern, batch, deadline - time.monotonic())
            if hit is not None:
                return hit
            batch, size = [], 0
        batch.append(line)
        size += len(line)
    return bounded_regex.search_lines(pattern, batch, deadline - time.monotonic())


def classify(gates, row, log, launch):
    host = row.get('host')
    if (row.get('witness') != 'wrapper' or type(row.get('exit')) is not int or row['exit'] == 0 or row['exit'] in UNCLASSIFIABLE_EXITS
            or row.get('dirty') is not False or row.get('cancelled') or row.get('cancelRequested') or row.get('timedOut') or row.get('attempt') or row.get('faultPair')
            or not isinstance(host, dict) or not isinstance(launch, dict)
            or not row.get('manifestSha256') or row['manifestSha256'] != launch.get('manifestSha256')
            or row.get('nonce') != launch.get('nonce') or not floor(log, row.get('tree', ''), host)):
        return None
    deadline = time.monotonic() + 5
    for fault in getattr(gates, 'faults', []):
        if row['gate'] not in fault['gates'] or row['exit'] not in fault['exit'] or fault['host'] != '{} {}'.format(host.get('version'), host.get('platform')):
            continue
        try:
            lines = log.splitlines()
            matched = [{'pattern': pattern, 'line': _search(pattern, lines, deadline)} for pattern in fault['logMust']]
            if all(item['line'] is not None for item in matched) and not any(_search(pattern, lines, deadline) is not None for pattern in fault['logMustNot']):
                return {'id': fault['id'], 'matched': matched}
        except ValueError:
            if time.monotonic() >= deadline:
                return None
            continue
    return None
