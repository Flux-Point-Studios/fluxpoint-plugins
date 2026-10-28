#!/usr/bin/env python3
"""Exercise restart recovery after a native process exits without handlers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from process_tree import identity

CRASH = '''import importlib.util,os,sys
sys.path.insert(0,os.path.dirname(sys.argv[1]))
spec=importlib.util.spec_from_file_location('guard_guard',sys.argv[1])
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
real=g.subprocess.run
root=sys.argv[2]
def run(*a,**kw):
    if 'if False' in open(os.path.join(root,'server.py')).read():
        os._exit(73)
    return real(*a,**kw)
g.subprocess.run=run
sys.argv=[sys.argv[1],'--root',root,'--verify']
g.main()
'''


class GuardRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'server.py'
        self.original = b'def fee_ok(amount):\r\n    if amount > 100:\r\n        return False\r\n    return True\r\n'
        self.target.write_bytes(self.original)
        (self.root / 'test_fee.py').write_text('import server\nassert not server.fee_ok(500), "guard gone"\n')
        (self.root / '.fluxpoint-guards.json').write_text(json.dumps({'guards': [{
            'id': 'fee', 'protects': 'fee cap', 'guard': {'file': 'server.py', 'contains': 'if amount > 100'},
            'proof': {'file': 'test_fee.py', 'test': 'server'},
            'mutation': {'find': 'if amount > 100', 'replace': 'if False'}, 'expect': 'guard gone',
            'run': '"{py}" test_fee.py'}]}))
        self.sentinel = self.root / '.fluxpoint-guards-restoring'

    def cli(self, mode='--check'):
        return subprocess.run([sys.executable, str(SCRIPTS / 'guard-guard.py'), '--root', str(self.root), mode],
                              capture_output=True, text=True, timeout=30)

    def crash(self):
        result = subprocess.run([sys.executable, '-c', CRASH, str(SCRIPTS / 'guard-guard.py'), str(self.root)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn(b'if False', self.target.read_bytes())
        self.assertTrue(self.sentinel.exists())

    def test_restart_recovers_exact_bytes_after_exit_before_proof_spawn(self):
        self.crash()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse(self.sentinel.exists())
        self.assertFalse(list(self.root.glob('.fluxpoint-guard-backup-*')))
        self.assertEqual(self.cli('--verify').returncode, 0)

    def test_restart_refuses_to_overwrite_new_edits(self):
        self.crash()
        edited = b'# somebody edited this file\n'
        self.target.write_bytes(edited)
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('changed since', result.stderr)
        self.assertEqual(self.target.read_bytes(), edited)
        self.assertTrue(self.sentinel.exists())

    def test_restart_preserves_an_active_verifiers_mutation(self):
        self.crash()
        journal = json.loads(self.sentinel.read_text())
        journal.update(pid=os.getpid(), owner=identity(os.getpid()))
        self.sentinel.write_text(json.dumps(journal))
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('still running', result.stderr)
        self.assertIn(b'if False', self.target.read_bytes())
        self.assertTrue(self.sentinel.exists())

    def test_restart_rejects_corrupt_backups_and_escaping_targets(self):
        self.crash()
        journal = json.loads(self.sentinel.read_text())
        backup = self.root / journal['backup']
        backup.write_bytes(b'corrupt backup')
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('backup digest', result.stderr)
        self.assertTrue(self.sentinel.exists())
        journal['target'] = '../outside.py'
        self.sentinel.write_text(json.dumps(journal))
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside', result.stderr)
        self.assertIn(b'if False', self.target.read_bytes())


if __name__ == '__main__':
    unittest.main()
