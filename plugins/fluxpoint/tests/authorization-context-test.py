#!/usr/bin/env python3
"""A recorded ruling supplies provenance, and never extends the launch message."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('authorization_compiler', PLUGIN / 'scripts/compile-graph.py')
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(PLUGIN / 'contracts'))
RECORD = {'question': 'May this graph implement the approved repair?', 'options': [
    {'option': s, 'argued_by': 'operator', 'strongest_objection': 'This alternative has a concrete operational risk.'}
    for s in ('run the repair', 'defer the repair')], 'chosen': 'run the repair',
    'rationale': 'The operator approves implementing this repair within the campaign scope and its verification obligations.',
    'overturned_prior': False, 'frozen_by': 'none', 'reversible': True, 'evidence': ['operator ruling']}


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'decisions.jsonl'
        self.store.write_text(json.dumps({'id': 'run-ruling', 'recordId': 'dec_fixture',
            'when': '2026-10-15 12:34:56', 'source': 'decision.py', 'record': RECORD}) + '\n')
        self.ir = {'version': 1, 'campaign': 'authorization context', 'treeGuard': False,
                   'imports': {'run-ruling': 'latest'}, 'authorizedBy': 'run-ruling',
                   'nodes': [{'id': 'first', 'mutates': True, 'prompt': 'first mutation', 'contract': 'DesignV1'},
                             {'id': 'read', 'prompt': 'read only', 'contract': 'DesignV1'},
                             {'id': 'second', 'mutates': True, 'prompt': 'second mutation', 'contract': 'DesignV1'}]}

    def resolve(self, ir=None):
        return cg.resolve_imports(ir or self.ir, CONTRACTS, str(self.root / 'runs'), str(self.store))

    def test_resolved_metadata_and_runtime_mutating_prompts(self):
        resolved, findings = self.resolve()
        self.assertEqual(findings, [])
        self.assertEqual(resolved['run-ruling']['recordId'], 'dec_fixture')
        self.assertEqual(resolved['run-ruling']['when'], '2026-10-15 12:34:56')
        script = cg.emit(self.ir, CONTRACTS, resolved)
        script = script.replace('export const meta', 'const meta')
        out = self.root / 'out.json'
        harness = ("import {writeFileSync} from 'node:fs';\nconst prompts=[];"
                   "const args={_base:{sha:'" + 'a' * 40 + "',branch:'test',porcelain:''}};"
                   "const phase=()=>{},log=()=>{},budget={remaining:()=>10000000};const agent=async(p,o)=>{prompts.push({p,label:o.label});return {}};const spawn=agent;"
                   "(async()=>{\n" + script + "\n})().then(()=>writeFileSync(" + json.dumps(str(out))
                   + ",JSON.stringify(prompts))).catch(e=>{console.error(e);process.exitCode=1});")
        path = self.root / 'run.mjs'
        path.write_text(harness)
        p = subprocess.run(['node', str(path)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        prompts = json.loads(out.read_text())
        for row in prompts:
            self.assertEqual('RECORDED AUTHORIZATION CONTEXT' in row['p'], row['label'] in ('first', 'second'))
            if row['label'] in ('first', 'second'):
                for literal in ('dec_fixture', '2026-10-15 12:34:56', 'run the repair', 'cannot override or extend'):
                    self.assertIn(literal, row['p'])

    def test_authorization_requires_import_and_timestamp(self):
        bad = copy.deepcopy(self.ir)
        bad.pop('imports')
        self.assertTrue(any('authorizedBy' in f for f in cg.validate(bad, CONTRACTS)))
        row = json.loads(self.store.read_text())
        row.pop('when')
        self.store.write_text(json.dumps(row) + '\n')
        self.assertIn('no recorded timestamp', self.resolve()[1][0])

    def test_warning_and_emission_backstop(self):
        bad = copy.deepcopy(self.ir)
        bad.pop('authorizedBy')
        self.assertTrue(any('no authorizedBy' in w for w in cg.warnings(bad)))
        with self.assertRaises(cg.GraphError):
            cg.emit(self.ir, CONTRACTS, {})


if __name__ == '__main__':
    unittest.main()
