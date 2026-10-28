#!/usr/bin/env python3
"""CI identities cannot be available locally or replace deployed resolution."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import attest
import forge_evidence as forge

CI = {'witness': {'statusCreators': ['ci-bot'], 'checkRunApps': []}}


class IdentityTests(unittest.TestCase):
    def call(self, *, stored=(), active='agent', shadowed='other', status_code=0, raw=None, error=''):
        self.calls = []
        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if argv[1:3] == ['auth', 'status']:
                body = raw if raw is not None else {'hosts': {'github.com': list(stored)}}
                return subprocess.CompletedProcess(argv, status_code, json.dumps(body), error)
            self.assertEqual(argv, ['gh', 'api', 'user', '--hostname', 'github.com'])
            login = shadowed if 'env' in kwargs else active
            return subprocess.CompletedProcess(argv, 0, json.dumps({'login': login}), '')
        with patch.object(forge.subprocess, 'run', side_effect=run):
            forge.GitHub('.').identity(CI)

    def test_inactive_stored_pinned_identity_is_refused(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(forge.PolicyError, 'available on this host'):
                self.call(stored=[{'login': 'agent', 'state': 'success'},
                                  {'login': 'CI-BOT', 'state': 'success'}])

    def test_shadowed_github_token_is_checked_without_printing_it(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'fixture-active', 'GITHUB_TOKEN': 'fixture-shadowed'}, clear=True):
            with self.assertRaisesRegex(forge.PolicyError, 'GITHUB_TOKEN'):
                self.call(shadowed='ci-bot')
        shadowed_calls = [kwargs for argv, kwargs in self.calls
                          if argv[1] == 'api' and 'env' in kwargs]
        self.assertEqual(len(shadowed_calls), 1)
        self.assertNotIn('GH_TOKEN', shadowed_calls[0]['env'])
        self.assertTrue(shadowed_calls[0]['env'].get('GITHUB_TOKEN'))
        self.assertFalse(any('fixture-' in word for argv, _ in self.calls for word in argv))

    def test_stored_inventory_ignores_active_token_environment(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'fixture-active', 'GITHUB_TOKEN': 'fixture-shadowed'}, clear=True):
            self.call(stored=[{'login': 'agent', 'state': 'success'}])
        inventory = [kwargs for argv, kwargs in self.calls if argv[1] == 'auth'][0]
        self.assertNotIn('GH_TOKEN', inventory['env'])
        self.assertNotIn('GITHUB_TOKEN', inventory['env'])
        self.assertNotIn('--show-token', self.calls[1][0])

    def test_empty_stored_inventory_is_allowed_even_with_nonzero_status(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'fixture-active'}, clear=True):
            self.call(status_code=1, raw={'hosts': {}}, error='You are not logged into any GitHub hosts.')

    def test_unreadable_or_malformed_inventory_cannot_pass(self):
        with patch.dict(os.environ, {}, clear=True):
            for raw in ({}, {'hosts': []}, {'hosts': {'github.com': [{}]}}):
                with self.subTest(raw=raw), self.assertRaises((RuntimeError, forge.PolicyError)):
                    self.call(raw=raw)
            with self.assertRaisesRegex(RuntimeError, 'inventory failed'):
                self.call(status_code=1, raw={'hosts': {}}, error='credential inventory is unreadable')

    def test_expired_pinned_account_is_still_available_and_refused(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(forge.PolicyError, 'available on this host'):
                self.call(stored=[{'login': 'ci-bot', 'state': 'error'}], status_code=1)

    def test_active_identity_keeps_existing_fixture_boundary(self):
        with self.assertRaisesRegex(forge.PolicyError, 'own gh login'):
            forge.GitHub('.', api=lambda path: {'login': 'ci-bot'}).identity(CI)


class DeployedCITests(unittest.TestCase):
    def test_forge_and_reuse_cannot_replace_deployed_wrapper(self):
        gates = attest.Gates({'suite-deployed': 'python suite.py'})
        gates.deployed = {'api': {'gate': 'suite-deployed', 'pins': 'missing-requirements.txt'}}
        with tempfile.TemporaryDirectory() as root:
            for witness in ('forge', 'reuse'):
                check = {'status': 'ATTESTED'}
                attest._check_deployed(root, gates, 'suite-deployed', {'witness': witness, 'faultPair': {'forged': True}}, {'exit': 0}, check)
                self.assertEqual(check['status'], 'MISMATCH')
                self.assertIn('wrapper', check['detail'])

    def test_matching_wrapper_still_establishes_deployed_resolution(self):
        gates = attest.Gates({'suite-deployed': 'python suite.py'})
        gates.deployed = {'api': {'gate': 'suite-deployed', 'pins': 'requirements.txt'}}
        with tempfile.TemporaryDirectory() as root:
            pins = b'demo==1.0\n'
            (Path(root) / 'requirements.txt').write_bytes(pins)
            row = {'witness': 'wrapper', 'resolutionChecked': True,
                   'pinsSha256': {'api': hashlib.sha256(pins).hexdigest()},
                   'commandSha': attest.sha(gates['suite-deployed']),
                   'deployedLaneSha256': attest.deployed_lane_sha(gates, 'suite-deployed')}
            check = {'status': 'ATTESTED'}
            attest._check_deployed(root, gates, 'suite-deployed', row, {'exit': 0}, check)
            self.assertEqual(check['status'], 'ATTESTED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
