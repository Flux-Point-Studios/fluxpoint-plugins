#!/usr/bin/env python3
"""Crash-only reruns execute real processes; image adapter is an explicit test double."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import attest as at
import fault_attempts as attempts
import fault_policy as policy
import background as bg

IMAGE = 'fixture-ci@sha256:' + 'a' * 64
CRASH = '''....Fatal Python error: Segmentation fault

Current thread 0x0001 (most recent call first):
  File "/host/python/site-packages/_pytest/unraisableexception.py", line 83 in collect_unraisable
  File "/host/python/site-packages/_pytest/runner.py", line 173 in pytest_runtest_call

Thread 0x0002 (most recent call first):
  File "/repo/tests/test_other.py", line 20 in other_thread
'''


class FaultAttempts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fpl-fault-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'fault fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / '.gitignore').write_text('.claude/\n')
        (self.root / 'gate.py').write_text('''import json,os,pathlib,sys
state=pathlib.Path(os.environ['FPL_FAULT_STATE']); state.parent.mkdir(parents=True,exist_ok=True)
n=int(state.read_text())+1 if state.exists() else 1; state.write_text(str(n))
if os.environ.get('FPL_FAULT_SLEEP'):
 import time; time.sleep(float(os.environ['FPL_FAULT_SLEEP']))
reference=os.environ.get('FPL_REFERENCE_FIXTURE')=='1'
if reference:
 assert not pathlib.Path('.git').exists(), 'reference must judge an archive'
 if os.environ.get('FPL_REFERENCE_RED'): print('FAILED test_reference.py'); sys.exit(4)
elif n==1 or os.environ.get('FPL_NATIVE_RED'):
 print(os.environ['FPL_CRASH_LOG']); sys.exit(9)
if not reference and os.environ.get('FPL_NATIVE_MUTATE'): pathlib.Path('gate.py').write_text('print("changed")')
print('all fixture checks passed')
''')
        runner = Path(self.tmp.name) / 'reference_fixture.py'
        runner.write_text('''import os,subprocess,sys
root,image,*command=sys.argv[1:]
assert image==''' + repr(IMAGE) + '''
if os.environ.get('FPL_REFERENCE_BAD_PINS'):
 import pathlib
 pathlib.Path(root,'fpl_fixture-1.0.dist-info','METADATA').write_text('Name: fpl-fixture\\nVersion: 2.0\\n')
env=dict(os.environ,FPL_REFERENCE_FIXTURE='1')
sys.exit(subprocess.call(command,cwd=root,env=env))
''')
        exe = sys.executable.replace('\\', '/')
        self.doc = {'version': 1, 'gates': {'suite': shlex.quote(exe) + ' gate.py'},
                    'reference': {'image': IMAGE, 'run': shlex.quote(exe) + ' ' + shlex.quote(str(runner).replace('\\', '/')) + ' {tree} {image} {command}'},
                    'faults': [{'id': 'native-crash', 'gates': ['suite'],
                                'host': platform.python_version() + ' ' + platform.system(), 'exit': [9],
                                'logMust': ['Fatal Python error:', 'collect_unraisable'], 'logMustNot': ['never-classify-this']}]}
        self.write_manifest()
        self.git('add', '.')
        self.git('commit', '-qm', 'declared pinned policy')
        self.env = patch.dict(os.environ, {'FPL_FAULT_STATE': str(self.root / '.claude' / 'state'), 'FPL_CRASH_LOG': CRASH})
        self.env.start()
        self.addCleanup(self.env.stop)
        # The runner probes `python` from gate PATH; bind it to the interpreter
        # running this regression, even when Python3.9 is not the default PATH.
        host = {'version': platform.python_version(), 'platform': platform.system(), 'executable': sys.executable}
        self.host_patch = patch.object(policy, 'host_for', return_value=host)
        self.host_patch.start()
        self.addCleanup(self.host_patch.stop)
        self.launch = at.stamp(str(self.root))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True, check=True).stdout.strip()

    def write_manifest(self):
        (self.root / '.fluxpoint-gates.json').write_text(json.dumps(self.doc))

    def rows(self):
        return at.read(str(self.root))

    def run_gate(self):
        code = at.run_gate(str(self.root), 'suite', nonce=self.launch['nonce'])
        return code, self.rows()[-1]

    def crash(self):
        code, row = self.run_gate()
        self.assertEqual(code, 9)
        self.assertEqual(row['fault']['id'], 'native-crash')
        return row

    def pair(self, original):
        code = attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce'])
        return code, self.rows()[-1]

    def check(self, row, used=None, node='gate'):
        summary = {'launch': self.launch, 'tree': {'baseline': {'head': self.git('rev-parse', 'HEAD')}},
                   'contracts': {node: 'HarnessCheckV1'}, 'prove': {node: 'suite'},
                   'results': {node: {'gate': 'suite', 'exit': row['exit'], 'attestId': row['attestId']}}}
        if used is not None:
            return at._check_cited(self.rows(), node, 'suite', summary['results'][node], self.launch,
                                   self.git('rev-parse', 'HEAD'), used, str(self.root))
        checks, findings = at.verify_claims(str(self.root), summary)
        self.assertFalse(findings)
        return checks[0]

    def test_manifest_refuses_mutable_reference_and_malformed_signatures(self):
        for mutate in (lambda d: d['reference'].update(image='fixture-ci:latest'),
                       lambda d: d['reference'].update(run='runner {tree} {image}'),
                       lambda d: d['faults'][0].update(logMust=['.*']),
                       lambda d: d['faults'][0].update(logMust=['Fatal Python error:', '[']),
                       lambda d: d['faults'][0].update(exit=[0]),
                       lambda d: d['faults'][0].update(exit=[124]),
                       lambda d: d['faults'][0].update(gates=['missing']),
                       lambda d: d['faults'][0].update(extra='ignored'),
                       lambda d: d.update(faults='not a list')):
            doc = copy.deepcopy(self.doc); mutate(doc)
            with self.subTest(doc=doc):
                _, _, findings = policy.policy(doc, {'suite': 'python gate.py'})
                self.assertTrue(findings)

    def test_plugin_floor_never_excuses_assertions_pytest_failures_or_repo_frames(self):
        host = {'platform': 'Linux'}
        self.assertTrue(policy.floor(CRASH, '/repo', host))
        self.assertTrue(policy.floor(CRASH + "\nERROR worker 'gw4' crashed while running tests\n", '/repo', host))
        for extra in ('\nAssertionError: timing bound exceeded', '\nFAILED tests/test_api.py::test_reply',
                      '\nERROR tests/test_api.py::test_reply', '\ntests/test_api.py FAILED [10%]'):
            self.assertFalse(policy.floor(CRASH + extra, '/repo', host))
        self.assertFalse(policy.floor(CRASH.replace('/host/python/site-packages/_pytest/unraisableexception.py', '/repo/tests/test_api.py'), '/repo', host))
        self.assertFalse(policy.floor(CRASH.replace('/host/python/site-packages/_pytest/unraisableexception.py', '/another-worktree/tests/test_api.py'), '/repo', host))
        self.assertFalse(policy.floor(CRASH.replace('Fatal Python error:', 'timed out:'), '/repo', host))
        self.assertFalse(policy.floor('Fatal Python error: crash without its current-thread dump', '/repo', host))
        self.assertFalse(policy.floor('Fatal Python error: crash\nCurrent thread 1:\n', '/repo', host))

    def test_stamp_and_runner_pin_manifest_and_measured_host(self):
        self.assertEqual(self.launch['manifestSha256'], policy.manifest_sha(str(self.root)))
        row = self.crash()
        self.assertEqual(row['host']['version'], platform.python_version())
        self.assertEqual(row['manifestSha256'], self.launch['manifestSha256'])
        self.assertEqual(attempts.witnessed(at, str(self.root), row).replace('\r\n', '\n'), CRASH + '\n')
        self.assertIsNotNone(attempts.load_launch(str(self.root), self.launch['nonce']))

    def test_host_probe_runs_the_gate_path_interpreter(self):
        self.host_patch.stop()
        gates, findings = at.load_gates(str(self.root))
        self.assertFalse(findings)
        host = policy.host_for(at, str(self.root), gates, 'suite')
        self.assertRegex(host['version'], r'^\d+\.\d+\.\d+$')
        self.assertTrue(host['executable'])
        gates['suite'] = 'bash suite.sh'
        self.assertIsNone(policy.host_for(at, str(self.root), gates, 'suite'))
        self.host_patch.start()

    def test_later_green_cannot_launder_red_or_clear_it_on_resume(self):
        original = self.crash()
        code, green = self.run_gate()
        self.assertEqual(code, 0)
        self.assertEqual(self.check(green)['status'], 'SUPERSEDED-RED')
        self.assertIn(original['attestId'], self.check(green)['detail'])
        self.assertEqual(self.check(green)['status'], 'SUPERSEDED-RED')
        other = dict(self.launch, nonce='another-run')
        gates, _ = at.load_gates(str(self.root))
        self.assertIsNone(policy.classify(gates, original, CRASH, other))

    def test_only_freshly_verified_forge_green_may_ignore_interrupted_local_loser(self):
        with patch.dict(os.environ, {'FPL_FAULT_SLEEP': '30'}):
            self.assertEqual(at.run_gate(str(self.root), 'suite', detach=True, nonce=self.launch['nonce']), 0)
        store = bg.Store(str(self.root), at.BACKGROUND)
        token = at._find_run(str(self.root), 'suite')['token']
        self.addCleanup(lambda: at.cancel_gate(str(self.root), token))
        deadline = time.monotonic() + 10
        while not store.read(token).get('commandPid') and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(store.read(token).get('commandPid'))
        self.assertEqual(at.cancel_gate(str(self.root), token), 0)
        cancelled = self.rows()[-1]
        self.assertTrue(cancelled['cancelled'])
        # Ensure the next execution is the first normal green independently
        # of whether cancellation reached the fixture before its counter.
        (self.root / '.claude/state').write_text('1')
        _, green = self.run_gate()
        rows = self.rows()
        self.assertEqual(attempts.unclosed_reds(at, str(self.root), rows, green, self.launch)[0], [cancelled['attestId']])
        self.assertEqual(attempts.unclosed_reds(at, str(self.root), rows, green, self.launch, fresh_forge_green=True)[0], [])
        completed = dict(cancelled, attestId='att_completed_red', exit=1, cancelled=True)
        self.assertEqual(attempts.unclosed_reds(at, str(self.root), [completed, green], green, self.launch, fresh_forge_green=True)[0], ['att_completed_red'])
        forged = dict(cancelled, attestId='att_agent_written', token='not-a-wrapper-completion')
        self.assertEqual(attempts.unclosed_reds(at, str(self.root), [forged, green], green, self.launch, fresh_forge_green=True)[0], ['att_agent_written'])

    def test_late_cancellation_cannot_relabel_a_completed_failure(self):
        finish = bg.finish
        def late(store, token, code, before_finish=None, **fields):
            store.write({'token': token, 'cancelRequested': True})
            return finish(store, token, code, before_finish=before_finish, **fields)
        with patch.object(bg, 'finish', side_effect=late):
            code, row = self.run_gate()
        self.assertEqual(code, 9)
        self.assertTrue(row['cancelRequested'])
        self.assertFalse(row['cancelled'])
        self.assertNotIn('fault', row)

    def test_one_sequential_native_reference_pair_settles_crash_and_reserves_all_rows(self):
        original = self.crash()
        code, pair = self.pair(original)
        self.assertEqual(code, 0)
        self.assertEqual(self.check(pair)['status'], 'ATTESTED')
        self.assertEqual(self.check(pair)['faultEvents'], 1)
        self.assertEqual(pair['faultPair']['image'], IMAGE)
        ids = attempts.verify_pair(at, str(self.root), self.rows(), pair, self.launch)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(pair['faultPair']['logs']), 3)
        used = {}
        self.assertEqual(self.check(pair, used)['status'], 'ATTESTED')
        leg = next(row for row in self.rows() if row['attestId'] == pair['faultPair']['native'])
        self.assertEqual(self.check(leg, used, 'borrowed')['status'], 'STALE')
        self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce']), 2)
        self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce'], token=pair['token']), 2)
        self.assertEqual(attempts.rerun(at, str(self.root), leg['attestId'], self.launch['nonce']), 2)

    def test_red_native_or_reference_leg_stays_red_without_second_pair(self):
        original = self.crash()
        with patch.dict(os.environ, {'FPL_NATIVE_RED': '1'}):
            code, pair = self.pair(original)
        self.assertEqual(code, 1)
        self.assertEqual(self.check(pair)['status'], 'MISMATCH')
        self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce']), 2)

    def test_reference_failure_stays_red(self):
        original = self.crash()
        with patch.dict(os.environ, {'FPL_REFERENCE_RED': '1'}):
            code, pair = self.pair(original)
        self.assertEqual(code, 1)
        self.assertEqual(self.check(pair)['status'], 'MISMATCH')

    def configure_deployed(self):
        info = self.root / 'fpl_fixture-1.0.dist-info'
        info.mkdir()
        (info / 'METADATA').write_text('Name: fpl-fixture\nVersion: 1.0\n')
        (self.root / 'pins.txt').write_text('fpl-fixture==1.0\n')
        self.doc['deployed'] = {'suite-installed': {'pins': 'pins.txt', 'python': sys.executable, 'gate': 'suite'}}
        self.write_manifest()
        self.git('add', '.')
        self.git('commit', '-qm', 'declare checked deployed lane')
        self.launch = at.stamp(str(self.root))

    def test_deployed_pair_measures_resolution_in_both_runtimes(self):
        self.configure_deployed()
        original = self.crash()
        code, pair = self.pair(original)
        self.assertEqual(code, 0)
        self.assertEqual(self.check(pair)['status'], 'ATTESTED')
        reference = next(row for row in self.rows() if row['attestId'] == pair['faultPair']['reference'])
        self.assertTrue(reference['resolutionChecked'])
        self.assertEqual(reference['dependencies'], {'suite-installed': {'fpl-fixture': '1.0'}})
        bad_rows = copy.deepcopy(self.rows())
        next(row for row in bad_rows if row['attestId'] == reference['attestId'])['dependencies']['suite-installed']['fpl-fixture'] = '2.0'
        with self.assertRaises(ValueError):
            attempts.verify_pair(at, str(self.root), bad_rows, pair, self.launch)

    def test_reference_deployed_mismatch_is_red(self):
        self.configure_deployed()
        original = self.crash()
        with patch.dict(os.environ, {'FPL_REFERENCE_BAD_PINS': '1'}):
            code, pair = self.pair(original)
        self.assertEqual(code, 1)
        self.assertEqual(self.check(pair)['status'], 'MISMATCH')

    def test_dirty_staged_untracked_or_moved_head_refuses_before_consuming(self):
        original = self.crash()
        (self.root / 'untracked').write_text('new')
        self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce']), 2)
        (self.root / 'untracked').unlink()
        (self.root / 'gate.py').write_text('print("changed")')
        self.git('add', 'gate.py')
        self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce']), 2)
        self.git('reset', '--hard', 'HEAD')
        self.git('commit', '--allow-empty', '-qm', 'moved')
        self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce']), 2)
        self.assertIsNone(bg.Store(str(self.root), at.BACKGROUND).read('fault_' + original['attestId']))

    def test_added_after_launch_signature_does_not_classify_or_verify(self):
        original = self.crash()
        self.doc['faults'][0]['logMustNot'].append('edited-after-crash')
        self.write_manifest()
        self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce']), 2)
        (self.root / '.claude' / 'state').unlink()
        _, row = self.run_gate()
        self.assertNotIn('fault', row)

    def test_log_tampering_and_agent_written_rows_do_not_settle_red(self):
        original = self.crash()
        _, pair = self.pair(original)
        log = self.root / at.BACKGROUND / (original['token'] + '.log')
        raw = log.read_bytes()
        log.write_bytes(b'green now\n')
        self.assertEqual(self.check(pair)['status'], 'MISMATCH')
        log.write_bytes(raw)
        self.assertEqual(self.check(pair)['status'], 'ATTESTED')
        fake = dict(original, attestId='att_invented', token='bg_invented')
        at.append_row(str(self.root), fake)
        self.assertEqual(attempts.rerun(at, str(self.root), fake['attestId'], self.launch['nonce']), 2)

    def test_native_rerun_cannot_change_the_tree_and_still_get_a_pair(self):
        original = self.crash()
        with patch.dict(os.environ, {'FPL_NATIVE_MUTATE': '1'}):
            code, last = self.pair(original)
        self.assertEqual(code, 2)
        self.assertTrue(last['dirty'])
        self.assertFalse(any(row.get('faultPair') for row in self.rows()))

    def test_all_measured_runner_facts_are_bound_to_retained_completion(self):
        original = self.crash()
        _, pair = self.pair(original)
        for field in ('host', 'tree', 'commandSha', 'dirty', 'headSha', 'referenceImage',
                      'pinsSha256', 'deployedLaneSha256', 'dependencies', 'resolutionChecked'):
            rows = copy.deepcopy(self.rows())
            native = next(row for row in rows if row['attestId'] == pair['faultPair']['native'])
            native[field] = True if field == 'dirty' else 'altered-after-the-run'
            with self.subTest(field=field), self.assertRaises(ValueError):
                attempts.verify_pair(at, str(self.root), rows, pair, self.launch)

    def test_pair_links_digest_or_recorded_fault_cannot_be_rewritten(self):
        original = self.crash()
        _, pair = self.pair(original)
        for mutate in (lambda p: p['faultPair'].update(image='fixture-ci@sha256:' + 'b' * 64),
                       lambda p: p['faultPair'].update(native=original['attestId']),
                       lambda p: p['faultPair']['logs'][0].update(logSha256='0' * 64)):
            bad = copy.deepcopy(pair); mutate(bad)
            with self.assertRaises(ValueError):
                attempts.verify_pair(at, str(self.root), self.rows(), bad, self.launch)
        gates, _ = at.load_gates(str(self.root))
        for field, value in [('cancelled', True), ('timedOut', True), ('host', {'version': '0.0.0', 'platform': platform.system()}), ('manifestSha256', '0' * 64)]:
            self.assertIsNone(policy.classify(gates, dict(original, **{field: value}), CRASH, self.launch))

    def test_detached_pair_uses_existing_await_and_cancel_paths(self):
        original = self.crash()
        code = attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce'], detach=True)
        self.assertEqual(code, 0)
        token = bg.Store(str(self.root), at.BACKGROUND).read('fault_' + original['attestId'])['pairToken']
        self.assertEqual(at.await_gate(str(self.root), token, 20), 0)
        pair = self.rows()[-1]
        self.assertIn('faultPair', pair)
        self.assertEqual(self.check(pair)['status'], 'ATTESTED')

    def test_cancelled_pair_never_classifies_or_clears_original_crash(self):
        original = self.crash()
        with patch.dict(os.environ, {'FPL_FAULT_SLEEP': '30'}):
            self.assertEqual(attempts.rerun(at, str(self.root), original['attestId'], self.launch['nonce'], detach=True), 0)
        store = bg.Store(str(self.root), at.BACKGROUND)
        token = store.read('fault_' + original['attestId'])['pairToken']
        self.addCleanup(lambda: at.cancel_gate(str(self.root), token))
        deadline = time.monotonic() + 10
        while not store.read(token).get('commandPid') and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(store.read(token).get('commandPid'))
        self.assertEqual(at.cancel_gate(str(self.root), token), 0)
        self.assertEqual(store.read(token)['status'], 'CANCELLED')
        self.assertFalse(any(row.get('faultPair') for row in self.rows()))
        self.assertTrue(all(not row.get('fault') for row in self.rows() if row.get('attempt')))
        _, green = self.run_gate()
        self.assertEqual(self.check(green)['status'], 'SUPERSEDED-RED')

    def test_compiler_warns_on_unwitnessed_verbatim_gate_and_teaches_fault_pair(self):
        graph = {'version': 1, 'campaign': 'gate fixture', 'budget': {'maxNodes': 10}, 'nodes': [
            {'id': 'gate', 'contract': 'HarnessCheckV1', 'prompt': 'Run ' + self.doc['gates']['suite'],
             'verify': 'schema-only', 'haltWhen': 'exit != 0'}]}
        path = self.root / 'WORK.md'
        path.write_text('```json graph-ir\n' + json.dumps(graph) + '\n```\n')
        command = [sys.executable, str(SCRIPTS / 'compile-graph.py'), str(path), '--gates-root', str(self.root), '--check']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('schema-only exit gate runs declared gate', result.stderr)
        source = (SCRIPTS / 'compile-graph.py').read_text()
        self.assertIn('A red whose wrapper row carries fault can be settled only by attest.py --rerun-fault', source)

    def test_record_run_blocks_unsettled_red_and_retains_fault_event_count(self):
        self.crash()
        _, green = self.run_gate()
        summary = {'campaign': 'fault fixture', 'outcome': 'COMPLETE', 'launch': self.launch,
                   'tree': {'baseline': {'head': self.git('rev-parse', 'HEAD')}}, 'contracts': {'gate': 'HarnessCheckV1'},
                   'prove': {'gate': 'suite'}, 'results': {'gate': {'gate': 'suite', 'exit': 0, 'attestId': green['attestId']}}}
        graph = self.root / 'WORK.md'
        graph.write_text('## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'), '--root', str(self.root),
                               '--graph', str(graph), '--run-id', 'fault-red'], input=json.dumps(summary), text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        artifact = json.loads((self.root / '.claude/fluxpoint/runs/fault-red.json').read_text())
        self.assertEqual(artifact['outcome'], 'TAMPERED-EXECUTION')
        self.assertEqual(artifact['attestation']['tally']['fault_events'], 1)
        self.assertIn('1 verified host crash event(s) retained', graph.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
