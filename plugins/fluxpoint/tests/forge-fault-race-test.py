#!/usr/bin/env python3
"""Combined #169/#170: live forge rereads cannot hide retained local reds."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import attest as at
import background as bg
import forge_evidence as forge

spec = importlib.util.spec_from_file_location('fault_race_fixture', HERE / 'fault-attempts-test.py')
fault_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fault_fixture)


class ForgeFaultRace(unittest.TestCase):
    def setUp(self):
        self.fixture = fault_fixture.FaultAttempts('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.git = self.fixture.root, self.fixture.git
        code = (self.root / 'gate.py').read_text()
        code = code.replace("reference=os.environ.get", "if os.environ.get('FPL_ASSERT'):\n print('AssertionError: genuine local failure'); sys.exit(1)\nreference=os.environ.get")
        (self.root / 'gate.py').write_text(code)
        (self.root / '.ci.yml').write_text('commands:\n - python gate.py\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'pipeline and assertion fixture')
        self.base = self.git('rev-parse', 'HEAD')
        self.fixture.doc['ci'] = {'forge': 'github', 'repository': 'fixture/repo', 'contexts': ['suite'],
                                 'witness': {'statusCreators': ['ci-bot'], 'checkRunApps': []},
                                 'packetSha': self.base,
                                 'covers': {'suite': {'contexts': ['suite'], 'config': ['.ci.yml', 'gate.py']}}}
        self.fixture.write_manifest()
        self.git('add', '.')
        self.git('commit', '-qm', 'independent CI policy')
        self.head = self.git('rev-parse', 'HEAD')
        self.fixture.launch = at.stamp(str(self.root))
        self.launch = self.fixture.launch
        self.api = {'user': {'login': 'agent'}, 'repos/fixture/repo/pulls/7': {'head': {'sha': self.head}},
                    'repos/fixture/repo/commits/' + self.head + '/statuses?per_page=100&page=1': [
                        {'id': 100, 'context': 'suite', 'state': 'success', 'creator': {'login': 'ci-bot'}}],
                    'repos/fixture/repo/commits/' + self.head + '/check-runs?filter=all&per_page=100&page=1': {'total_count': 0, 'check_runs': []}}
        for commit in (self.base, self.head):
            rows = []
            for entry in subprocess.run(['git', '-C', str(self.root), 'ls-tree', '-rz', commit], capture_output=True, check=True).stdout.split(b'\0'):
                if not entry:
                    continue
                facts, filename = entry.split(b'\t', 1)
                mode, kind, oid = facts.decode().split()
                rows.append(dict(mode=mode, type=kind, sha=oid, path=filename.decode()))
                if kind == 'blob':
                    raw = subprocess.run(['git', '-C', str(self.root), 'cat-file', 'blob', oid], capture_output=True, check=True).stdout
                    self.api['repos/fixture/repo/git/blobs/' + oid] = dict(sha=oid, encoding='base64', size=len(raw), content=base64.b64encode(raw).decode())
            self.api['repos/fixture/repo/git/trees/' + commit + '?recursive=1'] = dict(truncated=False, tree=rows)
        identities = patch.object(forge.GitHub, 'stored_logins', return_value=set())
        identities.start()
        self.addCleanup(identities.stop)
        transport = patch.object(forge.GitHub, 'request', lambda client, path, timeout: copy.deepcopy(self.api[path]))
        transport.start()
        self.addCleanup(transport.stop)

    def forge_row(self):
        self.assertEqual(at.attest_ci(str(self.root), None, pr=7, nonce=self.launch['nonce'], reuse='suite'), 0)
        return self.fixture.rows()[-1]

    def check(self, row):
        claim = dict(gate='suite', exit=0, attestId=row['attestId'], sha=self.head, target='pr:7')
        summary = {'contracts': {'gate': 'HeadCheckV1'}, 'cites': {'gate': ['suite']},
                   'results': {'gate': {'sha': self.head, 'citations': [claim]}},
                   'launch': self.launch, 'tree': {'baseline': {'head': self.head}}}
        checks, problems = at.verify_claims(str(self.root), summary)
        self.assertFalse(problems)
        return checks[0]

    def write_rows(self, rows):
        Path(at.path_for(str(self.root))).write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_completed_assertion_or_crash_dominates_fresh_forge_green(self):
        with patch.dict(os.environ, {'FPL_ASSERT': '1'}):
            code, local = self.fixture.run_gate()
        self.assertEqual(code, 1)
        self.assertFalse(local['cancelled'])
        self.assertEqual(self.check(self.forge_row())['status'], 'SUPERSEDED-RED')

    def test_actual_interrupted_loser_can_yield_only_to_fresh_forge_green(self):
        with patch.dict(os.environ, {'FPL_FAULT_SLEEP': '30'}):
            self.assertEqual(at.run_gate(str(self.root), 'suite', detach=True, nonce=self.launch['nonce']), 0)
        token = at._find_run(str(self.root), 'suite')['token']
        self.addCleanup(lambda: at.cancel_gate(str(self.root), token))
        store = bg.Store(str(self.root), at.BACKGROUND)
        deadline = time.monotonic() + 10
        while not (store.read(token) or {}).get('commandPid') and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(store.read(token).get('commandPid'))
        self.assertEqual(at.cancel_gate(str(self.root), token), 0)
        while not any(r.get('token') == token for r in self.fixture.rows()) and time.monotonic() < deadline:
            time.sleep(.05)
        local = next(r for r in self.fixture.rows() if r.get('token') == token)
        self.assertTrue(local['cancelled'])
        winner = self.forge_row()
        self.assertEqual(self.check(winner)['status'], 'ATTESTED')
        # A stored pointer alone does not authorize ignoring cancellation.
        self.api['repos/fixture/repo/commits/' + self.head + '/statuses?per_page=100&page=1'][0]['state'] = 'pending'
        self.assertEqual(self.check(winner)['status'], 'MISMATCH')

    def test_forged_cancelled_flag_cannot_hide_completed_assertion(self):
        with patch.dict(os.environ, {'FPL_ASSERT': '1'}):
            _, local = self.fixture.run_gate()
        winner = self.forge_row()
        rows = self.fixture.rows()
        for row in rows:
            if row['attestId'] == local['attestId']:
                row.update(cancelled=True, cancelRequested=True)
        self.write_rows(rows)
        self.assertEqual(self.check(winner)['status'], 'SUPERSEDED-RED')

    def test_missing_or_scope_mutated_red_rows_remain_red_for_both_winners(self):
        original = self.fixture.crash()
        _, local_green = self.fixture.run_gate()
        forge_green = self.forge_row()
        baseline = self.fixture.rows()
        mutations = {'missing': None, 'gate': 'other', 'nonce': 'other-run', 'treeSha': '0'*40,
                     'headSha': '0'*40, 'started': '1999-01-01T00:00:00Z', 'exit': 0,
                     'token': 'bg_wrong', 'attestId': 'att_rewritten'}
        for field, value in mutations.items():
            rows = copy.deepcopy(baseline)
            for row in rows:
                if row['attestId'] == original['attestId']:
                    if field == 'missing':
                        rows.remove(row)
                    else:
                        row[field] = value
                    break
            self.write_rows(rows)
            for winner in (local_green, forge_green):
                with self.subTest(field=field, witness=winner['witness']):
                    check = self.check(winner)
                    self.assertEqual(check['status'], 'SUPERSEDED-RED', check)
                    self.assertIn('retained red history', check['detail'])
        self.write_rows(baseline)


if __name__ == '__main__':
    unittest.main()
