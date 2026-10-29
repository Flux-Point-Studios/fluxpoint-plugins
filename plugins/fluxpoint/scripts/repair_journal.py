"""Corroborate positive repair seats against completed Workflow journal calls.

This binds returned seat values to runtime transcripts. A helper's command
outputs are matched to native Bash results with exact helper ownership and
command flags. Semantic adequacy of an experiment remains a review obligation.
"""
import hashlib
import json
import re
import shlex
import math
import os
import shutil
import sys
from pathlib import Path

from repair_diff import compact_manifest, manifest_digest


def expected_calls(summary, retained=None, root=None):
    """Label, required returned fields for each positive repair receipt."""
    calls = []
    for row in summary.get('fixOrigins') or []:
        if row.get('verdict') != 'PASS':
            continue
        context, check = row['context'], row['check']
        item, number = context['item'], context['round']
        current = context.get('integrated') or context['current']
        if retained is None:
            patch_file = Path(current['patch'])
        else:
            from repair_checks import _evidence_file
            patch_file = _evidence_file(root, current['patch'], retained)
        measured = compact_manifest(patch_file.read_bytes())
        prefix = 'repair-witness:'
        suffix = ':' + item + ':r' + str(number) + ':'
        reading = dict(status='READ', base=context['base'], item=item, round=number,
                       patch=current['patch'], manifest=measured, manifestSha256=manifest_digest(measured))
        for phase in ('before-check', 'after-review'):
            calls.append((prefix + 'read' + suffix + phase, reading))
        label = row['node'] + (':r' if row.get('integration') else ':' + item + ':r') + str(number)
        raw_check = {k: v for k, v in check.items() if k not in ('checkerWitness', 'packetCheck', 'diffAttack')}
        calls.append((label, raw_check))
        proof = row['diffAttack']
        for attacker in proof['attackers']:
            raw_findings = [f for f in [*proof['findings'], *proof.get('followups', [])] if f.get('source') == attacker['spawnId']]
            raw_findings.sort(key=lambda f: int(f['id'][1:]) if re.fullmatch(r'P[0-9]+', str(f.get('id'))) else 0)
            findings = [{k: v for k, v in f.items() if k not in ('id', 'source', 'votes', 'disposition', 'ruling')} for f in raw_findings]
            calls.append((attacker['spawnId'], dict(base=attacker['base'], patchSha256=attacker['patchSha256'],
                completed=True, evidence=attacker.get('evidence', []), blockers=[], findings=findings)))
        for vote in proof['refuters']:
            calls.append((vote['spawnId'], {k: vote[k] for k in ('resolution', 'attack', 'control', 'contradiction')}))
        snapshots = [check['checkerWitness'], *proof['snapshots']]
        for snapshot in snapshots:
            seat = snapshot['spawnId']
            calls.append((prefix + 'materialize' + suffix + seat,
                dict(status='READY', base=context['base'], patchSha256=measured['patchSha256'],
                     snapshot=snapshot['snapshot'], token=snapshot['token'])))
            calls.append((prefix + 'cleanup' + suffix + seat,
                {k: snapshot[k] for k in ('status', 'snapshot', 'token', 'observations')}))
        if isinstance(check.get('packetCheck'), dict):
            calls.append((prefix + 'check' + suffix + label, check['packetCheck']))
    return calls


