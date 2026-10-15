#!/usr/bin/env python3
"""Replay real Git patches, then exercise origin/resource guards with schema-skipping stubs."""
import copy
import importlib.util
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CG = module('origin_compiler', SCRIPTS / 'compile-graph.py')
METRICS = module('origin_metrics', SCRIPTS / 'metrics.py')
REPLAY = module('origin_replay', SCRIPTS / 'repair-replay.py')
CONTRACTS = CG.load_contracts(str(PLUGIN / 'contracts'))


def python_command(code):
    if os.name == 'nt':
        return subprocess.list2cmdline([sys.executable, '-c', code])
    return shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)



def schema_valid(value, schema):
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool}
    if kind == 'integer' and type(value) is not int:
        return False
    if kind in types and not isinstance(value, types[kind]):
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get('required', [])):
            return False
        if any(key in value and not schema_valid(value[key], child) for key, child in schema.get('properties', {}).items()):
            return False
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', len(value)):
            return False
        if schema.get('uniqueItems') and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            return False
        if 'items' in schema and not all(schema_valid(x, schema['items']) for x in value):
            return False
    if isinstance(value, str) and (len(value) < schema.get('minLength', 0) or 'pattern' in schema and not re.search(schema['pattern'], value)):
        return False
    if type(value) is int and value < schema.get('minimum', value):
        return False
    if not all(schema_valid(value, child) for child in schema.get('allOf', [])):
        return False
    return not ('if' in schema and schema_valid(value, schema['if']) and not schema_valid(value, schema.get('then', {})))


class RealReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'origin test')
        self.git('config', 'user.email', 'origin@example.invalid')
        self.state = {'bug': False, 'left': False, 'right': False}
        (self.root / 'state.json').write_text(json.dumps(self.state))
        self.git('add', 'state.json')
        self.git('commit', '-qm', 'launch base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.control = python_command('import json; assert isinstance(json.load(open("state.json")), dict); print("valid control passed")')
        self.command = python_command('import json,sys; s=json.load(open("state.json")); sys.exit(1 if s["bug"] else 0)')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, text=True, capture_output=True).stdout

    def patch(self, item, round_, state):
        (self.root / 'state.json').write_text(json.dumps(state))
        diff = self.git('diff', 'HEAD', '--', 'state.json')
        path = self.root / '.claude' / 'fluxpoint' / 'rounds' / 'launch' / item / ('r' + str(round_)) / 'patch.diff'
        path.parent.mkdir(parents=True)
        path.write_text(diff)
        self.git('checkout', '--', 'state.json')
        return {'item': item, 'round': round_, 'patch': str(path)}

    def run_replay(self, context, targets=None, command=None):
        return REPLAY.replay(str(self.root), context, 'decode', targets, self.control, command or self.command, 10)

    def test_first_reproducing_patch_decides_origin_and_round(self):
        r1 = self.patch('parser', 1, dict(self.state, left=True))
        r2 = self.patch('parser', 2, dict(self.state, bug=True))
        ctx = {'base': self.base, 'round': 2, 'patches': [r1, r2], 'current': r2, 'targetClasses': ['decode']}
        result = self.run_replay(ctx, ['decode'])
        self.assertEqual((result['origin'], result['introducedIn']), ('fix-introduced', 2))
        self.assertEqual([result['originEvidence']['base']['reproduces']] + [r['reproduces'] for r in result['originEvidence']['rounds']], [False, False, True])
        self.assertEqual(self.git('status', '--porcelain', '--', 'state.json'), '')

    def test_base_harm_remains_incomplete_even_through_new_fix_code(self):
        self.state['bug'] = True
        (self.root / 'state.json').write_text(json.dumps(self.state))
        self.git('add', 'state.json'); self.git('commit', '-qm', 'old bug')
        self.base = self.git('rev-parse', 'HEAD').strip()
        patch = self.patch('parser', 1, dict(self.state, new_fix_identifier=True))
        ctx = {'base': self.base, 'round': 1, 'patches': [patch], 'current': patch, 'targetClasses': ['decode']}
        self.assertEqual(self.run_replay(ctx, ['decode'])['origin'], 'incomplete-fix')
        self.assertEqual(self.run_replay(ctx)['origin'], 'incomplete-fix')
        for alternate in ([], ['different']):
            with self.assertRaisesRegex(ValueError, 'differ from the compiler context'):
                self.run_replay(ctx, alternate)
        untargeted = dict(ctx, targetClasses=[])
        self.assertEqual(self.run_replay(untargeted)['origin'], 'pre-existing')

    def test_missing_or_invalid_context_targets_cannot_be_selected_by_checker(self):
        patch = self.patch('parser', 1, dict(self.state, bug=True))
        ctx = {'base': self.base, 'round': 1, 'patches': [patch], 'current': patch}
        for targets in (None, [], ['decode'], ['decode', 'decode'], [None], 'decode'):
            bad = dict(ctx)
            if targets is not None:
                bad['targetClasses'] = targets
            if targets == [] or targets == ['decode']:
                continue  # Both are valid pinned declarations, covered by replay cases.
            with self.subTest(targets=targets), self.assertRaisesRegex(ValueError, 'explicit pinned targetClasses'):
                self.run_replay(bad, ['decode'])
        bad = dict(ctx, targetClasses=None)
        with self.assertRaisesRegex(ValueError, 'explicit pinned targetClasses'):
            self.run_replay(bad, ['decode'])

    def test_only_integrated_harm_is_cross_slice(self):
        left = self.patch('left', 1, dict(self.state, left=True))
        right = self.patch('right', 1, dict(self.state, right=True))
        combined = self.patch('_integration', 1, dict(self.state, left=True, right=True))
        command = python_command('import json,sys; s=json.load(open("state.json")); sys.exit(1 if s["left"] and s["right"] else 0)')
        ctx = {'base': self.base, 'round': 1, 'patches': [left, right], 'integrated': combined, 'targetClasses': []}
        result = self.run_replay(ctx, command=command)
        self.assertEqual(result['origin'], 'cross-slice')
        self.assertTrue(result['originEvidence']['integrated']['reproduces'])
        self.assertTrue(all(not row['reproduces'] for row in result['originEvidence']['rounds']))

    def test_timed_out_replay_terminates_and_does_not_classify(self):
        command = python_command('import time; time.sleep(30)')
        with self.assertRaises(subprocess.TimeoutExpired):
            REPLAY.execute(command, self.root, 0.05)

    def test_failed_controls_missing_patches_and_no_current_reproduction_refuse_origin(self):
        patch = self.patch('parser', 1, dict(self.state, left=True))
        ctx = {'base': self.base, 'round': 1, 'patches': [patch], 'current': patch, 'targetClasses': []}
        with self.assertRaises(ValueError):
            self.run_replay(ctx)
        with self.assertRaises(ValueError):
            REPLAY.replay(str(self.root), ctx, 'decode', [], 'exit 2', self.command, 10)
        bad = copy.deepcopy(ctx); bad['patches'][0]['patch'] += '.absent'
        with self.assertRaises(ValueError):
            self.run_replay(bad)


