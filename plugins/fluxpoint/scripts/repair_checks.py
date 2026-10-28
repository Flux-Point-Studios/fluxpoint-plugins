"""Validate durable positive repair receipts without reopening a later graph.

The recorder freezes checker descriptors from the actual graph IR. Discovery
corroborates `checked` against those descriptors and full PASS receipts, rather
than treating an arbitrary top-level list as evidence.
"""
import json
from pathlib import Path
import re

from repair_diff import manifest, sibling_issue, coverage_issue, diff_receipt_issue, checked_commits_issue, checker_evidence_issue

COMMIT = re.compile(r'[0-9a-fA-F]{7,40}')
FULL_COMMIT = re.compile(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})')
KEY = re.compile(r'[a-z][a-z0-9-]*')
NONCE = re.compile(r'[A-Za-z0-9_-]{1,64}')
FENCE = re.compile(r'```json\s+graph-ir\s*\n(.*?)\n```', re.S)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def classes(value):
    return (isinstance(value, list) and all(text(v) for v in value)
            and len(set(value)) == len(value))


def descriptors(ir):
    """Only compiler-supported independent fixer/checker pairs can certify."""
    nodes = ir.get('nodes', []) if isinstance(ir, dict) else []
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        return {}
    by_id = {n.get('id'): n for n in nodes if isinstance(n.get('id'), str)}
    if len(by_id) != len(nodes):
        return {}
    source_lists = ir.get('lists') or {}
    if not isinstance(source_lists, dict):
        return {}
    result = {}
    for loop in nodes:
        repeat = loop.get('repeat')
        if not isinstance(repeat, dict) or 'until' not in repeat:
            continue
        fixer = by_id.get(loop.get('verifies')) or {}
        limit = repeat.get('maxRounds')
        items = source_lists.get(loop.get('foreach'), [])
        if not isinstance(items, list):
            continue
        keys = [item.get('key') if isinstance(item, dict) else None for item in items]
        targets = {item['key']: item.get('targetClasses', []) for item in items
                   if isinstance(item, dict) and isinstance(item.get('key'), str)}
        if (loop.get('independent') is not True or fixer.get('mutates') is not True
                or loop.get('after') != fixer.get('id') or not loop.get('foreach')
                or fixer.get('foreach') != loop.get('foreach') or loop.get('isolation') != 'measure'
                or type(limit) is not int or limit < 1 or not keys
                or any(not isinstance(k, str) or not KEY.fullmatch(k) for k in keys)
                or len(set(keys)) != len(keys) or any(not classes(v) for v in targets.values())):
            continue
        until = repeat.get('until')
        accepted = 'PASS' if loop.get('contract') == 'FixCheckV1' else 'SHIP' if loop.get('contract') == 'RedTeamV1' else None
        if accepted is None or not re.fullmatch(r"\s*verdict\s*==\s*'" + accepted + r"'\s*", str(until)):
            continue
        if loop.get('contract') == 'FixCheckV1' and re.fullmatch(r"\s*verdict\s*==\s*'PASS'\s*", str(until)):
            result[loop['id']] = {'contract': 'FixCheckV1', 'independent': True,
                'verifies': fixer['id'], 'after': fixer['id'],
                'fixer': {'id': fixer['id'], 'mutates': True}, 'loop': loop['id'],
                'maxRounds': limit, 'items': keys, 'targetClasses': targets, 'integration': False}
        integration = repeat.get('integrate')
        if not isinstance(integration, list) or len(integration) != 2 or not all(isinstance(k, str) for k in integration):
            continue
        merge, review = (by_id.get(k) or {} for k in integration)
        if (merge.get('mutates') is True and merge.get('after') == loop.get('id')
                and review.get('contract') == 'FixCheckV1' and review.get('independent') is True
                and review.get('verifies') == merge.get('id') and review.get('after') == merge.get('id')
                and review.get('isolation') == 'measure'
                and re.fullmatch(r"\s*verdict\s*!=\s*'PASS'\s*", str(review.get('haltWhen')))):
            result[review['id']] = {'contract': 'FixCheckV1', 'independent': True,
                'verifies': merge['id'], 'after': merge['id'],
                'fixer': {'id': merge['id'], 'mutates': True}, 'loop': loop['id'],
                'integrationNodes': integration, 'maxRounds': limit,
                'items': keys, 'targetClasses': targets, 'integration': True}
    return result


def from_graph(path, root=None):
    try:
        contents = Path(path).read_text(encoding='utf-8')
        blocks = FENCE.findall(contents)
        frozen = descriptors(json.loads(blocks[0])) if len(blocks) == 1 else {}
        import specification
        packet = specification.headers(contents).get('SPEC')
        if packet is None and (Path(root) if root is not None else Path(path).parent).joinpath(specification.SPEC).is_file():
            packet = specification.SPEC
        if packet:
            for descriptor in frozen.values():
                descriptor['packetPath'] = str(specification.in_repo(packet, 'SPEC:'))
        return frozen
    except (OSError, ValueError, TypeError):
        return {}


