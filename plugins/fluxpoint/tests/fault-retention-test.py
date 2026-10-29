#!/usr/bin/env python3
"""Content-bound red retention, abrupt-runner records and crash floor regressions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import signal
import time
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
import attest as at
import background as bg
import fault_attempts as attempts
import fault_policy as policy
spec = importlib.util.spec_from_file_location('fault_fixture', HERE / 'fault-attempts-test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class RetentionFollowups(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FaultAttempts('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root

    def claim(self, row, sha=None):
        claim = dict(gate='suite', exit=row['exit'], attestId=row['attestId'], sha=sha or row['treeSha'])
        return at._check_cited(self.f.rows(), 'gate', 'suite', claim, self.f.launch,
                              self.f.git('rev-parse', 'HEAD'), root=str(self.root))

    def test_empty_commit_preserves_red_content_and_nonce_less_reds(self):
        red = self.f.crash()
        self.f.git('commit', '--allow-empty', '-qm', 'same bytes')
        _, green = self.f.run_gate()
        self.assertEqual(self.claim(green)['status'], 'SUPERSEDED-RED')
        self.assertIn(red['attestId'], self.claim(green)['detail'])
        rows = [dict(r, nonce='') if r['attestId'] == red['attestId'] else r for r in self.f.rows()]
        self.assertIn(red['attestId'], attempts.unclosed_reds(at, str(self.root), rows, green, self.f.launch)[0])

    def test_every_local_citation_names_the_exact_judged_commit(self):
        self.f.crash()
        _, green = self.f.run_gate()
        self.assertEqual(self.claim(green, 'f' * 40)['status'], 'MISMATCH')
        claim = dict(gate='suite', exit=0, attestId=green['attestId'])
        self.assertEqual(at._check_cited(self.f.rows(), 'gate', 'suite', claim, self.f.launch,
                                        root=str(self.root))['status'], 'MISMATCH')

    def test_dead_running_and_error_records_block_a_later_green(self):
        self.f.crash()
        _, green = self.f.run_gate()
        # Isolate the pending record from the separately covered completed-red case.
        rows = [green]
        store = bg.Store(str(self.root), at.BACKGROUND)
        for record in list(Path(store.directory).glob('bg_*.json')):
            record.unlink()
        for status in ('RUNNING', 'ERROR'):
            token = 'bg_unfinished_' + status.lower()
            store.write(dict(token=token, status=status, gate='suite', pid=999999999,
                             runnerIdentity='gone', started=at.now(), nonce=self.f.launch['nonce'],
                             treeSha=green['treeSha'], treeOid=green.get('treeOid'), dirty=False))
            problems = attempts.unclosed_reds(at, str(self.root), rows, green, self.f.launch)[0]
            self.assertTrue(any(token in problem for problem in problems), problems)

    def test_clean_linked_tree_ignores_unrelated_coordinator_edits(self):
        tree = Path(self.f.tmp.name) / 'gate-tree'
        self.f.git('worktree', 'add', '--detach', str(tree), 'HEAD')
        (self.root / 'gate.py').write_text('# coordinator edit\n')
        with patch.dict(os.environ, {'FPL_FAULT_STATE': str(self.root / '.claude/state')}):
            (self.root / '.claude/state').write_text('1')
            code = at.run_gate(str(self.root), 'suite', tree=str(tree), nonce=self.f.launch['nonce'])
        row = self.f.rows()[-1]
        self.assertEqual(code, 0)
        self.assertIs(row['dirty'], False, row.get('dirtyPaths'))
        self.assertEqual(self.claim(row)['status'], 'ATTESTED')

    def test_clean_linked_tree_cannot_use_a_changed_coordinator_manifest(self):
        tree = Path(self.f.tmp.name) / 'gate-tree'
        self.f.git('worktree', 'add', '--detach', str(tree), 'HEAD')
        self.f.doc['gates']['suite'] = 'true'
        self.f.write_manifest()
        self.assertEqual(at.run_gate(str(self.root), 'suite', tree=str(tree), nonce=self.f.launch['nonce']), 0)
        row = self.f.rows()[-1]
        self.assertIs(row['dirty'], False)
        check = self.claim(row)
        self.assertEqual(check['status'], 'MISMATCH', check)
        self.assertIn('manifest', check['detail'])

    def test_failure_hook_records_runtime_error_shape_and_retains_it(self):
        payload = dict(hook_event_name='PostToolUseFailure', cwd=str(self.root), tool_name='Bash',
                       tool_use_id='failure-hook', tool_input={'command': self.f.doc['gates']['suite']},
                       error='Command failed with exit code 7')
        hook = subprocess.run([shutil.which('bash'), str(HERE.parent / 'scripts/exec-attest.sh')],
                              cwd=self.root, input=json.dumps(payload), capture_output=True, text=True,
                              env=dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root), FPL_PY=sys.executable), timeout=30)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        row = self.f.rows()[-1]
        self.assertEqual(row['exit'], 7)
        self.assertTrue(row['treeOid'])
        (self.root / '.claude/state').write_text('1')
        _, green = self.f.run_gate()
        self.assertEqual(self.claim(green)['status'], 'SUPERSEDED-RED')

    def test_frozen_and_worker_pseudo_frames_do_not_hide_external_crashes(self):
        log = fixture.CRASH.replace('\n\nThread', '\n  File "<frozen runpy>", line 198 in _run_module_as_main\n  File "<string>", line 1 in <module>\n\nThread')
        self.assertTrue(policy.floor(log, str(self.root), {'platform': 'Linux'}))
        self.assertFalse(policy.floor(log, str(self.root), {'platform': 'Linux'}, inline_program=True))
        node = 'tests/unit/test_config_loader.py::test_rejects_unknown_keys'
        xdist = log + "\n[gw0] node down: Not properly terminated\nreplacing crashed worker gw0\nworker 'gw0' crashed while running '" + node + "'\nFAILED " + node + '\n'
        self.assertTrue(policy.floor(xdist, str(self.root), {'platform': 'Linux'}))
        self.assertFalse(policy.floor(xdist + '\nFAILED tests/test_real_assertion.py::test_error\n', str(self.root), {'platform': 'Linux'}))

    @unittest.skipIf(os.name == 'nt', 'native forced runner kill is exercised on POSIX here')
    def test_actual_killed_detached_runner_remains_unknown(self):
        with patch.dict(os.environ, {'FPL_FAULT_SLEEP': '30'}):
            self.assertEqual(at.run_gate(str(self.root), 'suite', nonce=self.f.launch['nonce'], detach=True), 0)
        token = at._find_run(str(self.root), 'suite')['token']
        self.addCleanup(lambda: at.cancel_gate(str(self.root), token))
        store = bg.Store(str(self.root), at.BACKGROUND)
        deadline = time.monotonic() + 10
        while not (store.read(token) or {}).get('commandPid') and time.monotonic() < deadline:
            time.sleep(.05)
        record = store.read(token)
        self.assertTrue(record.get('commandPid'))
        os.kill(record['pid'], signal.SIGKILL)
        self.assertEqual(at.await_gate(str(self.root), token, 2), 4)
        (self.root / '.claude/state').write_text('1')
        _, green = self.f.run_gate()
        result = self.claim(green)
        self.assertEqual(result['status'], 'SUPERSEDED-RED', result)
        self.assertIn(token, result['detail'])

    @unittest.skipUnless(sys.version_info >= (3, 11), 'frozen runpy frames require CPython 3.11+')
    def test_real_module_and_worker_style_crash_dumps(self):
        outside = Path(self.f.tmp.name) / 'external'
        outside.mkdir()
        code = ("import os\nif os.name != 'nt':\n import resource; resource.setrlimit(resource.RLIMIT_CORE,(0,0))\nos.abort()\n")
        (outside / 'nativecrash.py').write_text(code)
        env = dict(os.environ, PYTHONPATH=str(outside))
        for program in (['-m', 'nativecrash'], ['-c', 'import nativecrash']):
            crash = subprocess.run([sys.executable, '-X', 'faulthandler', *program], cwd=self.root,
                                   env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(crash.returncode, 0)
            self.assertTrue(policy.floor(crash.stderr, str(self.root), {'platform': 'Linux'}), crash.stderr)
            if program[0] == '-c':
                self.assertFalse(policy.floor(crash.stderr, str(self.root), {'platform': 'Linux'}, inline_program=True))
        self.assertFalse(policy.inline_gate('python -m pytest -c pytest.ini'))
        self.assertTrue(policy.inline_gate('python -X faulthandler -c "import nativecrash"'))

    def test_reference_snapshot_has_git_and_ignores_export_attributes(self):
        (self.root / '.gitattributes').write_text('gate.py export-ignore\nversion.txt export-subst\n')
        (self.root / 'version.txt').write_text('$Format:%H$\n')
        self.f.git('add', '.')
        self.f.git('commit', '-qm', 'archive-altering attributes')
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(attempts._snapshot(str(self.root), self.f.git('rev-parse', 'HEAD'), scratch))
            self.assertTrue((target / '.git').is_dir())
            self.assertTrue((target / 'gate.py').exists())
            self.assertEqual((target / 'version.txt').read_text(), '$Format:%H$\n')
            self.assertEqual(subprocess.run(['git', '-C', str(target), 'ls-files', '--error-unmatch', 'gate.py'],
                                            capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
