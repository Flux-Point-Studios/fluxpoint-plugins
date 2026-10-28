#!/usr/bin/env python3
"""Exercise compiled witness pins and producer checks against actual files."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import plugin_identity
import attest
spec = importlib.util.spec_from_file_location('pinned_compiler', SCRIPTS / 'compile-graph.py')
cg = importlib.util.module_from_spec(spec); spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(SCRIPTS.parent / 'contracts'))

class Identity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='plugin-identity-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def installation(self):
        target = self.root / 'plugin'
        shutil.copytree(SCRIPTS.parent, target)
        return target

    def test_emitted_graph_and_summary_carry_exact_pin_and_explicit_witness(self):
        ir = {'version': 1, 'campaign': 'Pin test', 'treeGuard': False, 'nodes': [
            {'id': 'gate', 'contract': 'ExecutionV1', 'verify': 'prove:suite', 'prompt': 'Check suite.'}]}
        code = cg.emit(ir, CONTRACTS)
        self.assertIn('const PLUGIN = ', code)
        self.assertIn('plugin: PLUGIN', code)
        self.assertIn('compilerVersion:', code)
        self.assertIn('--plugin-pin', code)
        self.assertNotIn("run the fluxpoint plugin's scripts/py.sh", code)
        path = self.root / 'script.js'; path.write_text(code)
        self.assertEqual(plugin_identity.from_script(path), plugin_identity.current())

    def test_preflight_refuses_entrypoint_helper_and_missing_installation(self):
        plugin = self.installation(); pin = plugin_identity.current(plugin)
        for relative in ('scripts/attest.py', 'scripts/py.sh', 'scripts/process_tree.py'):
            target = plugin / relative; original = target.read_bytes()
            target.write_bytes(original + b'\n# drift\n')
            with self.assertRaisesRegex(ValueError, 'PLUGIN-PIN.*changed'): plugin_identity.verify(pin)
            target.write_bytes(original)
        shutil.rmtree(plugin / 'scripts')
        with self.assertRaisesRegex(ValueError, 'PLUGIN-PIN.*unavailable'): plugin_identity.verify(pin)

    def test_unpinned_script_refused(self):
        path = self.root / 'script.js'; path.write_text('export const meta = {}\n')
        with self.assertRaisesRegex(ValueError, 'no pinned'): plugin_identity.from_script(path)

    def test_cli_self_check_matches_compiler_and_rejects_changed_pin(self):
        pin = plugin_identity.current()
        command = [sys.executable, str(SCRIPTS / 'attest.py'), '--self-check', '--plugin-pin', json.dumps(pin)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), pin)
        wrong = dict(pin, attestSha256='0' * 64)
        result = subprocess.run(command[:-1] + [json.dumps(wrong)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PLUGIN-PIN', result.stderr)

    def test_row_records_its_loaded_producer(self):
        attest.append_row(str(self.root), {'attestId': 'one', 'gate': 'suite', 'exit': 0})
        rows = attest.read(str(self.root))
        self.assertEqual(rows[0]['producer'], attest.PRODUCER)

    def test_cited_different_or_missing_producer_is_mismatch(self):
        pin = plugin_identity.current()
        row = {'attestId': 'one', 'gate': 'suite', 'exit': 0, 'producer': pin,
               'when': '2026-01-01T10:00:00Z', 'nonce': 'n', 'headSha': 'a' * 40, 'treeSha': 'a' * 40, 'dirty': False}
        launch = {'since': '2026-01-01T09:00:00Z', 'nonce': 'n'}
        result = {'exit': 0, 'attestId': 'one', 'sha': 'a' * 40}
        for producer in (None, dict(pin, version='0.0.0')):
            changed = dict(row, producer=producer)
            check = attest._check_cited([changed], 'gate', 'suite', result, launch, plugin=pin)
            self.assertEqual(check['status'], 'MISMATCH')
            self.assertIn('producer', check['detail'])
        good = attest._check_cited([row], 'gate', 'suite', result, launch, plugin=pin)
        self.assertNotEqual(good['status'], 'MISMATCH', good)

    def test_pinned_command_quotes_shell_metacharacters(self):
        plugin = self.root / "plugin'$(touch PWN)"
        shutil.copytree(SCRIPTS.parent, plugin)
        pin = plugin_identity.current(plugin)
        ir = {'version': 1, 'campaign': 'Pin quoting', 'treeGuard': False, 'nodes': [
            {'id': 'gate', 'contract': 'ExecutionV1', 'verify': 'prove:suite', 'prompt': 'Check.'}]}
        from unittest.mock import patch
        with patch.object(plugin_identity, 'current', return_value=pin):
            # The compiler loads a fresh sibling module; patch its loader instead.
            original = cg._sibling
            with patch.object(cg, '_sibling', side_effect=lambda name: plugin_identity if name == 'plugin_identity' else original(name)):
                code = cg.emit(ir, CONTRACTS)
        lines = [line for line in code.splitlines() if line.startswith(('const PLUGIN =', 'const quoteShell =', 'const ATTEST ='))]
        command = subprocess.run(['node', '-e', '\n'.join(lines) + '\nconsole.log(ATTEST)'], capture_output=True, text=True, check=True).stdout.strip()
        result = subprocess.run(['bash', '-c', command + ' --self-check'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), pin)
        self.assertFalse((self.root / 'PWN').exists())

    def test_verify_claims_passes_compiled_pin_to_citation_check(self):
        (self.root / '.fluxpoint-gates.json').write_text(json.dumps({'version': 1, 'gates': {'suite': 'true'}}))
        pin = plugin_identity.current()
        attest.append_row(str(self.root), {'attestId': 'one', 'gate': 'suite', 'exit': 0})
        summary = {'plugin': dict(pin, version='different'), 'prove': {'gate': 'suite'},
                   'contracts': {'gate': 'ExecutionV1'}, 'results': {'gate': {'gate': 'suite', 'exit': 0, 'attestId': 'one'}}}
        checks, findings = attest.verify_claims(str(self.root), summary)
        self.assertFalse(findings)
        self.assertEqual(checks[0]['status'], 'MISMATCH')
        self.assertIn('producer', checks[0]['detail'])

    def test_actual_launch_preflight_rejects_missing_and_changed_pin(self):
        ir = {'version': 1, 'campaign': 'Launch pin', 'treeGuard': False, 'budget': {'maxNodes': 2},
              'nodes': [{'id': 'read', 'contract': 'DesignV1', 'prompt': 'Read.'}]}
        graph = self.root / 'WORK.md'; graph.write_text('```json graph-ir\n' + json.dumps(ir) + '\n```\n')
        script = self.root / 'run.js'
        compile_command = [sys.executable, str(SCRIPTS / 'compile-graph.py'), str(graph), '--gates-root', str(self.root)]
        compiled = subprocess.run(compile_command + ['-o', str(script)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        check = compile_command + ['--check', '--check-script', str(script)]
        self.assertEqual(subprocess.run(check, capture_output=True).returncode, 0)
        code = script.read_text()
        for altered in (code.replace('const PLUGIN = ', 'const LEGACY_PLUGIN = '), code.replace('"attestSha256": "', '"attestSha256": "0')):
            script.write_text(altered)
            result = subprocess.run(check, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('PLUGIN-PIN', result.stderr)

    def test_resume_refuses_unavailable_prior_pin_before_launch(self):
        ir = {'version': 1, 'campaign': 'Resume pin', 'treeGuard': False,
              'nodes': [{'id': 'read', 'contract': 'DesignV1', 'prompt': 'Read.'}]}
        (self.root / 'WORK.md').write_text('```json graph-ir\n' + json.dumps(ir) + '\n```\n')
        runs = self.root / '.claude/fluxpoint/runs'; runs.mkdir(parents=True)
        pin = dict(plugin_identity.current(), root=str(self.root / 'missing-plugin'))
        (runs / 'wf1.json').write_text(json.dumps({'runId': 'wf1', 'summary': {'plugin': pin}}))
        result = subprocess.run([sys.executable, str(SCRIPTS / 'resume.py'), '--root', str(self.root), '--graph', 'WORK.md', '--run', 'wf1'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PLUGIN-PIN', result.stderr)

    def test_resume_cannot_silently_switch_from_available_other_installation(self):
        other = self.installation()
        pin = plugin_identity.current(other)
        ir = {'version': 1, 'campaign': 'Other resume pin', 'treeGuard': False,
              'nodes': [{'id': 'read', 'contract': 'DesignV1', 'prompt': 'Read.'}]}
        (self.root / 'WORK.md').write_text('```json graph-ir\n' + json.dumps(ir) + '\n```\n')
        runs = self.root / '.claude/fluxpoint/runs'; runs.mkdir(parents=True)
        (runs / 'wf1.json').write_text(json.dumps({'runId': 'wf1', 'summary': {'plugin': pin}}))
        result = subprocess.run([sys.executable, str(SCRIPTS / 'resume.py'), '--root', str(self.root), '--graph', 'WORK.md', '--run', 'wf1'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('resume helper differs', result.stderr)

if __name__ == '__main__': unittest.main()