def descriptor_valid(node, value):
    if not isinstance(value, dict):
        return False
    fixer = value.get('fixer') or {}
    keys = value.get('items')
    return (text(node) and value.get('contract') == 'FixCheckV1'
            and value.get('independent') is True and isinstance(fixer, dict)
            and fixer.get('mutates') is True and text(fixer.get('id')) and fixer['id'] != node
            and value.get('verifies') == fixer['id'] and value.get('after') == fixer['id']
            and text(value.get('loop')) and type(value.get('maxRounds')) is int and value['maxRounds'] >= 1
            and isinstance(keys, list) and bool(keys)
            and all(isinstance(k, str) and KEY.fullmatch(k) for k in keys)
            and len(set(keys)) == len(keys) and isinstance(value.get('targetClasses'), dict)
            and set(value['targetClasses']) == set(keys)
            and all(classes(v) for v in value['targetClasses'].values())
            and type(value.get('integration')) is bool
            and (value['integration'] is False and value['loop'] == node
                 or value['integration'] is True and value.get('integrationNodes') == [fixer['id'], node]))


def commit_list(values):
    return (isinstance(values, list) and bool(values)
            and all(isinstance(c, str) and COMMIT.fullmatch(c) for c in values)
            and len({c.lower() for c in values}) == len(values))


def _patch_key(patch):
    return (patch.get('item'), patch.get('round'), patch.get('patch')) if isinstance(patch, dict) else None


