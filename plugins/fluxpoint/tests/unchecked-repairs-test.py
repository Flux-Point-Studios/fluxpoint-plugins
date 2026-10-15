#!/usr/bin/env python3
"""Real Git repair windows, checked records, and explicit operator acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


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
        self.artifact('repair', {'checked': [self.first[:12]]})
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

    def test_recorder_copies_only_valid_pass_from_independent_repair_checker(self):
        recorder = module('record-run')
        self.set_graph([{'id': 'check', 'contract': 'FixCheckV1', 'independent': True,
                         'repeat': {'until': "verdict == 'PASS'"}}])
        summary = {'contracts': {'check': 'FixCheckV1'}, 'fixOrigins': [
            {'node': 'check', 'verdict': 'FAIL', 'commits': [self.first]},
            {'node': 'check', 'verdict': 'PASS', 'commits': [self.second]},
            {'node': 'other', 'verdict': 'PASS', 'commits': [self.first]}]}
        self.assertEqual(recorder.checked_commits(summary, self.graph), [self.second])


if __name__ == '__main__':
    unittest.main()
