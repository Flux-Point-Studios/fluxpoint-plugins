import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / 'scripts/specification.py'
COMPILER = PLUGIN / 'scripts/compile-graph.py'


def packet():
    return {
        'version': 1, 'goal': 'Reject unchecked work before implementation',
        'asset': 'The existing graph compiler and harness',
        'scope': ['Spec enforcement'], 'outOfScope': ['Installing a theorem prover'],
        'decisions': [{
            'id': 'enforcement', 'status': 'defaulted', 'dependsOn': [],
            'record': {
                'question': 'How should the requirement boundary be enforced?',
                'options': [{'option': option, 'argued_by': 'model',
                             'strongest_objection': 'This option has maintenance and migration costs.'}
                            for option in ['manifest', 'prose']],
                'chosen': 'manifest',
                'rationale': 'The compiler already validates structured inputs before it emits code.',
                'overturned_prior': False, 'frozen_by': 'none', 'reversible': True,
                'evidence': ['Read scripts/compile-graph.py']},
            'costs': [{'option': option, 'cost': 'One maintained artifact to review',
                       'basis': 'estimate', 'evidence': ['Design estimate, not a timing measurement']}
                      for option in ['manifest', 'prose']]}],
        'requirements': [{'id': 'reject-missing', 'statement': 'An absent spec prevents compilation',
                          'decisions': ['enforcement'], 'checks': ['spec-tests'],
                          'counterexample': 'A mutating graph compiles with no spec'}],
        'checks': [{'id': 'spec-tests', 'kind': 'test',
                    'argv': ['{python}', '-c', 'print("checked")'],
                    'scope': 'Spec gate CLI behavior', 'assumptions': [], 'timeoutSeconds': 10}],
        'challenges': [{'claim': 'The model could mark every decision accepted',
                        'resolution': 'Defaulted is distinct from user-confirmed and grants no consent'}]}


def graph():
    return {'version': 1, 'name': 'spec-test', 'campaign': 'Implement a checked slice',
            'budget': {'maxNodes': 8}, 'nodes': [
                {'id': 'build', 'prompt': 'Implement the requirements.',
                 'contract': 'SliceV1', 'mutates': True},
                {'id': 'gate', 'after': 'build', 'prompt': 'Verify {{prev}}',
                 'contract': 'HarnessCheckV1', 'independent': True, 'verifies': 'build',
                 'haltWhen': 'exit != 0'}]}


class SpecificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.doc = packet()
        self.save()

    def save(self):
        (self.root / '.fluxpoint-spec.json').write_text(json.dumps(self.doc), encoding='utf-8')

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root,
                              capture_output=True, text=True, encoding='utf-8',
                              env=dict(os.environ, PYTHONIOENCODING='utf-8'), timeout=20)

    def lock(self):
        self.save()
        r = self.run_cli('--lock')
        self.assertEqual(r.returncode, 0, r.stderr)

    def compile(self, ir=None):
        (self.root / 'WORK.md').write_text('```json graph-ir\n' + json.dumps(ir or graph()) + '\n```')
        return subprocess.run([sys.executable, str(COMPILER), 'WORK.md'], cwd=self.root,
                              capture_output=True, encoding='utf-8', timeout=20,
                              env=dict(os.environ, PYTHONIOENCODING='utf-8'))

    def test_lock_then_execute_actual_check(self):
        self.lock()
        r = self.run_cli('--run')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('checked', r.stdout)

    def test_lock_supports_python39_path_write_text_signature(self):
        bootstrap = '''
from pathlib import Path
import runpy, sys
original = Path.write_text
def legacy_write_text(self, data, encoding=None, errors=None):
    return original(self, data, encoding=encoding, errors=errors)
Path.write_text = legacy_write_text
script = sys.argv[1]
sys.argv = [script, '--lock']
runpy.run_path(script, run_name='__main__')
'''
        r = subprocess.run([sys.executable, '-c', bootstrap, str(SCRIPT)], cwd=self.root,
                           env=dict(os.environ, PYTHONPATH=str(PLUGIN / 'scripts'), PYTHONIOENCODING='utf-8'),
                           capture_output=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_cli('--check').returncode, 0)

    def test_check_requires_lock(self):
        r = self.run_cli('--check')
        self.assertEqual(r.returncode, 1)
        self.assertIn('lock', r.stderr)

    def test_spec_edit_invalidates_lock(self):
        self.lock()
        self.doc['goal'] = 'A different goal'
        self.save()
        self.assertIn('changed', self.run_cli('--check').stderr)

    def test_packet_replaced_during_read_cannot_borrow_replacement_identity(self):
        self.lock()
        path = self.root / '.fluxpoint-spec.json'
        locked_bytes = path.read_bytes()
        self.doc['goal'] = 'Unlocked earlier packet'
        self.save()
        module_spec = importlib.util.spec_from_file_location('specification', SCRIPT)
        runner = importlib.util.module_from_spec(module_spec)
        with mock.patch.object(sys, 'path', [str(PLUGIN / 'scripts')] + sys.path):
            module_spec.loader.exec_module(runner)
        original_bytes, original_text = Path.read_bytes, Path.read_text

        def replace_after_read(original):
            def read(file, *args, **kwargs):
                data = original(file, *args, **kwargs)
                if file == path:
                    path.write_bytes(locked_bytes)
                return data
            return read

        with mock.patch.object(Path, 'read_bytes', replace_after_read(original_bytes)), \
                mock.patch.object(Path, 'read_text', replace_after_read(original_text)):
            with self.assertRaisesRegex(ValueError, 'changed since lock'):
                runner.load(self.root)

    def test_check_descendants_cannot_write_after_timeout_or_parent_exit(self):
        grandchild = '''
from pathlib import Path
import time
Path('descendant-ready').write_text('ready')
deadline = time.monotonic() + 8
while not Path('release-descendant').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if Path('release-descendant').exists():
    Path('late-write').write_text('escaped')
'''
        middle = ('import subprocess, sys; subprocess.Popen([sys.executable, "grandchild.py"], '
                  'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)')
        parent = '''
from pathlib import Path
import subprocess, sys, time
subprocess.run([sys.executable, 'middle.py'], check=True)
deadline = time.monotonic() + 5
while not Path('descendant-ready').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
assert Path('descendant-ready').exists()
time.sleep(float(sys.argv[1]))
'''
        (self.root / 'grandchild.py').write_text(grandchild)
        (self.root / 'middle.py').write_text(middle)
        (self.root / 'parent.py').write_text(parent)
        next_check = ('from pathlib import Path; import time; '
                      'assert Path("descendant-ready").exists(); '
                      'Path("release-descendant").touch(); time.sleep(0.4); '
                      'assert not Path("late-write").exists()')
        for delay, expected in [('10', 1), ('0', 0)]:
            with self.subTest(parent_delay=delay):
                for name in ('descendant-ready', 'release-descendant', 'late-write'):
                    (self.root / name).unlink(missing_ok=True)
                self.doc = packet()
                self.doc['checks'][0].update(argv=['{python}', 'parent.py', delay], timeoutSeconds=2)
                self.doc['checks'].append(dict(self.doc['checks'][0], id='next-check',
                                               argv=['{python}', '-c', next_check], timeoutSeconds=5))
                self.doc['requirements'][0]['checks'].append('next-check')
                self.lock()
                r = self.run_cli('--run')
                self.assertTrue((self.root / 'descendant-ready').exists(), r.stderr)
                self.assertFalse((self.root / 'late-write').exists(), r.stdout + r.stderr)
                self.assertEqual(r.returncode, expected, r.stderr)
                if expected:
                    self.assertIn('timed out', r.stderr)

    def test_git_line_ending_conversion_preserves_lock(self):
        path = self.root / '.fluxpoint-spec.json'
        data = json.dumps(self.doc, indent=2).encode('utf-8')
        path.write_bytes(data.replace(b'\n', b'\r\n'))
        self.assertEqual(self.run_cli('--lock').returncode, 0)
        path.write_bytes(data)
        self.assertEqual(self.run_cli('--check').returncode, 0)

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / '.fluxpoint-spec.json'
        path.write_text(json.dumps(self.doc)[:-1] + ', "goal": "overwritten"}')
        self.assertEqual(self.run_cli('--lock').returncode, 1)

    def test_recursive_check_fails_without_spawning_forever(self):
        self.lock()
        r = subprocess.run([sys.executable, str(SCRIPT), '--run'], cwd=self.root,
                           env=dict(os.environ, FPL_SPEC_STACK=json.dumps([str(self.root.resolve())])),
                           capture_output=True, encoding='utf-8')
        self.assertEqual(r.returncode, 1)
        self.assertIn('recursive', r.stderr)

    def test_proof_rebaseline_invalidates_lock(self):
        baseline = self.root / '.fluxpoint-proof-baseline.json'
        baseline.write_text('{}')
        self.lock()
        baseline.write_text('{"counts": {}}')
        self.assertIn('baseline', self.run_cli('--check').stderr)

    def test_bad_decisions_and_mappings_cannot_lock(self):
        changes = [
            lambda d: d['decisions'].clear(),
            lambda d: d['decisions'][0].update(status='blocked'),
            lambda d: d['decisions'][0].update(status='approved'),
            lambda d: d['decisions'][0].update(dependsOn=['enforcement']),
            lambda d: d['decisions'][0].update(dependsOn=['absent']),
            lambda d: d['decisions'][0]['record'].update(evidence=[]),
            lambda d: d['decisions'][0]['costs'].pop(),
            lambda d: d['decisions'][0]['costs'][0].update(basis='measured', evidence=[]),
            lambda d: d['requirements'][0].update(checks=['absent']),
            lambda d: d['requirements'][0].update(decisions=['absent']),
            lambda d: d['requirements'].append(copy.deepcopy(d['requirements'][0])),
            lambda d: d['checks'][0].update(timeoutSeconds=True),
            lambda d: d['checks'][0].update(kind='formal-ish'),
            lambda d: d.update(unknown='typo'),
            lambda d: d['checks'][0].update(argz=['ignored']),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.doc = packet()
                change(self.doc)
                self.save()
                r = self.run_cli('--lock')
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_failed_missing_and_timed_out_checks_are_red(self):
        for argv, timeout in [(['{python}', '-c', 'raise SystemExit(7)'], 10),
                              (['fpl-nonexistent-checker'], 10),
                              (['{python}', '-c', 'import time; time.sleep(5)'], 1)]:
            with self.subTest(argv=argv):
                self.doc['checks'][0].update(argv=argv, timeoutSeconds=timeout)
                self.lock()
                self.assertEqual(self.run_cli('--run').returncode, 1)

    def test_formal_check_requires_and_runs_witness(self):
        self.doc['checks'][0]['kind'] = 'proof'
        self.save()
        self.assertIn('witness', self.run_cli('--lock').stderr)
        self.doc['checks'][0]['witness'] = ['{python}', '-c', 'raise SystemExit(6)']
        self.formal_obligation()
        self.arm_proof_guard()
        self.lock()
        r = self.run_cli('--run')
        self.assertEqual(r.returncode, 1)
        self.assertIn('witness exit 6', r.stdout)

    def arm_proof_guard(self):
        subprocess.run([sys.executable, str(PLUGIN / 'scripts/proof-guard.py'), '--baseline'],
                       cwd=self.root, check=True, capture_output=True)

    def test_formal_execution_requires_armed_escape_hatch_ratchet(self):
        self.formal_obligation()
        self.doc['checks'][0].update(kind='proof', witness=['{python}', '-c', 'print("reachable")'])
        self.lock()
        self.assertEqual(self.run_cli('--run').returncode, 1)
        self.arm_proof_guard()
        self.lock()
        self.assertEqual(self.run_cli('--run').returncode, 0)
        path = self.root / 'proof.dfy'
        path.write_text(path.read_text().replace('x := 1;', 'assume false; x := 1;'))
        self.assertEqual(self.run_cli('--run').returncode, 1)

    def formal_obligation(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        (self.root / 'proof.dfy').write_text('method Positive() returns (x: int)\n ensures x > 0\n{ x := 1; }\n')
        subprocess.run(['git', 'add', 'proof.dfy'], cwd=self.root, check=True, capture_output=True)
        self.doc['checks'][0]['obligations'] = ['dafny:proof.dfy:Positive']

    def test_formal_check_must_name_obligations(self):
        self.doc['checks'][0].update(kind='proof', witness=['{python}', '-c', 'print("reachable")'])
        self.save()
        self.assertEqual(self.run_cli('--lock').returncode, 1)

    def test_obligation_drift_invalidates_lock_without_baseline(self):
        self.formal_obligation()
        self.doc['checks'][0].update(kind='proof', witness=['{python}', '-c', 'print("reachable")'])
        self.lock()
        path = self.root / 'proof.dfy'
        path.write_text(path.read_text().replace('x > 0', 'x >= 0'))
        r = self.run_cli('--check')
        self.assertEqual(r.returncode, 1)
        self.assertIn('obligation', r.stderr)

    def test_unknown_obligation_cannot_lock(self):
        self.doc['checks'][0]['obligations'] = ['dafny:absent.dfy:Missing']
        self.save()
        self.assertEqual(self.run_cli('--lock').returncode, 1)

    def test_mutating_graph_needs_packet(self):
        (self.root / '.fluxpoint-spec.json').unlink()
        r = self.compile()
        self.assertEqual(r.returncode, 1)
        self.assertIn('spec', r.stderr)

    def test_compiled_context_and_summary_bind_locked_spec(self):
        self.lock()
        r = self.compile()
        self.assertEqual(r.returncode, 0, r.stderr)
        digest = hashlib.sha256((self.root / '.fluxpoint-spec.json').read_bytes()).hexdigest()
        self.assertIn(digest, r.stdout)
        self.assertIn('reject-missing', r.stdout)
        self.assertIn('specification:', r.stdout)

    def test_spec_prompt_is_included_in_compilation_budget(self):
        ir = graph()
        ir['treeGuard'] = False
        ir['budget']['maxEstimatedTokens'] = 100000
        self.lock()
        self.assertEqual(self.compile(ir).returncode, 0)
        self.doc['goal'] = 'x' * 500000
        self.lock()
        r = self.compile(ir)
        self.assertEqual(r.returncode, 1)
        self.assertIn('maxEstimatedTokens', r.stderr)
        checked = subprocess.run([sys.executable, str(COMPILER), 'WORK.md', '--check'],
                                 cwd=self.root, capture_output=True, encoding='utf-8',
                                 env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(checked.returncode, 1)

    def test_read_only_graph_needs_no_packet(self):
        ir = graph()
        ir['nodes'] = [{'id': 'read', 'prompt': 'Read and report findings.', 'contract': 'FindingsV1'}]
        (self.root / '.fluxpoint-spec.json').unlink()
        self.assertEqual(self.compile(ir).returncode, 0)

    def test_legacy_absence_is_explicit_only_for_runner(self):
        (self.root / '.fluxpoint-spec.json').unlink()
        r = self.run_cli('--run', '--if-present')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('legacy', r.stdout)
        (self.root / 'WORK.md').write_text('SPEC: .fluxpoint-spec.json\n')
        self.assertEqual(self.run_cli('--run', '--if-present').returncode, 1)

    def test_record_refuses_stale_spec_identity(self):
        self.lock()
        summary = {'outcome': 'COMPLETE', 'specification': {'sha256': 'wrong'}}
        (self.root / 'result.json').write_text(json.dumps(summary))
        r = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'),
                            '--run-id', 'spec-stale', '--result', 'result.json'],
                           cwd=self.root, capture_output=True, encoding='utf-8',
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('spec', r.stderr)
        self.assertFalse((self.root / '.claude/fluxpoint/runs/spec-stale.json').exists())

    def test_record_cannot_omit_identity_when_packet_exists(self):
        self.lock()
        (self.root / 'result.json').write_text('{"outcome":"COMPLETE"}')
        r = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'),
                            '--run-id', 'spec-missing', '--result', 'result.json'],
                           cwd=self.root, capture_output=True, encoding='utf-8',
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(r.returncode, 1)

    # Issue #95: a graph file's SPEC: header names its packet, so two
    # campaigns on one line stop overwriting each other's packet.
    def rollout(self):
        doc = packet()
        doc['goal'] = 'Roll the merged product out without re-deciding it'
        (self.root / '.fluxpoint-spec.rollout.json').write_text(json.dumps(doc), encoding='utf-8')
        r = self.run_cli('--lock', '--spec', '.fluxpoint-spec.rollout.json')
        self.assertEqual(r.returncode, 0, r.stderr)
        text = 'STATUS: READY\nMODE: graph\nSPEC: .fluxpoint-spec.rollout.json\n\n'
        (self.root / 'GRAPH.rollout.md').write_text(
            text + '```json graph-ir\n' + json.dumps(graph()) + '\n```\n', encoding='utf-8')
        return hashlib.sha256((self.root / '.fluxpoint-spec.rollout.json').read_bytes()).hexdigest()

    def test_spec_header_names_the_packet_and_its_own_lock(self):
        self.lock()
        digest = self.rollout()
        self.assertTrue((self.root / '.fluxpoint-spec.rollout-lock.json').exists())
        # The default packet changing does not stale the rollout's lock.
        self.doc['goal'] = 'The follow-on campaign locked its own packet'
        self.lock()
        r = self.run_cli('--check', '--graph', 'GRAPH.rollout.md')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(digest, r.stdout)
        compiled = subprocess.run([sys.executable, str(COMPILER), 'GRAPH.rollout.md'], cwd=self.root,
                                  capture_output=True, encoding='utf-8', timeout=20,
                                  env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertIn(digest, compiled.stdout)
        self.assertIn('Roll the merged product out', compiled.stdout)
        self.assertNotIn('The follow-on campaign', compiled.stdout)
        checked = subprocess.run([sys.executable, str(COMPILER), 'GRAPH.rollout.md', '--check'],
                                 cwd=self.root, capture_output=True, encoding='utf-8', timeout=20,
                                 env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertIn('.fluxpoint-spec.rollout.json', checked.stdout)
        self.assertIn(digest[:12], checked.stdout)

    def test_record_run_honors_the_graph_spec_header(self):
        self.lock()
        digest = self.rollout()
        identity = json.loads((self.root / '.fluxpoint-spec.rollout-lock.json').read_text())
        default = json.loads((self.root / '.fluxpoint-spec-lock.json').read_text())
        self.assertEqual(identity['sha256'], digest)
        for run_id, ident, want in (('rollout-ok', identity, 0), ('rollout-bad', default, 1)):
            (self.root / 'result.json').write_text(json.dumps(
                {'outcome': 'COMPLETE', 'campaign': 'c', 'specification': ident,
                 'specificationPath': '.fluxpoint-spec.rollout.json'}))
            r = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'),
                                '--run-id', run_id, '--graph', 'GRAPH.rollout.md', '--result', 'result.json'],
                               cwd=self.root, capture_output=True, encoding='utf-8',
                               env=dict(os.environ, PYTHONIOENCODING='utf-8', FPL_MEMORY_INDEX='0'))
            self.assertEqual(r.returncode, want, r.stderr)

    def test_runner_honors_the_state_file_spec_header(self):
        # The scaffolded harness runs `--run --if-present` with no --graph;
        # a WORK.md naming its own packet was reported as a legacy loop with
        # no spec, and nothing it required was checked.
        (self.root / '.fluxpoint-spec.json').unlink()
        doc = packet()
        (self.root / 'specs').mkdir()
        (self.root / 'specs' / 'rollout.json').write_text(json.dumps(doc), encoding='utf-8')
        (self.root / 'WORK.md').write_text('STATUS: READY\nSPEC: specs/rollout.json\n',
                                           encoding='utf-8')
        r = self.run_cli('--run', '--if-present')
        self.assertNotIn('legacy', r.stdout)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)  # declared, never locked
        self.assertEqual(self.run_cli('--lock', '--graph', 'WORK.md').returncode, 0)
        self.assertTrue((self.root / 'specs' / 'rollout-lock.json').exists())
        self.assertEqual(self.run_cli('--run', '--if-present').returncode, 0)
        sys.path.insert(0, str(PLUGIN / 'scripts'))
        import specification
        self.assertTrue(specification.locked(self.root))

    def test_bare_lock_keeps_the_default_packet(self):
        # A flow that just wrote .fluxpoint-spec.json locks that file, even
        # where WORK.md names another packet for the runner.
        (self.root / 'WORK.md').write_text('SPEC: specs/a.json\n', encoding='utf-8')
        r = self.run_cli('--lock')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / '.fluxpoint-spec-lock.json').exists())

    def test_spec_and_graph_spellings_of_one_packet_agree(self):
        self.lock()
        self.rollout()
        r = self.run_cli('--check', '--graph', 'GRAPH.rollout.md',
                         '--spec', './.fluxpoint-spec.rollout.json')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sibling_campaign_files_its_row_into_work_md(self):
        # --graph names the packet the run is held to; --evidence the file
        # whose tables take the row. A GRAPH.<name>.md with a packet of its
        # own had no way to reach WORK.md's Evidence table.
        self.lock()
        self.rollout()
        (self.root / 'WORK.md').write_text(
            'STATUS: READY\nSPEC: .fluxpoint-spec.json\n\n## Evidence\n\n'
            '| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n',
            encoding='utf-8')
        identity = json.loads((self.root / '.fluxpoint-spec.rollout-lock.json').read_text())
        (self.root / 'result.json').write_text(json.dumps(
            {'outcome': 'COMPLETE', 'campaign': 'c', 'specification': identity,
             'specificationPath': '.fluxpoint-spec.rollout.json'}))
        r = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'),
                            '--run-id', 'sibling', '--graph', 'GRAPH.rollout.md',
                            '--evidence', 'WORK.md', '--result', 'result.json'],
                           cwd=self.root, capture_output=True, encoding='utf-8',
                           env=dict(os.environ, PYTHONIOENCODING='utf-8', FPL_MEMORY_INDEX='0'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('| sibling |', (self.root / 'WORK.md').read_text(encoding='utf-8'))

    def test_spec_header_spellings_of_one_packet_agree(self):
        self.lock()
        self.rollout()
        text = (self.root / 'GRAPH.rollout.md').read_text(encoding='utf-8')
        (self.root / 'GRAPH.rollout.md').write_text(
            text.replace('SPEC: .fluxpoint-spec.rollout.json', 'SPEC: ./.fluxpoint-spec.rollout.json'),
            encoding='utf-8')
        identity = json.loads((self.root / '.fluxpoint-spec.rollout-lock.json').read_text())
        (self.root / 'result.json').write_text(json.dumps(
            {'outcome': 'COMPLETE', 'campaign': 'c', 'specification': identity,
             'specificationPath': '.fluxpoint-spec.rollout.json'}))
        r = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'),
                            '--run-id', 'dot-slash', '--graph', 'GRAPH.rollout.md', '--result', 'result.json'],
                           cwd=self.root, capture_output=True, encoding='utf-8',
                           env=dict(os.environ, PYTHONIOENCODING='utf-8', FPL_MEMORY_INDEX='0'))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_spec_refusal_still_records_the_irreversible_effect(self):
        self.lock()
        summary = {'outcome': 'COMPLETE', 'campaign': 'c', 'specification': {'sha256': 'stale'},
                   'ledger': [{'key': 'c|mint|abc', 'node': 'mint', 'campaign': 'c',
                               'result': {'txHash': 'deadbeef'}}]}
        (self.root / 'result.json').write_text(json.dumps(summary))
        r = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'),
                            '--run-id', 'spec-stale-ledger', '--result', 'result.json'],
                           cwd=self.root, capture_output=True, encoding='utf-8',
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertNotEqual(r.returncode, 0)
        ledger = self.root / '.claude/fluxpoint/irreversible.jsonl'
        self.assertTrue(ledger.exists(), r.stderr)
        self.assertIn('c|mint|abc', ledger.read_text())
        self.assertIn('ledger', r.stderr)

    def test_spec_header_paths_are_checked(self):
        for header, needle in (('SPEC: ../outside.json\n', 'repository'),
                               ('SPEC: /abs/spec.json\n', 'repository'),
                               ('SPEC: .fluxpoint-spec.yaml\n', '.json'),
                               ('SPEC: a.json\nSPEC: b.json\n', 'twice')):
            with self.subTest(header=header):
                (self.root / 'GRAPH.x.md').write_text(header + '```json graph-ir\n'
                                                      + json.dumps(graph()) + '\n```\n')
                r = subprocess.run([sys.executable, str(COMPILER), 'GRAPH.x.md', '--check'],
                                   cwd=self.root, capture_output=True, encoding='utf-8', timeout=20,
                                   env=dict(os.environ, PYTHONIOENCODING='utf-8'))
                self.assertEqual(r.returncode, 1)
                self.assertIn(needle, r.stderr)

    def test_check_cannot_change_the_spec_during_execution(self):
        self.doc['checks'][0]['argv'] = ['{python}', '-c',
            'from pathlib import Path; Path(".fluxpoint-spec.json").write_text("{}")']
        self.lock()
        self.assertEqual(self.run_cli('--run').returncode, 1)

    def test_missing_aiken_and_apalache_are_red(self):
        (self.root / '.fluxpoint-spec.json').unlink()
        bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else 'bash'
        for tool in ('aiken', 'apalache-mc'):
            with self.subTest(tool=tool):
                manifest = self.root / 'aiken.toml'
                if tool == 'aiken':
                    manifest.write_text('name = "test/project"')
                elif manifest.exists():
                    manifest.unlink()
                env = dict(os.environ, FPL_PLUGIN_ROOT=PLUGIN.as_posix(),
                           FPL_PY=Path(sys.executable).as_posix(), PYTHONIOENCODING='utf-8')
                if tool == 'apalache-mc':
                    env['FPL_APALACHE_ARGS'] = '--inv=Inv Spec.tla'
                command = 'function command() { if [ "$1" = -v ] && [ "$2" = "' + tool + '" ]; then return 1; fi; builtin command "$@"; }; export -f command; bash "$1" --full'
                r = subprocess.run([bash, '-c', command, 'probe', (PLUGIN / 'templates/harness.sh').as_posix()],
                                   cwd=self.root, env=env, capture_output=True, timeout=30)
                self.assertNotEqual(r.returncode, 0)

    def test_emitted_javascript_passes_packet_to_agents(self):
        self.lock()
        ir = graph()
        ir['treeGuard'] = False
        ir['nodes'].append({'id': 'review', 'after': 'gate', 'prompt': 'Review the requirements and {{prev}}.',
                            'contract': 'FindingsV1', 'verify': 'panel:3',
                            'verifyOver': 'findings', 'expectItems': 1})
        r = self.compile(ir)
        self.assertEqual(r.returncode, 0, r.stderr)
        prelude = '''
const prompts = [];
const args = {_base: {sha: 'abc', branch: 'main'}};
const log = () => {}, phase = () => {};
const budget = {remaining: () => 1000000};
const workflow = {};
const parallel = tasks => Promise.all(tasks.map(task => task()));
const agent = async (prompt) => {prompts.push(prompt); return {exit: 0, branch: 'main', findings: [{claim: 'review'}], refuted: false};};
'''
        script = prelude + '(async () => {\n' + r.stdout.replace('export const meta', 'const meta') + '''
})().then(result => {console.log(JSON.stringify({result, prompts}));});
'''
        (self.root / 'run.cjs').write_text(script, encoding='utf-8')
        result = subprocess.run(['node', 'run.cjs'], cwd=self.root, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed['result']['outcome'], 'COMPLETE')
        self.assertEqual(len(observed['prompts']), 6)
        self.assertTrue(all('reject-missing' in p for p in observed['prompts']))
        self.assertIn('sha256', observed['result']['specification'])
        total = observed['result']['estimate']['total']
        self.assertEqual(total, sum(p['estimatedTokens'] for p in observed['result']['profile'].values()))
        ir['budget']['maxEstimatedTokens'] = total
        self.assertEqual(self.compile(ir).returncode, 0)
        checked = subprocess.run([sys.executable, str(COMPILER), 'WORK.md', '--check'],
                                 cwd=self.root, capture_output=True, encoding='utf-8',
                                 env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn(f'~{total:,} estimated tokens', checked.stdout)
        ir['budget']['maxEstimatedTokens'] -= 1
        self.assertEqual(self.compile(ir).returncode, 1)
        rejected = subprocess.run([sys.executable, str(COMPILER), 'WORK.md', '--out', 'over.graph.js'],
                                  cwd=self.root, capture_output=True,
                                  env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(rejected.returncode, 1)
        self.assertFalse((self.root / 'over.graph.js').exists())

    def test_kani_without_cargo_cannot_pass_full_harness(self):
        (self.root / '.fluxpoint-spec.json').unlink()
        (self.root / 'Cargo.toml').write_text('[package]\nname="test"\nversion="0.1.0"\n')
        (self.root / 'src').mkdir()
        (self.root / 'src/lib.rs').write_text('#[kani::proof]\nfn property() { assert!(true); }\n')
        env = dict(os.environ, FPL_PLUGIN_ROOT=PLUGIN.as_posix(),
                   FPL_PY=Path(sys.executable).as_posix(), PYTHONIOENCODING='utf-8')
        bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else 'bash'
        command = 'function command() { if [ "$1" = -v ] && [ "$2" = cargo ]; then return 1; fi; builtin command "$@"; }; export -f command; bash "$1" --full'
        r = subprocess.run([bash, '-c', command, 'probe', (PLUGIN / 'templates/harness.sh').as_posix()],
                           cwd=self.root, env=env, capture_output=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)

    def test_harness_requires_spec_when_work_declares_it(self):
        (self.root / '.fluxpoint-spec.json').unlink()
        (self.root / 'WORK.md').write_text('SPEC: .fluxpoint-spec.json\n')
        env = dict(os.environ, FPL_PLUGIN_ROOT=PLUGIN.as_posix(),
                   FPL_PY=Path(sys.executable).as_posix(), PYTHONIOENCODING='utf-8')
        bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else 'bash'
        r = subprocess.run([bash, (PLUGIN / 'templates/harness.sh').as_posix(), '--full'],
                           cwd=self.root, env=env, capture_output=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('spec', r.stderr)

    def test_spec_marker_parsers_agree_without_installed_runner(self):
        (self.root / '.fluxpoint-spec.json').unlink()
        env = dict(os.environ, FPL_PLUGIN_ROOT='', CLAUDE_PLUGIN_ROOT='', PLUGIN_ROOT='',
                   FPL_PY=Path(sys.executable).as_posix(), PYTHONIOENCODING='utf-8')
        bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else 'bash'
        command = 'function find() { return 1; }; export -f find; bash "$1" --full'
        cases = [(f'SPEC:{gap}.fluxpoint-spec.json\n', 1) for gap in ('', ' ', '  ', '\t')]
        cases += [('SPEC: .fluxpoint-spec.json \t\n', 1),
                  ('SPEC:\t.fluxpoint-spec.json\r\n', 1),
                  ('SPEC:\n.fluxpoint-spec.json\n', 0),
                  # A header naming no JSON packet is an error in both, never
                  # a legacy loop: the runner now reads WORK.md's SPEC:.
                  ('SPEC: xfluxpoint-specxjson\n', 1)]
        for marker, expected in cases:
            with self.subTest(marker=marker):
                (self.root / 'WORK.md').write_bytes(marker.encode('utf-8'))
                runner = self.run_cli('--run', '--if-present')
                harness = subprocess.run([bash, '-c', command, 'probe', (PLUGIN / 'templates/harness.sh').as_posix()],
                                         cwd=self.root, env=env, capture_output=True, timeout=30)
                self.assertEqual(runner.returncode, expected, runner.stderr)
                self.assertEqual(harness.returncode, expected, harness.stderr)

    def test_unavailable_dafny_cannot_pass_full_harness(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        (self.root / 'proof.dfy').write_text('method Positive() returns (x: int)\n ensures x > 0\n{ x := 0; }\n')
        subprocess.run(['git', 'add', 'proof.dfy'], cwd=self.root, check=True, capture_output=True)
        (self.root / '.fluxpoint-spec.json').unlink()
        env = dict(os.environ, FPL_PLUGIN_ROOT=PLUGIN.as_posix(),
                   FPL_PY=Path(sys.executable).as_posix(), PYTHONIOENCODING='utf-8')
        bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else 'bash'
        command = 'function command() { if [ "$1" = -v ] && [ "$2" = dafny ]; then return 1; fi; builtin command "$@"; }; export -f command; bash "$1" --full'
        r = subprocess.run([bash, '-c', command, 'probe', (PLUGIN / 'templates/harness.sh').as_posix()],
                           cwd=self.root, env=env, capture_output=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(r.returncode, 0)

    def test_decision_section_mention_does_not_waive_statement_drift(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        proof = self.root / 'proof.dfy'
        proof.write_text('method Positive() returns (x: int)\n ensures x > 0\n{ x := 1; }\n')
        subprocess.run(['git', 'add', 'proof.dfy'], cwd=self.root, check=True, capture_output=True)
        guard = PLUGIN / 'scripts/spec-guard.py'
        subprocess.run([sys.executable, str(guard), '--baseline'], cwd=self.root, check=True, capture_output=True)
        proof.write_text(proof.read_text().replace('x > 0', 'x >= 0'))
        (self.root / 'WORK.md').write_text('## Decisions\nStill review dafny:proof.dfy:Positive.\n')
        r = subprocess.run([sys.executable, str(guard), '--check'], cwd=self.root, capture_output=True)
        self.assertEqual(r.returncode, 1)


if __name__ == '__main__':
    unittest.main()
