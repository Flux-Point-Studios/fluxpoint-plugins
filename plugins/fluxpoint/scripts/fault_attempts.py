"""One consumed crash, one sequential native/reference pair, retained provenance."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

import background as bg
import fault_policy

DIRECTORY = os.path.join('.claude', 'fluxpoint', 'attest-bg')
RUNNER_FIELDS = ('gate', 'exit', 'nonce', 'headSha', 'treeSha', 'manifestSha256', 'logSha256',
                 'attempt', 'dirty', 'tree', 'host', 'commandSha', 'referenceImage', 'pinsSha256',
                 'deployedLaneSha256', 'dependencies', 'resolutionChecked', 'cancelled')


def runner_facts(row):
    return {key: row.get(key) for key in RUNNER_FIELDS}


def save_launch(root, launch):
    store = bg.Store(root, DIRECTORY)
    token = 'launch_' + launch['nonce']
    with store.locked(token):
        if store.read(token):
            raise ValueError('launch nonce already exists')
        store._write(dict(launch, token=token))


def load_launch(root, nonce):
    if not isinstance(nonce, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', nonce):
        return None
    return bg.Store(root, DIRECTORY).read('launch_' + nonce)


def _row(rows, ident):
    hits = [row for row in rows if isinstance(row, dict) and row.get('attestId') == ident]
    if len(hits) != 1:
        raise ValueError('attestation identity is missing or duplicated: ' + str(ident))
    return hits[0]


def witnessed(at, root, row, cancellation=False):
    """Check the retained wrapper completion, exact token and raw log bytes."""
    token = row.get('token')
    if row.get('witness') != 'wrapper' or not isinstance(token, str):
        raise ValueError('fault attempts require a wrapper-witnessed row')
    rec = bg.Store(root, DIRECTORY).read(token)
    if cancellation:
        valid_state = (rec and rec.get('status') == 'CANCELLED' and rec.get('cancelRequested') is True
                       and row.get('cancelRequested') is True and row.get('cancelled') is True
                       and row.get('exit') in fault_policy.CANCELLATION_EXITS)
    else:
        valid_state = rec and rec.get('status') == 'DONE' and not rec.get('cancelRequested') and not row.get('cancelled')
    if (not valid_state or rec.get('attestId') != row.get('attestId')
            or any(rec.get(k) != row.get(k) for k in RUNNER_FIELDS)):
        raise ValueError('wrapper completion does not bind this attestation')
    path = os.path.join(os.path.abspath(root), DIRECTORY, token + '.log')
    if not rec.get('log') or os.path.realpath(rec['log']) != os.path.realpath(path) or os.path.islink(path):
        raise ValueError('wrapper log token/path does not match its completion')
    with open(path, 'rb') as fh:
        raw = fh.read()
    if hashlib.sha256(raw).hexdigest() != row.get('logSha256'):
        raise ValueError('retained wrapper log hash changed')
    return raw.decode('utf-8', 'replace')


def _policy(at, root, launch):
    gates, findings = at.load_gates(root)
    if findings:
        raise ValueError('; '.join(findings))
    if not getattr(gates, 'reference', None):
        raise ValueError('no digest-pinned reference runtime is declared')
    if not launch or not launch.get('manifestSha256') or fault_policy.manifest_sha(root) != launch['manifestSha256']:
        raise ValueError('manifest differs from the recorded launch stamp')
    retained = load_launch(root, launch.get('nonce'))
    if not retained or any(retained.get(k) != launch.get(k) for k in ('nonce', 'since', 'manifestSha256')):
        raise ValueError('launch stamp does not match the retained original launch')
    return gates


def clean_tree(at, tree, expected):
    if not isinstance(tree, str) or at.head_sha(tree) != expected:
        raise ValueError('gate tree HEAD moved since the failed execution')
    for args in (['diff', '--quiet', 'HEAD'], ['ls-files', '--others', '--exclude-standard']):
        result = subprocess.run(['git', '-C', tree, *args], capture_output=True, text=True, timeout=30)
        if result.returncode or result.stdout.strip():
            raise ValueError('gate tree must be clean, including staged changes and untracked files')


def reference_command(at, root, gates, gate, raw):
    """Run the same deployed pin probes inside the reference before the suite."""
    hashes, prefixes = {}, []
    for name, lane in getattr(gates, 'deployed', {}).items():
        if lane['gate'] != gate:
            continue
        with open(os.path.join(root, lane['pins']), 'rb') as fh:
            data = fh.read()
        hashes[name] = hashlib.sha256(data).hexdigest()
        pins = exact_pins(data)
        argv, cwd, flags = at.deployed_invocation(root, gates, gate)
        relative = (argv[0] if os.path.isabs(lane['python']) else os.path.relpath(argv[0], cwd)).replace('\\', '/')
        if '/' not in relative:
            relative = './' + relative
        probe = ('import importlib.metadata as m,json,sys\n'
                 'pins=json.loads(sys.argv[1]); actual={}\n'
                 'for p in pins:\n'
                 ' try: actual[p]=m.version(p)\n'
                 ' except m.PackageNotFoundError: actual[p]=None\n'
                 'ok=actual==pins\n'
                 'print("FPL_REFERENCE_RESOLUTION="+json.dumps(dict(lane=sys.argv[2],versions=actual,ok=ok),sort_keys=True))\n'
                 'sys.exit(0 if ok else 1)')
        command = ' '.join(shlex.quote(part) for part in [relative, *flags, '-c', probe, json.dumps(pins), name])
        cd = os.path.relpath(cwd, root).replace('\\', '/')
        prefixes.append('(cd ' + shlex.quote(cd) + ' && ' + command + ')')
    return ' && '.join(prefixes + [raw]), hashes


def exact_pins(data):
    pins = {}
    for line in data.decode('utf-8').splitlines():
        line = re.split(r'\s+#', line, maxsplit=1)[0].strip()
        if line and not line.startswith('#'):
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;*]+)', line)
            if not match:
                raise ValueError('reference deployed resolution needs exact package==version pins')
            key = re.sub(r'[-_.]+', '-', match[1]).lower()
            if key in pins and pins[key] != match[2]:
                raise ValueError('conflicting reference deployed pins')
            pins[key] = match[2]
    if not pins:
        raise ValueError('reference deployed pins are empty')
    return pins


def reference_resolution(log, hashes):
    versions, problems = {}, []
    for line in log.splitlines():
        if line.startswith('FPL_REFERENCE_RESOLUTION='):
            try:
                obj = json.loads(line.split('=', 1)[1])
                if obj['lane'] in versions or obj.get('ok') is not True or not isinstance(obj.get('versions'), dict):
                    raise ValueError('invalid or duplicate resolution')
                versions[obj['lane']] = obj['versions']
            except (ValueError, KeyError, TypeError):
                problems.append('reference deployed resolution was not verified')
    if set(versions) != set(hashes):
        problems.append('reference omitted a deployed resolution probe')
    return versions, problems


def _snapshot(tree, commit, target):
    result = subprocess.run(['git', '-C', tree, 'archive', commit], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as package:
        for member in package.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or member.isdev():
                raise ValueError('unsafe archive member')
            if member.issym() or member.islnk():
                link = Path(member.linkname)
                if link.is_absolute() or '..' in link.parts:
                    raise ValueError('escaping archive link')
        package.extractall(target)
    prefix = subprocess.run(['git', '-C', tree, 'rev-parse', '--show-prefix'], text=True,
                            capture_output=True, check=True).stdout.strip()
    return os.path.join(target, prefix)


def verify_pair(at, root, rows, pair, launch, gates=None):
    """Re-derive every link and observation; a stored green/fault is no proof."""
    gates = _policy(at, root, launch)
    link = pair.get('faultPair')
    if not isinstance(link, dict) or set(link) != {'original', 'native', 'reference', 'image', 'logs'}:
        raise ValueError('invalid faultPair links')
    if link['image'] != gates.reference['image']:
        raise ValueError('fault pair reference digest differs from the pinned manifest')
    ids = [link[key] for key in ('original', 'native', 'reference')]
    if len(set(ids + [pair.get('attestId')])) != 4:
        raise ValueError('fault pair requires three distinct executions')
    original, native, reference = [_row(rows, ident) for ident in ids]
    if any(row.get('dirty') is not False for row in (pair, original, native, reference)):
        raise ValueError('fault pair requires clean original and rerun snapshots')
    if json.loads(witnessed(at, root, pair)) != link:
        raise ValueError('fault pair payload differs from its retained wrapper log')
    logs = [witnessed(at, root, row) for row in (original, native, reference)]
    for row in (original, native, reference):
        if (any(row.get(key) != pair.get(key) for key in ('gate', 'headSha', 'treeSha', 'nonce', 'manifestSha256'))
                or row.get('manifestSha256') != launch.get('manifestSha256')
                or row.get('commandSha') != at.sha(gates[pair['gate']])
                or str(row.get('started') or '') < str(launch.get('since') or '')):
            raise ValueError('fault pair execution identity differs from its launch/gate/tree')
    classification = fault_policy.classify(gates, original, logs[0], launch)
    if not classification or classification != original.get('fault'):
        raise ValueError('original crash cannot be reclassified from the retained log')
    consumed = bg.Store(root, DIRECTORY).read('fault_' + original['attestId'])
    if not consumed or consumed.get('pairToken') != pair['token'] or consumed.get('original') != original['attestId']:
        raise ValueError('fault pair does not own the one consumed crash attempt')
    for role, row in (('native', native), ('reference', reference)):
        if row.get('attempt') != {'original': original['attestId'], 'role': role, 'pairToken': pair['token']} or row.get('faultPair') or row.get('fault'):
            raise ValueError('rerun rows cannot be paired again or carry another fault')
        if row.get('exit') != 0:
            raise ValueError('both recorded reruns must be green')
    if reference.get('referenceImage') != link['image']:
        raise ValueError('reference execution did not name the pinned runtime digest')
    expected_logs = [{'token': row['token'], 'logSha256': row['logSha256']} for row in (original, native, reference)]
    if link['logs'] != expected_logs:
        raise ValueError('fault pair log links differ from the three witnessed logs')
    lanes = {name: lane for name, lane in getattr(gates, 'deployed', {}).items() if lane['gate'] == pair['gate']}
    if lanes:
        current, expected = {}, {}
        for name, lane in lanes.items():
            with open(os.path.join(root, lane['pins']), 'rb') as fh:
                raw = fh.read()
            current[name] = hashlib.sha256(raw).hexdigest()
            expected[name] = exact_pins(raw)
        for row in (native, reference):
            if (not row.get('resolutionChecked') or row.get('pinsSha256') != current
                    or row.get('deployedLaneSha256') != at.deployed_lane_sha(gates, pair['gate'])
                    or row.get('dependencies') != expected):
                raise ValueError('rerun deployed resolution or pins do not match the declared lane')
        observed, problems = reference_resolution(logs[2], current)
        if problems or reference.get('dependencies') != observed:
            raise ValueError('reference deployed resolution cannot be re-derived')
    if pair.get('exit') != 0:
        raise ValueError('red fault pair cannot back a pass')
    return ids


def retained_red_mismatches(at, root, rows, cited, launch):
    """Check retained execution scope before selecting mutable log rows.

    A wrapper completion is a second retained view of a known execution.
    Removing its row or editing gate/tree/nonce/time must not erase a red.
    This does not make two locally writable files a cryptographic boundary.
    """
    store = bg.Store(root, DIRECTORY)
    tree = cited.get('treeSha') or cited.get('headSha')
    problems = []
    for path in sorted(Path(store.directory).glob('*.json')):
        rec = store.read(path.stem)
        if (not rec or rec.get('status') not in ('DONE', 'CANCELLED') or
                not rec.get('attestId') or type(rec.get('exit')) is not int or rec['exit'] == 0 or
                rec.get('gate') != cited.get('gate') or rec.get('nonce') != launch.get('nonce') or
                not at._same_commit(rec.get('treeSha') or rec.get('headSha'), tree) or
                str(rec.get('started') or '') < str(launch.get('since') or '')):
            continue
        hits = [r for r in rows if r.get('attestId') == rec['attestId']]
        if (len(hits) != 1 or hits[0].get('witness') != 'wrapper' or
                hits[0].get('token') != rec['token'] or
                hits[0].get('started') != rec.get('started') or
                bool(hits[0].get('cancelRequested')) != bool(rec.get('cancelRequested')) or
                any(hits[0].get(k) != rec.get(k) for k in RUNNER_FIELDS)):
            problems.append(str(rec['attestId']) + ' (retained red history missing or altered)')
    return problems


def unclosed_reds(at, root, rows, cited, launch, fresh_forge_green=False):
    """A later pass never erases a red attempt in the same run and tree."""
    retained_issues = retained_red_mismatches(at, root, rows, cited, launch)
    tree = cited.get('treeSha') or cited.get('headSha')
    reds = [row for row in rows if row.get('gate') == cited.get('gate') and at._same_commit(row.get('treeSha') or row.get('headSha'), tree)
            and row.get('nonce') == launch.get('nonce') and type(row.get('exit')) is int and row['exit'] != 0
            and str(row.get('started') or row.get('when') or '') >= str(launch.get('since') or '')]
    if fresh_forge_green:
        kept = []
        for row in reds:
            try:
                witnessed(at, root, row, cancellation=True)
            except (OSError, ValueError, TypeError, KeyError):
                kept.append(row)
        reds = kept
    closed, receipts = set(), set()
    for pair in rows:
        if pair.get('gate') == cited.get('gate') and at._same_commit(pair.get('treeSha'), tree) and pair.get('nonce') == launch.get('nonce') and pair.get('faultPair'):
            try:
                ids = verify_pair(at, root, rows, pair, launch)
                closed.add(pair['faultPair']['original'])
                receipts.update(ids + [pair['attestId']])
            except (OSError, ValueError, KeyError, TypeError):
                pass
    events = set(closed)
    for row in reds:
        if not row.get('fault'):
            continue
        try:
            gates = _policy(at, root, launch)
            log = witnessed(at, root, row)
            if fault_policy.classify(gates, row, log, launch) == row['fault']:
                events.add(row['attestId'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return [row.get('attestId') for row in reds if row.get('attestId') not in closed] + retained_issues, receipts, sorted(events)


def rerun(at, root, original_id, nonce, detach=False, token=None, tree=None):
    root = os.path.abspath(root)
    store = bg.Store(root, DIRECTORY)
    try:
        rows = at.read(root)
        original = _row(rows, original_id)
        if not nonce or original.get('nonce') != nonce or original.get('attempt') or original.get('faultPair'):
            raise ValueError('only an original crash from this run nonce can be paired once')
        launch = load_launch(root, nonce)
        gates = _policy(at, root, launch)
        log = witnessed(at, root, original)
        classification = fault_policy.classify(gates, original, log, launch)
        if not classification or classification != original.get('fault'):
            raise ValueError('original row has no reproducible declared crash classification')
        tree = os.path.abspath(tree or original.get('tree') or root)
        if not at._is_place(tree, root, root):
            raise ValueError('rerun tree must be the project or its linked worktree twin')
        clean_tree(at, tree, original['treeSha'])
        consume_token = 'fault_' + original_id
        with store.locked(consume_token):
            consumed = store.read(consume_token)
            if token is None:
                if consumed:
                    raise ValueError('crash already consumed by a rerun pair; no further rerun is allowed')
                token = 'pair_' + at.sha(original_id + '|' + nonce)[:20]
                store._write({'token': consume_token, 'original': original_id, 'pairToken': token, 'nonce': nonce})
                store.write({'token': token, 'gate': original['gate'], 'status': 'RUNNING', 'started': at.now(),
                             'pid': None, 'faultOriginal': original_id, 'nonce': nonce, 'treeSha': original['treeSha']})
            elif not consumed or consumed.get('pairToken') != token or consumed.get('nonce') != nonce:
                raise ValueError('rerun worker has no matching consumed-attempt receipt')
            if not detach:
                consumed = store.read(consume_token)
                if consumed.get('workerStarted'):
                    raise ValueError('rerun pair worker already started; attempts cannot be repeated')
                consumed['workerStarted'] = True
                store._write(consumed)
        if detach:
            proc = bg.spawn([sys.executable, at.__file__, '--root', root, '--tree', tree,
                             '--rerun-fault', original_id, '--nonce', nonce, '--token', token], tree)
            store.write({'token': token, 'pid': proc.pid})
            print('attest: fault pair started as {} — collect with --await {}'.format(token, token), flush=True)
            return 0
        rec = store.write({'token': token, 'pid': os.getpid()})
        if rec.get('cancelRequested'):
            bg.finish(store, token, None)
            return 130
        started = rec['started']
        leg_rows = []
        for role in ('native', 'reference'):
            if (store.read(token) or {}).get('cancelRequested'):
                bg.finish(store, token, None)
                return 130
            clean_tree(at, tree, original['treeSha'])
            _policy(at, root, launch)
            leg_token = token + '_' + role
            attempt = {'original': original_id, 'role': role, 'pairToken': token}
            if role == 'reference':
                with tempfile.TemporaryDirectory(prefix='fluxpoint-reference-') as scratch:
                    snapshot = _snapshot(tree, original['treeSha'], scratch)
                    at._execute(root, original['gate'], at.now(), leg_token, nonce, snapshot,
                                control_token=token, attempt=attempt, reference={'treeSha': original['treeSha']})
            else:
                at._execute(root, original['gate'], at.now(), leg_token, nonce, tree,
                            control_token=token, attempt=attempt)
            leg = next((row for row in at.read(root) if row.get('token') == leg_token), None)
            if leg is None or (store.read(token) or {}).get('cancelRequested'):
                bg.finish(store, token, None)
                return 130
            leg_rows.append(leg)
        native, reference = leg_rows
        link = {'original': original_id, 'native': native['attestId'], 'reference': reference['attestId'],
                'image': gates.reference['image'], 'logs': [{'token': row['token'], 'logSha256': row['logSha256']}
                                                          for row in (original, native, reference)]}
        code = 0 if native['exit'] == reference['exit'] == 0 and reference.get('resolutionChecked') else 1
        body = (json.dumps(link, sort_keys=True) + '\n').encode('utf-8')
        log_path = os.path.join(root, DIRECTORY, token + '.log')
        with open(log_path, 'xb') as fh:
            fh.write(body)
        pair = {'attestId': 'att_' + at.sha(token)[:20], 'token': token, 'witness': 'wrapper',
                'gate': original['gate'], 'command': original['command'], 'commandSha': original['commandSha'],
                'exit': code, 'headSha': original['headSha'], 'treeSha': original['treeSha'], 'nonce': nonce,
                'dirty': False,
                'cancelled': False, 'cancelRequested': False,
                'manifestSha256': launch['manifestSha256'], 'started': started, 'when': at.now(),
                'logSha256': hashlib.sha256(body).hexdigest(), 'faultPair': link}
        def publish(current):
            if not current.get('cancelRequested'):
                current.update(runner_facts(pair))
                at.append_row(root, pair)
                current['attestId'] = pair['attestId']
        complete = bg.finish(store, token, code, before_finish=publish, log=log_path,
                             manifestSha256=pair['manifestSha256'], logSha256=pair['logSha256'],
                             headSha=pair['headSha'], dirty=False)
        print('attest: fault pair {} exit {} -> {}'.format(token, code, complete.get('attestId') or 'cancelled'), flush=True)
        return code if complete.get('attestId') else 130
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        if token:
            bg.fail(store, token, exc)
        print('attest: fault rerun refused: ' + str(exc), file=sys.stderr)
        return 2
