"""Corroborate positive repair seats against completed Workflow journal calls.

This binds returned seat values to runtime transcripts. A helper's command
output still crosses the model relay; this is not tool-output attestation.
"""
import hashlib
import re
from pathlib import Path

from repair_diff import compact_manifest, manifest_digest


def expected_calls(summary):
    """Label, required returned fields for each positive repair receipt."""
    calls = []
    for row in summary.get('fixOrigins') or []:
        if row.get('verdict') != 'PASS':
            continue
        context, check = row['context'], row['check']
        item, number = context['item'], context['round']
        current = context.get('integrated') or context['current']
        measured = compact_manifest(Path(current['patch']).read_bytes())
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
            findings = [{k: v for k, v in f.items() if k not in ('id', 'source', 'votes')}
                        for f in proof['findings'] if f.get('source') == attacker['spawnId']]
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
    return type(expected) is type(actual) and expected == actual


def corroborate(summary, run_id, projects=None):
    """A hand-written summary cannot invent completed checker/witness seats."""
    import replay_memo
    expected = expected_calls(summary)
    if not expected:
        return None
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
        certificate.append(dict(label=label, agentId=call['agentId'],
            resultSha256=hashlib.sha256(replay_memo.canonical(result).encode('utf-8')).hexdigest()))
    return dict(version=1, runId=run_id, seats=certificate)
