"""Deterministic repair-patch identity and PASS coverage checks (#172)."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

AXES = ('field', 'encoding', 'caller', 'network', 'role', 'path')
SURFACES = ('outbound-calls', 'alarms', 'timers', 'caches', 'parsers', 'guards', 'other')
SURFACE_PATTERNS = (
    ('outbound-calls', r'\b(fetch|requests?|https?|providers?|clients?|rpc|outbound)\b'),
    ('alarms', r'\b(alarm\w*|alert\w*|pag(?:e|es|ing)|notify\w*)\b'),
    ('timers', r'\b(timer\w*|sleep|interval\w*|cadence|timeout\w*|deadline\w*|latency|rate.?limit\w*)\b'),
    ('caches', r'\b(cache\w*|memo\w*|ttl)\b'),
    ('parsers', r'\b(pars\w*|decod\w*|encod\w*|json|wrappers?)\b'),
    ('guards', r'\b(if|assert|validat\w*|guard\w*|requir\w*)\b'),
)


def surfaces(content):
    return [name for name, pattern in SURFACE_PATTERNS if re.search(pattern, content, re.I | re.ASCII)] or ['other']


def text(value):
    return isinstance(value, str) and bool(value.strip())


def observation(value):
    return isinstance(value, dict) and all(text(value.get(k)) for k in ('command', 'result'))


def finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def manifest(data):
    """Hash exact bytes and inventory every added text/binary hunk ourselves."""
    digest = hashlib.sha256(data).hexdigest()
    lines = data.decode('utf-8').split('\n')
    hunks, path = [], None
    for i, line in enumerate(lines):
        if line.startswith('diff --git '):
            path = line[len('diff --git '):]
        elif line.startswith('+++ '):
            path = line[4:]
        elif line.startswith('@@ '):
            match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
            if not match or path is None:
                raise ValueError('malformed patch hunk')
            added = []
            for body in lines[i + 1:]:
                if body.startswith(('@@ ', 'diff --git ')):
                    break
                if body.startswith('+'):
                    added.append(body[1:])
            content = '\n'.join(added)
            classified = surfaces(content)
            obligations = []
            if any(s in classified for s in ('outbound-calls', 'timers')):
                obligations.append('latency')
            if 'alarms' in classified:
                obligations.append('alarm-rate')
            if re.search(r'rate.?limit|token.?bucket|burst', content, re.I | re.ASCII):
                obligations.append('provider-pool')
            hunks.append(dict(id='h' + str(i), path=path, newStart=int(match[1]),
                              newLines=int(match[2]) if match[2] else 1,
                              added=added, surfaces=classified, obligations=obligations))
        elif line == 'GIT binary patch' or line.startswith('Binary files '):
            if path is None:
                raise ValueError('binary patch lacks file identity')
            hunks.append(dict(id='h' + str(i), path=path, newStart=0, newLines=0,
                              added=[], surfaces=['other'], obligations=[], binary=True))
    changed = '\n'.join(line[1:] for line in lines if line.startswith(('+', '-')) and not line.startswith(('+++ ', '--- ')))
    return dict(patchSha256=digest, hunks=hunks, changedSurfaces=surfaces(changed))


def compact_manifest(data):
    """Bounded review inventory; source lines stay in the patch artifact."""
    result = manifest(data)
    for hunk in result['hunks']:
        hunk['added'] = bool(hunk['added'])
    return result


def manifest_digest(measured):
    canonical = json.dumps(measured, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()


def sibling_issue(rows, targets, fixer_rows=()):
    if not isinstance(rows, list) or targets and not rows:
        return 'SIBLINGS-UNPROVEN: PASS requires checker sibling observations'
    for row in rows:
        if (not isinstance(row, dict) or not all(text(row.get(k)) for k in ('finding', 'invariant', 'surface', 'reason'))
                or row.get('axis') not in AXES or row.get('outcome') not in ('DEFENDED', 'NOT-APPLICABLE')
                or not observation(row.get('attack')) or not observation(row.get('control'))):
            return 'SIBLINGS-UNPROVEN: malformed or unexecuted sibling row'
        if row['outcome'] == 'NOT-APPLICABLE' and not re.search(r'[^\s:]+:\d+', row['reason']):
            return 'SIBLINGS-UNPROVEN: NOT-APPLICABLE needs inspected snapshot file:line'
    identity = lambda r: (r.get('finding'), r.get('axis'), r.get('surface'))
    if len({identity(r) for r in rows}) != len(rows):
        return 'SIBLINGS-UNPROVEN: duplicate sibling observation'
    for claim in fixer_rows:
        if (not isinstance(claim, dict) or not all(text(claim.get(k)) for k in ('finding', 'axis', 'surface'))
                or not observation(claim.get('attack')) or not observation(claim.get('control'))):
            return 'SIBLINGS-UNPROVEN: malformed fixer sibling claim'
        checked = next((r for r in rows if identity(r) == identity(claim)), None)
        if not checked or any((checked.get(k) or {}).get('command') != (claim.get(k) or {}).get('command') for k in ('attack', 'control')):
            return 'SIBLINGS-UNPROVEN: checker must rerun every fixer-listed attack and control'
    listed = {identity(r) for r in fixer_rows}
    for target in targets:
        for axis in AXES:
            if not any(r['finding'] == target and r['axis'] == axis and identity(r) not in listed for r in rows):
                return 'SIBLINGS-UNPROVEN: each targeted class needs an unlisted sibling on every axis'
    return None


def coverage_issue(coverage, measured, resources=(), live_series=False):
    if not isinstance(coverage, dict) or coverage.get('patchSha256') != measured['patchSha256']:
        return 'DIFF-UNPROVEN: coverage must name the harness-measured patch SHA-256'
    rows = coverage.get('hunks')
    if (not isinstance(rows, list) or len(rows) != len(measured['hunks'])
            or any(not isinstance(r, dict) or not text(r.get('id')) for r in rows)
            or len({r.get('id') for r in rows}) != len(rows)
            or {r.get('id') for r in rows} != {r['id'] for r in measured['hunks']}):
        return 'DIFF-UNPROVEN: every harness-listed added hunk needs exactly one coverage row'
    if any(r.get('surface') not in SURFACES or not observation(r.get('attack')) or not observation(r.get('control')) for r in rows):
        return 'DIFF-UNPROVEN: hunk surface, attack and control observations are required'
    obligations = {v for h in measured['hunks'] for v in h['obligations']}
    operational = coverage.get('operational') or {}
    if not isinstance(operational, dict):
        return 'DIFF-UNPROVEN: operational measurements must be an object'
    if 'latency' in obligations:
        row = operational.get('latency')
        if (not observation(row) or not finite(row.get('deadlineMs'), True) or not finite(row.get('observedMs'))
                or row['observedMs'] > row['deadlineMs']):
            return 'DIFF-UNPROVEN: latency must be measured within a declared deadline'
        if 'provider-pool' in obligations and (not text(row.get('pool')) or not any(isinstance(r, dict) and r.get('resource') == row['pool'] for r in resources)):
            return 'DIFF-UNPROVEN: rate-limited provider pool must be declared in sharedResources'
    if 'alarm-rate' in obligations:
        row = operational.get('alarmRate')
        if (not observation(row) or not text(row.get('seriesPath')) or not Path(row['seriesPath']).is_absolute()
                or not isinstance(row.get('seriesSha256'), str) or not re.fullmatch('[0-9a-f]{64}', row['seriesSha256'])
                or not finite(row.get('durationSeconds'), True) or type(row.get('falsePositives')) is not int or row['falsePositives'] < 0
                or not finite(row.get('maxPerDay')) or row['falsePositives'] * 86400 / row['durationSeconds'] > row['maxPerDay']):
            return 'DIFF-UNPROVEN: alarm rate needs a recorded production series and measured acceptable false-positive rate'
        if live_series:
            try:
                if hashlib.sha256(Path(row['seriesPath']).read_bytes()).hexdigest() != row['seriesSha256']:
                    return 'DIFF-UNPROVEN: production series digest differs from the recorded measurement'
            except OSError:
                return 'DIFF-UNPROVEN: recorded production series cannot be read'
    return None


def diff_receipt_issue(receipt, context, measured):
    def executed(rows):
        return (isinstance(rows, list) and bool(rows)
                and all(isinstance(o, dict) and o.get('base') == context['base']
                        and o.get('patchSha256') == measured['patchSha256']
                        and observation(o.get('attack')) and observation(o.get('control'))
                        and type(o.get('reproduces')) is bool for o in rows))
    if (not isinstance(receipt, dict) or type(receipt.get('version')) is not int or receipt['version'] != 1 or receipt.get('base') != context.get('base')
            or receipt.get('item') != context.get('item') or type(receipt.get('round')) is not int or receipt['round'] != context.get('round')
            or receipt.get('patch') != (context.get('integrated') or context.get('current') or {}).get('patch')
            or receipt.get('patchSha256') != measured['patchSha256'] or receipt.get('clean') is not True
            or receipt.get('blockers') != []):
        return 'DIFF-ATTACK-UNPROVEN: missing, stale or blocked harness-spawned diff receipt'
    attackers, refuters, findings = (receipt.get(k) for k in ('attackers', 'refuters', 'findings'))
    if (not isinstance(attackers, list) or len(attackers) < 2
            or not any(isinstance(a, dict) and a.get('lens') == 'composition' for a in attackers)
            or not all(isinstance(a, dict) and text(a.get('spawnId')) and a.get('completed') is True
                       and a.get('patchSha256') == measured['patchSha256'] and a.get('base') == context['base']
                       and executed(a.get('observations')) for a in attackers)
            or len({a['spawnId'] for a in attackers}) != len(attackers)
            or not isinstance(refuters, list) or not isinstance(findings, list)):
        return 'DIFF-ATTACK-UNPROVEN: complete independent attacker identities are required'
    surfaces = set(measured['changedSurfaces'])
    expected = {'state', 'composition'}
    prefix = 'repair-diff:' + context['item'] + ':r' + str(context['round'])
    if surfaces.intersection(('guards', 'other')) or context.get('targetClasses'):
        expected.add('authority')
    if surfaces.intersection(('parsers', 'other')):
        expected.add('input')
    if surfaces.intersection(('outbound-calls', 'alarms', 'timers', 'caches', 'other')):
        expected.add('operations')
    if surfaces.intersection(('other', 'outbound-calls')):
        expected.add('value')
    skeptic_seat = prefix + ':attack:coverage-skeptic'
    if not any(isinstance(f, dict) and f.get('source') != skeptic_seat for f in findings):
        expected.add('coverage-skeptic')
    if {a.get('lens') for a in attackers} != expected or len(attackers) != len(expected):
        return 'DIFF-ATTACK-UNPROVEN: receipt omits a required surface or no-claims coverage skeptic'
    if any(a['spawnId'] != prefix + ':attack:' + a['lens'] for a in attackers):
        return 'DIFF-ATTACK-UNPROVEN: attacker identity is not its actual harness seat'
    if any(not isinstance(f, dict) or f.get('source') not in {a['spawnId'] for a in attackers} for f in findings):
        return 'DIFF-ATTACK-UNPROVEN: finding has no actual source attacker'
    target = context.get('integrated') or context.get('current')
    for attacker in attackers:
        for observed in attacker['observations']:
            if observed['reproduces'] is not True:
                continue
            represented = False
            for finding in findings:
                if not isinstance(finding, dict) or finding.get('source') != attacker['spawnId']:
                    continue
                evidence = finding.get('originEvidence')
                if not isinstance(evidence, dict):
                    continue
                rounds = evidence.get('rounds') if isinstance(evidence.get('rounds'), list) else []
                current = evidence.get('integrated') or next((r for r in rounds if isinstance(r, dict) and all(r.get(k) == target.get(k) for k in ('item', 'round', 'patch'))), None)
                if (isinstance(current, dict) and current.get('reproduces') is True
                        and current.get('command') == observed['attack']['command'] and current.get('result') == observed['attack']['result']
                        and current.get('controlCommand') == observed['control']['command'] and current.get('controlResult') == observed['control']['result']):
                    represented = True
            if not represented:
                return 'DIFF-ATTACK-UNPROVEN: a logged reproducing attack disappeared without a refuted finding'
    by_id = {}
    for vote in refuters:
        if not isinstance(vote, dict) or not text(vote.get('spawnId')) or vote['spawnId'] in by_id:
            return 'DIFF-ATTACK-UNPROVEN: invalid or duplicate refuter spawn id'
        by_id[vote['spawnId']] = vote
    if set(by_id).intersection(a['spawnId'] for a in attackers):
        return 'DIFF-ATTACK-UNPROVEN: attacker and refuter seats must be disjoint'
    used = set()
    finding_ids = set()
    for finding in findings:
        votes = finding.get('votes') if isinstance(finding, dict) else None
        if (not isinstance(finding, dict) or not text(finding.get('id')) or finding['id'] in finding_ids
                or not isinstance(votes, list) or len(votes) != 3
                or any(not isinstance(v, dict) or not text(v.get('spawnId')) for v in votes)
                or len({v['spawnId'] for v in votes}) != 3):
            return 'DIFF-ATTACK-UNPROVEN: each claim requires three independent executed refutations'
        finding_ids.add(finding['id'])
        expected_votes = {prefix + ':refute:' + finding['id'] + ':' + lens for lens in ('reproduce', 'reachability', 'scope')}
        if {v['spawnId'] for v in votes} != expected_votes:
            return 'DIFF-ATTACK-UNPROVEN: refutation must name the three distinct harness refuter seats'
        for vote in votes:
            if (not isinstance(vote, dict) or vote.get('spawnId') not in by_id or vote != by_id[vote['spawnId']]
                    or vote['spawnId'] in used
                    or vote.get('resolution') != 'REFUTED' or not observation(vote.get('attack'))
                    or not observation(vote.get('control')) or not text(vote.get('contradiction'))
                    or not executed(vote.get('observations')) or any(o['reproduces'] is not False for o in vote['observations'])
                    or not any(o['reproduces'] is False and all(o[k][field] == vote[k][field] for k in ('attack', 'control') for field in ('command', 'result')) for o in vote['observations'])):
                return 'DIFF-ATTACK-UNPROVEN: an open or unexecuted finding cannot discharge a commit'
            used.add(vote['spawnId'])
    if used != set(by_id):
        return 'DIFF-ATTACK-UNPROVEN: receipt contains unrelated refuter votes'
    cleanup = receipt.get('snapshots')
    if (not isinstance(cleanup, list) or len(cleanup) != len(attackers) + len(refuters)
            or any(not isinstance(r, dict) or r.get('status') != 'CLEANED'
                   or not isinstance(r.get('token'), str) or not re.fullmatch('[0-9a-f]{64}', r['token'])
                   or not text(r.get('spawnId')) or not text(r.get('snapshot')) for r in cleanup)
            or len({r['token'] for r in cleanup}) != len(cleanup)
            or len({r['spawnId'] for r in cleanup}) != len(cleanup)
            or len({r['snapshot'] for r in cleanup}) != len(cleanup)
            or {r['spawnId'] for r in cleanup} != {a['spawnId'] for a in attackers}.union(by_id)):
        return 'DIFF-ATTACK-UNPROVEN: every independent snapshot needs a confirmed cleanup receipt'
    seats = {a['spawnId']: a for a in attackers + refuters}
    if any(r.get('observations') != seats[r['spawnId']].get('observations') for r in cleanup):
        return 'DIFF-ATTACK-UNPROVEN: command evidence differs from the fixed cleanup witness execution log'
    return None


def checker_evidence_issue(check, context, measured, node, integration, read_nonce):
    w = check.get('checkerWitness')
    expected = node + (':r' if integration else ':' + context['item'] + ':r') + str(context['round'])
    if (not isinstance(w, dict) or w.get('status') != 'CLEANED' or w.get('spawnId') != expected
            or w.get('base') != context['base'] or w.get('patchSha256') != measured['patchSha256'] or w.get('readNonce') != read_nonce
            or not text(w.get('snapshot')) or not isinstance(w.get('token'), str) or not re.fullmatch('[0-9a-f]{64}', w['token'])):
        return 'CHECKER-UNPROVEN: missing independent checker archive/cleanup witness'
    all_observations = w.get('observations')
    packet = check.get('packetCheck')
    if packet is not None and (not isinstance(packet, dict) or packet.get('kind') != 'packet-check'
            or packet.get('status') != 'CHECKED' or packet.get('exit') != 0
            or packet.get('base') != context['base'] or packet.get('patchSha256') != measured['patchSha256']
            or not isinstance(all_observations, list) or packet not in all_observations):
        return 'PACKET-CHECK-UNPROVEN: fixed packet check did not pass on the exact repair snapshot'
    observations = [o for o in all_observations if not isinstance(o, dict) or o.get('kind') != 'packet-check'] if isinstance(all_observations, list) else all_observations
    if (not isinstance(observations, list) or not observations
            or any(not isinstance(o, dict) or o.get('base') != context['base'] or o.get('patchSha256') != measured['patchSha256']
                   or o.get('reproduces') is not False or not observation(o.get('attack')) or not observation(o.get('control')) for o in observations)):
        return 'CHECKER-UNPROVEN: checker has no bound execution log'
    same = lambda a, b: observation(a) and observation(b) and a['command'] == b['command'] and a['result'] == b['result']
    for row in check['siblings'] + check['diffCoverage']['hunks']:
        if not any(o.get('reproduces') is False and same(row['attack'], o['attack']) and same(row['control'], o['control']) for o in observations):
            return 'CHECKER-UNPROVEN: sibling/hunk attack and control differ from actual checker executions'
    op = check['diffCoverage'].get('operational') or {}
    if 'latency' in op:
        row = op['latency']
        if not any(same(row, o['attack']) and finite(o['attack'].get('elapsedMs')) and row.get('observedMs') == o['attack']['elapsedMs'] for o in observations):
            return 'CHECKER-UNPROVEN: latency differs from helper-measured elapsed time'
    if 'alarmRate' in op:
        row = op['alarmRate']
        if not any(same(row, o['attack']) and row.get('seriesPath') == o.get('seriesPath') and row.get('seriesSha256') == o.get('seriesSha256')
                   and isinstance(o.get('metrics'), dict) and all(row.get(k) == o['metrics'].get(k) for k in ('durationSeconds', 'falsePositives', 'maxPerDay')) for o in observations):
            return 'CHECKER-UNPROVEN: alarm measurement differs from executed JSON and helper-read production series'
    return None


def checked_commits_issue(root, base, data, commits):
    """Bind claimed commits to the exact reviewed tree without changing Git."""
    if not commits:
        return None
    def git(*argv, env=None, input_=None):
        return subprocess.run(['git', '-C', str(root), *argv], env=env, input=input_,
                              capture_output=True, check=True).stdout.decode().strip()
    try:
        with tempfile.TemporaryDirectory(prefix='fluxpoint-repair-index-') as tmp:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / 'index'))
            git('read-tree', base, env=env)
            git('apply', '--cached', '--whitespace=nowarn', '-', env=env, input_=data)
            tree = git('write-tree', env=env)
        resolved = [git('rev-parse', '--verify', commit + '^{commit}') for commit in commits]
        candidates = [commit for commit in resolved if git('rev-parse', commit + '^{tree}') == tree]
        for head in candidates:
            try:
                git('merge-base', '--is-ancestor', base, head)
                for commit in resolved:
                    git('merge-base', '--is-ancestor', base, commit)
                    git('merge-base', '--is-ancestor', commit, head)
                return None
            except subprocess.CalledProcessError:
                continue
        return 'checked commits do not culminate in the exact reviewed patch tree on the launch base'
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return 'checked commit/patch identity cannot be corroborated: ' + str(exc)