def native_requirements(summary):
    """Reviewed helper modes and snapshot ownership for every positive seat."""
    requirements = {}
    for row in summary.get('fixOrigins') or []:
        if row.get('verdict') != 'PASS':
            continue
        context, check = row['context'], row['check']
        item, number = context['item'], context['round']
        current = context.get('integrated') or context['current']
        identity = dict(root=summary['launch']['root'], base=context['base'], patch=current['patch'], item=item, round=str(number))
        measured = compact_manifest(Path(current['patch']).read_bytes())
        suffix = ':' + item + ':r' + str(number) + ':'
        for phase in ('before-check', 'after-review'):
            requirements['repair-witness:read' + suffix + phase] = dict(mode='read', flags=identity)
        for snapshot in [check['checkerWitness'], *row['diffAttack']['snapshots']]:
            seat = snapshot['spawnId']
            observations = [o for o in snapshot['observations'] if o.get('kind') != 'packet-check']
            requirements[seat] = dict(mode='execute', snapshot=snapshot['snapshot'], token=snapshot['token'], observations=observations,
                origins=[f for f in [*row['diffAttack']['findings'], *row['diffAttack'].get('followups', [])] if f.get('source') == seat and f.get('origin') == 'pre-existing'])
            flags = dict(identity, sha256=measured['patchSha256'])
            flags['manifest-sha256'] = manifest_digest(measured)
            requirements['repair-witness:materialize' + suffix + seat] = dict(mode='materialize', flags=flags)
            requirements['repair-witness:cleanup' + suffix + seat] = dict(mode='cleanup', flags=dict(snapshot=snapshot['snapshot'], token=snapshot['token']))
        if isinstance(check.get('packetCheck'), dict):
            seat = check['checkerWitness']['spawnId']
            requirements['repair-witness:check' + suffix + seat] = dict(mode='check', flags=dict(snapshot=check['checkerWitness']['snapshot'], token=check['checkerWitness']['token'], spec=check['packetCheck']['spec']))
    return requirements