class RuntimeOriginGuards(unittest.TestCase):
    def run_graph(self, mode='introduced', integration=False):
        ir = {'version': 1, 'campaign': 'origin repair', 'treeGuard': False, 'budget': {'maxNodes': 30},
              'lists': {'slices': [{'key': 'parser', 'targetClasses': ['decode']}]},
              'nodes': [
                  {'id': 'fix', 'foreach': 'slices', 'mutates': True, 'prompt': 'fix {{roundDir}} {{lastCheck}}', 'contract': 'SliceV1'},
                  {'id': 'check', 'foreach': 'slices', 'after': 'fix', 'independent': True, 'verifies': 'fix', 'isolation': 'measure',
                   'prompt': 'check {{prev}}', 'contract': 'FixCheckV1', 'repeat': {'until': "verdict == 'PASS'", 'maxRounds': 2}, 'verifyOver': 'findings'}]}
        if integration:
            ir['nodes'][1]['repeat']['integrate'] = ['merge', 'review']
            ir['nodes'] += [
                {'id': 'merge', 'after': 'check', 'mutates': True, 'prompt': 'merge {{prev}}', 'contract': 'SliceV1'},
                {'id': 'review', 'after': 'merge', 'independent': True, 'verifies': 'merge', 'isolation': 'measure', 'prompt': 'review {{prev}}', 'contract': 'FixCheckV1', 'haltWhen': "verdict != 'PASS'"}]
        if mode == 'missing-targets':
            ir['lists']['slices'][0].pop('targetClasses')
        for name, value in [('null-targets', None), ('bad-targets', 'decode'), ('blank-targets', [' ']),
                            ('duplicate-targets', ['decode', 'decode']), ('empty-targets', [])]:
            if mode == name:
                ir['lists']['slices'][0]['targetClasses'] = value
        invalid_targets = mode in ('missing-targets', 'null-targets', 'bad-targets', 'blank-targets', 'duplicate-targets')
        if invalid_targets:
            self.assertTrue(any('targetClasses' in finding for finding in CG.validate(ir, CONTRACTS)))
        js = CG.emit(ir, CONTRACTS).replace('export const meta', 'const meta')
        prefix = r'''
import {writeFileSync} from 'node:fs';
const args={_base:{sha:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',porcelain:''},_launch:{root:'/campaign',nonce:'launch'}},calls=[],mode=MODE;
const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};
const parallel=async(xs)=>Promise.all(xs.map(f=>f()));
const observation=(reproduces)=>({command:'python attack.py',result:reproduces?'exit 1: harm observed':'exit 0: defended',controlCommand:'python valid.py',controlResult:'exit 0: valid control',controlPassed:true,reproduces});
const slice={branch:'repair',diffSummary:'a complete patch against the launch base',testsAdded:['test_attack'],harnessCommand:'bash scripts/harness.sh --full',harnessExit:0,evidence:['control and attack observed'],sharedResources:[{resource:'registry.sqlite',writers:['registry_builder'],readers:['executor_task']}]};
const agent=async(p,o)=>{const label=String(o.label);calls.push({label,p});
 if(label.startsWith('fix:')||label.startsWith('merge:')){const value=structuredClone(slice);
  if(mode==='missing-resources'&&label.startsWith('fix:')||mode==='missing-merge-resources'&&label.startsWith('merge:'))delete value.sharedResources;
  if(mode==='bad-resources')value.sharedResources=[{resource:'cache',writers:'new_route',readers:[]}];
  if(mode==='empty-resources')value.sharedResources=[];
  return value;}
 const c=JSON.parse(p.split('ORIGIN REPLAY CONTEXT: ')[1].split('\n')[0]);
 if(mode==='pass'||mode==='empty-targets'||mode==='empty-resources'||mode==='missing-merge-resources'||c.integrated&&mode==='integration-pass'||!c.integrated&&mode.startsWith('integration'))return {verdict:'PASS',findings:[],blockers:[],evidence:['independent checker ran full harness'],commits:['bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb']};
 const e={base:{snapshot:c.base,...observation(false)},targetClasses:['decode'],rounds:c.patches.map(x=>({...x,...observation(!c.integrated)})),...(c.integrated?{integrated:{...c.integrated,...observation(true)}}:{})};
 const finding={finding:'shared writer race reproduced',severity:'HIGH',defectClass:'decode',invariant:'one writer owns the store',classRewire:'bound writer concurrency',measurement:'python attack.py reached writer collision',origin:c.integrated?'cross-slice':'fix-introduced',...(c.integrated?{}:{introducedIn:1}),originEvidence:e};
 if(mode==='missing-origin')delete finding.origin;
 if(mode==='missing-evidence')delete finding.originEvidence;
 if(mode==='wrong-origin')finding.origin='pre-existing';
 if(mode==='future-round')finding.introducedIn=99;
 if(mode==='missing-round')e.rounds=[];
 if(mode==='wrong-patch')e.rounds[0].patch='/old-round/patch.diff';
 if(mode==='wrong-base')e.base.snapshot='wrong';
 if(mode==='changed-case')e.rounds[0].command='different attack';
 if(mode==='bad-control')e.base.controlPassed=false;
 if(mode==='checker-alternate-targets'){e.targetClasses=[];e.base.reproduces=true;finding.origin='pre-existing';delete finding.introducedIn;}
 return {verdict:'FAIL',findings:[finding],blockers:[],evidence:['independent replay completed']};
};
'''.replace('MODE', json.dumps(mode))
        with tempfile.TemporaryDirectory() as tmp:
            script, out = Path(tmp) / 'run.mjs', Path(tmp) / 'out.json'
            script.write_text(prefix + '\n(async()=>{\n' + js + '\n})().then(r=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({r,calls}))).catch(e=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({error:String(e)})));')
            proc = subprocess.run(['node', str(script)], text=True, capture_output=True, timeout=20)
            self.assertTrue(out.exists(), proc.stderr)
            data = json.loads(out.read_text())
            self.assertNotIn('error', data)
            return data

    def test_validated_origin_tallies_are_live_and_preserved_per_round(self):
        data = self.run_graph()
        self.assertEqual(data['r']['outcome'], 'INCOMPLETE')
        rows = data['r']['fixOrigins']
        self.assertEqual([r['round'] for r in rows], [1, 2])
        self.assertEqual([r['origins']['fix-introduced'] for r in rows], [1, 1])
        self.assertEqual(rows[0]['findings'][0]['originEvidence']['base']['snapshot'], 'a' * 40)
        self.assertEqual(rows[0]['context']['patches'][0]['round'], 1)
        notes = [r['data'] for r in data['r']['provenance'] if r.get('data', {}).get('origins')]
        self.assertEqual([r['origins']['fix-introduced'] for r in notes], [1, 1])
        self.assertIn('registry_builder', next(c['p'] for c in data['calls'] if c['label'].startswith('check:')))
        reports, findings = METRICS.fold_fix_origins([('run', {'summary': data['r']})])
        self.assertFalse(findings)
        self.assertEqual(reports[0]['fixIntroducedShare'], 1)
        self.assertEqual(reports[0]['findings'], 2)
        rendered = METRICS.render_fix_origins(reports)
        self.assertIn('fix-introduced 2/2 (100%)', rendered)
        self.assertIn('round 1', rendered); self.assertIn('round 2', rendered)

    def test_missing_resources_halt_before_checker_even_without_schema(self):
        for mode in ('missing-resources', 'bad-resources'):
            data = self.run_graph(mode)
            self.assertEqual(data['r']['outcome'], 'HALTED')
            self.assertFalse(any(c['label'].startswith('check:') for c in data['calls']))
        self.assertEqual(self.run_graph('empty-resources')['r']['outcome'], 'COMPLETE')
        self.assertEqual(self.run_graph('missing-merge-resources', True)['r']['outcome'], 'HALTED')

    def test_caller_targets_are_required_before_mutation_and_cannot_be_reselected(self):
        for mode in ('missing-targets', 'null-targets', 'bad-targets', 'blank-targets', 'duplicate-targets'):
            with self.subTest(mode=mode):
                data = self.run_graph(mode)
                self.assertEqual(data['r']['outcome'], 'INCOMPLETE')
                self.assertEqual(data['calls'], [])
                self.assertEqual(data['r']['fixOrigins'], [])
        self.assertEqual(self.run_graph('empty-targets')['r']['outcome'], 'COMPLETE')
        data = self.run_graph('checker-alternate-targets')
        self.assertEqual(data['r']['outcome'], 'INCOMPLETE')
        self.assertEqual(data['r']['fixOrigins'], [])
        self.assertTrue(any('differ from the slice targets' in p.get('detail', '') for p in data['r']['provenance']))

    def test_runtime_guard_refuses_missing_context_targets_even_for_pass(self):
        source = (SCRIPTS / 'repair-origins.js').read_text()
        program = source + '''
const check={verdict:'PASS',findings:[],blockers:[],evidence:['independent check completed']};
const patch={item:'parser',round:1,patch:'patch.diff'};
const base={base:'base',round:1,patches:[patch],current:patch};
const bad=[base,{...base,targetClasses:null},{...base,targetClasses:'decode'},{...base,targetClasses:['decode','decode']}];
if(bad.some(c=>!repairCheckOrigins(check,c).issue)) process.exit(1);
if(repairCheckOrigins(check,{...base,targetClasses:[]}).issue) process.exit(2);
'''
        proc = subprocess.run(['node', '-e', program], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unproved_origin_identity_rounds_and_controls_refuse_classification(self):
        for mode in ('missing-origin', 'missing-evidence', 'wrong-origin', 'future-round', 'missing-round', 'wrong-patch', 'wrong-base', 'changed-case', 'bad-control'):
            with self.subTest(mode=mode):
                data = self.run_graph(mode)
                self.assertEqual(data['r']['outcome'], 'INCOMPLETE')
                self.assertTrue(any(p['status'] == 'ORIGIN-UNPROVEN' for p in data['r']['provenance']))
                self.assertEqual(data['r']['fixOrigins'], [])

    def test_pass_and_integration_rounds_have_zero_or_cross_slice_tallies(self):
        passed = self.run_graph('pass')['r']
        self.assertEqual(passed['outcome'], 'COMPLETE')
        self.assertEqual(passed['fixOrigins'][0]['verdict'], 'PASS')
        self.assertEqual(passed['fixOrigins'][0]['check']['verdict'], 'PASS')
        self.assertEqual(passed['fixOrigins'][0]['blockers'], [])
        self.assertEqual(passed['fixOrigins'][0]['commits'], ['bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'])
        self.assertEqual(sum(passed['fixOrigins'][0]['origins'].values()), 0)
        integrated = self.run_graph('integration-cross', True)['r']
        records = [r for r in integrated['fixOrigins'] if r.get('integration')]
        self.assertEqual([r['origins']['cross-slice'] for r in records], [1, 1])
        self.assertTrue(all(not r['commits'] for r in records))

    def test_schema_requires_origins_introducing_round_and_resource_arrays(self):
        finding = CONTRACTS['FixCheckV1']['properties']['findings']['items']
        self.assertIn('origin', finding['required']); self.assertIn('originEvidence', finding['required'])
        self.assertTrue(any(rule.get('then', {}).get('required') == ['introducedIn'] for rule in finding['allOf']))
        self.assertIn('sharedResources', CONTRACTS['SliceV1']['required'])
        self.assertEqual(set(CONTRACTS['SliceV1']['properties']['sharedResources']['items']['required']), {'resource', 'writers', 'readers'})

    def test_schema_executions_require_origin_evidence_round_and_resource_shapes(self):
        data = self.run_graph()['r']['results']
        check = data['check'][0]
        self.assertTrue(schema_valid(check, CONTRACTS['FixCheckV1']))
        for mutate in [lambda f: f.pop('origin'), lambda f: f.pop('originEvidence'),
                       lambda f: f.pop('introducedIn'), lambda f: f.update(introducedIn=True),
                       lambda f: f['originEvidence']['base'].update(controlPassed=False)]:
            bad = copy.deepcopy(check)
            mutate(bad['findings'][0])
            self.assertFalse(schema_valid(bad, CONTRACTS['FixCheckV1']))
        fixed = data['fix'][0]
        self.assertTrue(schema_valid(fixed, CONTRACTS['SliceV1']))
        fixed.pop('sharedResources')
        self.assertFalse(schema_valid(fixed, CONTRACTS['SliceV1']))

    def test_metrics_cli_prints_per_round_and_run_shares(self):
        summary = self.run_graph()['r']
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / '.claude' / 'fluxpoint' / 'runs'
            runs.mkdir(parents=True)
            (runs / 'origin.json').write_text(json.dumps({'summary': summary, 'outcome': summary['outcome']}))
            result = subprocess.run([sys.executable, str(SCRIPTS / 'metrics.py'), '--root', tmp], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('repair origins: run origin: fix-introduced 2/2 (100%)', result.stdout)
            self.assertIn('parser round 1', result.stdout)
            result = subprocess.run([sys.executable, str(SCRIPTS / 'metrics.py'), '--root', tmp, '--json'], text=True, capture_output=True)
            self.assertEqual(json.loads(result.stdout)['fixOrigins'][0]['fixIntroducedShare'], 1)

    def test_malformed_metric_origins_are_named_and_excluded(self):
        row = {'node': 'check', 'item': 'parser', 'round': 1, 'origins': dict.fromkeys(METRICS.FIX_ORIGIN_NAMES, 0)}
        row['origins']['fix-introduced'] = True
        reports, findings = METRICS.fold_fix_origins([('run', {'summary': {'fixOrigins': [row]}})])
        self.assertIn('nonnegative integers', findings[0])
        self.assertEqual(reports[0]['invalidRounds'], 1)
        row['origins']['fix-introduced'] = 100
        reports, findings = METRICS.fold_fix_origins([('run', {'summary': {'fixOrigins': [row]}})])
        self.assertIn('preserve the checked observations', findings[0])
        self.assertEqual(reports[0]['findings'], 0)
        self.assertEqual(reports[0]['invalidRounds'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
