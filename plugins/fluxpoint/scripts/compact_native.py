"""Pinned Compact compiler and native-proof boundary. Never equate simulation with proof."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from process_tree import ProcessTree, remove_tree

CONFIG = '.fluxpoint-compact.json'
BASELINE = '.fluxpoint-compact-baseline.json'
CATEGORIES = ('compact.todo', 'compact.assert_constant', 'compact.no_assert_circuit', 'compact.unjustified_disclose')
SOURCE_TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
EXPORT = re.compile(r'\bexport\s+(?:pure\s+)?circuit\s+([A-Za-z_]\w*)\s*\(')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def relation(value):
    """Only object-key order/JSON whitespace are normalized; every IR field binds."""
    return digest(encoded(value))


def source_code(text):
    return SOURCE_TOKEN.sub(lambda match: ''.join('\n' if char == '\n' else ' ' for char in match[0]), text)


def tracked_native_sources(root):
    """State or witness declarations require native coverage; quoted prose does not."""
    tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '*.compact'], capture_output=True, check=False)
    if tracked.returncode:
        raise ValueError('cannot inventory tracked Compact sources: ' + tracked.stderr.decode('utf-8', errors='replace').strip())
    return {name for name in filter(None, tracked.stdout.decode('utf-8').split('\0'))
            if re.search(r'\b(?:ledger|witness)\b', source_code(local(root, name).read_text(encoding='utf-8')))}


def closing(text, start, opening='(', close=')'):
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening: depth += 1
        if text[index] == close:
            depth -= 1
            if not depth: return index
    raise ValueError('unbalanced Compact source; run the real compiler')


def circuit_statements(text):
    code, out = source_code(text), {}
    def statement(segment):
        # Literal data can carry the assertion itself. Hex escapes keep the
        # shared whitespace normalizer from erasing meaningful spaces in it.
        return ' '.join(SOURCE_TOKEN.sub(lambda m: ' ' if m[0].startswith(('//', '/*')) else 'LITERAL[' + m[0].encode('utf-8').hex() + ']', segment).split())
    for match in EXPORT.finditer(code):
        brace = code.find('{', match.end())
        if brace < 0: raise ValueError('exported circuit lacks a body')
        end = closing(code, brace, '{', '}')
        assertions = []
        for assertion in re.finditer(r'\bassert\s*\(', code[brace:end]):
            begin = brace + assertion.end() - 1
            finish = closing(code, begin)
            assertions.append(statement(text[begin:finish + 1]))
        out[match[1]] = statement(text[match.start():brace]) + ' | ' + ' | '.join(assertions)
    return out


def hatches(name, text, disclosures=None):
    code, rows, lines = source_code(text), [], text.splitlines()
    comments = {}
    for token in SOURCE_TOKEN.finditer(text):
        if token[0].startswith('//'):
            comments[text.count('\n', 0, token.start()) + 1] = token[0]
    def add(category, offset):
        line = code.count('\n', 0, offset) + 1
        rows.append((category, line, lines[line - 1]))
    for match in re.finditer(r'\btodo\b', code): add('compact.todo', match.start())
    for match in re.finditer(r'\bassert\s*\(\s*(?:true|false|[0-9]+)\s*(?=,|\))', code): add('compact.assert_constant', match.start())
    for match in EXPORT.finditer(code):
        brace = code.find('{', match.end())
        end = closing(code, brace, '{', '}')
        if not re.search(r'\bassert\s*\(', code[brace:end]): add('compact.no_assert_circuit', match.start())
    for match in re.finditer(r'\bdisclose\s*\(', code):
        line = code.count('\n', 0, match.start()) + 1
        nearby = '\n'.join(comments.get(n, '') for n in range(max(1, line - 2), line + 1))
        comment = re.search(r'//\s*(?:disclose|disclosure):([^\n]*)', nearby)
        justified = comment and len(comment[1].strip()) >= 20
        reason = (disclosures or {}).get(f'{name}:{line}', '')
        if not justified and not (isinstance(reason, str) and len(reason.strip()) >= 20):
            add('compact.unjustified_disclose', match.start())
    return rows


def mutants(name, text):
    code = source_code(text)
    def row(kind, start, end, replacement):
        return {'file': name, 'line': code.count('\n', 0, start) + 1, 'kind': kind,
                'text': text[:start] + replacement + text[end:]}
    for match in re.finditer(r'\bassert\s*\(', code):
        end = closing(code, match.end() - 1) + 1
        if code[end:end + 1] == ';': end += 1
        yield row('drop-assert', match.start(), end, ' ' * (end - match.start()))
    for match in re.finditer(r'==|!=|<=|>=|(?<=\s)<(?=\s)|(?<=\s)>(?=\s)', code):
        # Comparisons only inside assert expressions; generic type brackets are data.
        if not any(m.start() < match.start() < closing(code, m.end() - 1) for m in re.finditer(r'\bassert\s*\(', code)):
            continue
        yield row('flip-comparison', match.start(), match.end(), {'==': '!=', '!=': '==', '<=': '>', '>=': '<', '<': '>=', '>': '<='}[match[0]])
    for match in re.finditer(r'\bdisclose\s*\(', code):
        end = closing(code, match.end() - 1)
        inside = text[match.end():end]
        yield row('remove-disclose', match.start(), end + 1, inside)
    witnesses = {m[1] for m in re.finditer(r'\bwitness\s+([A-Za-z_]\w*)\s*\(', code)}
    for match in re.finditer(r'\b([A-Za-z_]\w*)\s*\(', code):
        if match[1] not in witnesses or re.search(r'\bwitness\s*$', code[max(0, match.start() - 20):match.start()]): continue
        end = closing(code, match.end() - 1)
        yield row('add-disclose', match.start(), end + 1, 'disclose(' + text[match.start():end + 1] + ')')


def local(root, rel, exists=True):
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
        raise ValueError('Compact artifact path must stay inside its root')
    path = Path(root) / rel
    if not path.resolve().is_relative_to(Path(root).resolve()): raise ValueError('Compact artifact path escapes its root')
    if exists and not path.is_file(): raise ValueError('Compact artifact does not exist: ' + rel)
    return path


def pinned_tool(tool):
    if not isinstance(tool, dict) or set(tool) != {'argv', 'files'}:
        raise ValueError('tool needs exactly argv and pinned files')
    argv = tool['argv']
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv): raise ValueError('invalid tool argv')
    if not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file(): raise ValueError('tool executable must be an existing absolute path')
    files = tool['files']
    if not isinstance(files, dict) or str(Path(argv[0])) not in files: raise ValueError('tool executable must have a SHA-256 pin')
    for arg in argv[1:]:
        if not arg.startswith('-') and not Path(arg).is_absolute():
            raise ValueError('tool launcher paths must be absolute and pinned')
        if Path(arg).is_absolute() and Path(arg).is_file() and str(Path(arg)) not in files:
            raise ValueError('tool launcher input also needs a SHA-256 pin: ' + arg)
    for file, expected in files.items():
        if not Path(file).is_absolute(): raise ValueError('tool pin paths must be absolute')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected) or digest(Path(file).read_bytes()) != expected:
            raise ValueError('toolchain file differs from its pin: ' + file)
    if '--modules' in argv:
        modules = Path(argv[argv.index('--modules') + 1])
        if not modules.is_absolute() or not modules.is_dir(): raise ValueError('inspector modules need an absolute directory')
        for path in modules.rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(modules.resolve()): raise ValueError('module tree symlink escapes its pinned directory')
            if path.is_file():
                if not path.resolve().is_relative_to(modules.resolve()): raise ValueError('module tree file escapes its pinned directory')
                if str(path) not in files: raise ValueError('every inspector module tree file must be pinned: ' + str(path))
    return argv


def run(argv, root, timeout, request=None, env=None):
    """Bound the whole process tree; stdin requests go through an owned launcher."""
    with tempfile.TemporaryDirectory(prefix='fpl-compact-command-') as temp:
        if request is not None:
            path = Path(temp) / 'stdin.json'; path.write_bytes(encoded(request))
            argv = [sys.executable, str(Path(__file__).resolve()), '--native-call', str(path), *argv]
        output = Path(temp) / 'output.log'
        prior = {key: os.environ.get(key) for key in (env or {})}
        try:
            os.environ.update(env or {})
            with output.open('wb') as stream:
                tree = ProcessTree(argv, str(root), stream)
                try:
                    tree.resume()
                    status = tree.proc.wait(timeout=timeout)
                finally:
                    try:
                        if tree.running(): tree.terminate()
                    finally: tree.close()
        finally:
            for key, value in prior.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
        if output.stat().st_size > 8 * 1024 * 1024: raise ValueError('Compact command output exceeds 8 MiB')
        return status, output.read_text(encoding='utf-8')


def load(root, config=CONFIG):
    value = json.loads(local(root, config).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or set(value) != {'version', 'compiler', 'compilerVersion', 'compilerLedger', 'runtimeVersion', 'native', 'inspector', 'params', 'projects'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('invalid Compact manifest shape/version')
    pinned_tool(value['compiler']); pinned_tool(value['native']); pinned_tool(value['inspector'])
    if (value['compilerVersion'], value['compilerLedger'], value['runtimeVersion']) != ('0.31.1', 'ledger-8.0.2', '0.16.0'):
        raise ValueError('supported native profile is compactc 0.31.1 / ledger-8 / compact-runtime 0.16.0; ledger-9 drift refuses')
    if not value['params'] or not isinstance(value['params'], dict): raise ValueError('pin the proving parameter files')
    for name, expected in value['params'].items():
        if not Path(name).is_absolute(): raise ValueError('proving parameter paths must be absolute')
        if digest(Path(name).read_bytes()) != expected: raise ValueError('proving parameters differ from their pin')
    if not isinstance(value['projects'], list) or not value['projects']: raise ValueError('Compact manifest must name projects')
    value['_configSha256'] = relation(value)
    seen = set()
    for project in value['projects']:
        if set(project) != {'source', 'circuits', 'driver', 'cases', 'timeoutSeconds'}: raise ValueError('invalid Compact project shape')
        local(root, project['source'])
        if project['source'] in seen: raise ValueError('duplicate Compact source project')
        seen.add(project['source'])
        if not project['source'].endswith('.compact') or not project['circuits'] or len(set(project['circuits'])) != len(project['circuits']): raise ValueError('invalid Compact source/circuits')
        if not project['cases'] or any(type(v) is not bool for v in project['cases'].values()) or set(project['cases'].values()) != {True, False}: raise ValueError('native cases require positive and negative expectations')
        if not isinstance(project['driver'], list) or not project['driver'] or any(not isinstance(v, str) for v in project['driver']): raise ValueError('invalid proof driver argv')
        if type(project['timeoutSeconds']) is not int or not 0 < project['timeoutSeconds'] <= 7200: raise ValueError('invalid Compact timeout')
    missing = tracked_native_sources(root) - seen
    if missing: raise ValueError('tracked stateful/private Compact sources lack native project coverage: ' + ', '.join(sorted(missing)))
    return value


def native_result(value, expected):
    if not isinstance(value, dict) or set(value) not in ({'valid', 'code'}, {'valid', 'code', 'metrics'}) or type(value['valid']) is not bool:
        raise ValueError('native verifier returned an invalid result')
    if value['code'] not in ('verified', 'invalid-proof', 'invalid-transaction', 'invalid-time', 'untrusted-state', 'unexpected-transaction'):
        raise ValueError('native verifier did not complete a proof check: ' + str(value['code']))
    if value['valid'] != (value['code'] == 'verified'): raise ValueError('inconsistent native proof result')
    return value['valid'] == expected


class CompileFailure(ValueError): pass
class NativeFailure(ValueError): pass


def measure(root, cfg, require_semantic=False):
    compiler, native, inspector = pinned_tool(cfg['compiler']), pinned_tool(cfg['native']), pinned_tool(cfg['inspector'])
    for flag, expected in [('--version', cfg['compilerVersion']), ('--ledger-version', cfg['compilerLedger']), ('--runtime-version', cfg['runtimeVersion'])]:
        status, output = run(compiler + [flag], root, 30)
        if status or output.strip() != expected: raise ValueError('pinned Compact toolchain version mismatch: ' + flag)
    ledger = re.search(r'ledger-(\d+)', cfg['compilerLedger'])
    if not ledger or (tuple(int(v) for v in cfg['compilerVersion'].split('.')) >= (0, 34, 0) and ledger[1] == '8'):
        raise ValueError('Compact compiler/ledger target is incompatible')
    result = {'pins': {}, 'relations': {}, 'costs': {}, 'configSha256': cfg.get('_configSha256', relation(cfg))}
    param_dirs = {str(Path(path).parent) for path in cfg['params'] if Path(path).name.startswith('bls_midnight_')}
    if len(param_dirs) != 1: raise ValueError('proving parameters need one pinned directory')
    for index, project in enumerate(cfg['projects']):
        with tempfile.TemporaryDirectory(prefix='fpl-compact-build-') as temp:
            target = Path(temp) / 'compiled'
            status, output = run(compiler + [str(local(root, project['source'])), str(target)], root, project['timeoutSeconds'], env={'MIDNIGHT_PP': next(iter(param_dirs))})
            if status:
                if status < 0 or (os.name == 'nt' and status >= 0x80000000): raise ValueError('Compact compiler crashed or was killed; no mutant was scored')
                if (target / 'zkir').is_dir() and any((target / 'zkir').glob('*.zkir')):
                    raise ValueError('Compact proving-key backend failed after frontend compilation; mutant was not scored: ' + output[-1500:])
                if not re.search(r'\.compact\s+line\s+\d+\s+char\s+\d+:', output):
                    raise ValueError('Compact compiler failed without an ordinary source diagnostic; no mutant was scored: ' + output[-1500:])
                raise CompileFailure('Compact compilation refused: ' + output[-1500:])
            info = json.loads(local(target, 'compiler/contract-info.json').read_text())
            proof = {c['name'] for c in info.get('circuits', []) if c.get('proof') is True}
            if proof != set(project['circuits']): raise ValueError('compiled proof circuit set differs from manifest (empty/incomplete results refuse)')
            target.joinpath('package.json').write_text('{"type":"module"}')
            for circuit in sorted(proof):
                key = project['source'] + '#' + circuit
                ir = json.loads(local(target, f'zkir/{circuit}.zkir').read_text())
                if not ir.get('instructions'): raise ValueError('empty Compact relation')
                result['relations'][key] = relation(ir)
                for suffix in ('prover', 'verifier'):
                    file = local(target, f'keys/{circuit}.{suffix}')
                    result['pins'][key + ':' + suffix] = digest(file.read_bytes())
                result['pins'][key + ':binary-ir'] = digest(local(target, f'zkir/{circuit}.bzkir').read_bytes())
                result['costs'][key] = {'verifierKeyBytes': local(target, f'keys/{circuit}.verifier').stat().st_size}
            result['pins'][project['source'] + ':code'] = digest(local(target, 'contract/index.js').read_bytes())
            frozen = {path.relative_to(target).as_posix(): digest(path.read_bytes()) for path in target.rglob('*') if path.is_file()}
            def unchanged():
                for name, expected in frozen.items():
                    if digest(local(target, name).read_bytes()) != expected: raise ValueError('proof driver changed a fresh compiler artifact: ' + name)
            argv = []
            for arg in project['driver']:
                value = arg.replace('{artifacts}', str(target)).replace('{root}', str(root))
                if value == arg and (Path(root) / arg).is_file(): value = str((Path(root) / arg).resolve())
                argv.append(value)
            executable = shutil.which(argv[0]) if not Path(argv[0]).is_file() else argv[0]
            if not executable: raise ValueError('proof driver executable is missing')
            argv[0] = str(Path(executable).resolve())
            result['pins'][project['source'] + ':driver-executable'] = digest(Path(executable).read_bytes())
            for position, arg in enumerate(argv[1:], 1):
                if Path(arg).is_file(): result['pins'][project['source'] + ':driver-input-' + str(position)] = digest(Path(arg).read_bytes())
            status, output = run(argv, root, project['timeoutSeconds'], env={'FPL_COMPACT_PARAMS': next(iter(param_dirs))})
            if status: raise ValueError('proof driver failed before complete native controls: ' + output[-1500:])
            unchanged()
            report = json.loads(output)
            if set(report) != {'version', 'complete', 'registry', 'controls', 'witnesses'} or report['version'] != 1 or report['complete'] is not True:
                raise ValueError('proof driver did not produce a complete typed report')
            if not isinstance(report['witnesses'], dict) or set(report['witnesses']) != {row['name'] for row in info.get('witnesses', [])}: raise ValueError('proof driver must name every compiled witness')
            markers = []
            for values in report['witnesses'].values():
                if not isinstance(values, list) or not values or len(values) > 128: raise ValueError('invalid witness marker list')
                for value in values:
                    raw = base64.b64decode(value, validate=True)
                    if len(raw) > 4096: raise ValueError('witness marker exceeds its bound')
                    if len(raw) >= 16:
                        # Ledger field packing can split a Bytes<32> value.
                        # High-entropy 16-byte windows catch either field while
                        # avoiding meaningless one-byte matches in every proof.
                        for offset in range(len(raw) - 15):
                            part = raw[offset:offset + 16]
                            markers.extend((part, part[::-1]))
            registry_path = local(target, report['registry'])
            registry = json.loads(registry_path.read_text())
            controls = report['controls']
            if not isinstance(controls, list) or {row['id'] for row in controls} != set(project['cases']) or len(controls) != len(project['cases']): raise ValueError('native control case set is incomplete or duplicated')
            actual_positive = {circuit: 0 for circuit in proof}; actual_negative = dict(actual_positive); semantic_negative = dict(actual_positive)
            for control in controls:
                if set(control) == {'id', 'circuit', 'proverRefused'} and control['proverRefused'] is True:
                    if control['circuit'] not in proof: raise ValueError('unknown refused proof circuit')
                    if project['cases'][control['id']]: raise NativeFailure('generated circuit refused the genuine proof control: ' + control['id'])
                    semantic_negative[control['circuit']] += 1
                    continue
                if set(control) != {'id', 'circuit', 'request'} or control['circuit'] not in proof: raise ValueError('invalid native control')
                entry = registry['circuits'][control['request']['circuitId']]
                if entry['entryPoint'] != control['circuit']: raise ValueError('native control names a different compiled circuit')
                verifier = local(registry_path.parent, entry['verifierKeyPath'])
                expected_hash = frozen[f"keys/{control['circuit']}.verifier"]
                if digest(verifier.read_bytes()) != expected_hash: raise ValueError('native registry key differs from the fresh compiler output')
                state = local(registry_path.parent, entry['initialContractStatePath'])
                state_key = project['source'] + ':initial-state'
                state_hash = digest(state.read_bytes())
                if state_key in result['pins'] and result['pins'][state_key] != state_hash: raise ValueError('native controls use different constructor state templates')
                result['pins'][state_key] = state_hash
                ir_path = local(target, f"zkir/{control['circuit']}.zkir")
                unchanged()
                status, output = run(inspector + ['--native', json.dumps(native), '--registry', str(registry_path), '--ir', str(ir_path)], root, project['timeoutSeconds'], request=control['request'])
                unchanged()
                if status: raise ValueError('native verifier process failed: ' + output[-1000:])
                observed = json.loads(output)
                if observed.get('valid') is True:
                    costs = observed.get('metrics')
                    if not isinstance(costs, dict) or set(costs) != {'k', 'proofBytes', 'transcriptGas'}: raise ValueError('native inspector must measure k, proof bytes and transcript gas')
                    flat = {k: v for k, v in costs.items() if k != 'transcriptGas'}
                    if not isinstance(costs['transcriptGas'], dict) or not costs['transcriptGas']: raise ValueError('empty native transcript gas measurement')
                    flat.update({'gas.' + k: v for k, v in costs['transcriptGas'].items()})
                    if any(type(v) is not int or v < 0 for v in flat.values()) or flat['k'] <= 0 or flat['proofBytes'] <= 0: raise ValueError('invalid inspected native cost')
                    if not any(Path(file).name == 'bls_midnight_2p' + str(flat['k']) for file in cfg['params']): raise ValueError('observed circuit requested an unpinned proving parameter level')
                    measured_costs = result['costs'][project['source'] + '#' + control['circuit']]
                    for name, value in flat.items(): measured_costs[name] = max(value, measured_costs.get(name, 0))
                if not native_result(observed, project['cases'][control['id']]): raise NativeFailure('native proof case failed: ' + control['id'])
                actual_positive[control['circuit']] += int(observed['valid']); actual_negative[control['circuit']] += int(observed['code'] == 'invalid-proof')
                if observed['valid']:
                    transaction = base64.b64decode(control['request']['transactionBase64'], validate=True)
                    if any(marker in transaction for marker in markers): raise NativeFailure('private witness marker bytes appeared in the native transaction')
            if any(not actual_positive[c] or not actual_negative[c] for c in proof): raise ValueError('each compiled circuit requires genuine and cryptographically rejected native controls')
            if require_semantic and any(not semantic_negative[c] for c in proof): raise ValueError('mutation baseline needs a semantic bad-input prover-refusal case for every circuit, separate from corrupted-proof controls')
    pinned_tool(cfg['compiler']); pinned_tool(cfg['native']); pinned_tool(cfg['inspector'])
    for file, expected in cfg['params'].items():
        if digest(Path(file).read_bytes()) != expected: raise ValueError('proving parameter files changed during measurement')
    return result


def compare(prior, current):
    findings = []
    if not isinstance(prior, dict) or not isinstance(current, dict): return ['Compact baseline must be an object']
    if prior.get('configSha256') != current.get('configSha256'): findings.append('Compact manifest/toolchain policy changed')
    for key in ('pins', 'relations'):
        if prior.get(key) != current.get(key): findings.append('Compact ' + key + ' changed; explicit reviewed baseline required')
    old, new = prior.get('costs', {}), current.get('costs', {})
    if set(old) != set(new): findings.append('Compact measured circuit set changed')
    for circuit, costs in old.items():
        if not isinstance(costs, dict) or any(type(v) is not int or v < 0 for v in costs.values()): findings.append('Compact baseline has invalid cost ceilings: ' + circuit); continue
        if set(costs) != set(new.get(circuit, {})): findings.append('Compact measured cost set changed: ' + circuit); continue
        for name, limit in costs.items():
            if new[circuit][name] > limit: findings.append(f'Compact cost rose: {circuit} {name}: {limit} -> {new[circuit][name]}')
    return findings


def mutation_measure(root, extra):
    config = CONFIG
    if extra:
        if len(extra) != 2 or extra[0] != '--config': return None, ['Compact mutation args support only --config <manifest>']
        config = extra[1]
    try:
        cfg = load(root, config)
        baseline = measure(root, cfg, require_semantic=True)  # A failed native baseline never scores mutants.
        candidates = [row for p in cfg['projects'] for row in mutants(p['source'], local(root, p['source']).read_text())]
        if not candidates: raise ValueError('no Compact circuit mutants; nothing measured')
        killed = survived = unviable = equivalent = 0; survivors = []
        files = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
        for row in candidates:
            temp = Path(tempfile.mkdtemp(prefix='fpl-compact-mutant-'))
            try:
                for name in filter(None, files):
                    source = local(root, name)
                    target = local(temp, name, exists=False); target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
                local(temp, row['file']).write_text(row['text'])
                try: current = measure(temp, cfg, require_semantic=True)
                except CompileFailure:
                    print(f"compact mutation: {row['kind']} {row['file']}:{row['line']} -> unviable")
                    unviable += 1; continue
                except NativeFailure as exc:
                    print(f"compact mutation: {row['kind']} {row['file']}:{row['line']} -> killed ({exc})")
                    killed += 1; continue
                if current['pins'] == baseline['pins'] and current['relations'] == baseline['relations']:
                    print(f"compact mutation: {row['kind']} {row['file']}:{row['line']} -> equivalent (exact generated relation/code/keys/state and driver identity; {relation(current['pins'])})")
                    equivalent += 1; continue
                print(f"compact mutation: {row['kind']} {row['file']}:{row['line']} -> survived")
                survived += 1
                if len(survivors) < 50: survivors.append({'file': row['file'], 'line': row['line'], 'what': row['kind']})
            finally: remove_tree(temp)
        scored = killed + survived
        if not scored: raise ValueError('no viable non-equivalent Compact mutants; nothing scored')
        return {'tool': 'compact', 'toolVersion': 'schema 1', 'score': round(killed / scored, 4), 'killed': killed, 'survived': survived,
                'scored': scored, 'unviable': unviable, 'equivalent': equivalent, 'timeout': 0, 'survivors': survivors}, []
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return None, ['Compact native mutation measurement refused: ' + str(exc)]


if __name__ == '__main__' and len(sys.argv) >= 4 and sys.argv[1] == '--native-call':
    with open(sys.argv[2], 'rb') as stream:
        raise SystemExit(subprocess.run(sys.argv[3:], stdin=stream).returncode)
