#!/usr/bin/env python3
"""Compact source and native protocol enforcement; real proof smoke is separate."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import hashlib
import subprocess
import os
import shutil
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class Compact(unittest.TestCase):
    def consumer_command(self, root):
        from process_tree import bash_path
        template = (SCRIPTS.parent / 'templates/harness.sh').read_text(encoding='utf-8')
        gate = template[template.index('need_gate() {'):template.index('\nchanged() {')]
        compact = template[template.index("  # Compact's native adapter"):template.index('  # Statement ratchet.')]
        script = root / 'consumer-harness.sh'
        script.write_bytes(('set -euo pipefail\nplugin_script() { return 0; }\n' + gate + compact).encode('utf-8'))
        env = {**os.environ, 'FPL_PY': Path(sys.executable).as_posix(), 'FPL_ALLOW_MISSING_GATES': '0'}
        return [bash_path(), script.as_posix()], env

    def fixture(self):
        from compact_native import load
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root / 'c.compact').write_text('export circuit check(x: Uint<8>): [] { assert(x > 0, "positive"); }')
        (root / 'param').write_text('pinned params')
        compiler = root / 'compiler.py'
        compiler.write_text('''import json,sys
from pathlib import Path
if sys.argv[-1].startswith('--'):
 print({'--version':'0.31.1','--ledger-version':'ledger-8.0.2','--runtime-version':'0.16.0'}[sys.argv[-1]])
else:
 out=Path(sys.argv[-1])
 for name in ['compiler','keys','contract','zkir']: (out/name).mkdir(parents=True)
 (out/'compiler/contract-info.json').write_text(json.dumps({'circuits':[{'name':'check','proof':True}]}))
 (out/'zkir/check.zkir').write_text(json.dumps({'instructions':[{'op':'assert','var':1}]}))
 (out/'zkir/check.bzkir').write_bytes(b'binary IR')
 (out/'keys/check.verifier').write_bytes(b'verifier')
 (out/'keys/check.prover').write_bytes(b'prover')
 (out/'contract/index.js').write_text('generated code')
''')
        native = root / 'native.py'
        native.write_text('import json,sys\nr=json.load(sys.stdin);print(json.dumps({"valid":r["valid"],"code":"verified" if r["valid"] else "invalid-proof"}))\n')
        inspector = root / 'inspector.py'
        inspector.write_text('''import json,sys,subprocess
request=sys.stdin.read();argv=json.loads(sys.argv[sys.argv.index('--native')+1]);registry=sys.argv[sys.argv.index('--registry')+1]
run=subprocess.run(argv+['--registry',registry],input=request,capture_output=True,text=True,check=True)
value=json.loads(run.stdout);value['metrics']={'k':13,'proofBytes':96,'transcriptGas':{'readTime':1,'computeTime':2}} if value['valid'] else None
print(json.dumps(value))
''')
        driver = root / 'driver.py'
        driver.write_text('''import json,sys
from pathlib import Path
out=Path(sys.argv[1]); (out/'initial-state.bin').write_bytes(b'constructed state')
(out/'registry.json').write_text(json.dumps({'circuits':{'fixture':{'entryPoint':'check','verifierKeyPath':'keys/check.verifier','initialContractStatePath':'initial-state.bin'}}}))
print(json.dumps({'version':1,'complete':True,'registry':'registry.json','witnesses':{},'controls':[{'id':'genuine','circuit':'check','request':{'circuitId':'fixture','valid':True,'transactionBase64':'AA=='}},{'id':'corrupted','circuit':'check','request':{'circuitId':'fixture','valid':False,'transactionBase64':'AA=='}}]}))
''')
        def tool(script):
            return {'argv':[sys.executable,str(script)],'files':{path:hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in [sys.executable,str(script)]}}
        params = root / 'bls_midnight_2p13'; params.write_bytes(b'parameters')
        cfg={'version':1,'compiler':tool(compiler),'compilerVersion':'0.31.1','compilerLedger':'ledger-8.0.2','runtimeVersion':'0.16.0','native':tool(native),'inspector':tool(inspector),'params':{str(params):hashlib.sha256(params.read_bytes()).hexdigest()},'projects':[{'source':'c.compact','circuits':['check'],'driver':[sys.executable,str(driver),'{artifacts}'],'cases':{'genuine':True,'corrupted':False},'timeoutSeconds':10}]}
        (root/'.fluxpoint-compact.json').write_text(json.dumps(cfg))
        return root, load(root)

    def test_full_adapter_protocol_and_generated_artifact_measurement(self):
        from compact_native import measure, compare
        root, cfg = self.fixture()
        first = measure(root, cfg); second = measure(root, cfg)
        self.assertEqual(compare(first, second), [])
        self.assertEqual(first['costs']['c.compact#check']['k'], 13)
        self.assertEqual(first['costs']['c.compact#check']['verifierKeyBytes'], 8)

    def test_incomplete_or_wrong_native_controls_refuse_measurement(self):
        from compact_native import measure, NativeFailure
        root, cfg = self.fixture()
        cfg['projects'][0]['cases']['genuine'] = False
        with self.assertRaises(NativeFailure): measure(root, cfg)
        cfg['projects'][0]['cases']['missing'] = True
        with self.assertRaises(ValueError): measure(root, cfg)

    def test_tool_and_parameter_drift_refuses_before_execution(self):
        from compact_native import load
        root, _ = self.fixture()
        (root / 'bls_midnight_2p13').write_bytes(b'changed')
        with self.assertRaises(ValueError): load(root)

    def test_absent_native_manifest_is_dormant_only_without_state_or_witnesses(self):
        root, _ = self.fixture()
        (root / '.fluxpoint-compact.json').unlink()
        script = [sys.executable, str(SCRIPTS / 'compact-guard.py'), '--root', str(root), '--check']
        def check(text):
            (root / 'c.compact').write_text(text)
            subprocess.run(['git', '-C', str(root), 'add', 'c.compact'], check=True)
            return subprocess.run(script, capture_output=True, text=True)
        self.assertEqual(check('// ledger state; witness secret();\nexport pure circuit helper(): Uint<8> { return 1; }').returncode, 0)
        self.assertEqual(check('const text = "ledger witness";').returncode, 0)
        mandatory = subprocess.run(script + ['--require-armed'], capture_output=True, text=True)
        self.assertNotEqual(mandatory.returncode, 0)
        self.assertIn('NOT ARMED', mandatory.stderr)
        for declaration in ('ledger value: Uint<8>;', 'witness secret(): Uint<8>;'):
            with self.subTest(declaration=declaration):
                result = check(declaration)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('NOT ARMED', result.stderr)

    def test_manifest_must_cover_all_tracked_stateful_or_private_sources(self):
        from compact_native import load
        root, _ = self.fixture()
        (root / 'omitted.compact').write_text('ledger balance: Uint<8>;')
        subprocess.run(['git', '-C', str(root), 'add', 'omitted.compact'], check=True)
        with self.assertRaisesRegex(ValueError, 'lack native project coverage'): load(root)

    def test_non_git_directories_are_dormant_but_broken_git_refuses(self):
        from compact_native import tracked_native_sources
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(tracked_native_sources(root), set())
            (root / '.git').write_text('gitdir: /missing/fluxpoint-native-test-gitdir\n')
            with self.assertRaisesRegex(ValueError, 'cannot inventory'): tracked_native_sources(root)

    @unittest.skipUnless(shutil.which('bash'), 'consumer harness requires Bash')
    def test_consumer_template_detects_missing_plugin_from_real_source_tokens(self):
        root, _ = self.fixture()
        (root / '.fluxpoint-compact.json').unlink()
        command, env = self.consumer_command(root)
        for text, expected in [('// ledger witness\nconst prose = "ledger witness";', 0), ('witness secret(): Uint<8>;', 1)]:
            with self.subTest(text=text):
                (root / 'c.compact').write_text(text)
                subprocess.run(['git', '-C', str(root), 'add', 'c.compact'], check=True)
                result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected: self.assertIn('compact-guard.py is not installed', result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command, env = self.consumer_command(root)
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_or_malformed_proof_controls_refuse_before_execution(self):
        from compact_native import load
        root, _ = self.fixture()
        path = root / '.fluxpoint-compact.json'
        initial = json.loads(path.read_text())
        for field, value in [('circuits', []), ('cases', {}), ('cases', {'genuine': 'true', 'corrupted': False})]:
            with self.subTest(field=field, value=value):
                current = json.loads(json.dumps(initial)); current['projects'][0][field] = value
                path.write_text(json.dumps(current))
                with self.assertRaises(ValueError): load(root)

    def test_only_equivalent_mutants_never_produce_an_empty_green_score(self):
        from compact_native import mutation_measure
        root, _ = self.fixture()
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        fixed = {'pins': {'code': 'identical'}, 'relations': {'check': 'identical'}}
        with patch('compact_native.measure', return_value=fixed):
            record, findings = mutation_measure(root, [])
        self.assertIsNone(record)
        self.assertIn('nothing scored', ' '.join(findings))

    def test_semantic_control_is_required_before_mutation_scoring(self):
        from compact_native import mutation_measure
        root, _ = self.fixture()
        record, findings = mutation_measure(root, [])
        self.assertIsNone(record)
        self.assertIn('semantic bad-input', ' '.join(findings))

    def test_driver_cannot_replace_fresh_compiler_artifacts(self):
        from compact_native import measure
        root, cfg = self.fixture()
        driver = root / 'driver.py'
        driver.write_text(driver.read_text().replace("print(json.dumps(", "(out/'keys/check.verifier').write_bytes(b'weaker key')\nprint(json.dumps(", 1))
        with self.assertRaisesRegex(ValueError, 'changed a fresh compiler artifact'): measure(root, cfg)

    def test_relative_extensionless_tool_launcher_is_refused(self):
        from compact_native import pinned_tool
        with self.assertRaisesRegex(ValueError, 'launcher paths'):
            pinned_tool({'argv': [sys.executable, 'compiler'], 'files': {sys.executable: hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}})

    def test_every_compiled_circuit_needs_crypto_controls(self):
        from compact_native import measure
        root, cfg = self.fixture()
        compiler = root / 'compiler.py'
        text = compiler.read_text().replace("'proof':True}]", "'proof':True},{'name':'other','proof':True}]")
        text += "\nif not sys.argv[-1].startswith('--'):\n import shutil\n for folder,suffix in [('keys','verifier'),('keys','prover'),('zkir','zkir'),('zkir','bzkir')]: shutil.copyfile(out/folder/('check.'+suffix), out/folder/('other.'+suffix))\n"
        compiler.write_text(text)
        cfg['compiler']['files'][str(compiler)] = hashlib.sha256(compiler.read_bytes()).hexdigest()
        cfg['projects'][0]['circuits'].append('other')
        with self.assertRaisesRegex(ValueError, 'each compiled circuit'): measure(root, cfg)

    def test_source_hatches_ignore_comments_and_strings(self):
        from compact_native import hatches
        text = '''// todo assert(true) disclose(x)
export circuit empty(): [] { log("todo disclose(x) assert(true)"); }
export circuit weak(): [] { assert(true, "constant"); todo; }
export circuit leak(): [] { const x = disclose(value); }
'''
        rows = hatches('contract.compact', text)
        self.assertEqual([r[0] for r in rows].count('compact.todo'), 1)
        self.assertEqual([r[0] for r in rows].count('compact.assert_constant'), 1)
        self.assertEqual([r[0] for r in rows].count('compact.no_assert_circuit'), 2)
        self.assertEqual([r[0] for r in rows].count('compact.unjustified_disclose'), 1)

    def test_disclosure_justification_and_circuit_statements(self):
        from compact_native import hatches, circuit_statements
        text = '''export circuit post(x: Uint<8>): [] {
  assert(x > 1, "bound");
  // disclose: The public counter is explicitly part of the application statement.
  public = disclose(x);
}'''
        self.assertFalse(hatches('contract.compact', text))
        before = circuit_statements(text)
        after = circuit_statements(text.replace('x > 1', 'x > 0'))
        self.assertNotEqual(before, after)

    def test_literal_values_and_real_comment_justifications_bind(self):
        from compact_native import hatches, circuit_statements
        first = 'export circuit check(x: Bytes<1>): [] { assert(x == pad(1, "A"), "bound"); }'
        self.assertNotEqual(circuit_statements(first), circuit_statements(first.replace('"A"', '"B"')))
        self.assertNotEqual(circuit_statements(first.replace('"A"', '"a b"')), circuit_statements(first.replace('"A"', '"a  b"')))
        prose = 'const note = "// disclose: this is just a string, never a justification";\nexport circuit leak(): [] { ledger = disclose(value); }'
        self.assertIn('compact.unjustified_disclose', [row[0] for row in hatches('c.compact', prose)])

    def test_mutation_surface_contains_all_families_without_prose(self):
        from compact_native import mutants
        text = '// assert(x == y); disclose(x);\nwitness secret(): Uint<8>;\nexport circuit c(x: Uint<8>): [] { assert(secret() == 3, "same"); ledger = disclose(x); }'
        rows = list(mutants('contract.compact', text))
        self.assertEqual({r['kind'] for r in rows}, {'drop-assert', 'flip-comparison', 'remove-disclose', 'add-disclose'})
        self.assertTrue(all(r['text'].startswith('// assert(x == y); disclose(x);') for r in rows))

    def test_native_results_fail_closed(self):
        from compact_native import native_result
        self.assertTrue(native_result({'valid': True, 'code': 'verified'}, True))
        self.assertTrue(native_result({'valid': False, 'code': 'invalid-proof'}, False))
        for result in ({'valid': False, 'code': 'configuration-error'}, {'valid': True}, {'valid': False, 'code': 'unknown-circuit'}, {'valid': 'true', 'code': 'verified'}):
            with self.subTest(result=result):
                with self.assertRaises(ValueError): native_result(result, True)

    def test_relation_canonicalization_and_costs_are_exact(self):
        from compact_native import relation, compare
        self.assertEqual(relation({'instructions': [{'op': 'assert', 'a': 1}], 'version': 2}), relation({'version': 2, 'instructions': [{'a': 1, 'op': 'assert'}]}))
        self.assertNotEqual(relation({'instructions': []}), relation({'instructions': [{'op': 'assert'}]}))
        old = {'pins': {'c': 'x'}, 'relations': {'c': 'y'}, 'costs': {'c': {'k': 13, 'proofBytes': 20}}}
        self.assertEqual(compare(old, old), [])
        self.assertTrue(compare(old, {**old, 'costs': {'c': {'k': 13, 'proofBytes': 21}}}))
        self.assertTrue(compare(old, {**old, 'relations': {'c': 'other'}}))
        self.assertTrue(compare(old, {**old, 'costs': {}}))

    def test_unregistered_compact_obligations_become_visible(self):
        spec = importlib.util.spec_from_file_location('spec_guard', SCRIPTS / 'spec-guard.py')
        guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
        rows = guard.scan_compact('c.compact', 'export circuit check(x: Uint<8>): [] { assert(x > 0, "positive"); }', {})
        self.assertEqual(rows[0]['id'], 'compact:c.compact:check')


if __name__ == '__main__':
    unittest.main()
