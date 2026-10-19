#!/usr/bin/env python3
"""Real patch reads/archives plus schema-skipping repair PASS regressions."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from repair_diff import manifest, AXES, coverage_issue
import repair_checks


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


CG = module('proof_compiler', SCRIPTS / 'compile-graph.py')
PROOF = module('proof_helper', SCRIPTS / 'repair-proof.py')
CONTRACTS = CG.load_contracts(str(PLUGIN / 'contracts'))


class RepairProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Repair Proof Test')
        self.git('config', 'user.email', 'proof@example.invalid')
        (self.root / 'state.py').write_text('flag = False\n', encoding='utf-8')
        self.git('add', 'state.py')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.prepare('flag = True\n')
        self.ir = {'version': 1, 'campaign': 'repair proof', 'treeGuard': False,
                   'budget': {'maxNodes': 200, 'cacheTtl': '1h'},
                   'lists': {'slices': [{'key': 'slice', 'targetClasses': ['decode']}]},
                   'nodes': [
                       {'id': 'fix', 'foreach': 'slices', 'mutates': True, 'contract': 'SliceV1',
                        'prompt': 'Fix ORIGINAL_SECRET_REASON {{roundDir}} {{lastCheck}}'},
                       {'id': 'check', 'foreach': 'slices', 'after': 'fix', 'verifies': 'fix',
                        'independent': True, 'isolation': 'measure', 'contract': 'FixCheckV1',
                        'prompt': 'Check CHECKER_SECRET_REASON {{prev}}', 'verifyOver': 'findings',
                        'repeat': {'until': "verdict == 'PASS'", 'maxRounds': 1, 'classBy': ['defectClass'], 'reframeAfter': 2}}]}

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    def prepare(self, content):
        (self.root / 'state.py').write_text(content, encoding='utf-8')
        self.git('add', 'state.py')
        self.git('commit', '-qm', 'repair')
        self.head = self.git('rev-parse', 'HEAD')
        self.data = subprocess.run(['git', '-C', str(self.root), 'diff', '--binary', self.base, self.head],
                                   capture_output=True, check=True).stdout
        self.patches = []
        for key in ('slice', '_integration'):
            for number in (1, 2, 3):
                path = self.root / '.claude/fluxpoint/rounds/launch' / key / ('r' + str(number)) / 'patch.diff'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.data)
                self.patches.append(path)
        self.series = self.root / 'production-series.json'
        self.series.write_text('[0,22,41,187,230]\n', encoding='utf-8')

    def run_graph(self, mode='pass', integration=False, rounds=1, nonce='1' * 64):
        ir = copy.deepcopy(self.ir)
        ir['nodes'][1]['repeat']['maxRounds'] = rounds
        if integration:
            ir['nodes'][1]['repeat']['integrate'] = ['merge', 'review']
            ir['nodes'] += [
                {'id': 'merge', 'after': 'check', 'mutates': True, 'contract': 'SliceV1', 'prompt': 'merge {{prev}}'},
                {'id': 'review', 'after': 'merge', 'independent': True, 'verifies': 'merge',
                 'isolation': 'measure', 'contract': 'FixCheckV1', 'prompt': 'review {{prev}}', 'haltWhen': "verdict != 'PASS'"}]
        js = CG.emit(ir, CONTRACTS).replace('export const meta', 'const meta')
        prefix = r'''
import {writeFileSync,readFileSync,appendFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const args=ARGS, mode=MODE, calls=[];
const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};
const parallel=async xs=>Promise.all(xs.map(f=>f()));
const obs={command:'python attack.py',result:'exit 0: defended'},ctl={command:'python valid.py',result:'exit 0: valid control'};
const slice={branch:'repair',diffSummary:'complete patch against pinned launch base',testsAdded:['test_attack'],harnessCommand:'pytest',harnessExit:0,evidence:['checked original attack and valid control'],sharedResources:[],siblings:[]};
const axes=['field','encoding','caller','network','role','path'];
const sibling=(finding,axis,surface='checker-'+axis)=>({finding,invariant:'authoritative invariant',axis,surface,attack:obs,control:ctl,outcome:'DEFENDED',reason:'independent attack and valid control executed'});
const executeProof=(p,harm=false)=>{
 const s=JSON.parse(p.split('SNAPSHOT IDENTITY: ')[1].split('\n')[0]);
 return JSON.parse(execFileSync(PYTHON_EXEC,[HELPER_SCRIPT,'execute','--snapshot',s.snapshot,'--token',s.token,'--control',CONTROL_COMMAND,'--attack',harm?HARM_COMMAND:ATTACK_COMMAND,'--series',SERIES],{encoding:'utf8'}));
};
const agent=async(p,o)=>{const label=String(o.label);calls.push({label,p});
 if(label.startsWith('repair-witness:')){
  if(mode==='missing-witness'&&label.includes(':read:'))return null;
  if(mode==='patch-drift'&&label.includes(':after-review'))appendFileSync(PATCH_PATH,'\n');
  const command=p.split('COMMAND: ')[1].split('\n')[0];
  try {return JSON.parse(execFileSync(BASH_EXEC,['-c',command],{encoding:'utf8'}));}
  catch(e){try{return JSON.parse(e.stdout.toString('utf8'))}catch(_){return {status:'INCOMPLETE',issue:String(e)}}}
 }
 if(label.startsWith('fix:')||label.startsWith('merge:')){
  const r=structuredClone(slice);
  if(mode==='packet-conflict-fix'&&label.startsWith('fix:'))r.packetConflict=null;
  if(mode==='packet-conflict-merge'&&label.startsWith('merge:'))r.packetConflict={checks:['locked'],nodes:['fix'],counterexample:'counterexample executed'};
  if(mode==='fixer-unrerun'||mode==='fixer-only')r.siblings=axes.map(axis=>sibling('decode',axis,'fixer-'+axis));
  return r;
 }
 if(label.startsWith('check:')||label.startsWith('review:')){
  const c=JSON.parse(p.split('ORIGIN REPLAY CONTEXT: ')[1].split('\n')[0]);
  const m=JSON.parse(p.split('HARNESS PATCH MANIFEST (derived from raw patch bytes, never your own inventory): ')[1].split('\n')[0]);
  if(mode==='checker-extra-harm')executeProof(p,true);
  const execution=mode==='no-checker-execution'?{attack:obs,control:ctl,metrics:{durationSeconds:86400,falsePositives:0,maxPerDay:1},seriesPath:SERIES,seriesSha256:SERIES_SHA}:executeProof(p,mode==='checker-reproduces');
  const r={verdict:'PASS',findings:[],blockers:[],evidence:['independent checker executed original case, control and full suite'],commits:[HEAD],targetClasses:c.targetClasses,
   siblings:c.targetClasses.flatMap(finding=>axes.map(axis=>({...sibling(finding,axis),attack:execution.attack,control:execution.control}))),
   diffCoverage:{patchSha256:m.patchSha256,hunks:m.hunks.map(h=>({id:h.id,surface:h.surfaces[0],attack:execution.attack,control:execution.control})),operational:{latency:{...execution.attack,deadlineMs:10000,observedMs:execution.attack.elapsedMs,pool:'provider-pool'},alarmRate:{...execution.attack,seriesPath:execution.seriesPath,seriesSha256:execution.seriesSha256,...execution.metrics}}}};
  if(mode==='missing-siblings')delete r.siblings;
  if(mode==='empty-siblings')r.siblings=[];
  if(mode==='missing-axis')r.siblings.pop();
  if(mode==='wrong-targets')r.targetClasses=[];
  if(mode==='uncited-na')r.siblings[0]={...r.siblings[0],outcome:'NOT-APPLICABLE',reason:'not applicable'};
  if(mode==='fixer-only')r.siblings=axes.map(axis=>sibling('decode',axis,'fixer-'+axis));
  if(mode==='wrong-sha')r.diffCoverage.patchSha256='0'.repeat(64);
  if(mode==='missing-hunk')r.diffCoverage.hunks=[];
  if(mode==='extra-hunk')r.diffCoverage.hunks.push({id:'invented',surface:'other',attack:obs,control:ctl});
  if(mode==='missing-control')delete r.diffCoverage.hunks[0].control;
  if(mode==='no-deadline')delete r.diffCoverage.operational.latency;
  if(mode==='late')r.diffCoverage.operational.latency.observedMs=10001;
  if(mode==='no-series')delete r.diffCoverage.operational.alarmRate;
  if(mode==='false-alarms')r.diffCoverage.operational.alarmRate.falsePositives=2;
  if(mode==='checker-forged-row')r.siblings[0].attack={command:'unexecuted case',result:'claimed defended'};
  if(mode==='checker-latency-fiction')r.diffCoverage.operational.latency.observedMs+=1;
  if(mode==='checker-series-fiction')r.diffCoverage.operational.alarmRate.falsePositives=1;
  return r;
 }
 if(label.includes(':attack:')){
  const m=JSON.parse(p.split('PINNED PATCH SUBJECT: ')[1].split('\n')[0]);
  const c=JSON.parse(p.split('ORIGIN REPLAY CONTEXT: ')[1].split('\n')[0]);
  const r={base:c.base,patchSha256:m.patchSha256,completed:true,evidence:['executed independent coverage attacks with valid controls'],blockers:[],findings:[]};
  if(mode==='dead-attacker'&&label.endsWith(':state'))return null;
  if(mode==='dead-skeptic'&&label.endsWith(':coverage-skeptic'))return null;
  if(mode!=='unexecuted-attacker')executeProof(p,mode==='attacker-drops-harm'||mode==='claim-dropped-extra-harm'&&label.endsWith(':authority'));
  if(mode==='attacker-other-sha')r.patchSha256='0'.repeat(64);
  if(mode.startsWith('claim-')&&label.endsWith(':authority')){
   const observed=reproduces=>({command:'python sibling_attack.py',result:reproduces?'exit 1: sibling harm':'exit 0: defended',controlCommand:'python valid.py',controlResult:'exit 0: valid',controlPassed:true,reproduces});
   r.findings=[{finding:'unchecked sibling field in repaired invariant',severity:'HIGH',defectClass:'decode',invariant:'all sibling fields are guarded',classRewire:'guard the class at the shared boundary',measurement:'sibling_attack.py executed: harm observed',origin:'fix-introduced',introducedIn:1,
    originEvidence:{base:{snapshot:c.base,...observed(false)},targetClasses:c.targetClasses,rounds:c.patches.map(x=>({...x,...observed(!c.integrated)})),...(c.integrated?{integrated:{...c.integrated,...observed(true)}}:{})}}];
  }
  return r;
 }
 if(label.includes(':refute:')){
  if(mode==='claim-dead-refuter'&&label.endsWith(':scope'))return null;
  if(mode==='claim-harm-then-defended')executeProof(p,true);
  const measured=executeProof(p,mode==='claim-harm-refuted');
  return {resolution:mode==='claim-refuted'||mode==='claim-fake-evidence'||mode==='claim-harm-refuted'||mode==='claim-harm-then-defended'?'REFUTED':mode==='claim-unresolved'?'UNRESOLVED':'CONFIRMED',attack:mode==='claim-fake-evidence'?obs:measured.attack,control:measured.control,contradiction:'executed case contradicts the claim at the intended boundary'};
 }
 throw Error('unexpected call '+label);
};
'''
        args = {'_base': {'sha': self.base, 'porcelain': ''}, '_launch': {'root': str(self.root), 'nonce': 'launch'},
                '_replayLaunchNonce': nonce, 'findings': ['Original targeted finding']}
        values = {'ARGS': args, 'MODE': mode, 'PATCH_PATH': str(self.patches[0]), 'HEAD': self.head,
                  'BASH_EXEC': os.environ.get('FPL_BASH') or ('C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else 'bash'),
                  'SERIES_SHA': __import__('hashlib').sha256(self.series.read_bytes()).hexdigest(), 'SERIES': str(self.series),
                  'PYTHON_EXEC': sys.executable, 'HELPER_SCRIPT': str(SCRIPTS / 'repair-proof.py'),
                  'CONTROL_COMMAND': subprocess.list2cmdline([sys.executable, '-c', 'print("valid control")']) if os.name == 'nt' else shlex.join([sys.executable, '-c', 'print("valid control")']),
                  'HARM_COMMAND': subprocess.list2cmdline([sys.executable, '-c', 'import sys; print(\'{"durationSeconds":86400,"falsePositives":0,"maxPerDay":1}\'); sys.exit(1)']) if os.name == 'nt' else shlex.join([sys.executable, '-c', 'import sys; print(\'{"durationSeconds":86400,"falsePositives":0,"maxPerDay":1}\'); sys.exit(1)']),
                  'ATTACK_COMMAND': subprocess.list2cmdline([sys.executable, '-c', 'print(\'{"durationSeconds":86400,"falsePositives":0,"maxPerDay":1}\')']) if os.name == 'nt' else shlex.join([sys.executable, '-c', 'print(\'{"durationSeconds":86400,"falsePositives":0,"maxPerDay":1}\')'])}
        for key, value in values.items():
            prefix = prefix.replace(key, json.dumps(value))
        script, out = self.root / 'run.mjs', self.root / 'out.json'
        script.write_text(prefix + '\n(async()=>{\n' + js + '\n})().then(r=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({r,calls}))).catch(e=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({error:String(e),stack:e.stack,calls})));', encoding='utf-8')
        p = subprocess.run(['node', str(script)], capture_output=True, text=True, encoding='utf-8', timeout=90,
                           env=dict(os.environ, FPL_PY=sys.executable))
        self.assertTrue(out.exists(), p.stderr)
        result = json.loads(out.read_text(encoding='utf-8'))
        if nonce:
            self.assertNotIn('error', result, result.get('stack'))
        if nonce and mode in ('pass', 'claim-refuted') and result.get('r', {}).get('outcome') != 'COMPLETE':
            print('Unexpected repair proof outcome: ' + json.dumps({
                'mode': mode, 'outcome': result.get('r', {}).get('outcome'),
                'provenance': result.get('r', {}).get('provenance', []),
            }), file=sys.stderr)
        return result

    def test_clean_pass_spawns_independent_diff_attack_and_live_patch_receipt(self):
        result = self.run_graph()
        self.assertEqual(result['r']['outcome'], 'COMPLETE')
        review = result['r']['repairDiffReviews'][0]
        self.assertTrue(review['clean'])
        self.assertEqual(review['base'], self.base)
        self.assertEqual(review['patchSha256'], manifest(self.data)['patchSha256'])
        self.assertIn('coverage-skeptic', [a['lens'] for a in review['attackers']])
        self.assertEqual(review['settled'], ['Original targeted finding'])
        self.assertTrue(all(r['status'] == 'CLEANED' for r in review['snapshots']))
        for call in result['calls']:
            if ':attack:' in call['label'] or ':refute:' in call['label']:
                self.assertNotIn('ORIGINAL_SECRET_REASON', call['p'])
                self.assertNotIn('CHECKER_SECRET_REASON', call['p'])
        checked, problems = repair_checks.checked(result['r'], repair_checks.descriptors(self.ir))
        self.assertEqual((checked, problems), ([self.head], []))

    def test_sibling_proof_failures_never_reach_diff_attack(self):
        for mode in ('missing-siblings', 'empty-siblings', 'missing-axis', 'wrong-targets', 'uncited-na', 'fixer-unrerun', 'fixer-only'):
            with self.subTest(mode=mode):
                result = self.run_graph(mode)
                self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
                self.assertTrue(any(p['status'] == 'SIBLINGS-UNPROVEN' for p in result['r']['provenance']))
                self.assertFalse(any(':attack:' in c['label'] for c in result['calls']))

    def test_sha_hunk_and_executed_control_failures_block_pass(self):
        for mode in ('wrong-sha', 'missing-hunk', 'extra-hunk', 'missing-control', 'missing-witness'):
            with self.subTest(mode=mode):
                result = self.run_graph(mode)
                self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
                self.assertFalse(result['r']['fixOrigins'])

    def test_checker_rows_and_operational_numbers_need_actual_checker_executions(self):
        for mode in ('no-checker-execution', 'checker-forged-row', 'checker-latency-fiction', 'checker-series-fiction', 'checker-reproduces', 'checker-extra-harm'):
            with self.subTest(mode=mode):
                result = self.run_graph(mode)
                self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
                self.assertTrue(any(p['status'] == 'CHECKER-UNPROVEN' for p in result['r']['provenance']))
                self.assertFalse(any(':attack:' in c['label'] for c in result['calls']))

    def test_operational_rows_are_mandatory_for_new_deadlines_and_alarms(self):
        self.prepare('flag = True\n# outbound provider deadline timer alarm paging\n')
        self.assertEqual(self.run_graph()['r']['outcome'], 'COMPLETE')
        for mode in ('no-deadline', 'late', 'no-series', 'false-alarms'):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_graph(mode)['r']['outcome'], 'INCOMPLETE')

    def test_missing_attackers_or_no_claims_skeptic_cannot_pass(self):
        for mode in ('dead-attacker', 'attacker-other-sha', 'dead-skeptic', 'unexecuted-attacker', 'attacker-drops-harm', 'claim-dropped-extra-harm'):
            with self.subTest(mode=mode):
                result = self.run_graph(mode)
                self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
                self.assertFalse(result['r']['repairDiffReviews'][0]['clean'])

    def test_only_three_executed_refutations_clear_a_claim(self):
        defended = self.run_graph('claim-refuted')
        self.assertEqual(defended['r']['outcome'], 'COMPLETE')
        self.assertEqual(len(defended['r']['repairDiffReviews'][0]['refuters']), 3)
        for mode in ('claim-confirmed', 'claim-unresolved', 'claim-dead-refuter', 'claim-fake-evidence', 'claim-harm-refuted', 'claim-harm-then-defended'):
            with self.subTest(mode=mode):
                result = self.run_graph(mode)
                self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
                self.assertEqual(result['r']['fixOrigins'][0]['verdict'], 'FAIL')
                self.assertEqual(result['r']['fixOrigins'][0]['origins']['fix-introduced'], 1)
                self.assertEqual(repair_checks.checked(result['r'], repair_checks.descriptors(self.ir))[0], [])

    def test_confirmed_sibling_feeds_class_pressure_and_next_fixer(self):
        result = self.run_graph('claim-confirmed', rounds=3)
        self.assertEqual(result['r']['outcome'], 'REFRAME-REQUIRED')
        self.assertEqual(len(result['r']['fixOrigins']), 2)
        fix_round2 = next(c for c in result['calls'] if c['label'] == 'fix:slice:r2')
        self.assertIn('unchecked sibling field', fix_round2['p'])
        self.assertTrue(any(p['status'] == 'RECURRING-CLASS' for p in result['r']['provenance']))

    def test_integration_runs_its_own_proof_tier(self):
        result = self.run_graph(integration=True)
        self.assertEqual(result['r']['outcome'], 'COMPLETE')
        self.assertEqual([r['item'] for r in result['r']['repairDiffReviews']], ['slice', '_integration'])
        self.assertTrue(result['r']['fixOrigins'][-1]['integration'])

    def test_deleted_guard_still_selects_its_surface_attacker(self):
        (self.root / 'state.py').write_text('if valid:\n    flag = False\n')
        self.git('add', 'state.py')
        self.git('commit', '-qm', 'guarded new base')
        self.base = self.git('rev-parse', 'HEAD')
        self.prepare('')
        measured = manifest(self.data)
        self.assertEqual(measured['hunks'], [])
        self.assertIn('guards', measured['changedSurfaces'])
        result = self.run_graph()
        self.assertEqual(result['r']['outcome'], 'COMPLETE')
        self.assertIn('authority', [r['lens'] for r in result['r']['repairDiffReviews'][0]['attackers']])

    def test_builtin_diff_tier_preserves_checker_effect_and_generic_panel_prohibitions(self):
        self.assertFalse(CG.validate(self.ir, CONTRACTS))
        for field, value in [('haltWhen', "verdict != 'PASS'"), ('irreversible', True), ('verify', 'panel:3')]:
            ir = copy.deepcopy(self.ir)
            ir['nodes'][1][field] = value
            self.assertTrue(any('repair checker cannot combine' in issue for issue in CG.validate(ir, CONTRACTS)))

    def test_packet_conflicts_halt_before_checking_or_integrating(self):
        fixed = self.run_graph('packet-conflict-fix')
        self.assertEqual(fixed['r']['outcome'], 'HALTED')
        self.assertEqual([c['label'] for c in fixed['calls']], ['fix:slice:r1'])
        merged = self.run_graph('packet-conflict-merge', integration=True)
        self.assertEqual(merged['r']['outcome'], 'HALTED')
        self.assertFalse(any(c['label'].startswith('review:') for c in merged['calls']))

    def test_live_post_review_read_detects_patch_drift(self):
        result = self.run_graph('patch-drift')
        self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
        self.assertFalse(result['r']['repairDiffReviews'][0]['clean'])

    def test_missing_fresh_nonce_refuses_and_every_new_launch_reads_again(self):
        refused = self.run_graph(nonce='')
        if 'error' in refused:
            self.assertIn('_replayLaunchNonce', refused['error'])
            self.assertEqual(refused['calls'], [])
        else:
            self.assertEqual(refused['r']['outcome'], 'INCOMPLETE')
        first = self.run_graph(nonce='1' * 64)
        second = self.run_graph(nonce='2' * 64)
        self.assertEqual(second['r']['outcome'], 'COMPLETE')
        for data, nonce in ((first, '1' * 64), (second, '2' * 64)):
            witnesses = [c for c in data['calls'] if c['label'].startswith('repair-witness:read:')]
            self.assertEqual(len(witnesses), 2)
            self.assertTrue(all(nonce in c['p'] for c in witnesses))

    def test_recorder_reopens_missing_stale_or_incomplete_diff_receipts(self):
        summary = self.run_graph()['r']
        frozen = repair_checks.descriptors(self.ir)
        artifact = dict(checked=[self.head], runId='run-proof', graph='WORK.md', summary=summary, repairCheckers=frozen)
        self.assertEqual(repair_checks.corroborated(artifact), [self.head])
        for mode in ('missing', 'other-base', 'other-round', 'no-skeptic', 'open-finding', 'no-cleanup', 'wrong-commits'):
            value = copy.deepcopy(summary)
            row = value['fixOrigins'][0]
            proof = row['diffAttack']
            if mode == 'missing':
                row.pop('diffAttack')
            elif mode == 'other-base':
                proof['base'] = '0' * 40
            elif mode == 'other-round':
                proof['round'] = 99
            elif mode == 'no-skeptic':
                proof['attackers'] = [a for a in proof['attackers'] if a['lens'] != 'coverage-skeptic']
            elif mode == 'open-finding':
                proof['findings'] = [{'votes': []}]
            elif mode == 'no-cleanup':
                proof['snapshots'] = []
            elif mode == 'wrong-commits':
                row['commits'] = row['check']['commits'] = [self.base]
            if mode != 'missing':
                row['check']['diffAttack'] = copy.deepcopy(proof)
                value['repairDiffReviews'] = [copy.deepcopy(proof)]
            with self.subTest(mode=mode):
                checked, problems = repair_checks.checked(value, frozen)
                self.assertEqual(checked, [])
                self.assertTrue(problems)
        self.patches[0].write_bytes(self.data + b'\n')
        self.assertEqual(repair_checks.checked(summary, frozen)[0], [])
        with self.assertRaises(ValueError):
            repair_checks.corroborated(artifact)

    def test_js_hash_and_manifest_match_exact_host_bytes(self):
        import base64
        import hashlib
        vectors = [b'', b'abc', bytes(range(256)), self.data,
                   'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+é guard\n'.encode()]
        script = (SCRIPTS / 'repair-proof.js').read_text(encoding='utf-8') + '\n' + 'const vectors=' + json.dumps([base64.b64encode(v).decode() for v in vectors]) + ';\nconsole.log(JSON.stringify(vectors.map(v=>repairSHA256(repairBytes(v)))));'
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout), [hashlib.sha256(v).hexdigest() for v in vectors])
        script = (SCRIPTS / 'repair-proof.js').read_text(encoding='utf-8') + '\nconsole.log(JSON.stringify(repairManifest(repairBytes(' + json.dumps(base64.b64encode(vectors[-1]).decode()) + '))));'
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout), manifest(vectors[-1]))
        # Execute the real witness guard with native pathlib-style Windows
        # responses. Different roots and POSIX literal backslashes stay distinct.
        cases = [
            ('drive-mixed', r'C:\repo/.claude/patch.diff', r'C:\repo\.claude\patch.diff', True),
            ('drive-letter-case', r'c:/repo/patch.diff', r'C:\repo\patch.diff', True),
            ('different-drive', r'C:\repo/patch.diff', r'D:\repo\patch.diff', False),
            ('unc-native', '//server/share/patch.diff', r'\\server\share\patch.diff', True),
            ('different-share', '//server/share/patch.diff', r'\\server\other\patch.diff', False),
            ('different-server', '//server/share/patch.diff', r'\\elsewhere\share\patch.diff', False),
            ('unc-local-root', '//server/share/patch.diff', '/server/share/patch.diff', False),
            ('posix-literal-backslash', r'/repo/literal\name/patch.diff', '/repo/literal/name/patch.diff', False),
        ]
        setup = "const A={_replayLaunchNonce:'" + '1' * 64 + "'},REPAIR_LAUNCH={root:'ignored'},REPAIR_PROOF_HELPER='ignored';let reply;const repairText=v=>typeof v==='string'&&!!v.trim();const agent=async()=>reply;\n"
        script = setup + (SCRIPTS / 'repair-proof.js').read_text(encoding='utf-8') + '\nconst cases=' + json.dumps(cases) + ';\n' + '''
(async()=>{const results=[];for(const [name,expected,actual]of cases){
 const context={base:'a'.repeat(40),item:'slice',round:1,current:{patch:expected}};
 reply={status:'READ',base:context.base,item:context.item,round:1,patch:actual,rawPatch:''};
 const result=await repairReadPatch(context);results.push([name,!result.issue]);
}console.log(JSON.stringify(results));})();'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout), [[name, expected] for name, _, _, expected in cases])

    def test_helper_materializes_exact_bytes_and_confirms_owned_cleanup(self):
        patch = str(self.patches[0])
        measured = manifest(self.data)
        with self.assertRaisesRegex(ValueError, 'patch changed'):
            PROOF.materialize(self.root, self.base, patch, '0' * 64, 'slice', 1)
        s = PROOF.materialize(self.root, self.base, patch, measured['patchSha256'], 'slice', 1)
        self.assertEqual((Path(s['snapshot']) / 'state.py').read_text(), 'flag = True\n')
        self.assertFalse((Path(s['snapshot']) / '.git').exists())
        code = 'import pathlib; assert pathlib.Path("state.py").read_text()=="flag = True\\n"'
        command = subprocess.list2cmdline([sys.executable, '-c', code]) if os.name == 'nt' else shlex.join([sys.executable, '-c', code])
        observed = PROOF.execute(s['snapshot'], s['token'], command, command, 10)
        self.assertFalse(observed['reproduces'])
        marker = Path(s['snapshot']).parent / 'active-command'
        marker.touch()
        with self.assertRaisesRegex(ValueError, 'undrained'):
            PROOF.cleanup(s['snapshot'], s['token'])
        marker.unlink()
        self.assertEqual(PROOF.cleanup(s['snapshot'], s['token'])['status'], 'CLEANED')
        self.assertFalse(Path(s['snapshot']).exists())

    def test_alarm_series_is_reread_and_provider_pool_cannot_be_omitted(self):
        measured = manifest(b'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+provider rate-limit alarm timer\n')
        digest = __import__('hashlib').sha256(self.series.read_bytes()).hexdigest()
        c = {'patchSha256': measured['patchSha256'], 'hunks': [{'id': measured['hunks'][0]['id'], 'surface': 'outbound-calls', 'attack': {'command': 'a', 'result': 'r'}, 'control': {'command': 'c', 'result': 'r'}}],
             'operational': {'latency': {'command': 'a', 'result': 'r', 'deadlineMs': 10, 'observedMs': 1, 'pool': 'provider-pool'},
                             'alarmRate': {'command': 'a', 'result': 'r', 'seriesPath': str(self.series), 'seriesSha256': digest, 'durationSeconds': 86400, 'falsePositives': 0, 'maxPerDay': 1}}}
        self.assertIn('sharedResources', coverage_issue(c, measured, [], True))
        resources = [{'resource': 'provider-pool'}]
        self.assertIsNone(coverage_issue(c, measured, resources, True))
        self.series.write_text('changed')
        self.assertIn('digest differs', coverage_issue(c, measured, resources, True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
