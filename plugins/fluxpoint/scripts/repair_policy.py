"""Pinned repair severity/scope policy and explicit named operator residuals."""
import hashlib
import json
from pathlib import Path

SEVERITIES = ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')


def disposition(finding, context, digest, rulings=()):
    for ruling in rulings:
        if ruling.get('finding') == finding.get('finding') and ruling.get('patchSha256') == digest:
            return dict(disposition='OPERATOR-RESIDUAL', ruling={k: ruling[k] for k in ('decisionId', 'recordId')})
    floor = context.get('severityFloor', 'MEDIUM')
    if finding.get('severity') in SEVERITIES and floor in SEVERITIES and SEVERITIES.index(finding['severity']) < SEVERITIES.index(floor):
        return dict(disposition='BELOW-SEVERITY-FLOOR')
    if finding.get('origin') == 'pre-existing' and finding.get('defectClass') not in context.get('targetClasses', []):
        return dict(disposition='OUTSIDE-TARGET-CLASSES')
    return None


def origin_issue(finding, context):
    """Replay shape for residuals, mirrored from repair-origins.js."""
    def text(value):
        return isinstance(value, str) and bool(value.strip())
    def observed(row):
        return (isinstance(row, dict) and all(text(row.get(k)) for k in ('command', 'result', 'controlCommand', 'controlResult'))
                and row.get('controlPassed') is True and type(row.get('reproduces')) is bool)
    def key(row):
        return tuple(row.get(k) for k in ('item', 'round', 'patch'))
    if not isinstance(finding, dict) or finding.get('severity') not in SEVERITIES or not all(text(finding.get(k)) for k in ('finding','defectClass','invariant','classRewire','measurement')):
        return 'residual lacks finding, severity or defect-measurement fields'
    introduced = finding.get('introducedIn')
    if introduced is not None and (type(introduced) is not int or not 1 <= introduced <= context['round']):
        return 'residual introducedIn is outside the repair rounds'
    evidence = finding.get('originEvidence')
    if not isinstance(evidence, dict):
        return 'residual lacks origin evidence'
    if finding.get('origin') == 'spec-text':
        row = evidence.get('text')
        return None if isinstance(row, dict) and row.get('kind') in ('requirement','scope','argv','graph') and all(text(row.get(k)) for k in ('path','command','result')) else 'residual spec-text origin lacks measured text evidence'
    base, rounds = evidence.get('base'), evidence.get('rounds')
    if not observed(base) or base.get('snapshot') != context['base'] or sorted(evidence.get('targetClasses') or []) != sorted(context.get('targetClasses') or []):
        return 'residual origin has a different base or target scope'
    if not isinstance(rounds, list) or len(rounds) != len(context['patches']):
        return 'residual origin does not cover actual repair patches'
    if any(not observed(row) or row['command'] != base['command'] or row['controlCommand'] != base['controlCommand'] for row in rounds):
        return 'residual origin changed its attack or control'
    if len({key(row) for row in rounds}) != len(rounds) or {key(row) for row in rounds} != {key(row) for row in context['patches']}:
        return 'residual origin names different repair patches'
    if context.get('integrated'):
        current = evidence.get('integrated')
        if not observed(current) or key(current) != key(context['integrated']) or current['command'] != base['command'] or current['controlCommand'] != base['controlCommand']:
            return 'residual integration origin differs from the current patch'
    else:
        current = next((row for row in rounds if key(row) == key(context['current'])), None)
    if not current or current.get('reproduces') is not True:
        return 'residual does not reproduce on the current patch'
    first = min((row['round'] for row in rounds if row['reproduces']), default=None)
    expected = ('incomplete-fix' if finding['defectClass'] in context.get('targetClasses', []) else 'pre-existing') if base['reproduces'] else 'fix-introduced' if first else 'cross-slice'
    if finding.get('origin') != expected or expected == 'fix-introduced' and finding.get('introducedIn') != first:
        return 'residual origin contradicts the recorded replay'
    return None


def verified_rulings(root, imports, advertised):
    """Resolve only decision.py records explicitly imported by this graph."""
    if not advertised:
        return []
    if not isinstance(advertised, list):
        raise ValueError('repairResidualRulings must be an array')
    import decision
    records = decision.read_store(str(Path(root) / decision.STORE))
    result = []
    for claim in advertised:
        if not isinstance(claim, dict) or claim.get('decisionId') not in imports:
            raise ValueError('repair residual has no frozen decision import')
        ref = imports[claim['decisionId']]
        if ref != 'latest' and ref != claim.get('recordId'):
            raise ValueError('repair residual ruling differs from the frozen import')
        rows = [r for r in records if isinstance(r, dict) and r.get('recordId') == claim.get('recordId')
                and r.get('id') == claim['decisionId'] and r.get('source') == 'decision.py']
        rows = list({json.dumps(row, sort_keys=True): row for row in rows}.values())
        if len(rows) != 1:
            raise ValueError('repair residual needs one recorded operator decision')
        row = rows[0]
        record = row.get('record')
        if decision.validate(record, decision.load_schema(str(Path(__file__).resolve().parents[1]))):
            raise ValueError('repair residual decision is malformed')
        body = json.dumps(record, sort_keys=True)
        expected = 'dec_' + hashlib.sha256(f"{row['id']}|{row['when']}|{body}".encode()).hexdigest()[:12]
        if expected != row['recordId']:
            raise ValueError('repair residual decision content differs from its record id')
        declared = {k: v for k, v in claim.items() if k not in ('decisionId', 'recordId')}
        if declared not in record.get('acceptedRepairClaims', []):
            raise ValueError('repair residual does not name an accepted claim at this patch digest')
        result.append(claim)
    return result
