#!/usr/bin/env python3
"""Compact packet references retain the compiled identity at execution."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import specification
loader = importlib.util.spec_from_file_location('spec_fixture', Path(__file__).with_name('specification-test.py'))
fixture = importlib.util.module_from_spec(loader); loader.loader.exec_module(fixture)
loader = importlib.util.spec_from_file_location('reference_compiler', SCRIPTS / 'compile-graph.py')
cg = importlib.util.module_from_spec(loader); loader.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(SCRIPTS.parent / 'contracts'))

class ReferenceContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='packet-reference-'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.doc = fixture.packet()
        self.marker = 'UNIQUE_ARCHIVED_SCOPE_' + '\u03bb' * 20000
        (self.root / 'archive.json').write_text(json.dumps({'scope': self.marker}), encoding='utf-8')
        self.doc['checks'][0]['scope'] = 'archive.json#/scope'
        self.save_lock()
        self.ir = {'version': 1, 'campaign': 'Reference context', 'treeGuard': False,
                   'budget': {'maxNodes': 2}, 'nodes': [{'id': 'read', 'prompt': 'Read the needed requirement.', 'contract': 'DesignV1'}]}

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'specification.py'), '--root', str(self.root), *args], capture_output=True, text=True)

    def save_lock(self):
        (self.root / '.fluxpoint-spec.json').write_text(json.dumps(self.doc), encoding='utf-8')
        result = self.cli('--lock'); self.assertEqual(result.returncode, 0, result.stderr)
        packet, identity = specification.load(self.root)
        self.spec = {'path': '.fluxpoint-spec.json', 'packet': packet, 'identity': identity}

    def test_reference_script_omits_archive_and_reduces_price_and_bytes(self):
        inline = cg.emit(self.ir, CONTRACTS, specification=self.spec)
        ir = dict(self.ir, specificationContext='reference')
        reference = cg.emit(ir, CONTRACTS, specification=self.spec)
        self.assertNotIn('UNIQUE_ARCHIVED_SCOPE_', reference)
        self.assertLess(len(reference.encode()), len(inline.encode()) / 2)
        low = cg.estimate_tokens(ir, CONTRACTS, self.spec)
        high = cg.estimate_tokens(self.ir, CONTRACTS, self.spec)
        self.assertLess(low['total'], high['total'] / 2)
        self.assertEqual(low['perNode']['read']['specificationInputTokens'], low['assumptions']['specificationInputTokens'])
        self.assertIn('--check-identity', reference)
        self.assertIn('First command', reference)

    def test_identity_check_refuses_changed_packet_even_after_relock(self):
        old = hashlib.sha256(json.dumps(self.spec['identity'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(self.cli('--check', '--check-identity', old).returncode, 0)
        self.doc['goal'] = 'A deliberately revised requirement goal'
        self.save_lock()
        result = self.cli('--check', '--check-identity', old)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('compiled identity', result.stderr)

    def test_archive_digest_is_part_of_compiled_expectation(self):
        old = hashlib.sha256(json.dumps(self.spec['identity'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        (self.root / 'archive.json').write_text(json.dumps({'scope': 'A revised scope with a concrete new boundary'}))
        self.save_lock()
        result = self.cli('--check', '--check-identity', old)
        self.assertNotEqual(result.returncode, 0)

    def test_reference_summary_keeps_full_identity_and_prompt_keeps_only_catalogue(self):
        ir = dict(self.ir, specificationContext='reference')
        code = cg.emit(ir, CONTRACTS, specification=self.spec)
        out = self.root / 'out.json'; script = self.root / 'run.js'
        prelude = 'const prompts=[];const args={};const agent=async(p,o)=>{prompts.push(p);return {summary:"read",plan:"done",files:[],risks:[]}};const log=()=>{},phase=()=>{},budget={total:null,spent:()=>0};const parallel=async xs=>Promise.all(xs.map(f=>f()));'
        script.write_text('const fs=require("fs");'+prelude+'\n(async()=>{\n'+code.replace('export const meta =', 'const meta =')+'\n})().then(r=>fs.writeFileSync('+json.dumps(str(out))+',JSON.stringify({r,prompts})));')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(out.read_text())
        self.assertEqual(value['r']['specification'], self.spec['identity'])
        self.assertNotIn('UNIQUE_ARCHIVED_SCOPE_', value['prompts'][0])
        self.assertIn('reject-missing', value['prompts'][0])
        self.assertIn('spec-tests', value['prompts'][0])

    def test_emitted_first_command_checks_nested_project_in_worker_tree(self):
        import re
        import shlex
        nested = self.root / 'apps/package with space'
        nested.mkdir(parents=True)
        for name in ('archive.json', '.fluxpoint-spec.json', '.fluxpoint-spec-lock.json'):
            (self.root / name).rename(nested / name)
        # A different packet at repository root must never be read instead.
        (self.root / '.fluxpoint-spec.json').write_text('wrong repository-root packet')
        ir = dict(self.ir, specificationContext='reference')
        code = cg.emit(ir, CONTRACTS, specification=self.spec, project_prefix='apps/package with space/')
        prelude = 'const prompts=[];const args={};const agent=async(p)=>{prompts.push(p);return {summary:"read",plan:"done",files:[],risks:[]}};const log=()=>{},phase=()=>{},budget={total:null,spent:()=>0};const parallel=async xs=>Promise.all(xs.map(f=>f()));'
        program = prelude + '\n(async()=>{\n' + code.replace('export const meta =', 'const meta =') + '\n})().then(r=>process.stdout.write(JSON.stringify(prompts)))'
        rendered = subprocess.run(['node', '-'], input=program, cwd=self.root, text=True, capture_output=True, check=True)
        prompt = json.loads(rendered.stdout)[0]
        command = re.search(r"First command, from your working tree's repository root: (.+?)\. Stop if it fails", prompt).group(1)
        actual = subprocess.run(shlex.split(command), cwd=self.root, text=True, capture_output=True)
        self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr + '\n' + command)
        self.assertIn(self.spec['identity']['sha256'], actual.stdout)

    def test_invalid_context_mode_refused(self):
        findings = cg.validate(dict(self.ir, specificationContext='guess'), CONTRACTS)
        self.assertTrue(any('specificationContext' in f for f in findings))

    def test_shipped_templates_request_reference_context(self):
        for path in (SCRIPTS.parent / 'templates').glob('WORK*.md'):
            text = path.read_text()
            if '```json graph-ir' in text:
                self.assertEqual(cg.extract_ir(text).get('specificationContext'), 'reference', str(path))

if __name__ == '__main__': unittest.main()
