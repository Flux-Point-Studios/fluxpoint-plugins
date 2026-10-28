#!/usr/bin/env python3
"""Real Git repair windows, checked records, and explicit operator acceptance."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class UncheckedRepairsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Repair Test')
        self.git('config', 'user.email', 'repair@example.invalid')
        self.base = self.commit('base')
        self.first = self.commit('repair one\n\nFluxpoint-Executor: codex')
        self.second = self.commit('repair two')
        self.runs = self.root / '.claude/fluxpoint/runs'
        self.runs.mkdir(parents=True)
        self.artifact('block', {'recordedAt': '2026-10-15 12:00:00', 'redTeam': 'BLOCK',
                               'summary': {'tree': {'baseline': {'head': self.base}}}})
        self.graph = self.root / 'WORK.md'
        self.set_graph([{'id': 'review', 'contract': 'RedTeamV1'}])

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        (self.root / 'file').write_text(message)
        self.git('add', 'file')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def artifact(self, name, data):
        (self.runs / (name + '.json')).write_text(json.dumps(data))

    def set_graph(self, nodes):
        self.graph.write_text('```json graph-ir\n' + json.dumps({'nodes': nodes}) + '\n```\n\n'
                              '| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |\n'
                              '|---|---|---|---|---|---|\n')

    def repair_graph(self):
        self.set_graph([
            {'id': 'fix', 'mutates': True, 'foreach': 'slices', 'contract': 'SliceV1'},
            {'id': 'check', 'after': 'fix', 'verifies': 'fix', 'independent': True,
             'foreach': 'slices', 'isolation': 'measure', 'contract': 'FixCheckV1',
             'repeat': {'until': "verdict == 'PASS'", 'maxRounds': 3}},
        ])
        content = self.graph.read_text()
        content = content.replace('"nodes":', '"lists": {"slices": [{"key": "slice", "targetClasses": ["legacy-defect"]}]}, "nodes":', 1)
        self.graph.write_text(content + '\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')

    def pass_summary(self, commits):
        check = {'verdict': 'PASS', 'findings': [], 'blockers': [],
                 'evidence': ['pytest regression.py: original attack rejected, valid control passed'],
                 'commits': commits}
        patch = {'item': 'slice', 'round': 1,
                 'patch': str(self.root) + '/.claude/fluxpoint/rounds/launch/' + '1' * 64 + '/slice/r1/patch.diff'}
        row = {'node': 'check', 'item': 'slice', 'round': 1,
               'verdict': 'PASS', 'findings': [], 'blockers': [],
               'evidence': check['evidence'], 'commits': commits, 'check': check,
               'context': {'base': self.base, 'item': 'slice', 'round': 1,
                           'patches': [patch], 'current': patch, 'targetClasses': ['legacy-defect']}}
        summary = {'outcome': 'COMPLETE', 'contracts': {'check': 'FixCheckV1'},
                'launch': {'root': str(self.root), 'nonce': 'launch'},
                'tree': {'baseline': {'head': self.base}}, 'fixOrigins': [row]}
        # A positive fixture includes the new complete diff proof; negative
        # cases below still mutate one independent obligation at a time.
        from repair_diff import manifest, AXES
        data = subprocess.run(['git', '-C', str(self.root), 'diff', '--binary', self.base, commits[-1]],
                              capture_output=True, check=True).stdout
        path = Path(patch['patch'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        measured = manifest(data)
        from repair_diff import compact_manifest, manifest_digest
        patch['patchSha256'] = measured['patchSha256']
        observed = {'command': 'pytest regression.py', 'result': 'executed attack and valid control passed'}
        check['targetClasses'] = ['legacy-defect']
        check['siblings'] = [dict(finding='legacy-defect', invariant='all sibling paths preserve the invariant',
                                 axis=axis, surface='independent-' + axis, attack=observed, control=observed,
                                 outcome='DEFENDED', reason='independently executed sibling case') for axis in AXES]
        check['diffCoverage'] = {'patchSha256': measured['patchSha256'], 'hunks': [
            dict(id=h['id'], surface='other', attack=observed, control=observed) for h in measured['hunks']]}
        nonce = '1' * 64
        check['checkerWitness'] = dict(status='CLEANED', spawnId='check:slice:r1', base=self.base,
                                       patchSha256=measured['patchSha256'], readNonce=nonce,
                                       snapshot='/snapshot/checker', token='f' * 64,
                                       observations=[dict(base=self.base, patchSha256=measured['patchSha256'], attack=observed, control=observed, reproduces=False)])
        proof = dict(version=1, base=self.base, item='slice', round=1, patch=patch['patch'],
                     patchSha256=measured['patchSha256'], manifestSha256=manifest_digest(compact_manifest(data)), readNonce=nonce, clean=True, blockers=[], refuters=[], findings=[])
        lenses = ['authority', 'value', 'state', 'input', 'operations', 'composition', 'coverage-skeptic']
        proof['attackers'] = [dict(spawnId='repair-diff:slice:r1:attack:' + lens, lens=lens, completed=True,
                                   base=self.base, patchSha256=measured['patchSha256'], observations=[
                                       dict(base=self.base, patchSha256=measured['patchSha256'], attack=observed, control=observed, reproduces=False)]) for lens in lenses]
        proof['snapshots'] = [dict(status='CLEANED', token=format(i + 1, '064x'), snapshot='/snapshot/' + str(i),
                                  spawnId=a['spawnId'], observations=a['observations']) for i, a in enumerate(proof['attackers'])]
        row['diffAttack'] = check['diffAttack'] = proof
        summary['repairDiffReviews'] = [proof]
        summary['repairReadNonce'] = nonce
        return summary

    def record_repair(self, commits):
        self.repair_graph()
        summary = self.pass_summary(commits)
        p = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'),
                            '--root', str(self.root), '--graph', str(self.graph), '--run-id', 'repair'],
                           input=json.dumps(summary), text=True, capture_output=True,
                           env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.set_graph([{'id': 'review', 'contract': 'RedTeamV1'}])
        return json.loads((self.runs / 'repair.json').read_text())

    def run_check(self):
        return subprocess.run([sys.executable, str(SCRIPTS / 'unchecked-repairs.py'),
                               '--root', str(self.root), '--json'], text=True,
                              capture_output=True, timeout=20)

    def test_any_producer_is_checked_and_only_final_trailers_are_reported(self):
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stderr)
        rows = json.loads(p.stdout)['unchecked']
        self.assertEqual([r['commit'] for r in rows], [self.first, self.second])
        self.assertEqual([r['executor'] for r in rows], ['codex', 'unlabelled'])

    def test_checked_prefix_and_explicit_decision_discharge_window(self):
        self.record_repair([self.first[:12]])
        decision = module('decision')
        rec = {'question': 'Accept the remaining unchecked repair?', 'options': [
            {'option': s, 'argued_by': 'operator', 'strongest_objection': 'This alternative carries a concrete verification risk.'}
            for s in ('accept', 'check')], 'chosen': 'accept',
            'rationale': 'The operator explicitly accepts this repair after reviewing its remaining verification risk.',
            'overturned_prior': False, 'frozen_by': 'none', 'reversible': True,
            'evidence': ['manual review'], 'acceptedCommits': [self.second]}
        p = subprocess.run([sys.executable, str(SCRIPTS / 'decision.py'), '--record', '--root',
                            str(self.root), '--graph', str(self.graph), '--id', 'accept-repair'],
                           input=json.dumps(rec), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_check()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['unchecked'], [])
        self.assertTrue(decision.validate(dict(rec, acceptedCommits=['not-a-sha']), decision.load_schema(str(SCRIPTS.parent))))

    def test_repair_graph_can_run_but_irreversible_graph_cannot(self):
        self.set_graph([{'id': 'check', 'contract': 'FixCheckV1'}])
        p = self.run_check()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)['unchecked']), 2)
        self.set_graph([{'id': 'ship', 'irreversible': True}])
        self.assertEqual(self.run_check().returncode, 1)

    def test_latest_local_block_and_subject_repository_binding(self):
        self.artifact('new', {'recordedAt': '2026-10-15 13:00:00', 'redTeam': 'BLOCK',
            'summary': {'subject': [{'repository': str(self.root), 'head': self.first}]}})
        p = self.run_check()
        self.assertEqual([r['commit'] for r in json.loads(p.stdout)['unchecked']], [self.second])

    def test_malformed_records_and_nonancestor_block_stop_preflight(self):
        (self.runs / 'broken.json').write_text('{')
        self.assertEqual(self.run_check().returncode, 2)
        (self.runs / 'broken.json').unlink()
        self.git('checkout', '-q', '--orphan', 'other')
        self.git('commit', '-q', '--allow-empty', '-m', 'unrelated')
        self.assertEqual(self.run_check().returncode, 2)

    def test_recorder_copies_only_coherent_pass_from_actual_pair(self):
        recorder = module('record-run')
        self.repair_graph()
        summary = self.pass_summary([self.second])
        summary['outcome'] = 'HALTED'  # A later halt does not undo an exact earlier check.
        self.assertEqual(recorder.checked_commits(summary, self.graph), [self.second])
        for mutation in ('blocker', 'finding', 'no-check', 'no-evidence', 'wrong-round', 'stale-patch', 'different-commits', 'target-override'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(summary)
                row = bad['fixOrigins'][0]
                if mutation == 'blocker': row['check']['blockers'] = ['not run']
                if mutation == 'finding': row['check']['findings'] = [{'severity': 'HIGH'}]
                if mutation == 'no-check': row.pop('check')
                if mutation == 'no-evidence': row['check']['evidence'] = []
                if mutation == 'wrong-round': row['round'] = 2
                if mutation == 'stale-patch': row['context']['current']['patch'] = '/old/patch.diff'
                if mutation == 'different-commits': row['check']['commits'] = [self.first]
                if mutation == 'target-override': row['context']['targetClasses'] = ['new-defect']
                self.assertEqual(recorder.checked_commits(bad, self.graph), [])
        self.set_graph([{'id': 'check', 'contract': 'FixCheckV1', 'independent': True,
                         'repeat': {'until': "verdict == 'PASS'", 'maxRounds': 3}}])
        self.assertEqual(recorder.checked_commits(summary, self.graph), [])

    def test_positive_artifact_requires_frozen_coherent_receipts(self):
        self.artifact('bare', {'checked': [self.first, self.second]})
        self.assertEqual(self.run_check().returncode, 2)
        (self.runs / 'bare.json').unlink()
        record = self.record_repair([self.first])
        self.assertEqual(json.loads(self.run_check().stdout)['unchecked'][0]['commit'], self.second)
        for mutation in ('missing-descriptors', 'self-verifier', 'extra-commit', 'contradictory-pass'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(record)
                if mutation == 'missing-descriptors': bad.pop('repairCheckers')
                if mutation == 'self-verifier': bad['repairCheckers']['check']['fixer']['id'] = 'check'
                if mutation == 'extra-commit': bad['checked'].append(self.second)
                if mutation == 'contradictory-pass': bad['summary']['fixOrigins'][0]['check']['blockers'] = ['no check ran']
                self.artifact('repair', bad)
                self.assertEqual(self.run_check().returncode, 2)
        self.artifact('repair', record)
        self.assertEqual(self.run_check().returncode, 1)  # Graph changed after recording; descriptor stays valid.

    def test_foreign_review_does_not_borrow_local_tree_head(self):
        self.artifact('foreign', {'recordedAt': '2026-10-15 14:00:00', 'redTeam': 'BLOCK',
            'summary': {'coverage': {'subject': [{'repository': '/another/repository', 'head': self.second}]},
                        'tree': {'baseline': {'head': self.second}}}})
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual([r['commit'] for r in json.loads(p.stdout)['unchecked']], [self.first, self.second])

    def test_local_subject_missing_head_cannot_silently_remove_block_window(self):
        self.artifact('bad-local', {'recordedAt': '2026-10-15 15:00:00', 'redTeam': 'BLOCK',
            'summary': {'coverage': {'subject': [{'repository': str(self.root), 'head': None}]},
                        'tree': {'baseline': {'head': self.second}}}})
        self.assertEqual(self.run_check().returncode, 2)

    def test_integration_checker_requires_its_real_merge_link(self):
        helper = module('repair_checks')
        self.repair_graph()
        content = self.graph.read_text()
        ir = json.loads(content.split('```json graph-ir\n')[1].split('\n```')[0])
        ir['nodes'][1]['repeat']['integrate'] = ['merge', 'review']
        ir['nodes'].extend([
            {'id': 'merge', 'mutates': True, 'after': 'check'},
            {'id': 'review', 'contract': 'FixCheckV1', 'independent': True,
             'verifies': 'merge', 'after': 'merge', 'isolation': 'measure',
             'haltWhen': "verdict != 'PASS'"}])
        self.assertIn('review', helper.descriptors(ir))
        ir['nodes'][-1]['verifies'] = 'fix'
        self.assertNotIn('review', helper.descriptors(ir))


if __name__ == '__main__':
    unittest.main()