def receipt_issue(row, descriptor, summary):
    if not isinstance(row, dict) or row.get('verdict') != 'PASS':
        return 'receipt is not a PASS object'
    check = row.get('check')
    if (not isinstance(check, dict) or check.get('verdict') != 'PASS'
            or check.get('findings') != [] or check.get('blockers') != []
            or not isinstance(check.get('evidence'), list) or not check['evidence']
            or not all(text(e) for e in check['evidence'])):
        return 'PASS lacks a coherent full check with empty findings/blockers and executed evidence'
    if any(row.get(field) != check.get(field) for field in ('verdict', 'findings', 'blockers', 'evidence')) or row.get('commits', []) != check.get('commits', []):
        return 'receipt disagrees with the full checker result'
    if check.get('commits') and not commit_list(check['commits']):
        return 'PASS does not name unique checked commits'
    if (summary.get('contracts') or {}).get(row.get('node')) != 'FixCheckV1':
        return 'receipt contract is not FixCheckV1'
    number, item = row.get('round'), row.get('item')
    if type(number) is not int or not 1 <= number <= descriptor['maxRounds']:
        return 'receipt round is outside the frozen repair graph'
    integration = descriptor['integration']
    if (integration and (item != '_integration' or row.get('integration') is not True)
            or not integration and (item not in descriptor['items'] or row.get('integration') is True)):
        return 'receipt item or integration identity differs from the checker'
    context, launch = row.get('context'), summary.get('launch')
    if (not isinstance(context, dict) or context.get('item') != item or context.get('round') != number
            or not isinstance(context.get('base'), str) or not FULL_COMMIT.fullmatch(context['base'])
            or not isinstance(launch, dict) or not text(launch.get('root'))
            or not isinstance(launch.get('nonce'), str) or not NONCE.fullmatch(launch['nonce'])):
        return 'receipt lacks the matching base, item, round and launch context'
    tree = summary.get('tree') or {}
    if not isinstance(tree, dict) or not isinstance(tree.get('baseline') or {}, dict):
        return 'invalid recorded tree context'
    baseline = (tree.get('baseline') or {}).get('head')
    if baseline and baseline != context['base']:
        return 'receipt base differs from the recorded tree'
    root = launch['root'].replace('\\', '/').rstrip('/')

    def patch_valid(patch, allowed):
        if not isinstance(patch, dict) or patch.get('item') not in allowed:
            return False
        r = patch.get('round')
        expected = root + '/.claude/fluxpoint/rounds/' + launch['nonce'] + '/' + str(summary.get('repairReadNonce', '')) + '/' + patch['item'] + '/r' + str(r) + '/patch.diff'
        return (type(r) is int and 1 <= r <= number and isinstance(patch.get('patch'), str)
                and patch['patch'].replace('\\', '/') == expected)

    patches = context.get('patches')
    if (not isinstance(patches, list) or not patches
            or not all(patch_valid(p, descriptor['items'] if integration else [item]) for p in patches)
            or len({_patch_key(p) for p in patches}) != len(patches)):
        return 'receipt does not identify unique actual round patches'
    target_items = {p['item'] for p in patches} if integration else {item}
    expected_targets = {v for key in target_items for v in descriptor['targetClasses'][key]}
    if not classes(context.get('targetClasses')) or set(context['targetClasses']) != expected_targets:
        return 'receipt target classes differ from the frozen repair inputs'
    current = context.get('integrated' if integration else 'current')
    if not patch_valid(current, ['_integration'] if integration else [item]) or current.get('round') != number:
        return 'receipt has no exact current patch'
    if not integration and _patch_key(current) != _patch_key(patches[-1]):
        return 'receipt current patch is not the last slice patch'
    if row.get('origins') is not None and (not isinstance(row['origins'], dict) or any(type(v) is not int or v != 0 for v in row['origins'].values())):
        return 'PASS contradicts recorded defect origins'
    try:
        data = Path(current['patch']).read_bytes()
        measured = manifest(data)
        from repair_diff import compact_manifest, manifest_digest
        expected_manifest = manifest_digest(compact_manifest(data))
        if current.get('patchSha256') != measured['patchSha256']:
            return 'DIFF-UNPROVEN: fixer identity differs from live patch bytes'
    except (OSError, ValueError, UnicodeError) as exc:
        return 'DIFF-UNPROVEN: live patch cannot be read: ' + str(exc)
    packet_path = descriptor.get('packetPath') or summary.get('specificationPath')
    if packet_path and (not isinstance(check.get('packetCheck'), dict) or check['packetCheck'].get('spec') != packet_path):
        return 'PACKET-CHECK-UNPROVEN: fixed check for the declared packet is missing'
    proof = row.get('diffAttack')
    if isinstance(proof, dict) and proof.get('manifestSha256') != expected_manifest:
        return 'DIFF-UNPROVEN: recorded manifest differs from live patch bytes'
    if proof != check.get('diffAttack') or not isinstance(summary.get('repairDiffReviews'), list) or proof not in summary['repairDiffReviews']:
        return 'DIFF-ATTACK-UNPROVEN: full checker and harness summary lack the same diff receipt'
    if not isinstance(proof, dict) or proof.get('readNonce') != summary.get('repairReadNonce') or not isinstance(proof.get('readNonce'), str) or not re.fullmatch('[0-9a-f]{64}', proof['readNonce']):
        return 'DIFF-ATTACK-UNPROVEN: receipt lacks its fresh launch read identity'
    if not classes(check.get('targetClasses')) or set(check['targetClasses']) != expected_targets:
        return 'SIBLINGS-UNPROVEN: checker target classes differ from frozen slice targets'
    issue = (sibling_issue(check.get('siblings'), sorted(expected_targets), row.get('fixerSiblings') or [])
             or coverage_issue(check.get('diffCoverage'), measured, row.get('sharedResources') or [], live_series=True)
             or checker_evidence_issue(check, context, measured, row['node'], integration, summary['repairReadNonce'])
             or diff_receipt_issue(proof, context, measured)
             or checked_commits_issue(launch['root'], context['base'], data, check.get('commits') or []))
    if issue:
        return issue
    witness = check['checkerWitness']
    if any(snapshot.get('snapshot') == witness['snapshot'] or snapshot.get('token') == witness['token'] or snapshot.get('spawnId') == witness['spawnId'] for snapshot in proof['snapshots']):
        return 'CHECKER-UNPROVEN: checker and diff reviewers must use distinct owned snapshots and seats'
    return None


def checked(summary, frozen):
    """Return only individually corroborated PASS commit names and problems."""
    result, problems = set(), []
    if not isinstance(summary, dict) or not isinstance(frozen, dict):
        return [], ['invalid repair receipt container']
    for row in summary.get('fixOrigins') or []:
        if not isinstance(row, dict) or row.get('verdict') != 'PASS':
            continue
        node = row.get('node')
        descriptor = frozen.get(node)
        issue = ('PASS is not from a frozen independent repair checker' if not descriptor_valid(node, descriptor)
                 else receipt_issue(row, descriptor, summary))
        if issue:
            problems.append(str(node) + ': ' + issue)
        else:
            result.update(c.lower() for c in row.get('commits') or [])
    return sorted(result), problems


def corroborated(artifact):
    """Do not upgrade historical nonpositive artifacts or trust bare checked[]."""
    names = artifact.get('checked', [])
    if not isinstance(names, list):
        raise ValueError('checked must be an array')
    if not names:
        return []
    if not commit_list(names) or not text(artifact.get('runId')) or not text(artifact.get('graph')):
        raise ValueError('checked is not a recorder-produced repair claim')
    frozen = artifact.get('repairCheckers')
    if not isinstance(frozen, dict) or not frozen:
        raise ValueError('checked has no frozen independent repair checker descriptors')
    observed, problems = checked(artifact.get('summary'), frozen)
    if problems or sorted(c.lower() for c in names) != observed:
        raise ValueError('checked is not corroborated by coherent independent PASS receipts: ' + '; '.join(problems))
    return observed
