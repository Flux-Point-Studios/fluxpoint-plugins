#!/usr/bin/env python3
"""Execute generated lane/replay programs; no Workflow/model calls are made."""
import copy
import hashlib
import importlib.util
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('scheduler_compiler', SCRIPTS / 'compile-graph.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
CONTRACTS = c.load_contracts(str(SCRIPTS.parent / 'contracts'))
CONTRACTS['TestV1'] = {'type':'object', 'properties': {'outcome':{'type':'string'}, 'exit':{'type':'integer'}, 'sha':{'type':'string'}, 'packetConflict':{'type':'object'}}}
BASE = {'_base': {'sha':'a'*40, 'branch':'main', 'root':'/repo', 'porcelain':''}, '_launch': {'nonce':'original','root':'/repo'}, '_replayLaunchNonce':'fresh'}


def graph(nodes):
    return {'version':1,'campaign':'test concurrent slices','budget':{'maxNodes':100,'nodeFloorTokens':10,'maxEstimatedTokens':100000000},'nodes':nodes}


def node(nid, **extra):
    return dict({'id':nid,'prompt':'Do '+nid,'contract':'TestV1'}, **extra)


def execute(ir, args=None, setup='', body='', compiled_source=None):
    args = dict(args or {})
    state = {key: args.pop(key) for key in ('_memo', '_prefix', '_capture', '_ledgerCalls') if key in args}
    if '_capture' not in state and any('capturedResult' in row for row in state.get('_prefix', [])):
        pool = {}
        for row in state['_prefix']:
            if 'capturedResult' in row:
                encoded = json.dumps(row.pop('capturedResult'), ensure_ascii=True, separators=(',', ':'))
                row['resultRef'] = hashlib.sha256(encoded.encode()).hexdigest()
                pool[row['resultRef']] = encoded
        state['_capture'] = {'version':1,'launch':args.get('_launch',BASE['_launch']),'source':{'runId':'source-run','artifactSha256':'f'*64},'results':pool}
    source = (compiled_source if compiled_source is not None else c.emit(ir, CONTRACTS, replay_state=state)).replace('export const meta =', 'const meta =')
    harness = r'''
const fs = require('fs'); const input = JSON.parse(fs.readFileSync(0,'utf8'));
const events = []; const rawCalls = []; let active=0, maximum=0;
const root=input.args._base.root, head=input.args._base.sha; let tips = {'repo-a':'b'.repeat(40),'repo-b':'c'.repeat(40)}, remainingTokens=1000000;
let snapshotHeads = repository => (tips[repository] || 'b'.repeat(40))+'\trefs/heads/main';
SETUP
const agent = async (prompt,opts) => {
 rawCalls.push({prompt,opts}); events.push('start:'+opts.label); active++; maximum=Math.max(active,maximum);
 if (opts.label.startsWith('push-check:')) {
   const repository=JSON.parse(/git -C ("(?:[^"\\]|\\.)*") ls-remote --heads origin/.exec(prompt)[1]);
   active--; events.push('end:'+opts.label);
   return {repository,heads:snapshotHeads(repository)};
 }
 if (opts.label.startsWith('tree-check:')) {
   let value = {root,head,porcelain:''};
   const target = /git -C ("(?:[^"\\]|\\.)*") ls-remote origin ("(?:[^"\\]|\\.)*")/.exec(prompt);
   if (target) { value.repository=JSON.parse(target[1]); value.branch=JSON.parse(target[2]).replace('refs/heads/',''); value.tip=tips[value.repository] || 'b'.repeat(40) }
   active--; events.push('end:'+opts.label); return value;
 }
 let result={outcome:'committed',exit:0,sha:'b'.repeat(40),...(opts.schema.required?.includes('baseSeen')?{baseSeen:head}:{})};
 BODY
 await new Promise(resolve=>setTimeout(resolve,opts.label.startsWith('a')?25:5));
 active--; events.push('end:'+opts.label); return result;
};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const fn=new AsyncFunction('args','agent','budget','parallel','phase','log',input.source);
fn(input.args,agent,{total:1000000,remaining:()=>remainingTokens,spent:()=>0},fns=>Promise.all(fns.map(f=>f())),()=>{},()=>{}).then(result=>console.log(JSON.stringify({result,events,rawCalls,maximum}))).catch(error=>{console.error(error.stack);process.exitCode=1});
'''.replace('SETUP', setup).replace('BODY', body)
    p = subprocess.run(['node','-e',harness],input=json.dumps({'source':source,'args':dict(BASE,**(args or {}))},ensure_ascii=False),text=True,encoding='utf-8',capture_output=True,timeout=15)
    if p.returncode: raise AssertionError(p.stderr)
    return json.loads(p.stdout)


class GraphReplay(unittest.TestCase):
    def test_unicode_memo_transport_ignores_windows_default_codepage(self):
        ir=self.replay_graph(); ir['argDefaults']={'repairA':'réparé 🦀 東京','repairB':'unchanged λ'}
        body="if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        with patch.object(locale,'getpreferredencoding',return_value='cp1252'), patch.object(locale,'getencoding',return_value='cp1252',create=True):
            first=execute(ir,body=body)['result']
            out=execute(ir,args=self.memo_args(first),body=body)
        self.assertNotIn('start:a',out['events']); self.assertNotIn('start:b',out['events'])
        replayed=next(r for r in out['result']['memoRecords'] if r['node']=='a')
        self.assertIn('réparé 🦀 東京',replayed['prompt'])

    def test_production_plan_transports_json_as_utf8_under_windows_codepage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'répertoire 🦀'; root.mkdir()
            def git(*args): subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,encoding='utf-8')
            git('init','-q'); git('config','user.name','Replay Test'); git('config','user.email','replay@example.invalid')
            git('commit','-q','--allow-empty','-m','baseline')
            ir=graph([node('inspect',fresh=False,prompt='Inspect {{A.language}}')])
            source={'launch':{'nonce':'original','root':str(root)},'results':{'inspect':{'outcome':'réparé 🦀 東京'}},'inputs':{'language':'réparé 🦀 東京'}}
            helpers={'plugin_identity':c._sibling('plugin_identity'), 'replay_memo':SimpleNamespace(build_memo=lambda *a,**kw:{'_memo':{},'_prefix':[]}),
                     'run_artifacts':SimpleNamespace(read=lambda *a,**kw:({'summary':source},'f'*64))}
            with patch.object(c,'_sibling',side_effect=helpers.__getitem__), patch.object(locale,'getpreferredencoding',return_value='cp1252'), patch.object(locale,'getencoding',return_value='cp1252',create=True), patch.object(subprocess,'run',wraps=subprocess.run) as ran:
                rows=c.rerun_plan(ir,CONTRACTS,{'language':'réparé 🦀 東京'},str(root),'source-run')
            self.assertEqual(rows[0]['node'],'inspect')
            self.assertEqual(ran.call_args.kwargs['encoding'],'utf-8')
            self.assertIn('réparé 🦀 東京',ran.call_args.kwargs['input'])

    def memo_args(self, result):
        memo={r['key']:dict(r,runId='source-run',artifactSha256='f'*64,agentId='agent-'+r['node']) for r in result['memoRecords'] if r['result'] is not None}
        return {'_memo':memo,'_resumeFromRunId':'source-run','_resumeArtifactSha256':'f'*64,'_replayLaunchNonce':'new-fresh'}

    def replay_graph(self):
        return graph([node('a',lane='one',mutates=True,pushes='repo-a#main',replay='inputs',prompt='Repair {{A.repairA}}'),
                      node('a-gate',lane='one',after='a',verifies='a',independent=True,repository='repo-a',branch='main',replay='tree',prompt='Judge {{prev.outcome}}',haltWhen='exit != 0'),
                      node('b',lane='two',mutates=True,pushes='repo-b#main',replay='inputs',prompt='Repair {{A.repairB}}'),
                      node('b-gate',lane='two',after='b',verifies='b',independent=True,repository='repo-b',branch='main',fresh=True,prompt='Judge {{prev}}',haltWhen='exit != 0')])

    def lanes(self):
        return graph([node('a',lane='one'),node('a-gate',lane='one',after='a',prompt='Judge {{prev}}',independent=True,verifies='a'),
                      node('b',lane='two'),node('b-gate',lane='two',after='b',prompt='Judge {{prev}}',independent=True,verifies='b'),
                      node('joined',after=['a-gate','b-gate'],prompt='Use {{prev.a-gate}} and {{prev.b-gate.exit}}')])

    def test_generated_lanes_overlap_and_join_consumes_each_result(self):
        ir=self.lanes(); self.assertEqual(c.validate(ir,CONTRACTS),[])
        out=execute(ir); events=out['events']
        self.assertLess(events.index('start:b'),events.index('end:a'))
        self.assertGreater(events.index('start:joined'),events.index('end:a-gate'))
        self.assertGreater(events.index('start:joined'),events.index('end:b-gate'))
        self.assertEqual(out['result']['outcome'],'COMPLETE')
        self.assertIn('"exit":0',next(x['prompt'] for x in out['rawCalls'] if x['opts']['label']=='joined'))

    def test_join_requires_each_named_predecessor_and_real_fields(self):
        ir=self.lanes(); ir['nodes'][-1]['prompt']='Use {{prev.a-gate}}'
        self.assertTrue(any('never consumed' in x for x in c.validate(ir,CONTRACTS)))
        ir['nodes'][-1]['prompt']='Use {{prev.a-gate}} {{prev.b-gate.unknown}}'
        self.assertTrue(any('does not name a field' in x for x in c.validate(ir,CONTRACTS)))

    def test_halt_waits_inflight_and_stops_both_lanes(self):
        ir=graph([node('a',lane='one',haltWhen='exit != 0'),node('a-next',lane='one',after='a',prompt='Use {{prev}}'),node('b',lane='two',haltWhen='exit != 0'),node('b-next',lane='two',after='b',prompt='Use {{prev}}'),node('joined',prompt='Join')])
        out=execute(ir,body="if(opts.label==='b') result.exit=1;")
        self.assertEqual(out['result']['outcome'],'HALTED')
        self.assertIn('end:a',out['events'])
        self.assertNotIn('start:a-next',out['events']); self.assertNotIn('start:b-next',out['events']); self.assertNotIn('start:joined',out['events'])

    def test_shared_halt_does_not_rewrite_literal_prompt_code(self):
        ir=graph([node('a',lane='one',prompt="Explain the literal return summary('HALTED') without editing it.")])
        out=execute(ir)
        primary=next(call for call in out['rawCalls'] if call['opts']['label']=='a')
        self.assertIn("return summary('HALTED')",primary['prompt'])

    def test_nullable_conflict_halt_distinguishes_missing_and_present(self):
        ir=graph([node('a',lane='one',haltWhen='packetConflict != null')])
        self.assertEqual(c.validate(ir,CONTRACTS),[])
        self.assertEqual(execute(ir)['result']['outcome'],'COMPLETE')
        self.assertEqual(execute(ir,body="result.packetConflict={checks:['locked']};")['result']['outcome'],'HALTED')
        ir['nodes'][0]['haltWhen']='packetConflict >= null'
        self.assertTrue(any('only == or !=' in x for x in c.validate(ir,CONTRACTS)))

    def test_slice_packet_conflict_automatically_halts_scalar_and_fanout(self):
        for fanout in (False,True):
            with self.subTest(fanout=fanout):
                first=node('a',lane='one',contract='SliceV1')
                if fanout: first['foreach']='items'
                ir=graph([first,node('a-next',lane='one',after='a',prompt='Continue {{prev}}')])
                ir['lists']={'items':[{'key':'x'},{'key':'y'}]}
                out=execute(ir,body="result.packetConflict=null;")
                self.assertEqual(out['result']['outcome'],'HALTED')
                self.assertNotIn('start:a-next',out['events'])

    def test_shared_branch_mutators_cannot_run_in_different_lanes(self):
        ir=graph([node('a',lane='one',mutates=True,pushes='repo-a#main'),node('b',lane='two',mutates=True,pushes='repo-a#main')])
        self.assertTrue(any('both push' in x for x in c.validate(ir,CONTRACTS)))

    def test_verifier_never_runs_concurrently_with_its_producer(self):
        ir=graph([node('a',lane='one',mutates=True,pushes='repo-a#main'),node('check',lane='two',independent=True,verifies='a')])
        self.assertTrue(any('after ancestor' in x for x in c.validate(ir,CONTRACTS)))
        ir['nodes'][1].update(after='a',prompt='Check {{prev}}')
        self.assertEqual(c.validate(ir,CONTRACTS),[])
        out=execute(ir)
        self.assertGreater(out['events'].index('start:check'),out['events'].index('end:a'))
        for n in ir['nodes']: n.pop('lane')
        ir['nodes'][0]['replay']='inputs'; ir['nodes'][1].update(replay='tree',repository='repo-a',branch='main')
        ir['nodes'][1].pop('after'); ir['nodes'][1]['prompt']='Judge current tree'
        self.assertTrue(any('after ancestor' in x for x in c.validate(ir,CONTRACTS)))

    def test_verifier_target_is_the_actual_branch_its_producer_pushes(self):
        ir=self.replay_graph()
        ir['nodes'][1]['repository']='another-repository'
        self.assertTrue(any('differs from the pushes target' in x for x in c.validate(ir,CONTRACTS)))
        ir['nodes'][1]['repository']='repo-a'; ir['nodes'][1]['branch']='another-branch'
        self.assertTrue(any('differs from the pushes target' in x for x in c.validate(ir,CONTRACTS)))

    def test_shared_push_identity_normalizes_windows_and_common_origins(self):
        for left,right in [('C:/Repo','c:\\repo'),('/repo','/repo/.git'),('file:///C:/Repo','c:\\repo'),('file://server/share/repo','//server/share/repo'),('https://github.com/Owner/Project.git','git@github.com:owner/project'),('ssh://git.example/org/repo.git','git.example:org/repo.git')]:
            self.assertEqual(c.push_identity({'repository':left,'branch':'main'}),c.push_identity({'repository':right,'branch':'main'}))
        with tempfile.TemporaryDirectory() as tmp:
            targets=[]
            for name in ('one','two'):
                path=Path(tmp)/name; path.mkdir()
                subprocess.run(['git','init','-q',str(path)],check=True)
                subprocess.run(['git','-C',str(path),'remote','add','origin','https://example.invalid/org/shared.git'],check=True)
                targets.append({'repository':str(path),'branch':'main'})
            self.assertEqual(c.push_identity(targets[0]),c.push_identity(targets[1]))

    def test_effective_origin_rewrites_and_split_push_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'clone'; path.mkdir()
            subprocess.run(['git','init','-q',str(path)],check=True)
            def git(*args): subprocess.run(['git','-C',str(path),*args],check=True)
            git('remote','add','origin','https://old.invalid/org/repo.git')
            git('config','url.https://actual.invalid/.insteadOf','https://old.invalid/')
            self.assertEqual(c.push_identity({'repository':str(path),'branch':'main'}),c.push_identity({'repository':'https://actual.invalid/org/repo.git','branch':'main'}))
            self.assertEqual(c.push_identity({'repository':'clone','branch':'main'},repository_root=tmp),c.push_identity({'repository':str(path),'branch':'main'}))
            git('remote','set-url','--push','origin','https://different.invalid/org/repo.git')
            with self.assertRaisesRegex(ValueError,'fetch and push'): c.push_identity({'repository':str(path),'branch':'main'})
            git('remote','set-url','--add','--push','origin','https://another.invalid/org/repo.git')
            with self.assertRaisesRegex(ValueError,'one effective'): c.push_identity({'repository':str(path),'branch':'main'})

    def test_irreversible_guard_must_be_actual_ancestor(self):
        ir=self.lanes(); ir['nodes'][3]['haltWhen']='exit != 0'; ir['requiredArgs']=['confirm']; ir['nodes'].append(node('effect',irreversible=True,after='b-gate',prompt='Use {{prev}}'))
        self.assertEqual(c.validate(ir,CONTRACTS),[])
        ir['nodes'][-1]['after']='b'; self.assertTrue(any('gate must be ordered before' in x for x in c.validate(ir,CONTRACTS)))
        ir['nodes'][-1]['lane']='two'; self.assertTrue(any('lane-free barriers' in x for x in c.validate(ir,CONTRACTS)))

    def test_sha256_matches_python_for_unicode_nested_json(self):
        helpers=(SCRIPTS/'graph-replay.js').read_text().split('const CALL_RECORDS')[0]
        value={'emoji':'🦀 café','keys':{'z':1,'a':False},'array':[None,0,-1,'λ']}
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        p=subprocess.run(['node','-e',helpers+'\nconsole.log(sha256(canonical('+json.dumps(value)+')))'],text=True,encoding='utf-8',capture_output=True,check=True)
        self.assertEqual(p.stdout.strip(),hashlib.sha256(raw.encode()).hexdigest())

    def test_lane_tree_readings_after_fork_are_launch_unique(self):
        out=execute(self.lanes()); calls=out['rawCalls']
        self.assertNotIn('LIVE TREE READING NONCE',calls[0]['prompt'])
        for call in calls[1:]:
            if call['opts']['label'].startswith('tree-check:'):
                self.assertIn('LIVE TREE READING NONCE: fresh',call['prompt'])

    def test_unchanged_independent_branch_replays_after_other_builder_changes(self):
        ir=self.replay_graph(); ir['argDefaults']={'repairA':'old','repairB':'old'}
        body="if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first=execute(ir,body=body)['result']; self.assertEqual(first['outcome'],'COMPLETE')
        second=execute(ir,args=dict(self.memo_args(first),repairA='changed'),body=body)
        self.assertIn('start:a',second['events']); self.assertIn('start:a-gate',second['events'])
        self.assertNotIn('start:b',second['events']); self.assertIn('start:b-gate',second['events'])
        replayed=[r['node'] for r in second['result']['memoRecords'] if r['mode']=='memo']
        self.assertEqual(replayed,['b'])
        self.assertTrue(next(r for r in second['result']['memoRecords'] if r['node']=='b')['reading'])

    def test_call_options_and_reading_indices_bind_exact_raw_receipts(self):
        ir=self.replay_graph(); ir['argDefaults']={'repairA':'old','repairB':'old'}
        body="if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first=execute(ir,body=body)['result']
        second=execute(ir,args=self.memo_args(first),body=body)['result']
        for call in second['callRecords']:
            expected=hashlib.sha256(json.dumps(call['opts'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            self.assertTrue(call['prompt'].startswith('Fluxpoint-Call-Opts-SHA256: '+expected+'\n'))
        for receipt in second['memoRecords']:
            if receipt['mode']=='memo':
                sentinel=second['callRecords'][receipt['readingCallIndex']]
                self.assertTrue(sentinel['opts']['label'].startswith('tree-check:'))
                self.assertEqual(sentinel['result']['tip'],receipt['reading']['tip'])
                self.assertEqual(receipt['source']['artifactSha256'],'f'*64)

    def test_native_prefix_before_fork_keeps_original_call_slots(self):
        ir=self.replay_graph(); ir['argDefaults']={'repairA':'old','repairB':'old'}
        for n in ir['nodes']: n.pop('lane')
        body="if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first=execute(ir,body=body)['result']; args=self.memo_args(first)
        args['_prefix']=BranchReplayFollowup.prefix(self, first)
        out=execute(ir,args=args,body=body)
        self.assertNotIn('start:tree-check:replay a',out['events'])
        a=next(row for row in out['result']['memoRecords'] if row['node']=='a')
        self.assertEqual(a['mode'],'prefix')

    def test_fresh_fanout_calls_never_replay_from_runtime_prefix(self):
        ir=graph([node('quotes',fresh=True,foreach='assets',prompt='Read live {{item}}')]); ir['lists']={'assets':['a','b']}
        first=execute(ir)['result']
        def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        prefix=[dict(promptSha256=hashlib.sha256(row['prompt'].encode()).hexdigest(),optsSha256=digest(row['opts']),resultSha256=digest(row['result']),runId='source-run',artifactSha256='f'*64,agentId='p'+str(i)) for i,row in enumerate(first['callRecords'])]
        second=execute(ir,args={'_prefix':prefix,'_replayLaunchNonce':'another-fresh','_resumeFromRunId':'source-run'})['result']
        workers=[row for row in second['callRecords'] if row['opts']['label'].startswith('quotes:')]
        self.assertEqual(len(workers),2)
        for row in workers:
            self.assertEqual(row['mode'],'live'); self.assertIn('FRESH NODE LAUNCH NONCE: another-fresh',row['prompt'])

    def test_gate_replay_needs_replayed_ancestor_and_current_tip(self):
        ir=self.replay_graph(); ir['argDefaults']={'repairA':'old','repairB':'old'}
        body="if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first=execute(ir,body=body)['result']
        out=execute(ir,args=self.memo_args(first),body=body)
        self.assertNotIn('start:a',out['events']); self.assertNotIn('start:a-gate',out['events'])
        changed=execute(ir,args=self.memo_args(first),setup="tips['repo-a']='d'.repeat(40);",body=body)
        self.assertIn('start:a',changed['events']); self.assertIn('start:a-gate',changed['events'])
        self.assertEqual(changed['result']['outcome'],'TREE-MISMATCH')

    def test_gate_never_accepts_builder_or_gate_reported_sha_over_live_read(self):
        ir=self.replay_graph(); ir['argDefaults']={'repairA':'old','repairB':'old'}
        out=execute(ir,body="result.sha='0'.repeat(40);")
        self.assertEqual(out['result']['outcome'],'TREE-MISMATCH')

    def test_rejudge_note_and_nonce_only_reach_named_gate_and_its_tree_check(self):
        ir=self.replay_graph(); ir['argDefaults']={'repairA':'old','repairB':'old'}
        body="if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first=execute(ir,body=body)['result']
        args=dict(self.memo_args(first),rejudge={'a-gate':{'nonce':'new-judgment','reason':'operator attribution only','note':'faulty test fixture now corrected'}},_priorResults=first['results'])
        out=execute(ir,args=args,body=body)
        self.assertNotIn('start:a',out['events']); self.assertIn('start:a-gate',out['events'])
        for call in out['rawCalls']:
            self.assertNotIn('operator attribution only',call['prompt'])
            if 'new-judgment' in call['prompt']:
                self.assertIn(call['opts']['label'],['a-gate','tree-check:before a-gate'])
            if call['opts']['label'].startswith('tree-check:'):
                self.assertNotIn('faulty test fixture now corrected', call['prompt'])
        row=out['result']['rejudges'][0]
        self.assertEqual(row['reason'],'operator attribution only'); self.assertEqual(row['priorVerdict'],first['results']['a-gate'])

    def test_whole_args_never_leak_rejudge_metadata_or_invalidate_builder(self):
        ir=self.replay_graph(); ir['nodes'][0]['prompt']='Repair {{A}}'; ir['argDefaults']={'repairA':'old','repairB':'old'}
        body="if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first=execute(ir,body=body)['result']
        args=dict(self.memo_args(first),rejudge={'a-gate':{'nonce':'judge-again','reason':'SECRET attribution','note':'inspect valid control'}},_priorResults=first['results'])
        out=execute(ir,args=args,body=body)
        self.assertNotIn('start:a',out['events'])
        for call in out['rawCalls']: self.assertNotIn('SECRET attribution',call['prompt'])

    def test_concurrent_calls_reserve_the_floor_atomically(self):
        out=execute(graph([node('a',lane='one'),node('b',lane='two')]),setup='remainingTokens=10;')
        calls=[c for c in out['rawCalls'] if not c['opts']['label'].startswith('tree-check:')]
        self.assertEqual(len(calls),1)
        self.assertEqual(out['result']['outcome'],'BUDGET-EXHAUSTED')

    def test_legacy_runtime_prompts_options_and_results_match_baseline(self):
        # Compare the baseline compiler, not a hand-written expected string.
        original=subprocess.run(['git','show','c848f07:plugins/fluxpoint/scripts/compile-graph.py'],text=True,encoding='utf-8',capture_output=True,check=True).stdout
        scope={'__name__':'baseline','__file__':str(SCRIPTS/'compile-graph.py')}; exec(compile(original,'baseline','exec'),scope)
        ir=graph([node('a'),node('b',after='a',prompt='Use {{prev}}')])
        def compare_execution(graph):
            new = execute(graph)
            old = execute(graph, compiled_source=scope['emit'](graph, CONTRACTS))
            self.assertEqual(new['rawCalls'], old['rawCalls'])
            for key in ('outcome', 'results', 'provenance', 'planned', 'inputs'):
                self.assertEqual(new['result'][key], old['result'][key], key)
        compare_execution(ir)
        ir['argDefaults']={'configuration':'default'}; ir['nodes'][0]['prompt']='Read {{A.configuration}}'
        compare_execution(ir)

class ReplayFollowup(unittest.TestCase):
    def test_mixed_targeted_and_untargeted_builders_do_not_claim_unusable_readings(self):
        ir = graph([node('a', mutates=True), node('a-gate', after='a', verifies='a', independent=True, prompt='Judge {{prev}}'),
                    node('b', after='a-gate', mutates=True, pushes='repo-a#main', replay='inputs', prompt='Build {{prev}}'),
                    node('b-gate', after='b', verifies='b', independent=True, repository='repo-a', branch='main', replay='tree', prompt='Judge {{prev}}')])
        records = execute(ir)['result']['memoRecords']
        self.assertNotIn('verifiedReading', next(r for r in records if r['node'] == 'a'))
        self.assertIn('verifiedReading', next(r for r in records if r['node'] == 'b'))

    def test_lone_surrogates_have_a_defined_raw_hash_encoding(self):
        helpers = (SCRIPTS / 'graph-replay.js').read_text().split('const CALL_RECORDS')[0]
        run = subprocess.run(['node', '-e', helpers + '\nprocess.stdout.write(sha256("a\\ud800b"))'], capture_output=True, text=True, check=True)
        self.assertEqual(run.stdout, hashlib.sha256('a\ufffdb'.encode()).hexdigest())

    def test_inline_memo_is_rejected_when_state_is_compiled(self):
        ir = graph([node('a', fresh=False)])
        source = c.emit(ir, CONTRACTS, replay_state={})
        harness = 'const input=JSON.parse(require("fs").readFileSync(0,"utf8"));const f=new (Object.getPrototypeOf(async function(){}).constructor)("args","agent","budget","parallel","phase","log",input.source);f(input.args,async()=>({}),{total:0},async()=>[],()=>{},()=>{}).catch(e=>{console.log(String(e))})'
        # Compiled graphs exceed Windows CreateProcess's command-line limit.
        payload = {'source': source.replace('export const meta =', 'const meta ='),
                   'args': dict(BASE, _memo={'forged': {'result': {'exit': 0}}})}
        run = subprocess.run(['node', '-e', harness], input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True, check=True)
        self.assertIn('compiled replay state cannot be overridden inline', run.stdout)

    def test_ledger_uses_sha256_and_replays_prior_fnv_key(self):
        ir = graph([node('effect', irreversible=True)])
        first = execute(ir, args={'_ledger': {}, 'confirm': 'effect'})
        written = first['result']['ledger'][0]
        prompt = next(call['prompt'] for call in first['rawCalls'] if call['opts']['label'] == 'effect')
        prompt = prompt.split('\n',1)[1]  # The audit header is not part of the stable once-only intent key.
        self.assertEqual(written['key'], ir['campaign'] + '|effect|sha256:' + hashlib.sha256(prompt.encode('utf-8')).hexdigest())
        old = 2166136261
        units = prompt.encode('utf-16-le')
        for index in range(0, len(units), 2):
            old = ((old ^ int.from_bytes(units[index:index + 2], 'little')) * 16777619) & 0xffffffff
        oldkey = ir['campaign'] + '|effect|' + format(old, 'x')
        replayed = execute(ir, args={'_ledger': {oldkey: {'result': written['result']}}, 'confirm': ''})
        self.assertNotIn('start:effect', replayed['events'])
        self.assertEqual(replayed['result']['results']['effect'], written['result'])

    def test_replay_ancestors_of_irreversible_effect_are_refused(self):
        ir = graph([node('a', mutates=True, pushes='repo-a#main', replay='inputs'),
                    node('gate', after='a', verifies='a', independent=True, repository='repo-a', branch='main', replay='tree', prompt='Judge {{prev}}'),
                    node('effect', after='gate', irreversible=True, prompt='Act {{prev}}')])
        self.assertTrue(any('irreversible ancestor' in error for error in c.validate(ir, CONTRACTS)))


class BranchReplayFollowup(unittest.TestCase):
    memo_args = GraphReplay.memo_args
    replay_graph = GraphReplay.replay_graph
    def prefix(self, result):
        import re
        rows = []
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        for call in result['callRecords']:
            nonce = re.search(r'\nLIVE (?:TREE READING|REF SNAPSHOT) NONCE: ([A-Za-z0-9_-]+) point ', call['prompt'])
            row = dict(promptSha256=hashlib.sha256(call['prompt'].encode()).hexdigest(), optsSha256=digest(call['opts']), resultSha256=digest(call['result']), capturedResult=call['result'], label=call['opts']['label'], runId='source-run', artifactSha256='f'*64, agentId='p'+str(len(rows)))
            if nonce: row['readingNonce'] = nonce.group(1)
            rows.append(row)
        return rows

    def test_native_fixed_preflight_runs_live_across_captured_ledger_resume(self):
        ir = graph([node('fixed',contract='NativeReadingsV1',isolation='worktree'),
                    node('gate',after='fixed',verifies='fixed',independent=True,haltWhen='exit != 0',prompt='Judge {{prev}}'),
                    node('effect',after='gate',irreversible=True,prompt='Act {{prev}}')])
        ir.update(treeGuard=False,requiredArgs=['confirm'])
        self.assertEqual(c.validate(ir,CONTRACTS),[])
        body = "if(opts.label==='fixed') result={commands:['printf data'],readings:[{exit:0,output:'data\\n'}],...((opts.schema.required||[]).includes('baseSeen')?{baseSeen:head}:{})};"
        first = execute(ir,args={'_ledger':{},'confirm':'effect'},body=body)['result']
        self.assertEqual(first['outcome'],'COMPLETE')
        ledger = {row['key']:row for row in first['ledger']}
        for _ in range(2):
            out = execute(ir,args={'_ledger':ledger,'confirm':'effect','_prefix':self.prefix(first)},body=body)
            self.assertEqual(out['result']['outcome'],'COMPLETE')
            self.assertEqual([call['opts']['label'] for call in out['rawCalls']],['fixed','gate'])
            self.assertEqual(next(row for row in out['result']['callRecords'] if row['opts']['label']=='fixed')['mode'],'live')
            self.assertFalse(out['result']['ledgerReplays'][0]['verified'])
            first = out['result']

    def test_native_fixed_preflight_requires_current_invocation_and_refuses_once_only_role(self):
        ir = graph([node('fixed',contract='NativeReadingsV1',isolation='worktree')])
        ir['treeGuard']=False
        self.assertEqual(c.validate(ir,CONTRACTS),[])
        self.assertTrue(c.replay_features(ir))
        ir['nodes'].append(node('gate',after='fixed',verifies='fixed',independent=True,haltWhen='exit != 0'))
        ir['nodes'].append(node('effect',after='gate',contract='NativeReadingsV1',isolation='worktree',irreversible=True))
        ir['requiredArgs']=['confirm']
        self.assertTrue(any('current fixed-command preflight' in problem for problem in c.validate(ir,CONTRACTS)))

    def test_targeted_reading_preserves_prefix_before_first_fork(self):
        ir = self.replay_graph()
        for n in ir['nodes']: n.pop('lane', None)
        ir['argDefaults'] = {'repairA':'same', 'repairB':'old'}
        body = "if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first = execute(ir, body=body)['result']
        args = self.memo_args(first)
        args['_prefix'] = self.prefix(first)
        args['repairB'] = 'changed'
        second = execute(ir, args=args, body=body)['result']
        self.assertEqual(next(r for r in second['memoRecords'] if r['node']=='a-gate')['mode'], 'prefix')
        self.assertEqual(next(r for r in second['memoRecords'] if r['node']=='b')['mode'], 'live')

    def test_gate_falling_out_of_prefix_refreshes_cached_reading(self):
        ir = self.replay_graph()
        for n in ir['nodes']: n.pop('lane', None)
        ir['argDefaults'] = {'repairA':'same', 'repairB':'same', 'gateNote':'old'}
        ir['nodes'][1]['prompt'] += ' Note {{A.gateNote}}'
        body = "if(opts.label.startsWith('b')) result.sha='c'.repeat(40);"
        first = execute(ir, body=body)['result']
        args = self.memo_args(first)
        args['_prefix'] = self.prefix(first)
        args['gateNote'] = 'changed'
        second = execute(ir, args=args, body=body)['result']
        readings = [r for r in second['callRecords'] if r['opts']['label']=='tree-check:before a-gate']
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]['mode'], 'prefix')
        self.assertEqual(readings[1]['mode'], 'live')
        self.assertIn('new-fresh', readings[1]['prompt'])

    def test_builder_compares_live_branch_with_source_final_tip(self):
        ir = graph([node('a', mutates=True, pushes='repo-a#main', replay='inputs'),
                    node('a-gate', after='a', verifies='a', independent=True, repository='repo-a', branch='main', replay='tree', prompt='Judge {{prev}}'),
                    node('b', after='a-gate', mutates=True, pushes='repo-a#main', replay='inputs', prompt='Build {{prev}}'),
                    node('b-gate', after='b', verifies='b', independent=True, repository='repo-a', branch='main', replay='tree', prompt='Judge {{prev}}')])
        body = "if(opts.label==='a') tips['repo-a']='d'.repeat(40); if(opts.label==='b') tips['repo-a']='e'.repeat(40); result.sha=tips['repo-a'];"
        self.assertTrue(any('share repo-a#main' in text for text in c.warnings(ir)))
        first = execute(ir, body=body)['result']
        args = self.memo_args(first)
        final = next(r for r in reversed(first['tree']['checks']) if r.get('repository')=='repo-a')
        final = {k:final[k] for k in ['root','head','porcelain','repository','branch','tip']}
        for entry in args['_memo'].values(): entry['sourceFinalReading'] = final
        second = execute(ir, args=args, setup="tips['repo-a']='e'.repeat(40);", body=body)
        self.assertNotIn('start:a', second['events'])
        # The old gate judged d, so it must execute against current e.
        self.assertIn('start:a-gate', second['events'])
        replay = next(r for r in second['result']['memoRecords'] if r['node']=='a')
        self.assertEqual(replay['reading']['tip'], 'e'*40)
        self.assertEqual(replay['verifiedReading']['tip'], 'd'*40)

    def test_separate_lanes_do_not_emit_serial_adjacency_warning(self):
        ir = graph([node('a', lane='one'), node('b', lane='two')])
        self.assertFalse(any('run serially in declaration order' in text for text in c.warnings(ir)))
        ir['nodes'][1]['lane'] = 'one'
        self.assertTrue(any('run serially in declaration order' in text for text in c.warnings(ir)))

    def test_plan_uses_latest_corroborated_source_branch_reading(self):
        source = 'return {read:await agent(\'git -C "repo-a" ls-remote origin "refs/heads/main"\',{label:"tree-check:before gate"})}'
        query = dict(source=source, args={'_base':BASE['_base']}, results={}, readings=[dict(repository='repo-a', branch='main', tip='b'*40), dict(repository='repo-a', branch='main', tip='c'*40)])
        result = subprocess.run(['node', str(SCRIPTS/'graph-plan.js')], input=json.dumps(query), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)['summary']['read']['tip'], 'c'*40)

    def test_ssh_and_known_forge_www_aliases_share_push_identity(self):
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='hostname github.com\nport 22\n')):
            alias = c._destination_identity('git@forge-alias:org/repo.git', 'main')
            normal = c._destination_identity('https://github.com/org/repo.git', 'main')
            self.assertEqual(alias, normal)
        self.assertEqual(c._destination_identity('https://www.github.com/org/repo.git', 'main'), normal)
        with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesRegex(ValueError, 'cannot resolve effective SSH'):
                c._destination_identity('git@unknown-alias:org/repo.git', 'main')

    def test_declared_builder_snapshots_real_remote_heads_and_halts_other_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, clone = Path(tmp)/'origin.git', Path(tmp)/'worker'
            def git(*args):
                return subprocess.check_output(['git', *map(str,args)], text=True, encoding='utf-8', stderr=subprocess.PIPE).strip()
            git('init','--bare',remote)
            git('init','-b','main',clone)
            git('-C',clone,'config','user.name','Fixture')
            git('-C',clone,'config','user.email','fixture@example.test')
            git('-C',clone,'commit','--allow-empty','-m','initial')
            git('-C',clone,'remote','add','origin',remote)
            git('-C',clone,'push','origin','main')
            git('-C',clone,'commit','--allow-empty','-m','builder')
            ir=graph([node('build',mutates=True,pushes=clone.as_posix()+'#main')])
            graph_file=Path(tmp)/'WORK.md'; graph_file.write_text('```json graph-ir\n'+json.dumps(ir)+'\n```\n',encoding='utf-8')
            sys.path.insert(0,str(SCRIPTS))
            import replay_memo as rm
            setup="const cp=require('child_process'); snapshotHeads=repository=>cp.execFileSync('git',['-C',repository,'ls-remote','--heads','origin'],{encoding:'utf8'});"
            for ref, expected in [('main','COMPLETE'),('other','PUSH-REF-MISMATCH')]:
                with self.subTest(ref=ref):
                    body="cp.execFileSync('git',"+json.dumps(['-C',str(clone),'push','origin','HEAD:refs/heads/'+ref])+",{stdio:'pipe'});"
                    out=execute(ir,setup=setup,body=body)['result']
                    self.assertEqual(out['outcome'],expected)
                    check=out['pushChecks'][0]
                    self.assertEqual(check['changedRefs'],['refs/heads/'+ref])
                    self.assertEqual(check['declaredMoved'],ref=='main')
                    if ref=='other': self.assertTrue(any(p['status']=='PUSH-NO-CHANGE' for p in out['provenance']))
                    before=out['callRecords'][check['beforeCallIndex']]
                    after=out['callRecords'][check['afterCallIndex']]
                    self.assertEqual(before['mode'],'live'); self.assertEqual(after['mode'],'live')
                    self.assertNotEqual(before['result']['heads'],after['result']['heads'])
                    rm._verify_push_checks(tmp,out,[dict(call,live=True) for call in out['callRecords']],graph_file)

    def test_declared_ref_unchanged_retains_finding_and_deletion_halts(self):
        ir=graph([node('build',mutates=True,pushes='repo-a#main')])
        unchanged=execute(ir)['result']
        self.assertTrue(any(p['status']=='PUSH-NO-CHANGE' for p in unchanged['provenance']))
        self.assertEqual(unchanged['pushChecks'][0]['changedRefs'],[])
        setup="let extra=true; snapshotHeads=r=>tips[r]+'\\trefs/heads/main'+(extra?'\\n'+'e'.repeat(40)+'\\trefs/heads/other':'');"
        out=execute(ir,setup=setup,body='extra=false;')['result']
        self.assertEqual(out['outcome'],'PUSH-REF-MISMATCH')
        self.assertEqual(out['pushChecks'][0]['changedRefs'],['refs/heads/other'])

    def test_snapshot_failure_and_invalid_heads_halt_before_builder(self):
        ir=graph([node('build',mutates=True,pushes='repo-a#main')])
        for setup in ["snapshotHeads=()=>null;", "snapshotHeads=()=>('not-a-sha\\trefs/heads/main');",
                      "snapshotHeads=()=>('a'.repeat(40)+'\\trefs/heads/main\\n'+'b'.repeat(40)+'\\trefs/heads/main');"]:
            with self.subTest(setup=setup):
                out=execute(ir,setup=setup)
                self.assertEqual(out['result']['outcome'],'PUSH-UNKNOWN')
                self.assertNotIn('start:build',out['events'])
        after_failure=execute(ir,body='snapshotHeads=()=>null;')
        self.assertEqual(after_failure['result']['outcome'],'PUSH-UNKNOWN')
        self.assertIn('start:build',after_failure['events'])
        self.assertEqual(after_failure['result']['pushChecks'],[])

    def test_live_builder_refreshes_cached_before_snapshot(self):
        ir=graph([node('build',mutates=True,pushes='repo-a#main',prompt='Build {{A.note}}')])
        ir['argDefaults']={'note':'before'}
        first=execute(ir)['result']
        second=execute(ir,args={'_prefix':self.prefix(first),'note':'changed'})['result']
        before=[r for r in second['callRecords'] if r['opts']['label']=='push-check:before build']
        self.assertEqual([r['mode'] for r in before],['prefix','live'])
        pair=second['pushChecks'][0]
        self.assertEqual(second['callRecords'][pair['beforeCallIndex']]['mode'],'live')
        self.assertEqual(next(r for r in second['memoRecords'] if r['node']=='build')['mode'],'live')

    def test_generated_fanout_receipts_preserve_compiler_keys_and_every_invocation(self):
        sys.path.insert(0,str(SCRIPTS))
        import replay_memo as rm
        ir=graph([node('build',mutates=True,pushes='repo-a#main',foreach='items',prompt='Build {{item}}')])
        ir['lists']={'items':[{'key':'left'},{'key':'right'}]}
        result=execute(ir,body="result.key='worker-provided-key';")['result']
        self.assertEqual([value['key'] for value in result['results']['build']],['left','right'])
        self.assertEqual(len(result['pushChecks']),2)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); projects=root/'projects'; run='wf_fanout'
            directory=projects/'project/session/subagents/workflows'/run
            directory.mkdir(parents=True)
            graph_file=root/'WORK.md'; graph_file.write_text('```json graph-ir\n'+json.dumps(ir)+'\n```\n',encoding='utf-8')
            events=[]
            for index,call in enumerate(result['callRecords']):
                aid='worker'+str(index)
                events += [dict(type='started',agentId=aid,opts=call['opts']),dict(type='result',agentId=aid,result=call['result'])]
                (directory/('agent-'+aid+'.jsonl')).write_text(json.dumps(dict(type='user',message=dict(role='user',content=call['prompt'])))+'\n',encoding='utf-8')
            (directory/'journal.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events),encoding='utf-8')
            rm.validate_current(root,result,run,projects=projects,graph=graph_file)
            # Native Workflow journals may omit both options and labels; the
            # audited first-user task still identifies each fixed snapshot.
            for event in events: event.pop('opts',None)
            (directory/'journal.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events),encoding='utf-8')
            rm.validate_current(root,result,run,projects=projects,graph=graph_file)
            omitted=copy.deepcopy(result)
            removed={omitted['pushChecks'][1][key] for key in ('beforeCallIndex','callIndex','afterCallIndex')}
            retained=[i for i in range(len(omitted['callRecords'])) if i not in removed]
            remap={old:new for new,old in enumerate(retained)}
            omitted['callRecords']=[omitted['callRecords'][i] for i in retained]
            omitted['pushChecks']=omitted['pushChecks'][:1]
            for key in ('beforeCallIndex','callIndex','afterCallIndex'):
                omitted['pushChecks'][0][key]=remap[omitted['pushChecks'][0][key]]
            with self.assertRaisesRegex(rm.ReplayError,'incomplete invocation coverage|current-attempt remote snapshot'):
                rm.validate_current(root,omitted,run,projects=projects,graph=graph_file)

    def test_plan_renders_serial_counterfactual_and_measures_recorded_cost(self):
        ir=graph([node('a',lane='one'),node('a-gate',lane='one',after='a',verifies='a',independent=True,prompt='Judge {{prev}}'),
                  node('b',lane='two',prompt='Build {{A.note}}'),node('b-gate',lane='two',after='b',verifies='b',independent=True,prompt='Judge {{prev}}')])
        ir['argDefaults']={'note':'old'}
        source=execute(ir)['result']
        state={'_memo':{},'_prefix':[dict(label=n['id'],cost={'minutes':m,'tokens':m*100},promptSha256='0'*64,optsSha256='0'*64) for n,m in zip(ir['nodes'],[2,3,5,7])]}
        helpers={'plugin_identity':c._sibling('plugin_identity'), 'replay_memo':SimpleNamespace(build_memo=lambda *a,**kw:state),
                 'run_artifacts':SimpleNamespace(read=lambda *a,**kw:({'summary':source},'f'*64))}
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git','init','-q',tmp],check=True)
            subprocess.run(['git','-C',tmp,'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','--allow-empty','-qm','init'],check=True)
            with patch.object(c,'_sibling',side_effect=helpers.__getitem__):
                rows=c.rerun_plan(ir,CONTRACTS,{'note':'changed'},tmp,'source-run')
        self.assertEqual([row['serialStatus'] for row in rows],['prefix','prefix','live','live'])
        comparison=rows[0]['comparison']
        self.assertEqual(comparison['optIns'],{'nodes':4,'knownMinutes':17,'unknownMinutes':0})
        self.assertEqual(comparison['serial'],{'nodes':2,'knownMinutes':12,'unknownMinutes':0})
        self.assertIn('4 vs 2 live nodes',rows[0]['warning'])

    def test_plan_excludes_unreached_rows_from_measured_cost(self):
        ir=graph([node('a',lane='one',haltWhen='exit != 0'),node('later',lane='one',after='a',prompt='Use {{prev}}')])
        source=execute(ir,body="if(opts.label==='a') result.exit=1;")['result']
        state={'_memo':{},'_prefix':[dict(label=n['id'],cost={'minutes':m,'tokens':m*100},promptSha256='0'*64,optsSha256='0'*64) for n,m in zip(ir['nodes'],[2,100])]}
        helpers={'plugin_identity':c._sibling('plugin_identity'),'replay_memo':SimpleNamespace(build_memo=lambda *a,**kw:state),
                 'run_artifacts':SimpleNamespace(read=lambda *a,**kw:({'summary':source},'f'*64))}
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git','init','-q',tmp],check=True)
            subprocess.run(['git','-C',tmp,'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','--allow-empty','-qm','init'],check=True)
            with patch.object(c,'_sibling',side_effect=helpers.__getitem__):
                rows=c.rerun_plan(ir,CONTRACTS,{},tmp,'source-run')
        self.assertEqual(rows[1]['serialStatus'],'not reached')
        self.assertEqual(rows[0]['comparison']['optIns'],{'nodes':1,'knownMinutes':2,'unknownMinutes':0})


if __name__=='__main__': unittest.main()
