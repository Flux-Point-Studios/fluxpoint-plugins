#!/usr/bin/env python3
"""Exercise Blaster's process boundary, trust policy, freshness and concrete replay."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
ADAPTER = SCRIPTS / 'blaster.py'
MANIFEST = '.fluxpoint-blaster.json'


class BlasterIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Blaster Test')
        self.git('config', 'user.email', 'blaster@example.invalid')
        (self.root / '.gitignore').write_text('.lake/\n.claude/\nbin/\nbehavior.json\ncalls.jsonl\n')
        (self.root / 'Model.lean').write_text('def safe : Prop := ∀ n : Nat, n + 1 > n\ndef feasible : Prop := ∃ n : Nat, n = 1\n')
        (self.root / 'lean-toolchain').write_text('leanprover/lean4:v4.24.0\n')
        (self.root / 'lakefile.toml').write_text('name = "Probe"\n')
        dep = self.root / '.lake/packages/Blaster'
        dep.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(dep)], check=True)
        (dep / 'Blaster.lean').write_text('-- pinned backend\n')
        subprocess.run(['git', '-C', str(dep), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(dep), '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid',
                        'commit', '-qm', 'backend'], check=True)
        rev = subprocess.check_output(['git', '-C', str(dep), 'rev-parse', 'HEAD'], text=True).strip()
        (self.root / 'lake-manifest.json').write_text(json.dumps({'packagesDir': '.lake/packages', 'packages': [
            {'type': 'git', 'name': 'Blaster', 'rev': rev, 'url': 'https://github.com/input-output-hk/Lean-blaster'}]}))
        self.config = {'version': 1, 'toolchain': {'lean': '4.24.0', 'z3': '4.15.2', 'blaster': rev},
                       'timeoutSeconds': 5, 'buildTimeoutSeconds': 10,
                       'checks': [{'id': 'safety', 'module': 'Model', 'declaration': 'safe',
                                   'witness': 'feasible', 'expected': 'valid', 'trust': 'solver', 'seed': 1,
                                   'scope': 'Natural-number safety with an inhabited domain',
                                   'assumptions': ['Optimizer, SMT translation and Z3 are trusted']}],
                       'regressions': []}
        self.save()
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.behavior = {}
        self.write_behavior()
        bindir = self.root / 'bin'
        bindir.mkdir()
        # This fixture implements the structured backend boundary. Live Lean/Z3
        # validation is separate; no solver result is inferred from these tests.
        source = '''import json,pathlib,re,sys,time
root=pathlib.Path.cwd()
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='z3': print('Z3 version 4.15.2 - 64 bit'); sys.exit(0)
if '--version' in args: print('Lean (version 4.24.0, x86_64-unknown-linux-gnu, Release)'); sys.exit(0)
behavior=json.loads((root/'behavior.json').read_text())
if args[:1]==['build']:
 sys.exit(behavior.get('buildExit',0))
probe=pathlib.Path(args[-1]).read_text()
meta=json.loads(next(line.split('FPL_REQUEST ',1)[1] for line in probe.splitlines() if line.startswith('-- FPL_REQUEST ')))
if behavior.get('sleep'): time.sleep(behavior['sleep'])
if behavior.get('modify'): (root/'Model.lean').write_text('def changed := 1\\n')
for request in meta['requests']:
 with (root/'calls.jsonl').open('a') as stream: stream.write(json.dumps(request)+'\\n')
 if request['key'] in behavior.get('omit',[]): continue
 result=behavior.get(request['key'],{})
 row={'nonce':meta['nonce'],'key':request['key'],'status':result.get('status','valid'),
      'model':result.get('model',[]),'variables':result.get('variables',[]),
      'axioms':result.get('axioms',[]),'query':result.get('query','')}
 print('FLUXPOINT_BLASTER '+json.dumps(row))
 if behavior.get('duplicate'): print('FLUXPOINT_BLASTER '+json.dumps(row))
sys.exit(behavior.get('exit',0))
'''
        for tool in ('lake', 'z3'):
            path = bindir / tool
            path.write_text('#!' + sys.executable + '\n' + source)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def save(self):
        (self.root / MANIFEST).write_text(json.dumps(self.config, indent=2) + '\n')

    def write_behavior(self):
        (self.root / 'behavior.json').write_text(json.dumps(self.behavior))

    def cli(self, *args, ok=True, script=ADAPTER):
        result = subprocess.run([sys.executable, str(script), '--root', str(self.root), *args],
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def receipt(self):
        return json.loads((self.root / '.claude/fluxpoint/blaster/latest.json').read_text())

    def test_run_records_solver_trust_and_freshness(self):
        self.cli('--run')
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['checks'][0]['trust'], 'solver')
        self.assertEqual(receipt['checks'][0]['observed'], 'valid')
        self.cli('--check')
        (self.root / 'Model.lean').write_text('def safe : Prop := False\n')
        self.cli('--check', ok=False)

    def test_unknown_falsification_and_failed_witness_never_pass(self):
        for key, status in [('safety:query', 'undetermined'), ('safety:query', 'falsified'),
                            ('safety:witness', 'falsified'), ('safety:witness', 'undetermined')]:
            with self.subTest(key=key, status=status):
                self.behavior = {key: {'status': status}}
                self.write_behavior()
                self.cli('--run', ok=False)
                self.assertNotEqual(self.receipt()['status'], 'passed')
                self.cli('--check', ok=False)

    def test_kernel_policy_and_imported_axioms_are_explicit(self):
        self.config['checks'][0]['trust'] = 'kernel'
        self.save()
        self.cli('--run', ok=False)
        self.config['checks'][0]['trust'] = 'solver'
        self.save()
        self.behavior = {'safety:query': {'axioms': ['Blaster.Tactic.blasterProven']}}
        self.write_behavior()
        self.cli('--run')
        self.assertIn('Blaster.Tactic.blasterProven', self.receipt()['checks'][0]['axioms'])

    def test_missing_duplicate_output_and_nonzero_exit_are_errors(self):
        for behavior in ({'omit': ['safety:query']}, {'duplicate': True}, {'exit': 1}, {'buildExit': 1}):
            self.behavior = behavior
            self.write_behavior()
            self.cli('--run', ok=False)
            self.cli('--check', ok=False)

    def test_zero_checks_duplicate_ids_and_unknown_keys_refuse(self):
        original = copy.deepcopy(self.config)
        for change in ({'checks': []}, {'checks': self.config['checks'] * 2}, {'typo': True}):
            self.config = dict(copy.deepcopy(original), **change)
            self.save()
            self.cli('--run', ok=False)

    def test_backend_and_dependency_drift_refuse(self):
        self.cli('--run')
        (self.root / '.lake/packages/Blaster/Blaster.lean').write_text('-- different backend\n')
        self.cli('--check', ok=False)
        self.cli('--run', ok=False)

    def test_input_mutation_during_run_and_environment_drift_refuse(self):
        self.cli('--run')
        self.env['LEAN_PATH'] = str(self.root / 'different-imports')
        self.cli('--check', ok=False)
        del self.env['LEAN_PATH']
        self.behavior = {'modify': True}
        self.write_behavior()
        self.cli('--run', ok=False)
        self.assertFalse(self.receipt()['complete'])

    def test_manifest_polarity_and_added_inputs_make_receipt_stale(self):
        self.cli('--run')
        self.config['checks'][0]['expected'] = 'falsified'
        self.save()
        self.cli('--check', ok=False)
        self.config['checks'][0]['expected'] = 'valid'
        self.save()
        (self.root / 'Helper.lean').write_text('def extra := 1\n')
        self.git('add', 'Helper.lean')
        self.cli('--check', ok=False)

    def test_expected_falsification_is_not_an_open_bug(self):
        self.config['checks'][0]['expected'] = 'falsified'
        self.save()
        self.behavior = {'safety:query': {'status': 'falsified', 'model': ['n: 4\n'],
                                        'variables': [{'name': 'n', 'type': 'Nat'}]}}
        self.write_behavior()
        self.cli('--run')
        self.assertFalse((self.root / '.fluxpoint-cex.jsonl').exists())

    def test_counterexample_pin_requires_exact_successful_replay(self):
        self.behavior = {'safety:query': {'status': 'falsified', 'model': ['n: 4\n'],
                                        'variables': [{'name': 'n', 'type': 'Nat'}]}}
        self.write_behavior()
        self.cli('--run', ok=False)
        rows = [json.loads(line) for line in (self.root / '.fluxpoint-cex.jsonl').read_text().splitlines()]
        row = rows[-1]
        self.assertEqual(row['tool'], 'blaster')
        self.assertEqual(row['bindings'], [{'name': 'n', 'type': 'Nat', 'value': 4}])
        self.cli('--check', script=SCRIPTS / 'cex.py', ok=False)
        case = {'cexId': row['cexId'], 'check': 'safety', 'bindings': row['bindings']}
        self.config['regressions'] = [case]
        self.save()
        self.cli('--pin', row['cexId'], '--file', str(self.root / MANIFEST), '--test-name', row['cexId'],
                 script=SCRIPTS / 'cex.py', ok=False)
        # A repair makes the universal property and the exact old input pass.
        self.behavior = {}
        self.write_behavior()
        # The old input itself must be checked, independently of the universal
        # property's reported result.
        self.behavior = {row['cexId']: {'status': 'falsified'}}
        self.write_behavior()
        self.cli('--run', ok=False)
        self.cli('--pin', row['cexId'], '--file', str(self.root / MANIFEST), '--test-name', row['cexId'],
                 script=SCRIPTS / 'cex.py', ok=False)
        self.behavior = {}
        self.write_behavior()
        self.cli('--run')
        self.cli('--pin', row['cexId'], '--file', str(self.root / MANIFEST), '--test-name', row['cexId'],
                 script=SCRIPTS / 'cex.py')
        self.cli('--check', script=SCRIPTS / 'cex.py')
        self.config['regressions'][0]['bindings'][0]['value'] = 5
        self.save()
        self.cli('--check', script=SCRIPTS / 'cex.py', ok=False)

    def test_log_drift_and_false_receipt_status_are_refused(self):
        self.cli('--run')
        receipt = self.receipt()
        receipt['checks'][0]['observed'] = 'undetermined'
        latest = self.root / '.claude/fluxpoint/blaster/latest.json'
        latest.write_text(json.dumps(receipt))
        self.cli('--check', ok=False)
        self.cli('--run')
        log = next(iter(self.receipt()['logs']))
        (self.root / log).write_text('replaced evidence\n')
        self.cli('--check', ok=False)

    def test_opaque_models_remain_open_and_unpinnable(self):
        self.behavior = {'safety:query': {'status': 'falsified', 'model': ['xs: (List.cons 4 List.nil)\n'],
                                        'variables': [{'name': 'xs', 'type': 'List Nat'}]}}
        self.write_behavior()
        self.cli('--run', ok=False)
        row = json.loads((self.root / '.fluxpoint-cex.jsonl').read_text().splitlines()[-1])
        self.assertIsNone(row['bindings'])
        self.cli('--pin', row['cexId'], '--file', str(self.root / MANIFEST), '--test-name', row['cexId'],
                 script=SCRIPTS / 'cex.py', ok=False)


class LeanScannerCoverage(unittest.TestCase):
    def test_scalar_reification_refuses_partial_ambiguous_or_opaque_models(self):
        sys.path.insert(0, str(SCRIPTS))
        import blaster
        variables = [{'name': n, 'type': t} for n, t in [('n', 'Nat'), ('i', 'Int'), ('b', 'Bool'), ('s', 'String')]]
        bindings = blaster.parse_model(['s: "a""b"\n', 'b: false\n', 'i: (- 7)\n', 'n: 4\n'], variables)
        self.assertEqual([v['value'] for v in bindings], [4, -7, False, 'a"b'])
        for model in ([], ['n: 1', 'n: 2'], ['n: -1'], ['n: (List.cons 1 List.nil)']):
            self.assertIsNone(blaster.parse_model(model, [{'name': 'n', 'type': 'Nat'}]))

    @unittest.skipIf(os.name == 'nt', 'POSIX process-group probe; Windows cleanup is covered by process-tree suites')
    def test_timeout_cleans_descendants_before_returning(self):
        sys.path.insert(0, str(SCRIPTS))
        import blaster
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = 'import time; from pathlib import Path; time.sleep(0.8); Path("leaked").write_text("bad")'
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(60)'
            with self.assertRaisesRegex(ValueError, 'timed out'):
                blaster.supervised(root, [sys.executable, '-c', parent], root / 'process.log', 0.3)
            time.sleep(0.9)
            self.assertFalse((root / 'leaked').exists())

    def test_imported_tactic_and_command_obligations_are_visible(self):
        sys.path.insert(0, str(SCRIPTS))
        import importlib
        proof = importlib.import_module('proof-guard')
        spec = importlib.import_module('spec-guard')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = 'import Blaster\ndef p : Prop := True\n#blaster [p]\n#blaster [∀ n : Nat, n = n]\ntheorem t : True := by blaster\n'
            (root / 'Main.lean').write_text(source)
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            out = subprocess.check_output([sys.executable, str(SCRIPTS / 'proof-guard.py'), '--root', tmp, '--scan'], text=True)
            self.assertIn('lean.blaster', out)
            obligations, _ = spec.scan(tmp)
            self.assertEqual(len(obligations), 4)
            before = {key: value['statementSha'] for key, value in obligations.items()}
            (root / 'Main.lean').write_text(source.replace('p : Prop := True', 'p : Prop := False'))
            after, _ = spec.scan(tmp)
            queries = [key for key in before if '#blaster' in key]
            self.assertEqual(len(queries), 2)
            self.assertTrue(all(before[key] != after[key]['statementSha'] for key in queries))
            self.assertNotEqual(before['lean:Main.lean:p'], after['lean:Main.lean:p']['statementSha'])


if __name__ == '__main__':
    unittest.main()
