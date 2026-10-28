"""Validate red-team suite coverage against existing execution witnesses."""
import re

OID = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$', re.I)
DIGEST = re.compile(r'^[0-9a-f]{64}$')


def each(value):
    return [row for row in (value if isinstance(value, list) else [value]) if isinstance(row, dict)]


def declared(summary):
    """Typed review suite claims opt into execution-source enforcement."""
    if not isinstance(summary, dict):
        return False
    contracts, results = summary.get('contracts'), summary.get('results')
    if not isinstance(contracts, dict) or not isinstance(results, dict):
        return False
    for node, value in results.items():
        if contracts.get(node) != 'RedTeamV1':
            continue
        for result in each(value):
            coverage = result.get('coverage')
            if isinstance(coverage, dict) and any(coverage.get(field) for field in ('suites', 'citedGates', 'executedGates')):
                return True
    return False


def offered(summary):
    """Only declared prove/cited-step outputs are candidate reusable evidence."""
    pointers = {}
    for node, value in (summary.get('results') or {}).items():
        for result in each(value):
            gate = (summary.get('prove') or {}).get(node)
            if gate and isinstance(result.get('attestId'), str):
                pointers[(gate, result['attestId'])] = result
            citations = result.get('citations')
            for citation in citations if isinstance(citations, list) else []:
                if isinstance(citation, dict) and citation.get('gate') in (summary.get('cites') or {}).get(node, []):
                    pointers[(citation['gate'], citation.get('attestId'))] = citation
    return pointers


def shape(coverage):
    suites = coverage.get('suites', [])
    if not isinstance(suites, list) or any(not isinstance(gate, str) or not gate for gate in suites) or len(set(suites)) != len(suites):
        return 'review suites must be unique gate names'
    all_claims = []
    for field in ('citedGates', 'executedGates'):
        claims = coverage.get(field, [])
        if not isinstance(claims, list): return field + ' must be an array'
        for claim in claims:
            if (not isinstance(claim, dict) or any(not isinstance(claim.get(key), str) or not claim[key]
                    for key in ('gate', 'attestId', 'sha', 'nonce', 'logSha256'))
                    or not OID.fullmatch(claim['sha']) or not DIGEST.fullmatch(claim['logSha256'])
                    or type(claim.get('exit')) is not int):
                return field + ' requires gate, attestId, exact sha, nonce, logSha256 and integer exit'
            if field == 'executedGates' and (not isinstance(claim.get('reason'), str) or len(claim['reason'].strip()) < 10):
                return 'own suite rerun must name the claim it tests'
            all_claims.append(claim)
    if len({(row['gate'], row['attestId']) for row in all_claims}) != len(all_claims):
        return 'review repeats an execution citation'
    if any(not any(row['gate'] == gate for row in all_claims) for gate in suites):
        return 'review suite claim has neither a reused citation nor its own execution witness'
    return None


def verify(at, root, summary, rows, gates):
    checks = []
    upstream = offered(summary)
    for node, value in (summary.get('results') or {}).items():
        if (summary.get('contracts') or {}).get(node) != 'RedTeamV1': continue
        for index, result in enumerate(each(value)):
            coverage = result.get('coverage')
            if not isinstance(coverage, dict) or not any(key in coverage for key in ('suites', 'citedGates', 'executedGates')): continue
            label = f'{node}:review{index}'
            base = {'node': label, 'declared': True, 'reviewEvidence': True}
            problem = shape(coverage)
            if problem:
                checks.append(dict(base, status='MISMATCH', detail=problem)); continue
            subjects = coverage.get('subject')
            if not isinstance(subjects, list) or not subjects or any(not isinstance(subject, dict)
                    or not isinstance(subject.get('head'), str) or not OID.fullmatch(subject['head']) for subject in subjects):
                checks.append(dict(base, status='MISMATCH', detail='review suite evidence needs pinned reviewed subjects')); continue
            heads = {subject['head'] for subject in subjects}
            node_upstream = upstream
            if 'reviewGateEvidence' in summary:
                snapshots = summary.get('reviewGateEvidence')
                pointers = snapshots.get(node, []) if isinstance(snapshots, dict) else []
                offered_ids = {(pointer.get('gate'), pointer.get('attestId')) for pointer in pointers
                               if isinstance(pointer, dict) and isinstance(pointer.get('gate'), str)
                               and isinstance(pointer.get('attestId'), str)} if isinstance(pointers, list) else set()
                node_upstream = {key: row for key, row in upstream.items() if key in offered_ids}
            for field in ('citedGates', 'executedGates'):
                for claim in coverage.get(field, []):
                    check = dict(base, node=label + ':' + claim['gate'], gate=claim['gate'], claimedExit=claim['exit'], attestId=claim['attestId'])
                    problem = None
                    if claim['gate'] not in gates and claim['gate'] != 'ci':
                        problem = 'review suite is no longer a declared gate'
                    elif field == 'executedGates' and (claim['gate'], claim['attestId']) in node_upstream:
                        problem = 'own rerun cites an upstream execution; record it as reused'
                    elif field == 'citedGates' and (claim['gate'], claim['attestId']) not in node_upstream:
                        problem = 'review citation was not offered by an upstream prove/cited gate'
                    elif claim['sha'] not in heads:
                        problem = 'review citation names a different reviewed head'
                    elif claim['nonce'] != (summary.get('launch') or {}).get('nonce'):
                        problem = 'review citation names a different launch nonce'
                    elif claim['exit'] != 0:
                        problem = 'review cites a red suite as coverage; keep it as a blocker'
                    if problem:
                        checks.append(dict(check, status='MISMATCH', detail=problem)); continue
                    # Reuse is review evidence, not another independent verify node.
                    checked = at._check_cited(rows, check['node'], claim['gate'], claim,
                                              summary.get('launch'), used=None, root=root, gates=gates,
                                              plugin=summary.get('plugin'))
                    checked['reviewEvidence'] = True
                    row = next((row for row in rows if row.get('attestId') == claim['attestId']), {})
                    at._check_deployed(root, gates, claim['gate'], row, claim, checked)
                    actual_runtime = row.get('host') or row.get('referenceImage')
                    if 'runtime' in claim and claim['runtime'] != actual_runtime:
                        checked.update(status='MISMATCH', detail='review citation changes the measured runtime identity')
                    checks.append(checked)
    return checks
