#!/usr/bin/env python3
"""Real patch reads/archives plus schema-skipping repair PASS regressions."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from process_tree import CleanupPending
from repair_diff import manifest, compact_manifest, manifest_digest, AXES, coverage_issue
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
                   'lists': {'slices': [{'key': 'slice', 'targetClasses': ['decode'], 'operationalBounds': {'deadlineMs': 10000, 'maxPerDay': 1, 'providerPools': ['provider-pool']}}]},
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
        for key in ('slice', 'other', '_integration'):
            for number in (1, 2, 3):
                path = self.root / '.claude/fluxpoint/rounds/launch' / ('1' * 64) / key / ('r' + str(number)) / 'patch.diff'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.data)
                self.patches.append(path)
        self.series = self.root / 'production-series.json'
        self.series.write_text(json.dumps({'version': 1, 'startedAt': '2026-10-27T00:00:00Z', 'endedAt': '2026-10-28T00:00:00Z', 'events': []}), encoding='utf-8')

    def run_graph(self, mode='pass', integration=False, rounds=1, nonce='1' * 64, specification=None):
        ir = copy.deepcopy(self.ir)
        ir['nodes'][1]['repeat']['maxRounds'] = rounds
        if mode == 'partial':
            ir['lists']['slices'].append({'key': 'other', 'targetClasses': ['decode']})
        if nonce and nonce != '1' * 64:
            import shutil
            source = self.root / '.claude/fluxpoint/rounds/launch' / ('1' * 64)
            target = source.parent / nonce
            if not target.exists():
                shutil.copytree(source, target)
        if integration:
            ir['nodes'][1]['repeat']['integrate'] = ['merge', 'review']
            ir['nodes'] += [
                {'id': 'merge', 'after': 'check', 'mutates': True, 'contract': 'SliceV1', 'prompt': 'merge {{prev}}'},
                {'id': 'review', 'after': 'merge', 'independent': True, 'verifies': 'merge',
                 'isolation': 'measure', 'contract': 'FixCheckV1', 'prompt': 'review {{prev}}', 'haltWhen': "verdict != 'PASS'"}]
        imports_resolved = None
        if mode in ('claim-residual', 'claim-residual-wrong-patch'):
            import decision
            fixtures = module('residual_decision_fixture', Path(__file__).with_name('specification-test.py'))
            record = fixtures.packet()['decisions'][0]['record']
            record['acceptedRepairClaims'] = [{'finding': 'unchecked sibling field in repaired invariant', 'patchSha256': manifest(self.data)['patchSha256'] if mode == 'claim-residual' else '0' * 64, 'reason': 'The operator retains this named claim as an explicitly reviewed residual.'}]
            kept = decision.keep(str(self.root), 'accept-residual', record, 'WORK.md')
            ir['imports'] = {'accept-residual': kept['recordId']}
            imports_resolved = {'accept-residual': {'record': record, 'recordId': kept['recordId'], 'runId': kept['recordId'], 'when': kept['when']}}
        js = CG.emit(ir, CONTRACTS, imports_resolved=imports_resolved, specification=specification).replace('export const meta', 'const meta')
        prefix = r'''
import {writeFileSync,readFileSync,appendFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
const args=ARGS, mode=MODE, calls=[];
const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};
const parallel=async xs=>Promise.all(xs.map(f=>f()));
const obs={command:'python attack.py',result:'exit 0: defended'},ctl={command:'python valid.py',result:'exit 0: valid control'};
const slice={patchSha256:PATCH_SHA,branch:'repair',diffSummary:'complete patch against pinned launch base',testsAdded:['test_attack'],harnessCommand:'pytest',harnessExit:0,evidence:['checked original attack and valid control'],sharedResources:[{resource:'provider-pool',writers:['caller'],readers:['checker']}],siblings:[]};
const axes=['field','encoding','caller','network','role','path'];
const sibling=(finding,axis,surface='checker-'+axis)=>({finding,invariant:'authoritative invariant',axis,surface,attack:obs,control:ctl,outcome:'DEFENDED',reason:'independent attack and valid control executed'});
const executeProof=(p,harm=false,tag='')=>{
 const s=JSON.parse(p.split('SNAPSHOT IDENTITY: ')[1].split('\n')[0]);
 return JSON.parse(execFileSync(PYTHON_EXEC,[HELPER_SCRIPT,'execute','--snapshot',s.snapshot,'--token',s.token,'--control',CONTROL_COMMAND,'--attack',(harm?HARM_COMMAND:ATTACK_COMMAND)+(tag?' # '+tag:''),'--series',SERIES],{encoding:'utf8'}));
};
const agent=async(p,o)=>{const label=String(o.label);calls.push({label,p});
 if(label.startsWith('repair-witness:')){
  if(mode==='missing-witness'&&label.includes(':read:'))return null;
  if(mode==='patch-drift'&&label.includes(':after-review'))appendFileSync(PATCH_PATH,'\n');
  const command=p.split('COMMAND: ')[1].split('\n')[0];
  try {const result=JSON.parse(execFileSync(BASH_EXEC,['-c',command],{encoding:'utf8'}));if(mode==='manifest-drift'&&label.includes(':read:'))result.manifest.hunks=[];return result;}
  catch(e){try{return JSON.parse(e.stdout.toString('utf8'))}catch(_){return {status:'INCOMPLETE',issue:String(e)}}}
 }
 if(label.startsWith('fix:')||label.startsWith('merge:')){
  const r=structuredClone(slice);
  if(mode==='context-overflow')r.diffSummary='x'.repeat(40000);
  if(mode==='fixer-digest-drift')r.patchSha256='0'.repeat(64);
  if(mode==='packet-conflict-fix'&&label.startsWith('fix:'))r.packetConflict=null;
  if(mode==='packet-conflict-merge'&&label.startsWith('merge:'))r.packetConflict={checks:['locked'],nodes:['fix'],counterexample:'counterexample executed'};
  if(mode==='fixer-unrerun'||mode==='fixer-only')r.siblings=axes.map(axis=>sibling('decode',axis,'fixer-'+axis));
  return r;
 }
 if(label.startsWith('check:')||label.startsWith('review:')){
  const c=JSON.parse(p.split('ORIGIN REPLAY CONTEXT: ')[1].split('\n')[0]);
  const m=JSON.parse(p.split('HARNESS PATCH MANIFEST (derived from raw patch bytes, never your own inventory): ')[1].split('\n')[0]);
  if(mode==='checker-extra-harm')executeProof(p,true);
  const executed=[];
  const runCase=tag=>{
   if(mode==='no-checker-execution')return {attack:{...obs,command:'python attack.py # state.py'},control:ctl,metrics:{durationSeconds:86400,falsePositives:0},seriesPath:SERIES,seriesSha256:SERIES_SHA};
   if(mode==='checker-reused-execution'&&executed.length)return executed[0];
   const result=executeProof(p,mode==='checker-reproduces',mode==='checker-reused-execution'?'shared state.py':tag);executed.push(result);return result;
  };
  const siblings=c.targetClasses.flatMap(finding=>axes.map(axis=>{const e=runCase('sibling:'+finding+':'+axis);return {...sibling(finding,axis),attack:e.attack,control:e.control}}));
  const hunks=m.hunks.map(h=>{const e=runCase('hunk:'+h.path);return {id:h.id,surface:h.surfaces[0],attack:e.attack,control:e.control}});
  const execution=executed[executed.length-1]||runCase('operational');
  const r={verdict:'PASS',findings:[],blockers:[],evidence:['independent checker executed original case, control and full suite'],commits:[HEAD],targetClasses:c.targetClasses,siblings,
   diffCoverage:{patchSha256:m.patchSha256,hunks,operational:{latency:{...execution.attack,deadlineMs:10000,observedMs:execution.attack.elapsedMs,pool:'provider-pool'},alarmRate:{...execution.attack,seriesPath:execution.seriesPath,seriesSha256:execution.seriesSha256,maxPerDay:1,...execution.metrics}}}};
  if(mode==='missing-siblings')delete r.siblings;
  if(mode==='empty-siblings')r.siblings=[];
  if(mode==='missing-axis')r.siblings.pop();
  if(mode==='wrong-targets')r.targetClasses=[];
  if(mode==='uncited-na')r.siblings[0]={...r.siblings[0],outcome:'NOT-APPLICABLE',reason:'not applicable'};
  if(mode==='fixer-only')r.siblings=axes.map(axis=>sibling('decode',axis,'fixer-'+axis));
  if(mode==='partial'&&label.includes(':other:'))r.verdict='FAIL';
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
   const observed=reproduces=>({command:mode==='claim-unrelated-reproduction'?'python unrelated_claim.py':ATTACK_COMMAND,result:reproduces?'exit 1: sibling harm':'exit 0: defended',controlCommand:CONTROL_COMMAND,controlResult:'exit 0: valid',controlPassed:true,reproduces});
   r.findings=[{finding:'unchecked sibling field in repaired invariant',severity:'HIGH',defectClass:'decode',invariant:'all sibling fields are guarded',classRewire:'guard the class at the shared boundary',measurement:'sibling_attack.py executed: harm observed',origin:'fix-introduced',introducedIn:1,
    originEvidence:{base:{snapshot:c.base,...observed(false)},targetClasses:c.targetClasses,rounds:c.patches.map(x=>({...x,...observed(!c.integrated)})),...(c.integrated?{integrated:{...c.integrated,...observed(true)}}:{})}}];
  }
  if(mode==='claim-low'&&r.findings.length)r.findings[0].severity='LOW';
  if(mode==='claim-outside'&&r.findings.length){r.findings[0].origin='pre-existing';r.findings[0].defectClass='other-class';r.findings[0].originEvidence.base.reproduces=true;}
  return r;
 }
 if(label.includes(':refute:')){
  if(mode==='claim-dead-refuter'&&label.endsWith(':scope'))return null;
  if(mode==='claim-harm-then-defended')executeProof(p,true);
  const measured=executeProof(p,mode==='claim-harm-refuted');
  return {resolution:mode==='claim-refuted'||mode==='claim-unrelated-reproduction'||mode==='claim-fake-evidence'||mode==='claim-harm-refuted'||mode==='claim-harm-then-defended'?'REFUTED':mode==='claim-unresolved'?'UNRESOLVED':'CONFIRMED',attack:mode==='claim-fake-evidence'?obs:measured.attack,control:measured.control,contradiction:'executed case contradicts the claim at the intended boundary'};
 }
 throw Error('unexpected call '+label);
};
'''
        args = {'_base': {'sha': self.base, 'porcelain': ''}, '_launch': {'root': str(self.root), 'nonce': 'launch'},
                '_replayLaunchNonce': nonce, 'findings': ['Original targeted finding']}
        values = {'ARGS': args, 'MODE': mode, 'PATCH_SHA': manifest(self.data)['patchSha256'], 'PATCH_PATH': str(self.patches[0]), 'HEAD': self.head,
                  'BASH_EXEC': os.environ.get('FPL_BASH') or ('C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else 'bash'),
                  'SERIES_SHA': __import__('hashlib').sha256(self.series.read_bytes()).hexdigest(), 'SERIES': str(self.series),
                  'PYTHON_EXEC': sys.executable, 'HELPER_SCRIPT': str(SCRIPTS / 'repair-proof.py'),
                  'CONTROL_COMMAND': shlex.join([sys.executable, '-c', 'print("valid control")']),
                  'HARM_COMMAND': shlex.join([sys.executable, '-c', 'import sys; print(\'{"durationSeconds":86400,"falsePositives":0,"maxPerDay":1}\'); sys.exit(1)']),
                  'ATTACK_COMMAND': shlex.join([sys.executable, '-c', 'print(\'{"durationSeconds":86400,"falsePositives":0,"maxPerDay":1}\')'])}
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
        result['ir'] = ir
        return result

    def test_fixed_packet_runner_binds_red_and_green_to_patched_snapshot(self):
        fixtures = module('repair_packet_fixtures', Path(__file__).with_name('specification-test.py'))
        import specification
        for expect_green in (False, True):
            with self.subTest(expect_green=expect_green):
                doc = fixtures.packet()
                doc['checks'][0]['argv'] = ['{python}', '-c', 'import pathlib; assert pathlib.Path("state.py").read_text() == "flag = True\\n"' if expect_green else 'raise SystemExit(9)']
                (self.root / 'state.py').write_text('flag = False\n')
                (self.root / specification.SPEC).write_text(json.dumps(doc))
                locked = subprocess.run([sys.executable, str(SCRIPTS / 'specification.py'), '--root', str(self.root), '--lock'], capture_output=True, text=True)
                self.assertEqual(locked.returncode, 0, locked.stderr)
                self.git('add', 'state.py', specification.SPEC, specification.LOCK)
                self.git('commit', '-qm', 'packet base')
                self.base = self.git('rev-parse', 'HEAD')
                self.prepare('flag = True\n')
                packet, identity = specification.load(self.root)
                spec = {'packet': packet, 'identity': identity, 'path': specification.SPEC}
                result = self.run_graph(mode='packet-checked', specification=spec)
                self.assertEqual(result['r']['outcome'], 'COMPLETE' if expect_green else 'INCOMPLETE')
                calls = [c for c in result['calls'] if c['label'].startswith('repair-witness:check:')]
                self.assertEqual(len(calls), 1)
                if not expect_green:
                    self.assertTrue(any(p['status'] == 'PACKET-CHECK-UNPROVEN' for p in result['r']['provenance']))
                    self.assertFalse(any(':attack:' in c['label'] for c in result['calls']))

    def test_large_patch_read_returns_compact_digest_bound_manifest(self):
        self.prepare('flag = True\n# ' + 'large patch content ' * 7000 + '\n')
        result = PROOF.read(self.root, self.base, str(self.patches[0]), 'slice', 1)
        self.assertFalse('rawPatch' in result)
        self.assertLess(len(json.dumps(result)), 3000)
        self.assertEqual(result['manifest']['patchSha256'], manifest(self.data)['patchSha256'])
        self.assertRegex(result['manifestSha256'], r'^[0-9a-f]{64}$')

    def test_snapshot_has_index_for_directory_packet_inputs(self):
        import specification
        fixtures = module('repair_directory_packet', Path(__file__).with_name('specification-test.py'))
        (self.root / 'probes').mkdir()
        (self.root / 'probes' / 'run.py').write_text('print("checked")\n')
        doc = fixtures.packet()
        doc['checks'][0]['argv'] = ['{python}', 'probes/run.py']
        doc['harnessInputs'] = ['probes']
        (self.root / '.gitignore').write_text('probes/\n')
        self.git('add', '-f', 'probes')
        (self.root / specification.SPEC).write_text(json.dumps(doc))
        (self.root / 'WORK.md').write_text('SPEC: ' + specification.SPEC + '\n```json graph-ir\n' + json.dumps(fixtures.graph()) + '\n```\n')
        result = subprocess.run([sys.executable, str(SCRIPTS / 'specification.py'), '--root', str(self.root), '--lock'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git('add', '.gitignore', specification.SPEC, specification.LOCK, 'WORK.md')
        self.git('commit', '-qm', 'packet inputs')
        self.base = self.git('rev-parse', 'HEAD')
        self.prepare('flag = False\n')
        s = PROOF.materialize(self.root, self.base, str(self.patches[0]), manifest(self.data)['patchSha256'], 'slice', 1)
        self.addCleanup(lambda: PROOF.cleanup(s['snapshot'], s['token']))
        checked = PROOF.check_packet(s['snapshot'], s['token'], specification.SPEC)
        self.assertEqual(checked['exit'], 0, checked['output'])
        compiled = subprocess.run([sys.executable, str(SCRIPTS / 'compile-graph.py'), 'WORK.md', '--check'], cwd=s['snapshot'], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)

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
            if call['label'] == 'check:slice:r1' or call['label'].startswith('repair-diff:') and ':attack:' in call['label']:
                import repair_journal
                command = call['p'].split('NATIVE ORIGIN REPLAY:', 1)[1].split('invoke ', 1)[1].split(' --root ', 1)[0]
                parsed = repair_journal._helper_call(command + ' --root ' + shlex.quote(str(self.root))
                    + ' --context saved.json --defect-class decode --control true --command false',
                    str(PLUGIN), script='repair-replay.py')
                self.assertEqual(parsed[0], 'replay')
            if ':attack:' in call['label'] or ':refute:' in call['label']:
                self.assertNotIn('ORIGINAL_SECRET_REASON', call['p'])
                self.assertNotIn('CHECKER_SECRET_REASON', call['p'])
        checked, problems = repair_checks.checked(result['r'], repair_checks.descriptors(self.ir))
        self.assertEqual((checked, problems), ([self.head], []))

    def test_helper_commands_start_an_absolute_program(self):
        # A launcher with no shell hands argv[0] to CreateProcess, which searches
        # System32 before PATH; on Windows a bare `bash` there is WSL's launcher.
        calls = self.run_graph()['calls']
        commands = {}
        for marker in ('COMMAND: ', 'cases through ', 'controls/attacks through '):
            commands[marker] = [call['p'].split(marker, 1)[1].split('\n')[0] for call in calls if marker in call['p']]
            self.assertTrue(commands[marker], marker)
            for command in commands[marker]:
                program = shlex.split(command)[0]
                self.assertTrue(os.path.isabs(program) and os.path.isfile(program), command)
        read = next(command for command in commands['COMMAND: '] if ' read ' in command)
        log = self.root / 'read.log'
        runner = [sys.executable, str(SCRIPTS / 'longrun.py'), '--root', str(self.root)]
        token = subprocess.run(runner + ['start', '--log', str(log), '--'] + shlex.split(read), cwd=self.root,
                               capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        subprocess.run(runner + ['await', token, '--timeout', '60'], cwd=self.root, capture_output=True, check=True, timeout=90)
        self.assertIn('"status": "READ"', log.read_text(encoding='utf-8', errors='replace'))

    def test_sibling_proof_failures_never_reach_diff_attack(self):
        for mode in ('missing-siblings', 'empty-siblings', 'missing-axis', 'wrong-targets', 'uncited-na', 'fixer-unrerun', 'fixer-only'):
            with self.subTest(mode=mode):
                result = self.run_graph(mode)
                self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
                self.assertTrue(any(p['status'] == 'SIBLINGS-UNPROVEN' for p in result['r']['provenance']))
                self.assertFalse(any(':attack:' in c['label'] for c in result['calls']))

    def test_sha_hunk_and_executed_control_failures_block_pass(self):
        for mode in ('wrong-sha', 'missing-hunk', 'extra-hunk', 'missing-control', 'missing-witness', 'manifest-drift'):
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
        for mode in ('claim-confirmed', 'claim-unresolved', 'claim-dead-refuter', 'claim-fake-evidence', 'claim-unrelated-reproduction', 'claim-harm-refuted', 'claim-harm-then-defended'):
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

    def test_repair_context_bound_is_priced_and_enforced_before_spawning(self):
        ir = copy.deepcopy(self.ir)
        ir['budget']['repairContextBytes'] = 4096
        priced = CG.estimate_tokens(ir, CONTRACTS)
        ir['nodes'][1]['prompt'] += ' another {{prev}}'
        twice = CG.estimate_tokens(ir, CONTRACTS)
        self.assertGreaterEqual(twice['total'] - priced['total'], 4096)
        self.assertFalse(any(token == '{{prev}}' for token, _ in twice['unpriced']))
        self.assertTrue(any('whole {{prev}}' in w for w in CG.warnings(ir)))
        result = self.run_graph('context-overflow')
        self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
        self.assertTrue(any(p['status'] == 'CONTEXT-LIMIT' for p in result['r']['provenance']))
        self.assertFalse(any(c['label'].startswith('check:') for c in result['calls']))

    def test_low_outside_scope_and_named_residual_claims_become_followups(self):
        for mode in ('claim-low', 'claim-outside', 'claim-residual'):
            with self.subTest(mode=mode):
                result = self.run_graph(mode)
                self.assertEqual(result['r']['outcome'], 'COMPLETE')
                followups = result['r']['repairDiffReviews'][0]['followups']
                self.assertEqual(len(followups), 1)
                self.assertFalse(any(':refute:' in c['label'] for c in result['calls']))
                checked, problems = repair_checks.checked(result['r'], repair_checks.descriptors(result['ir']))
                self.assertEqual((checked, problems), ([self.head], []))
        wrong = self.run_graph('claim-residual-wrong-patch')
        self.assertEqual(wrong['r']['outcome'], 'INCOMPLETE')

    def test_residual_records_cannot_be_fabricated_or_retargeted(self):
        import decision
        from repair_policy import verified_rulings
        fixtures = module('residual_validation_fixture', Path(__file__).with_name('specification-test.py'))
        record = fixtures.packet()['decisions'][0]['record']
        claim = {'finding': 'Known retained defect', 'patchSha256': manifest(self.data)['patchSha256'], 'reason': 'The operator explicitly accepts this narrowly named residual.'}
        record['acceptedRepairClaims'] = [claim]
        self.assertEqual(decision.validate(record, decision.load_schema(str(PLUGIN))), [])
        kept = decision.keep(str(self.root), 'residual', record, 'WORK.md')
        advertised = [dict(claim, decisionId='residual', recordId=kept['recordId'])]
        imports = {'residual': kept['recordId']}
        self.assertEqual(verified_rulings(self.root, imports, advertised), advertised)
        with self.assertRaisesRegex(ValueError, 'frozen decision import'):
            verified_rulings(self.root, {}, advertised)
        changed = copy.deepcopy(advertised)
        changed[0]['patchSha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'accepted claim'):
            verified_rulings(self.root, imports, changed)
        path = self.root / decision.STORE
        data = json.loads(path.read_text())
        data['record']['rationale'] += ' changed after the ruling'
        path.write_text(json.dumps(data) + '\n')
        with self.assertRaisesRegex(ValueError, 'record id'):
            verified_rulings(self.root, imports, advertised)

    def test_one_checker_execution_cannot_cover_multiple_rows(self):
        result = self.run_graph('checker-reused-execution')
        self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
        self.assertTrue(any(p['status'] == 'CHECKER-UNPROVEN' for p in result['r']['provenance']))
        self.assertFalse(any(':attack:' in c['label'] for c in result['calls']))

    def test_terminal_partial_round_skips_integration_spend(self):
        result = self.run_graph('partial', integration=True)
        self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
        self.assertFalse(any(c['label'].startswith(('merge:', 'review:')) for c in result['calls']))
        self.assertEqual(len(result['r']['fixOrigins']), 2)

    def test_fixer_digest_must_match_attempt_artifact(self):
        result = self.run_graph('fixer-digest-drift')
        self.assertEqual(result['r']['outcome'], 'INCOMPLETE')
        self.assertFalse(any(c['label'].startswith('check:') for c in result['calls']))

    def test_integration_runs_its_own_proof_tier(self):
        result = self.run_graph(integration=True)
        self.assertEqual(result['r']['outcome'], 'COMPLETE')
        self.assertEqual([r['item'] for r in result['r']['repairDiffReviews']], ['slice', '_integration'])
        self.assertTrue(result['r']['fixOrigins'][-1]['integration'])
        merge = next(c for c in result['calls'] if c['label'].startswith('merge:'))
        self.assertNotIn('checkerWitness', merge['p'])
        self.assertNotIn('diffAttack', merge['p'])

    def test_deleted_guard_still_selects_its_surface_attacker(self):
        (self.root / 'state.py').write_text('if valid:\n    flag = False\n')
        self.git('add', 'state.py')
        self.git('commit', '-qm', 'guarded new base')
        self.base = self.git('rev-parse', 'HEAD')
        self.prepare('')
        measured = manifest(self.data)
        self.assertEqual(len(measured['hunks']), 1)
        self.assertFalse(measured['hunks'][0]['added'])
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

    def test_fixed_packet_check_refuses_a_checker_changed_tree(self):
        measured = manifest(self.data)
        snapshot = PROOF.materialize(self.root, self.base, str(self.patches[0]), measured['patchSha256'], 'slice', 1)
        self.addCleanup(lambda: PROOF.cleanup(snapshot['snapshot'], snapshot['token']))
        (Path(snapshot['snapshot']) / 'state.py').write_text('flag = False\n')
        with self.assertRaisesRegex(ValueError, 'changed the pinned repair snapshot'):
            PROOF.check_packet(snapshot['snapshot'], snapshot['token'], '.fluxpoint-spec.json')

    def test_operational_limits_are_pinned_and_alarm_counts_come_from_series(self):
        from repair_diff import coverage_issue
        measured = manifest(b'diff --git a/service.py b/service.py\n--- a/service.py\n+++ b/service.py\n@@ -0,0 +1 @@\n+client.get(url, timeout=5); notify(operator)\n')
        self.assertIn('latency', measured['hunks'][0]['obligations'])
        observation = {'command': 'check service.py', 'result': 'defended'}
        row = {'patchSha256': measured['patchSha256'], 'hunks': [{'id': measured['hunks'][0]['id'], 'surface': 'outbound-calls', 'attack': observation, 'control': observation}],
            'operational': {'latency': dict(observation, deadlineMs=10000, observedMs=1, pool='pool'),
                'alarmRate': dict(observation, seriesPath=str(self.series), seriesSha256=__import__('hashlib').sha256(self.series.read_bytes()).hexdigest(), durationSeconds=86400, falsePositives=0, maxPerDay=1)}}
        bounds = {'deadlineMs': 5000, 'maxPerDay': 0, 'providerPools': ['pool']}
        self.assertIn('pinned', coverage_issue(row, measured, [], True, bounds=bounds))
        row['operational']['latency']['deadlineMs'] = 5000
        row['operational']['alarmRate']['maxPerDay'] = 0
        self.assertIn('sharedResources', coverage_issue(row, measured, [], True, bounds=bounds))
        self.assertIsNone(coverage_issue(row, measured, [{'resource': 'pool'}], True, bounds=bounds))
        self.assertEqual(PROOF.series_metrics(self.series.read_bytes()), {'durationSeconds': 86400.0, 'falsePositives': 0})

    def test_helper_counts_alarm_events_instead_of_trusting_stdout(self):
        series = {'version': 1, 'startedAt': '2026-10-27T00:00:00Z', 'endedAt': '2026-10-28T00:00:00Z',
            'durationSeconds': 1, 'events': [{'id': 'alarm-1', 'at': '2026-10-27T12:00:00Z', 'falsePositive': True}]}
        self.series.write_text(json.dumps(series))
        snapshot = PROOF.materialize(self.root, self.base, str(self.patches[0]), manifest(self.data)['patchSha256'], 'slice', 1)
        self.addCleanup(lambda: PROOF.cleanup(snapshot['snapshot'], snapshot['token']))
        control = shlex.join([sys.executable, '-c', 'print("valid")'])
        fiction = shlex.join([sys.executable, '-c', 'print(\'{"durationSeconds":1,"falsePositives":0,"maxPerDay":9999}\')'])
        result = PROOF.execute(snapshot['snapshot'], snapshot['token'], control, fiction, 10, str(self.series))
        self.assertEqual(result['metrics'], {'durationSeconds': 86400.0, 'falsePositives': 1})
        self.assertEqual(result['seriesFormat'], 'RepairAlarmSeriesV1')

    def test_test_only_keywords_do_not_require_production_alarm_series(self):
        measured = manifest(b'diff --git a/tests/test_page.py b/tests/test_page.py\n--- a/tests/test_page.py\n+++ b/tests/test_page.py\n@@ -0,0 +1 @@\n+assert page == 2\n')
        self.assertEqual(measured['hunks'][0]['obligations'], [])

    def test_command_path_floor_decodes_git_quoted_unicode_and_deleted_paths(self):
        from repair_diff import manifest_path
        vectors = ['b/state.py', 'a/state.py', 'a/x b/x', r'"b/\303\251.py"', r'"a/\303\251.py" "b/\303\251.py"', r'"b/tab\tfile.py"']
        expected = ['state.py', 'state.py', 'x', 'é.py', 'é.py', 'tab\tfile.py']
        self.assertEqual([manifest_path(v) for v in vectors], expected)
        script = (SCRIPTS / 'repair-proof.js').read_text() + '\nconsole.log(JSON.stringify(' + json.dumps(vectors) + '.map(repairManifestPath)));'
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_surface_classification_matches_ascii_javascript_boundaries(self):
        from repair_diff import surfaces
        values = ['jsonñ = 1', 'ſleep(1)', 'Kif', 'json = 1']
        script = (SCRIPTS / 'repair-proof.js').read_text() + '\nconsole.log(JSON.stringify(' + json.dumps(values) + '.map(repairSurfaces)));'
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [surfaces(v) for v in values])

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
        setup = "const A={_replayLaunchNonce:'" + '1' * 64 + "'},REPAIR_LAUNCH={root:'ignored'},REPAIR_PROOF_ARGV=['ignored'];let reply;const repairText=v=>typeof v==='string'&&!!v.trim();const agent=async()=>reply;\n"
        script = setup + (SCRIPTS / 'repair-proof.js').read_text(encoding='utf-8') + '\nconst cases=' + json.dumps(cases) + ';\n' + '''
(async()=>{const results=[];for(const [name,expected,actual]of cases){
 const context={base:'a'.repeat(40),item:'slice',round:1,current:{patch:expected,patchSha256:'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'}};
 const manifest={patchSha256:'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',hunks:[],changedSurfaces:['other']};
 reply={status:'READ',base:context.base,item:context.item,round:1,patch:actual,manifest,manifestSha256:repairManifestDigest(manifest)};
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
        self.assertTrue((Path(s['snapshot']) / '.git' / 'index').is_file())
        code = 'import pathlib; assert pathlib.Path("state.py").read_text()=="flag = True\\n"'
        command = shlex.join([sys.executable, '-c', code])
        observed = PROOF.execute(s['snapshot'], s['token'], command, command, 10)
        self.assertFalse(observed['reproduces'])
        marker = Path(s['snapshot']).parent / 'active-command'
        marker.touch()
        with self.assertRaisesRegex(ValueError, 'undrained'):
            PROOF.cleanup(s['snapshot'], s['token'])
        marker.unlink()
        self.assertEqual(PROOF.cleanup(s['snapshot'], s['token'])['status'], 'CLEANED')
        self.assertFalse(Path(s['snapshot']).exists())

    def snapshot(self):
        return PROOF.materialize(self.root, self.base, str(self.patches[0]), manifest(self.data)['patchSha256'], 'slice', 1)

    def test_cleanup_removes_git_objects_an_attack_left_behind(self):
        s = self.snapshot()
        code = ('import subprocess,sys; git=["git","-c","user.name=t","-c","user.email=t@example.invalid"]; '
                'subprocess.run(git+["init","-q"],check=True); '
                'subprocess.run(git+["commit","-q","--allow-empty","-m","x"],check=True); sys.exit(1)')
        command = shlex.join([sys.executable, '-c', code])
        self.assertTrue(PROOF.execute(s['snapshot'], s['token'], 'git --version', command, 30)['reproduces'])
        cleaned = PROOF.cleanup(s['snapshot'], s['token'])
        self.assertEqual((cleaned['status'], len(cleaned['observations'])), ('CLEANED', 1))
        self.assertFalse(Path(s['snapshot']).parent.exists())

    def test_cleanup_names_what_it_could_not_remove(self):
        s = self.snapshot()
        held = Path(s['snapshot']) / 'held.txt'
        held.write_text('open elsewhere')
        unlink = os.unlink

        def refuse(path, *args, **kwargs):
            if os.path.basename(path) == 'held.txt':
                raise PermissionError(13, 'Access is denied')
            return unlink(path, *args, **kwargs)

        with mock.patch.object(os, 'unlink', refuse):
            with self.assertRaisesRegex(CleanupPending, re.escape(str(held))):
                PROOF.cleanup(s['snapshot'], s['token'])
        self.assertTrue(held.exists())
        self.assertEqual(PROOF.cleanup(s['snapshot'], s['token'])['status'], 'CLEANED')

    def test_alarm_series_is_reread_and_provider_pool_cannot_be_omitted(self):
        measured = manifest(b'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+provider rate-limit alarm timer\n')
        digest = __import__('hashlib').sha256(self.series.read_bytes()).hexdigest()
        c = {'patchSha256': measured['patchSha256'], 'hunks': [{'id': measured['hunks'][0]['id'], 'surface': 'outbound-calls', 'attack': {'command': 'a x', 'result': 'r'}, 'control': {'command': 'c', 'result': 'r'}}],
             'operational': {'latency': {'command': 'a x', 'result': 'r', 'deadlineMs': 10, 'observedMs': 1, 'pool': 'provider-pool'},
                             'alarmRate': {'command': 'a x', 'result': 'r', 'seriesPath': str(self.series), 'seriesSha256': digest, 'durationSeconds': 86400, 'falsePositives': 0, 'maxPerDay': 1}}}
        self.assertIn('sharedResources', coverage_issue(c, measured, [], True, bounds={'deadlineMs': 10, 'maxPerDay': 1, 'providerPools': ['provider-pool']}))
        resources = [{'resource': 'provider-pool'}]
        self.assertIsNone(coverage_issue(c, measured, resources, True, bounds={'deadlineMs': 10, 'maxPerDay': 1, 'providerPools': ['provider-pool']}))
        self.series.write_text('changed')
        self.assertIn('digest differs', coverage_issue(c, measured, resources, True, bounds={'deadlineMs': 10, 'maxPerDay': 1, 'providerPools': ['provider-pool']}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
