#!/usr/bin/env python3
"""Execute shared longrun/attest lifecycles, including real POSIX descendants.

The portable subtree test exercises taskkill when run on native Windows.
Separate mocked API-shape tests do not constitute native execution evidence.
Run this suite under both Python 3.9 and 3.11.
"""
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import background as bg
import longrun


class Longrun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = bg.Store(str(self.root), longrun.BACKGROUND)
        self.tokens = []

    def tearDown(self):
        for store, token in self.tokens:
            try:
                rec = store.read(token)
                if rec and rec.get('status') not in bg.TERMINAL:
                    bg.cancel(store, token)
            except (OSError, ValueError):
                pass
        self.tmp.cleanup()

    def cli(self, script, *args, code=0):
        result = subprocess.run([sys.executable, str(SCRIPTS / script), '--root', str(self.root), *args],
                                cwd=str(self.root), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def start(self, source):
        output = self.cli('longrun.py', 'start', '--log', str(self.root / 'run.log'), '--',
                          sys.executable, '-c', source)
        token = output.strip()
        self.assertRegex(token, r'^lr_[0-9a-f]+$')
        self.tokens.append((self.store, token))
        return token

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail('condition did not become true')

    def descendants(self, attest=False):
        # The grandchild deliberately ignores TERM, exercising group escalation.
        (self.root / 'child.py').write_text('''import json,os,subprocess,sys,time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(\"grand-ready\").touch(); time.sleep(300)'])
Path('pids.json').write_text(json.dumps({'child':os.getpid(),'grandchild':child.pid,'group':getattr(os, 'getpgrp', lambda: None)()}))
time.sleep(300)
''')
        source = '''import os,subprocess,sys,time
from pathlib import Path
child = subprocess.Popen([sys.executable, 'child.py'])
print('READY', flush=True)
time.sleep(300)
'''
        if attest:
            (self.root / 'gate.py').write_text(source)
            (self.root / '.fluxpoint-gates.json').write_text(json.dumps({'version': 1, 'gates': {'harness':
                '"' + sys.executable + '" gate.py'}}))
            output = self.cli('attest.py', '--run', 'harness', '--detach')
            token = re.search(r'as (bg_[0-9a-f]+)', output).group(1)
            store = bg.Store(str(self.root), '.claude/fluxpoint/attest-bg')
            self.tokens.append((store, token))
        else:
            token = self.start(source)
            store = self.store
        self.wait_for(lambda: (self.root / 'pids.json').exists() and (self.root / 'grand-ready').exists())
        pids = json.loads((self.root / 'pids.json').read_text())
        rec = store.read(token)
        self.assertEqual(rec['processGroup'], rec['commandPid'])
        if os.name != 'nt':
            self.assertEqual(pids['group'], rec['commandPid'])
        self.assertTrue(bg.alive(pids['child']))
        self.assertTrue(bg.alive(pids['grandchild']))
        return store, token, pids

    def test_resume_regex_wait_and_nonzero_completion_do_not_attest(self):
        token = self.start('''from pathlib import Path
import sys,time
print('READY to continue', flush=True)
while not Path('release').exists(): time.sleep(.02)
print('finished with a real failure', flush=True)
sys.exit(7)
''')
        out = self.cli('longrun.py', 'await', token, '--until', '^READY to continue$', '--timeout', '5')
        self.assertIn('RUNNING', out)
        self.assertIn('matched log line', out)
        self.cli('longrun.py', 'await', token, '--timeout', '0', code=3)
        (self.root / 'release').touch()
        out = self.cli('longrun.py', 'await', token, '--timeout', '5')
        self.assertIn('DONE; exit 7', out)
        self.assertIn('finished with a real failure', out)
        self.assertFalse((self.root / '.claude/fluxpoint/attest.jsonl').exists())
        self.assertEqual(self.store.read(token)['exit'], 7)

    def test_unknown_invalid_regex_and_launch_error_are_explicit(self):
        self.cli('longrun.py', 'await', 'unknown', '--timeout', '0', code=2)
        token = self.start('import time; time.sleep(300)')
        self.cli('longrun.py', 'await', token, '--until', '[', code=2)
        self.cli('longrun.py', 'await', token, '--timeout', 'nan', code=2)
        self.cli('longrun.py', 'cancel', '../elsewhere', code=2)
        output = self.cli('longrun.py', 'start', '--log', 'missing.log', '--', 'not-a-real-executable-143')
        failed = output.strip()
        self.tokens.append((self.store, failed))
        out = self.cli('longrun.py', 'await', failed, '--timeout', '5', code=4)
        self.assertIn('ERROR', out)
        self.assertIn('not-a-real-executable-143', out)
        self.assertNotIn('exit 0', out)

    def test_completion_without_an_exit_is_not_a_success(self):
        self.store.write({'token': 'broken', 'status': 'DONE'})
        out = self.cli('longrun.py', 'await', 'broken', '--timeout', '0', code=4)
        self.assertIn('no command exit', out)

    def test_cancel_kills_real_child_and_term_ignoring_grandchild(self):
        store, token, pids = self.descendants()
        self.cli('longrun.py', 'cancel', token)
        self.wait_for(lambda: not bg.alive(pids['child']) and not bg.alive(pids['grandchild']))
        self.assertEqual(store.read(token)['status'], 'CANCELLED')
        self.assertNotEqual(store.read(token).get('exit'), 0)
        self.cli('longrun.py', 'await', token, '--timeout', '0')
        self.cli('longrun.py', 'cancel', token)  # idempotent

    @unittest.skipIf(os.name == 'nt', 'real POSIX group test; Windows API shapes tested separately')
    def test_attest_shares_regex_wait_and_cancels_its_whole_command_group(self):
        store, token, pids = self.descendants(attest=True)
        out = self.cli('attest.py', '--await', token, '--until', '^READY$', '--timeout', '5')
        self.assertIn('matched log line', out)
        self.cli('attest.py', '--await', token, '--timeout', '0', code=3)
        self.cli('attest.py', '--cancel', token)
        self.wait_for(lambda: not bg.alive(pids['child']) and not bg.alive(pids['grandchild']))
        out = self.cli('attest.py', '--await', token, '--timeout', '5')
        self.assertIn('CANCELLED', out)
        self.assertNotIn('exit 0', out)
        rows = [json.loads(line) for line in (self.root / '.claude/fluxpoint/attest.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertLess(rows[0]['exit'], 0)
        self.assertEqual(rows[0]['witness'], 'wrapper')

    @unittest.skipIf(os.name == 'nt', 'real POSIX runner-loss test')
    def test_runner_loss_is_four_and_command_can_still_be_cancelled(self):
        store, token, pids = self.descendants()
        os.kill(store.read(token)['pid'], signal.SIGKILL)
        self.wait_for(lambda: not bg.alive(store.read(token)['pid']))
        out = self.cli('longrun.py', 'await', token, '--timeout', '0', code=4)
        self.assertIn('runner is gone', out)
        self.cli('longrun.py', 'cancel', token)
        self.wait_for(lambda: not bg.alive(pids['child']) and not bg.alive(pids['grandchild']))

    def test_cancel_before_attestation_publication_cannot_mint_a_green_row(self):
        spec = importlib.util.spec_from_file_location('longrun_attest', str(SCRIPTS / 'attest.py'))
        attest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(attest)
        (self.root / '.fluxpoint-gates.json').write_text(json.dumps({'version': 1, 'gates': {'harness': 'echo done'}}))
        def finish_racing_cancel(store, token, command, cwd, log):
            store.write({'token': token, 'cancelRequested': True})
            Path(log).write_text('command exited zero during cancellation\n')
            return 0
        with patch.object(bg, 'run_command', side_effect=finish_racing_cancel):
            result = attest.run_gate(str(self.root), 'harness', token='bg_cancelrace')
        self.assertEqual(result, 130)
        self.assertFalse((self.root / '.claude/fluxpoint/attest.jsonl').exists())
        rec = bg.Store(str(self.root), attest.BACKGROUND).read('bg_cancelrace')
        self.assertEqual(rec['status'], 'CANCELLED')
        self.assertEqual(rec['exit'], 0)  # preserve the actual code without minting evidence

    def test_timeout_is_clamped_without_waiting_570_seconds(self):
        rec = {'token': 'test', 'status': 'RUNNING', 'pid': 123, 'started': bg.now()}
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(bg.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(bg.time, 'sleep', side_effect=advance):
            code, _, _ = bg.wait(lambda _: rec, rec, 10000, is_alive=lambda *args: True)
        self.assertEqual(code, 3)
        self.assertGreaterEqual(clock[0], 570)
        self.assertLess(clock[0], 570.01)

    def test_mocked_windows_group_and_taskkill_api_shape(self):
        self.assertEqual(bg.detached_options('nt'), {'creationflags': 0x200})
        with patch.object(bg.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            bg.terminate_tree(12345, platform='nt')
        self.assertEqual(run.call_args.args[0], ['taskkill', '/T', '/F', '/PID', '12345'])
        self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_mocked_windows_handle_is_pointer_sized_and_closed(self):
        handle = 0x123456789ABC
        kernel = Mock()
        kernel.OpenProcess.return_value = handle
        def exit_code(observed, pointer):
            self.assertEqual(observed, handle)
            pointer._obj.value = 259
            return True
        kernel.GetExitCodeProcess.side_effect = exit_code
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            self.assertTrue(bg.alive_nt(123))
        self.assertIs(kernel.OpenProcess.restype, wintypes.HANDLE)
        kernel.CloseHandle.assert_called_once_with(handle)

    def test_compiler_injects_guidance_into_plain_prove_and_repair_nodes(self):
        spec = importlib.util.spec_from_file_location('longrun_compiler', str(SCRIPTS / 'compile-graph.py'))
        compiler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiler)
        plain = {'id': 'build', 'prompt': 'build it', 'contract': 'SliceV1'}
        prove = {'id': 'check', 'prompt': 'check it', 'contract': 'HarnessCheckV1', 'verify': 'prove:harness'}
        ir = {'nodes': [plain, prove], 'treeGuard': False, 'campaign': 'test'}
        self.assertIn('longPreamble() +', compiler.emit_node(plain, ir))
        self.assertIn('provePreamble("harness") +', compiler.emit_node(prove, ir))
        contracts = compiler.load_contracts(str(PLUGIN / 'contracts'))
        emitted = compiler.emit(ir, contracts)
        self.assertIn('return longPreamble() + (gate', emitted)
        fix = {'id': 'fix', 'foreach': 'items', 'mutates': True, 'prompt': 'fix {{item.key}}', 'contract': 'SliceV1'}
        check = {'id': 'check', 'foreach': 'items', 'after': 'fix', 'verifies': 'fix',
                 'prompt': 'check {{prev}}', 'contract': 'HarnessCheckV1',
                 'repeat': {'until': 'exit == 0', 'maxRounds': 2}}
        repair = {'nodes': [fix, check], 'treeGuard': False, 'lists': {'items': [{'key': 'a'}]}}
        self.assertGreaterEqual(compiler.emit_repair(check, repair).count('longPreamble() +'), 2)
        for text in ['570 seconds', 'on exit 3 call await again', 'Never end your turn', 'never wrap a suite in timeout']:
            self.assertIn(text, emitted)


if __name__ == '__main__':
    unittest.main()
