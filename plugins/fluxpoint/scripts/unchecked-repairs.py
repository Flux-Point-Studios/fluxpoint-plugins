#!/usr/bin/env python3
"""List commits since the latest recorded BLOCK that lack a check or acceptance.

Exit 1 stops a graph containing a RedTeamV1 or irreversible node while any
commit is unchecked. Repair-only graphs may run to discharge that list.
Exit 2 means the history or durable records cannot be read safely.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-fA-F]{7,40}")


def git(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if p.returncode:
        raise ValueError('git ' + ' '.join(args) + ': ' + p.stderr.strip())
    return p.stdout.strip()


def compiler():
    spec = importlib.util.spec_from_file_location('unchecked_graph', Path(__file__).with_name('compile-graph.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_json(path):
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise ValueError(str(path) + ': ' + str(e))
    if not isinstance(obj, dict):
        raise ValueError(str(path) + ': expected a run object')
    return obj


def same_repository(value, root):
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return os.path.normcase(os.path.realpath(path if path.is_absolute() else root / path)) == os.path.normcase(str(root))


def reviewed_head(artifact, root):
    summary = artifact.get('summary') or {}
    subjects = []
    for record in artifact.get('redTeamCoverage') or []:
        if isinstance(record, dict) and record.get('verdict') == 'BLOCK':
            subjects.extend((record.get('coverage') or {}).get('subject') or [])
    for container in (summary, summary.get('coverage') or {}, summary.get('review') or {}):
        subjects.extend(container.get('subject') or [])
    for value in (summary.get('results') or {}).values():
        for result in value if isinstance(value, list) else [value]:
            if isinstance(result, dict) and result.get('verdict') == 'BLOCK':
                subjects.extend((result.get('coverage') or {}).get('subject') or [])
    heads = {s.get('head') for s in subjects if isinstance(s, dict)
             and same_repository(s.get('repository'), root)}
    if heads:
        if any(not isinstance(h, str) or not SHA.fullmatch(h) for h in heads):
            raise ValueError('local BLOCK subject has no valid reviewed commit')
        if len(heads) != 1:
            raise ValueError('BLOCK record names multiple reviewed heads for ' + str(root))
        return heads.pop()
    if subjects:
        # Explicit review subjects are authoritative: a local tree sentinel
        # cannot turn a review of another repository into this one's BLOCK.
        return None
    tree = summary.get('tree') or {}
    if isinstance(tree, dict):
        reading = tree.get('baseline') or {}
        if isinstance(reading, dict) and reading.get('head'):
            return reading['head']
        if tree.get('head'):
            return tree['head']
    if subjects:
        # A multi-repository review of other repositories does not define
        # this checkout's repair window.
        return None
    raise ValueError('recorded BLOCK has no reviewed head; record its tree or subject[].head')


def commit_id(root, value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('invalid recorded Git commit ID: ' + repr(value))
    return git(root, 'rev-parse', '--verify', value + '^{commit}')


def inspect(root, ir):
    runs = root / '.claude/fluxpoint/runs'
    artifacts = [(p, load_json(p)) for p in sorted(runs.glob('*.json'))]
    blocks = []
    for path, art in artifacts:
        if art.get('redTeam') == 'BLOCK' or art.get('outcome') == 'BLOCKED-REDTEAM':
            stamp = str(art.get('recordedAt') or art.get('when') or '').replace('T', ' ').rstrip('Z')
            blocks.append((stamp, path.name, art))
    guarded = any(n.get('contract') == 'RedTeamV1' or n.get('irreversible') for n in ir.get('nodes', []))
    latest = None
    for _, source, art in sorted(blocks, key=lambda b: (b[0], b[1]), reverse=True):
        head = reviewed_head(art, root)
        if head:
            latest = (source, head)
            break
    if latest is None:
        return {'blockHead': None, 'unchecked': [], 'blocked': False, 'guarded': guarded}
    source, raw_head = latest
    head = commit_id(root, raw_head)
    git(root, 'merge-base', '--is-ancestor', head, 'HEAD')
    window = git(root, 'rev-list', '--reverse', head + '..HEAD').splitlines()
    checked = set()
    warnings = []

    def discharge(value):
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise ValueError('invalid recorded Git commit ID: ' + repr(value))
        matches = [c for c in window if c.startswith(value.lower())]
        if len(matches) > 1:
            raise ValueError('ambiguous checked commit prefix: ' + value)
        checked.update(matches)

    import repair_checks
    for path, art in artifacts:
        claimed = art.get('checked')
        if isinstance(claimed, list) and claimed and not any(
                isinstance(name, str) and SHA.fullmatch(name) and any(commit.startswith(name.lower()) for commit in window)
                for name in claimed):
            continue  # Old receipts outside this BLOCK window cannot discharge it.
        try:
            assurance = repair_checks.assurance(art, root, path)
            if assurance.startswith('legacy-unchecked'):
                warnings.append(dict(record=path.name, runId=art['runId'], commits=art['checked'],
                    code='LEGACY-REPAIR-UNCHECKED', message='Historical repair receipt lacks native helper-output assurance; '
                    'artifact and commits retained, checked credit revoked. '
                    + ('No immutable history snapshot exists for these legacy bytes. ' if assurance.endswith('-no-history') else '')
                    + 'Run a repair graph or record explicit operator acceptance.'))
                continue
            values = repair_checks.corroborated(art, root=root)
        except (ValueError, TypeError, AttributeError) as e:
            raise ValueError(str(path) + ': ' + str(e))
        for value in values:
            discharge(value)
    store = root / '.claude/fluxpoint/decisions.jsonl'
    if store.exists():
        import decision
        schema = decision.load_schema(str(Path(__file__).resolve().parents[1]))
        try:
            records = decision.read_store(str(store))
        except SystemExit as e:
            raise ValueError(str(e))
        for row in records:
            if not isinstance(row, dict):
                raise ValueError(str(store) + ': expected a decision object')
            record = row.get('record') or {}
            if 'acceptedCommits' not in record:
                continue
            bad = decision.validate(record, schema)
            if row.get('source') != 'decision.py' or not row.get('recordId') or bad:
                raise ValueError(str(store) + ': acceptance is not a valid decision.py record')
            for c in record['acceptedCommits']:
                discharge(c)
    unchecked = []
    for commit in window:
        if commit in checked:
            continue
        trailers = git(root, 'show', '-s', '--format=%(trailers:key=Fluxpoint-Executor,valueonly)', commit)
        unchecked.append({'commit': commit, 'executor': trailers or 'unlabelled',
                          'subject': git(root, 'show', '-s', '--format=%s', commit)})
    return {'blockHead': head, 'blockRecord': source, 'unchecked': unchecked,
            'guarded': guarded, 'blocked': guarded and bool(unchecked), 'warnings': warnings}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default='.')
    ap.add_argument('--graph', default='WORK.md')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    try:
        root = Path(git(args.root, 'rev-parse', '--show-toplevel')).resolve()
        graph = Path(args.graph)
        if not graph.is_absolute():
            graph = root / graph
        module = compiler()
        ir = module.extract_ir(graph.read_text(encoding='utf-8'))
        result = inspect(root, ir)
    except Exception as e:  # malformed durable records must stop preflight
        print('unchecked-repairs: ' + str(e), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print('unchecked-repairs: ' + (result['blockHead'] or 'no recorded BLOCK window'))
        for row in result['unchecked']:
            print(row['commit'] + ' [' + row['executor'] + '] ' + row['subject'])
        if result['blocked']:
            print('Launch stopped: run a repair graph whose FixCheckV1 PASS names these commits, '
                  'or record explicit DecisionV1.acceptedCommits with decision.py.', file=sys.stderr)
    for warning in result.get('warnings', []):
        print('unchecked-repairs: ' + warning['code'] + ' ' + warning['record'] + ': ' + warning['message'], file=sys.stderr)
    return 1 if result['blocked'] else 0


if __name__ == '__main__':
    sys.exit(main())
