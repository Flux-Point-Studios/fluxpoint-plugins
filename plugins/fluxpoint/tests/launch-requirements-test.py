#!/usr/bin/env python3
"""Versioned compiler launch contracts are reported and enforced (#141)."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('compiler', PLUGIN / 'scripts/compile-graph.py')
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(PLUGIN / 'contracts'))
BASE = {'version': 1, 'campaign': 'Launch contract', 'budget': {'maxNodes': 10},
        'nodes': [{'id': 'read', 'prompt': 'Read the target.', 'contract': 'DesignV1'}]}


def run(ir, args):
    js = cg.emit(ir, CONTRACTS)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / 'out.json'
        script = root / 'run.mjs'
        prefix = '''import {writeFileSync} from 'node:fs';
const calls=[];const agent=async(p,o)=>{calls.push(o.label);return String(o.label).startsWith('tree-check:')?{head:'base',porcelain:'',root:'/repo'}:{summary:'read',plan:'done',files:[],risks:[]}};
const parallel=async xs=>Promise.all(xs.map(f=>f())),pipeline=async()=>[],log=()=>{},phase=()=>{};
const budget={total:null,spent:()=>0,remaining:()=>1e9};const workflow=0;
'''
        script.write_text(prefix + 'const args=' + json.dumps(args) + ';\n(async()=>{\n' +
                          js.replace('export const meta =', 'const meta = globalThis.LAUNCH_META =') +
                          '\n})().then(r=>writeFileSync(' + json.dumps(str(out)) +
                          ',JSON.stringify({r,calls,meta:globalThis.LAUNCH_META}))).catch(e=>writeFileSync(' +
                          json.dumps(str(out)) + ',JSON.stringify({err:String(e.message||e),calls,meta:globalThis.LAUNCH_META})));')
        p = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
        if not out.exists():
            raise AssertionError(p.stderr)
        return json.loads(out.read_text())


class LaunchRequirements(unittest.TestCase):
    def test_shared_table_conditions_and_all_producers(self):
        ir = copy.deepcopy(BASE)
        ir['requiredArgs'] = ['target']
        ir['nodes'] = [dict(ir['nodes'][0], mutates=True, verify='prove:harness'),
                       {'id': 'human', 'actor': 'human'}, {'id': 'effect', 'irreversible': True},
                       {'id': 'repair', 'repeat': {'until': 'exit == 0'}}]
        requirements = cg.launch_requirements(ir)
        args = {item['name']: item for item in requirements['args']}
        self.assertEqual(set(args), {'_base', '_launch', '_releases', '_ledger', 'target'})
        self.assertEqual(args['_launch']['fields'], ['since', 'nonce', 'root'])
        for name, producer in [('_launch', 'attest.py --stamp'), ('_ledger', 'ledger.py --load'), ('_releases', 'release.py --load')]:
            self.assertEqual(args[name]['producer'], producer)
        self.assertIn('watchdog.py --run <runId>', requirements['postLaunch'][0]['command'])

    def test_missing_custom_arg_names_version_and_actual_producer_before_any_agent(self):
        ir = copy.deepcopy(BASE)
        ir['requiredArgs'] = ['target']
        with patch.object(cg, 'compiler_version', return_value='9.8.7'):
            result = run(ir, {})
        self.assertEqual(result['calls'], [])
        self.assertIn('compiled by fluxpoint 9.8.7', result['err'])
        self.assertIn('args.target', result['err'])
        self.assertIn('caller (requiredArgs)', result['err'])

    def test_old_base_shape_cannot_silently_disable_dirt_guard(self):
        for isolated in (False, True):
            ir = copy.deepcopy(BASE)
            if isolated:
                ir['nodes'][0]['isolation'] = True
            result = run(ir, {'_base': {'sha': 'base', 'branch': 'main'}})
            self.assertEqual(result['calls'], [])
            self.assertIn('git status --porcelain', result['err'])
        fine = run(BASE, {'_base': {'sha': 'base', 'branch': '', 'porcelain': '', 'root': '/repo'}})
        self.assertNotIn('err', fine)

    def test_meta_header_and_refusals_share_manifest_version_and_requirements(self):
        ir = copy.deepcopy(BASE)
        ir['nodes'][0]['isolation'] = True
        version = json.loads((PLUGIN / '.claude-plugin/plugin.json').read_text())['version']
        result = run(ir, {})
        self.assertIn('compiled by fluxpoint ' + version, cg.emit(ir, CONTRACTS))
        self.assertEqual(result['meta']['compilerVersion'], version)
        self.assertEqual(result['meta']['launchRequirements'], cg.public_launch_requirements(cg.launch_requirements(ir)))
        self.assertIn('compiled by fluxpoint ' + version, result['err'])

    def test_check_and_wrote_publish_launch_and_postlaunch_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / 'WORK.md'
            graph.write_text('```json graph-ir\n' + json.dumps(BASE) + '\n```\n')
            cli = [sys.executable, str(PLUGIN / 'scripts/compile-graph.py'), str(graph)]
            for flags in (['--check'], ['-o', str(Path(tmp) / 'run.js')]):
                result = subprocess.run(cli + flags, cwd=tmp, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(cg.launch_requirements_note(BASE), result.stdout)
                self.assertIn('watchdog.py', result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