def _helper_call(command, plugin, snapshot=None, script='repair-proof.py'):
    """Only one canonical helper invocation, optionally after cd to its tree."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) >= 3 and argv[0] == 'cd' and argv[2] == '&&':
        if snapshot is None or not _same(snapshot, argv[1], 'snapshot'):
            return None
        argv = argv[3:]
    scripts = str(Path(plugin) / 'scripts').replace('\\', '/')
    def path(value):
        return value.replace('\\', '/').rstrip('/')
    def executable(value, expected):
        resolved = value if Path(value).is_absolute() else None
        return bool(resolved and expected and os.path.normcase(os.path.realpath(resolved)) == os.path.normcase(os.path.realpath(expected)))
    try:
        from process_tree import bash_path
        bash = bash_path()
    except ImportError:
        bash = shutil.which('bash')
    if len(argv) >= 4 and path(argv[1]) == scripts + '/py.sh' and argv[2] == script and executable(argv[0], bash):
        argv = argv[3:]
    elif len(argv) >= 3 and path(argv[1]) == scripts + '/' + script and executable(argv[0], sys.executable):
        argv = argv[2:]
    else:
        return None
    mode, arguments = ('replay', argv) if script == 'repair-replay.py' else (argv[0], argv[1:])
    flags = {}
    if len(arguments) % 2:
        return None
    for index in range(0, len(arguments), 2):
        flag, value = arguments[index:index + 2]
        if not re.fullmatch('--[a-z][a-z0-9-]*', flag):
            return None
        if flag[2:] in flags:
            if script != 'repair-replay.py' or flag != '--target-class':
                return None
            flags[flag[2:]] = ([flags[flag[2:]]] if isinstance(flags[flag[2:]], str) else flags[flag[2:]]) + [value]
        else:
            flags[flag[2:]] = value
    return mode, flags


def _bind_tools(summary, run_id, call, fields, requirement, projects):
    import native_binding
    import replay_memo
    native = native_binding.tool_receipts(run_id, call['agentId'], projects=projects)
    plugin = str(Path(__file__).resolve().parents[1])
    claimed_plugin = (summary.get('plugin') or {}).get('root')
    if claimed_plugin and Path(claimed_plugin).resolve() != Path(plugin):
        raise ValueError('repair helper installation differs from the recorder installation')
    owned = requirement.get('snapshot')
    found = []
    for tool in native['calls']:
        parsed = _helper_call(tool['command'], plugin, owned)
        if parsed is None:
            continue
        mode, flags = parsed
        if mode != requirement['mode']:
            continue
        if mode == 'execute':
            if not _same(owned, flags.get('snapshot'), 'snapshot') or flags.get('token') != requirement['token']:
                continue
            if (set(flags) - {'snapshot','token','control','attack','timeout','series'}
                    or not {'snapshot','token','control','attack'}.issubset(flags)):
                raise ValueError('repair native execute command has unknown or missing flags')
            if 'timeout' in flags:
                try:
                    timeout = float(flags['timeout'])
                except ValueError as exc:
                    raise ValueError('repair native execute timeout is malformed') from exc
                if not math.isfinite(timeout) or not 0 < timeout < 86400:
                    raise ValueError('repair native execute timeout is outside the fixed helper bound')
        elif set(flags) != set(requirement['flags']) or not all(_same(v, flags[k], k) for k, v in requirement['flags'].items()):
            continue
        if tool['exit'] != 0:
            raise ValueError('repair helper native call did not complete successfully: ' + str(tool['toolUseId']))
        try:
            actual = json.loads(tool['output'])
        except (ValueError, TypeError) as exc:
            raise ValueError('repair helper native output is not complete JSON') from exc
        if mode == 'execute':
            if (not isinstance(actual, dict) or not isinstance(actual.get('attack'), dict) or not isinstance(actual.get('control'), dict)
                    or actual['attack'].get('command') != flags['attack']
                    or actual.get('control', {}).get('command') != flags['control']
                    or bool(flags.get('series')) != bool(actual.get('seriesPath'))
                    or flags.get('series') and not _same(flags['series'], actual['seriesPath'], 'patch')):
                raise ValueError('repair execution output differs from its native command flags')
        found.append((tool, actual))
    if requirement['mode'] == 'execute':
        expected = requirement['observations']
        if not expected or not found:
            raise ValueError('repair seat has no native helper executions')
        unused = list(found)
        for observation in expected:
            hit = next((pair for pair in unused if _json_equal(pair[1], observation)), None)
            if hit is None:
                raise ValueError('repair execution log contains an observation with no distinct native Bash helper result')
            unused.remove(hit)
        if unused:
            raise ValueError('repair native helper executions disappeared from the cleanup log')
    elif len(found) != 1 or not _json_equal(found[0][1], native['report']):
        raise ValueError('repair witness needs one native helper result identical to its returned JSON')
    for finding in requirement.get('origins', []):
        expected = {k: finding[k] for k in ('origin', 'originEvidence')}
        if 'introducedIn' in finding:
            expected['introducedIn'] = finding['introducedIn']
        origin = finding['originEvidence']['base']
        matched = None
        for tool in native['calls']:
            parsed = _helper_call(tool['command'], plugin, owned, script='repair-replay.py')
            if not parsed:
                continue
            _, flags = parsed
            if (set(flags) - {'root','context','defect-class','target-class','control','command','timeout'}
                    or not {'context','defect-class','control','command'}.issubset(flags)
                    or flags['defect-class'] != finding['defectClass'] or flags['control'] != origin['controlCommand'] or flags['command'] != origin['command']
                    or tool['exit'] != 0):
                continue
            root = flags.get('root', '.')
            if root != '.' and not _same(root, summary['launch']['root'], 'patch') and not _same(root, native['worktree'], 'patch'):
                continue
            try:
                actual = json.loads(tool['output'])
            except (ValueError, TypeError):
                continue
            if _same(expected, actual):
                matched = (tool, actual)
                break
        if matched is None:
            raise ValueError('pre-existing residual has no native origin replay matching its base and patch evidence')
        if matched not in found:
            found.append(matched)
    return dict(agentId=call['agentId'], provenance=native['provenance'], sources=native['sources'],
                calls=[dict(toolUseId=tool['toolUseId'], nativeSha256=tool['nativeSha256'], normalizations=tool['normalizations']) for tool, _ in found])


def _same(expected, actual, key=''):
    if key in ('patch', 'snapshot') and isinstance(expected, str) and isinstance(actual, str):
        if re.match(r'^(?:[A-Za-z]:[\\/]|[\\/]{2})', expected):
            def norm(value):
                return re.sub(r'^([a-z]):', lambda m: m[1].upper() + ':', value.replace('\\', '/'))
            return norm(expected) == norm(actual)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _same(v, actual[k], k) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(_same(a, b) for a, b in zip(expected, actual))
    if type(expected) in (int, float) and type(actual) in (int, float):
        return expected == actual
    return type(expected) is type(actual) and expected == actual


def _json_equal(expected, actual):
    """Exact JSON shape with numeric equality across JS/Python serialization."""
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and set(expected) == set(actual)
                and all(_json_equal(value, actual[key]) for key, value in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(expected) == len(actual)
                and all(_json_equal(a, b) for a, b in zip(expected, actual)))
    if type(expected) in (int, float) and type(actual) in (int, float):
        return (expected == actual and (type(expected) is int or math.isfinite(expected))
                and (type(actual) is int or math.isfinite(actual)))
    return type(expected) is type(actual) and expected == actual


def corroborate(summary, run_id, projects=None):
    """A hand-written summary cannot invent completed checker/witness seats."""
    import replay_memo
    expected = expected_calls(summary)
    if not expected:
        return None
    requirements = native_requirements(summary)
    calls = replay_memo._journal_calls(run_id, projects)
    by_label = {}
    for call in calls:
        start = call['start']
        options = start.get('opts', start.get('options')) or {}
        label = start.get('label', options.get('label'))
        if isinstance(label, str):
            by_label.setdefault(label, []).append(call)
    certificate = []
    seen = set()
    for label, fields in expected:
        if label in seen:
            raise ValueError('repair journal repeats a required seat: ' + label)
        seen.add(label)
        matches = by_label.get(label, [])
        if len(matches) != 1:
            raise ValueError('repair seat needs one started/completed journal call: ' + label)
        call = matches[0]
        result = replay_memo._result(call.get('done'))
        if not _same(fields, result):
            raise ValueError('repair seat result differs from journal: ' + label)
        native = _bind_tools(summary, run_id, call, fields, requirements[label], projects)
        certificate.append(dict(label=label, agentId=call['agentId'], native=native,
            requiredSha256=hashlib.sha256(replay_memo.canonical(fields).encode('utf-8')).hexdigest(),
            resultSha256=hashlib.sha256(replay_memo.canonical(result).encode('utf-8')).hexdigest()))
    return dict(version=2, assurance='native-helper-output', runId=run_id, seats=certificate)


def certificate_issue(summary, run_id, certificate, retained=None, root=None):
    """Validate the durable recorder certificate without requiring old transcripts."""
    digest = lambda value: isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
    if (not isinstance(certificate, dict) or type(certificate.get('version')) is not int or certificate.get('version') != 2
            or certificate.get('assurance') != 'native-helper-output' or certificate.get('runId') != run_id
            or not isinstance(certificate.get('seats'), list)):
        return 'missing or malformed native repair assurance certificate'
    import replay_memo
    expected = expected_calls(summary, retained=retained, root=root)
    seats = certificate['seats']
    if len(seats) != len(expected):
        return 'native repair certificate has missing or extra seats'
    seen_agents, seen_labels = set(), set()
    for seat, (label, fields) in zip(seats, expected):
        if not isinstance(seat, dict) or seat.get('label') != label or label in seen_labels:
            return 'native repair certificate seat identity differs from its receipt'
        seen_labels.add(label)
        agent = seat.get('agentId')
        if not isinstance(agent, str) or not agent or agent in seen_agents:
            return 'native repair certificate reuses or omits an agent'
        seen_agents.add(agent)
        if (not digest(seat.get('resultSha256'))
                or seat.get('requiredSha256') != hashlib.sha256(replay_memo.canonical(fields).encode('utf-8')).hexdigest()):
            return 'native repair certificate result differs from its receipt'
        native = seat.get('native')
        if (not isinstance(native, dict) or native.get('agentId') != agent
                or native.get('provenance') != 'local-runtime-transcript'
                or not isinstance(native.get('sources'), list) or not native['sources']
                or not isinstance(native.get('calls'), list) or not native['calls']):
            return 'native repair certificate lacks runtime source or tool calls'
        paths, ids = set(), set()
        for source in native['sources']:
            if (not isinstance(source, dict) or not isinstance(source.get('path'), str) or not source['path']
                    or source['path'] in paths or not digest(source.get('sha256'))):
                return 'native repair certificate has malformed source digests'
            paths.add(source['path'])
        for call in native['calls']:
            if (not isinstance(call, dict) or not isinstance(call.get('toolUseId'), str) or not call['toolUseId']
                    or call['toolUseId'] in ids or not digest(call.get('nativeSha256'))
                    or not isinstance(call.get('normalizations'), list)
                    or any(not isinstance(note, str) for note in call['normalizations'])):
                return 'native repair certificate has malformed or repeated tool evidence'
            ids.add(call['toolUseId'])
    return None
