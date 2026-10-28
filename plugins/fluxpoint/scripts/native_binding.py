"""Bind fixed readings to completed local Workflow capture bytes.

The source store is runtime-owned under ordinary operation. It is not signed,
and this module does not claim trust against an actor rewriting that store.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

import replay_memo

LIMIT = 64 * 1024 * 1024


class BindingError(ValueError):
    pass


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BindingError('duplicate JSON field in native capture: ' + key)
        result[key] = value
    return result


def _read(path, store):
    path = Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(store.resolve()):
        raise BindingError('native source escapes the configured projects store')
    try:
        with path.open('rb') as handle:
            raw = handle.read(LIMIT + 1)
    except OSError as exc:
        raise BindingError('cannot read native source ' + path.name) from exc
    if len(raw) > LIMIT:
        raise BindingError('native source exceeds the bounded capture size')
    return raw


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    except (ValueError, UnicodeError) as exc:
        raise BindingError('malformed native JSON') from exc


def _rows(raw):
    rows = [_json(line) for line in raw.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise BindingError('native records must be JSON objects')
    return rows


def _source(run_id, agent_id, projects):
    try:
        directory = replay_memo._run_dir(run_id, projects)
        agent_id = replay_memo._component(agent_id, 'agent id')
    except replay_memo.ReplayError as exc:
        raise BindingError(str(exc)) from exc
    store = replay_memo._projects(projects)
    hits = {path.resolve() for parent in (directory, directory.parent, directory.parent.parent)
            for path in parent.glob('agent-' + agent_id + '.jsonl') if path.is_file()}
    if len(hits) != 1:
        raise BindingError('agent needs exactly one native transcript')
    transcript = hits.pop()
    meta = transcript.with_suffix('.meta.json')
    paths = [directory / 'journal.jsonl', transcript, meta]
    raw = [_read(path, store) for path in paths]
    starts, terminals = [], []
    for event in _rows(raw[0]):
        if event.get('agentId') != agent_id:
            continue
        if event.get('type') == 'started':
            starts.append(event)
        elif event.get('type') in ('result', 'failed'):
            terminals.append(event)
        else:
            raise BindingError('unknown native agent event')
    if len(starts) != 1 or len(terminals) != 1 or terminals[0].get('type') != 'result':
        raise BindingError('native agent has no unique completed result')
    try:
        report = replay_memo._result(terminals[0])
    except (ValueError, TypeError) as exc:
        raise BindingError('native completion has no unambiguous result') from exc
    info = _json(raw[2])
    worktree = info.get('worktreePath') if isinstance(info, dict) else None
    if not isinstance(worktree, str) or not worktree.strip() or not re.match(r'^(?:/|[A-Za-z]:[\\/]|\\\\)', worktree):
        raise BindingError('native metadata has no absolute worktreePath')
    return dict(paths=paths, raw=raw, store=store, start=starts[0], report=report,
                worktree=worktree, rows=_rows(raw[1]))


def _text(content):
    if isinstance(content, str):
        if re.search(r'(?i)(?:\[output truncated|output (?:has been |was )?truncated|[0-9]+ (?:characters|lines) truncated)', content):
            raise BindingError('native output is explicitly truncated; use an artifact digest')
        return content
    if isinstance(content, list) and all(isinstance(item, dict) and item.get('type') == 'text'
                                          and isinstance(item.get('text'), str) for item in content):
        return ''.join(item['text'] for item in content)
    raise BindingError('native Bash output is not complete text')


def _normalize(text, error=None):
    notes = []
    if '\r\n' in text:
        text = text.replace('\r\n', '\n')
        notes.append('crlf-to-lf')
    code = None if error else 0
    prefix = re.match(r'^(?:Exit code:? |Error: Exit code |Command failed with exit code )(-?\d+)\n', text)
    if prefix:
        code = int(prefix[1])
        text = text[prefix.end():]
        notes.append('exit-prefix')
        if code == 0 and error is True or code != 0 and error is False:
            raise BindingError('native error flag contradicts the exit header')
    if text in ('(Bash completed with no output)', '(Bash completed with no output)\n'):
        text = ''
        notes.append('empty-output-marker')
    return code, text, notes


def _tools(rows):
    calls, results, order = {}, {}, []
    for row in rows:
        message = row.get('message', row)
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                raise BindingError('native content block is malformed')
            if item.get('type') == 'tool_use':
                ident = item.get('id')
                if not isinstance(ident, str) or not ident or not isinstance(item.get('input'), dict):
                    raise BindingError('native tool use lacks identity or input')
                if message.get('role', row.get('type')) not in (None, 'assistant'):
                    raise BindingError('native tool use is not an assistant event')
                cwd = row.get('cwd', item.get('cwd'))
                if ident in calls:
                    first = calls[ident]
                    if (first['tool'] != item or cwd is not None and first['cwd'] != cwd):
                        raise BindingError('conflicting duplicate native tool use')
                    continue  # The timestamped first row retains its cwd.
                calls[ident] = dict(tool=item, cwd=cwd)
                order.append(ident)
            elif item.get('type') == 'tool_result':
                if message.get('role', row.get('type')) not in (None, 'user'):
                    raise BindingError('native tool result is not a user transport event')
                if isinstance(row.get('toolUseResult'), dict) and row['toolUseResult'].get('backgroundTaskId'):
                    raise BindingError('native command auto-backgrounded without a completed result')
                ident = item.get('tool_use_id')
                if not isinstance(ident, str) or not ident:
                    raise BindingError('native tool result lacks its use id')
                if ident in results and results[ident] != item:
                    raise BindingError('conflicting duplicate native tool result')
                results.setdefault(ident, item)
    if set(results) - set(calls):
        raise BindingError('native result names an unknown tool use')
    return calls, results, order


def _artifact(worktree, artifact):
    if (not isinstance(artifact, dict) or set(artifact) != {'path', 'sha256'}
            or not isinstance(artifact['path'], str) or not artifact['path']
            or any(ord(c) < 32 for c in artifact['path']) or len(artifact['path']) > 4096
            or re.match(r'^[A-Za-z]:', artifact['path']) or '\\' in artifact['path'] or any(part in ('', '.', '..') for part in artifact['path'].split('/'))
            or not re.fullmatch(r'[0-9a-f]{64}', str(artifact['sha256']))):
        raise BindingError('invalid artifact path or digest')
    root = Path(worktree).resolve()
    path = root / artifact['path']
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise BindingError('artifact path escapes the native worktree')
    try:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
    except OSError as exc:
        raise BindingError('artifact cannot be read back') from exc
    if digest.hexdigest() != artifact['sha256']:
        raise BindingError('artifact bytes differ from the native output digest')
    return artifact['sha256'] + '  ' + artifact['path'] + '\n'


def _compare(source, commands, readings):
    if (not isinstance(commands, list) or not isinstance(readings, list) or len(commands) != len(readings)
            or any(not isinstance(command, str) or not command.strip() for command in commands)):
        raise BindingError('fixed report requires ordered commands and one reading per command')
    calls, results, order = _tools(source['rows'])
    bash = []
    final = []
    for ident in order:
        call = calls[ident]
        name = call['tool'].get('name')
        if name == 'StructuredOutput':
            final.append(ident)
        elif name == 'Bash':
            bash.append(ident)
        else:
            raise BindingError('unlisted native tool: ' + str(name))
    if (len(final) != 1 or not order or order[-1] != final[0]
            or calls[final[0]]['tool']['input'] != source['report']):
        raise BindingError('one final StructuredOutput must match the native completed report')
    if len(bash) != len(commands):
        raise BindingError('native Bash call count differs from the fixed command list')
    bound = []
    for index, (ident, command, reading) in enumerate(zip(bash, commands, readings)):
        call, result = calls[ident], results.get(ident)
        if (call['tool']['input'].get('command') != command
                or call['tool']['input'].get('run_in_background')
                or call['tool']['input'].get('background') or call['tool']['input'].get('async')):
            raise BindingError('native command text/order or foreground mode differs at index ' + str(index))
        if (not isinstance(call['cwd'], str) or replay_memo._tree_root(call['cwd']) != replay_memo._tree_root(source['worktree'])):
            raise BindingError('native Bash cwd differs from worktree metadata')
        if not isinstance(result, dict) or 'is_error' in result and type(result['is_error']) is not bool:
            raise BindingError('native Bash call has no complete result')
        raw = _text(result.get('content'))
        if result.get('backgroundTaskId') or re.search(r'(?im)^(?:Command running in background with ID|Command is running in the background|Background task started)', raw):
            raise BindingError('native command has no foreground completion')
        exit_, output, notes = _normalize(raw, result.get('is_error'))
        if (not isinstance(reading, dict) or set(reading) not in ({'exit', 'output'}, {'exit', 'artifact'})
                or reading['exit'] is not None and type(reading['exit']) is not int or reading['exit'] != exit_):
            raise BindingError('reported exit or reading shape differs from native output')
        expected = _artifact(source['worktree'], reading['artifact']) if 'artifact' in reading else reading['output']
        if not isinstance(expected, str):
            raise BindingError('reported output must be text')
        expected_notes = ['crlf-to-lf'] if '\r\n' in expected else []
        if expected in ('(Bash completed with no output)', '(Bash completed with no output)\n'):
            expected_notes.append('empty-output-marker')
        # Only the native result may carry an exit header. A reading's output
        # is stdout/stderr text, so never interpret an output line as its exit.
        normalized = expected.replace('\r\n', '\n')
        if normalized in ('(Bash completed with no output)', '(Bash completed with no output)\n'):
            normalized = ''
        if output != normalized:
            raise BindingError('reported output differs from native capture at command ' + str(index))
        bound.append(dict(toolUseId=ident, commandSha256=hashlib.sha256(command.encode()).hexdigest(),
                          exit=exit_, strictEqual=raw == expected,
                          nativeSha256=hashlib.sha256(raw.encode()).hexdigest(),
                          reportedSha256=hashlib.sha256(expected.encode()).hexdigest(),
                          normalizations=notes + ['reported-' + note for note in expected_notes if note != 'exit-prefix'],
                          **({'artifact': reading['artifact']} if 'artifact' in reading else {})))
    return bound


def bind(run_id, agent_id, projects=None, report=None):
    source = _source(run_id, agent_id, projects)
    native = source['report']
    if not isinstance(native, dict) or set(native) != {'commands', 'readings'}:
        raise BindingError('native completion is not a closed fixed-reading report')
    if report is not None and report != native:
        raise BindingError('caller report differs from the native completed report')
    commands = _compare(source, native['commands'], native['readings'])
    return _certificate(source, run_id, agent_id, commands)


def _certificate(source, run_id, agent_id, commands):
    for path, original in zip(source['paths'], source['raw']):
        if _read(path, source['store']) != original:
            raise BindingError('native source changed during binding')
    return dict(version=1, bound=True, provenance='local-runtime-transcript', runId=run_id,
                agentId=agent_id, worktree=source['worktree'], commands=commands,
                sources=[dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
                         for path, raw in zip(source['paths'], source['raw'])])


def _labels(run_id, projects):
    try:
        directory = replay_memo._run_dir(run_id, projects)
    except ValueError as exc:
        raise BindingError(str(exc)) from exc
    rows = _rows(_read(directory / 'journal.jsonl', replay_memo._projects(projects)))
    labels = {}
    for row in rows:
        if row.get('type') != 'started' or not row.get('agentId'):
            continue
        options = row.get('opts', row.get('options')) or {}
        label = row.get('label', options.get('label') if isinstance(options, dict) else None)
        if isinstance(label, str):
            labels.setdefault(label, []).append(row['agentId'])
    return labels


def bind_audit(run_id, audit, projects=None):
    """Postprocess probe command evidence; audit judgments remain audit judgments."""
    if (not isinstance(audit, dict) or not isinstance(audit.get('probes'), list)
            or audit.get('mode') not in ('multi', 'single') or audit.get('verdict') not in ('SOUND', 'REWIRE')):
        raise BindingError('invalid graph-audit result')
    labels = _labels(run_id, projects)
    probes, certificates = audit['probes'], []
    expected = {'probe:' + probe['node'] for probe in probes
                if isinstance(probe, dict) and isinstance(probe.get('node'), str)}
    if len(expected) != len(probes) or expected != {label for label in labels if label.startswith('probe:')}:
        raise BindingError('audit probe inventory differs from the native run')
    for probe in probes:
        label = 'probe:' + probe['node']
        if len(labels[label]) != 1:
            raise BindingError('audit probe has no unique source agent: ' + label)
        aid = labels[label][0]
        source = _source(run_id, aid, projects)
        report = source['report']
        if (not isinstance(report, dict) or not isinstance(report.get('commands'), list)
                or report.get('completed') is not True or probe.get('completed') is not True):
            raise BindingError('audit probe did not complete: ' + label)
        native = report['commands']
        relayed = [{key: value for key, value in row.items() if key != 'kind'} for row in probe.get('commands', [])]
        if native != relayed:
            raise BindingError('audit probe receipts differ from the native StructuredOutput')
        commands, readings = [], []
        for receipt in native:
            if (not isinstance(receipt, dict) or set(receipt) != {'original', 'safe', 'outcome', 'evidence', 'exit'}
                    or receipt['outcome'] not in ('ACCEPTED', 'REFUSED')
                    or receipt['outcome'] == 'ACCEPTED' and receipt['exit'] != 0):
                raise BindingError('audit receipt lacks exact command, exit and output evidence')
            commands.append(receipt['safe'])
            readings.append(dict(exit=receipt['exit'], output=receipt['evidence']))
        certificate = _certificate(source, run_id, aid, _compare(source, commands, readings))
        certificates.append(dict(node=probe['node'], label=label, **certificate))
    result = copy.deepcopy(audit)
    result['nativeCommandBinding'] = dict(version=1, bound=True, provenance='local-runtime-transcript',
                                         runId=run_id, probes=certificates)
    result['ready'] = audit['verdict'] == 'SOUND' and audit['mode'] == 'multi'
    return result


def verify_fixed(summary, run_id, projects=None):
    """Corroborate scalar fixed-reading preflight nodes during run recording."""
    contracts, results = summary.get('contracts') or {}, summary.get('results') or {}
    fixed = [node for node, contract in contracts.items() if contract == 'NativeReadingsV1']
    if not fixed:
        return []
    labels = _labels(run_id, projects)
    certificates = []
    for node in fixed:
        if len(labels.get(node, [])) != 1 or not isinstance(results.get(node), dict):
            raise BindingError('fixed reading node needs one scalar completed native call: ' + node)
        certificates.append(dict(node=node, **bind(run_id, labels[node][0], projects, results[node])))
    return certificates


def tool_receipts(run_id, agent_id, projects=None):
    """Read normalized Bash observations for a stricter domain-specific binder.

    Other tools are retained by the native capture but are not certified here.
    Consumers must bind commands to their own reviewed helper/inventory policy.
    """
    source = _source(run_id, agent_id, projects)
    calls, results, order = _tools(source['rows'])
    receipts = []
    for ident in order:
        call = calls[ident]
        if call['tool'].get('name') != 'Bash':
            continue
        inputs, result = call['tool']['input'], results.get(ident)
        command = inputs.get('command')
        if (not isinstance(command, str) or not command or inputs.get('run_in_background')
                or inputs.get('background') or inputs.get('async') or not isinstance(result, dict)):
            raise BindingError('native Bash call is incomplete or backgrounded')
        if (not isinstance(call['cwd'], str)
                or replay_memo._tree_root(call['cwd']) != replay_memo._tree_root(source['worktree'])):
            raise BindingError('native Bash cwd differs from worktree metadata')
        if 'is_error' in result and type(result['is_error']) is not bool:
            raise BindingError('native Bash result has an unknown error flag')
        raw = _text(result.get('content'))
        if result.get('backgroundTaskId') or re.search(r'(?im)^(?:Command running in background with ID|Command is running in the background|Background task started)', raw):
            raise BindingError('native command has no foreground completion')
        code, output, notes = _normalize(raw, result.get('is_error'))
        receipts.append(dict(toolUseId=ident, command=command, cwd=call['cwd'], exit=code,
                             output=output, normalizations=notes,
                             nativeSha256=hashlib.sha256(raw.encode()).hexdigest()))
    certificate = _certificate(source, run_id, agent_id, [])
    return dict(version=1, report=source['report'], worktree=source['worktree'],
                calls=receipts, sources=certificate['sources'], provenance=certificate['provenance'])
