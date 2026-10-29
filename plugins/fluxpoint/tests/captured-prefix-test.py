#!/usr/bin/env python3
"""Execute captured replay against local native-format evidence, without a provider."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import replay_memo as replay
import run_artifacts
import resume


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runtime = module('captured_runtime_fixture', TESTS / 'graph-replay-test.py')
spec_fixture = module('captured_spec_fixture', TESTS / 'specification-test.py')
compiler = runtime.c


class CapturedPrefix(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / '.claude/runtime/projects'
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.projects.parent), PYTHONIOENCODING='utf-8')
        self.ir = runtime.graph([
            runtime.node('prepare'),
            runtime.node('check', after='prepare', verifies='prepare', prompt='Check {{prev}}', independent=True, haltWhen='exit != 0'),
            runtime.node('ceremony', irreversible=True, after='check', prompt='Perform once with {{prev}} and {{A.intent}}'),
            runtime.node('gate', after='ceremony', independent=True, verifies='ceremony', haltWhen='exit != 0', prompt='Audit {{prev}}'),
            runtime.node('repair', prompt='Repair {{A.note}}')])
        self.ir.update(name='captured', requiredArgs=['confirm'], argDefaults={'intent':'release', 'note':'old'})
        self.git('init','-q'); self.git('config','user.name','Fixture'); self.git('config','user.email','fixture@example.test')
        (self.root / '.gitignore').write_text('.claude/\n', encoding='utf-8')
        contracts = self.root / 'contracts'; contracts.mkdir()
        (contracts / 'TestV1.schema.json').write_text(json.dumps(dict(runtime.CONTRACTS['TestV1'], **{'$id':'TestV1'})), encoding='utf-8')
        (self.root / '.fluxpoint-spec.json').write_text(json.dumps(spec_fixture.packet()), encoding='utf-8')
        self.cli('specification.py','--root',self.root,'--lock')
        self.graph = self.root / 'WORK.md'
        self.write_graph()
        self.git('add','.'); self.git('commit','-qm','fixture base')

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*map(str,args)],text=True,encoding='utf-8',stderr=subprocess.PIPE).strip()

    def write_graph(self):
        self.graph.write_text('STATUS: READY\nSPEC: .fluxpoint-spec.json\nCONTRACTS: contracts\n```json graph-ir\n'+json.dumps(self.ir)+'\n```\n\n## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n',encoding='utf-8')

    def cli(self, script, *args, want=0):
        out = subprocess.run([sys.executable,str(SCRIPTS / script),*map(str,args)],cwd=self.root,env=self.env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(out.returncode,want,out.stdout+'\n'+out.stderr)
        return out

    def initial(self):
        args = self.root / '.claude/initial.json'; args.parent.mkdir(exist_ok=True)
        args.write_text(json.dumps({'confirm':'ceremony'}),encoding='utf-8')
        return json.loads(self.cli('resume.py','--root',self.root,'--graph',self.graph,'--args',args).stdout)

    def execute(self, workflow, **kwargs):
        return runtime.execute(self.ir,args=workflow['args'],compiled_source=Path(workflow['scriptPath']).read_text(encoding='utf-8'),**kwargs)

    def seal(self, run_id, out, foreign=None):
        directory = self.projects / 'project/session/subagents/workflows' / run_id
        directory.mkdir(parents=True,exist_ok=True)
        events = [dict(type='launched')]
        for i,call in enumerate(out['rawCalls']):
            aid='native'+str(i)
            events += [dict(type='started',agentId=aid,opts=call['opts']),dict(type='result',agentId=aid,result=call.get('result'))]
            # The runtime fixture stores returned results in CALL_RECORDS.
            receipt=next(r for r in out['result']['callRecords'] if r['mode']=='live' and r['prompt']==call['prompt'] and r['opts']==call['opts'])
            events[-1]['result']=receipt['result']
            if foreign == call['opts']['label']: events[-1]['foreignArtifactContent']=True
            (directory/('agent-'+aid+'.jsonl')).write_text(json.dumps(dict(type='user',message=dict(role='user',content=call['prompt'])))+'\n',encoding='utf-8')
        (directory/'journal.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events),encoding='utf-8')
        path=self.root/'.claude/result.json'; path.write_text(json.dumps(out['result']),encoding='utf-8')
        recorded=self.cli('record-run.py','--root',self.root,'--graph',self.graph,'--run-id',run_id,'--result',path)
        artifact,digest=run_artifacts.read(self.root,run_id)
        self.assertNotEqual(artifact.get('outcome'),'REFUSED-REPLAY',recorded.stdout+recorded.stderr)
        return artifact,digest

    def assemble(self, run_id, note=None):
        args=['--root',self.root,'--graph',self.graph,'--run',run_id,'--projects',self.projects]
        if note is not None:
            path=self.root/'.claude/note.txt'; path.write_text(note,encoding='utf-8');args+=['--set','note=@'+str(path)]
        out=self.cli('resume.py',*args)
        return json.loads(out.stdout),out

    def raw_source(self, values, foreign=None):
        """Native-format source values, including schema-less agent results."""
        run='wf_values'; directory=self.projects/'project/session/subagents/workflows'/run
        directory.mkdir(parents=True,exist_ok=True)
        calls=[]; events=[dict(type='launched')]
        launch={'nonce':'source-values','root':str(self.root)}
        for i,value in enumerate(values):
            call=dict(prompt='Read '+str(i),opts={'label':'read-'+str(i)},result=value,mode='live')
            calls.append(call);aid='value'+str(i)
            events += [dict(type='started',agentId=aid,opts=call['opts']),dict(type='result',agentId=aid,result=value)]
            if i==foreign: events[-1]['foreignArtifactContent']=True
            (directory/('agent-'+aid+'.jsonl')).write_text(json.dumps(dict(type='user',message=dict(role='user',content=call['prompt'])))+'\n',encoding='utf-8')
        (directory/'journal.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events),encoding='utf-8')
        summary=dict(campaign=self.ir['campaign'],launch=launch,callRecords=calls,memoRecords=[],results={})
        _,digest=run_artifacts.write(self.root,run,dict(runId=run,summary=summary,graph=str(self.graph)))
        return run,launch,digest,directory,summary

    def build(self, source):
        run,launch,digest,*_=source
        return replay.build_memo(self.root,run,launch,compiler.compiler_version(),projects=self.projects,artifact_sha256=digest)

    def test_plain_irreversible_resume_preserves_all_slots_and_current_journal_can_be_empty(self):
        first=self.initial(); original=self.execute(first)
        self.assertEqual(original['result']['outcome'],'COMPLETE')
        self.assertIn('callRecords',original['result'])
        self.seal('wf_first',original)
        second,_=self.assemble('wf_first')
        self.assertNotIn('resumeFromRunId',second)
        self.assertEqual(second['args']['_resumeFromRunId'],'wf_first')
        self.assertEqual(second['args']['_launch'],first['args']['_launch'])
        self.assertFalse(any(key in second['args'] for key in ('_prefix','_memo','_capture','_ledgerCalls')))
        out=self.execute(second)
        self.assertEqual(out['result']['outcome'],'COMPLETE')
        self.assertEqual(out['rawCalls'],[])
        self.assertEqual(len(out['result']['callRecords']),len(original['result']['callRecords']))
        self.assertTrue(all(row['mode']=='prefix' for row in out['result']['callRecords']))
        self.assertEqual(out['result']['ledgerReplays'][0]['verified'],True)
        self.seal('wf_second',out)
        third,_=self.assemble('wf_second')
        out3=self.execute(third)
        self.assertEqual(out3['rawCalls'],[])
        self.seal('wf_third',out3)

    def test_changed_suffix_runs_live_in_fresh_outer_run_and_effect_never_repeats(self):
        original=self.execute(self.initial());self.seal('wf_first',original)
        second,_=self.assemble('wf_first','new repair')
        out=self.execute(second)
        self.assertEqual([row['opts']['label'] for row in out['rawCalls']],['repair','tree-check:campaign-end'])
        self.assertNotIn('resumeFromRunId',second)
        self.seal('wf_second',out)
        third,_=self.assemble('wf_second')
        out3=self.execute(third)
        self.assertEqual(out3['rawCalls'],[])
        self.seal('wf_third',out3)

    def test_prompt_upgrade_with_carried_confirm_refuses_without_rearming(self):
        original=self.execute(self.initial());self.seal('wf_first',original)
        self.ir['nodes'][2]['prompt'] += ' Updated compiler isolation instruction.'
        self.write_graph()
        second,_=self.assemble('wf_first')
        self.assertEqual(second['args']['_resumeFromRunId'],'wf_first')
        out=self.execute(second)
        self.assertEqual(out['result']['outcome'],'LEDGER-CONFLICT')
        self.assertNotIn('ceremony',[row['opts']['label'] for row in out['rawCalls']])

    def test_fresh_effect_decoration_retains_ledger_history_without_false_conflict(self):
        self.ir['nodes'][2]['fresh']=True;self.write_graph()
        first=self.initial();original=self.execute(first)
        self.assertEqual(original['result']['outcome'],'COMPLETE')
        _,digest=self.seal('wf_first',original)
        state=replay.build_memo(self.root,'wf_first',first['args']['_launch'],compiler.compiler_version(),self.projects,digest)
        key=original['result']['ledger'][0]['key']
        self.assertEqual(list(state['_ledgerCalls']),[key])
        self.assertTrue(state['_ledgerCalls'][key]['historyOnly'])
        second,_=self.assemble('wf_first');out=self.execute(second)
        self.assertEqual(out['result']['outcome'],'COMPLETE')
        self.assertNotIn('ceremony',[row['opts']['label'] for row in out['rawCalls']])
        self.assertIn('repair',[row['opts']['label'] for row in out['rawCalls']])
        self.assertFalse(out['result']['ledgerReplays'][0]['verified'])
        self.seal('wf_second',out)
        third,_=self.assemble('wf_second');out3=self.execute(third)
        self.assertEqual(out3['result']['outcome'],'COMPLETE')
        self.assertNotIn('ceremony',[row['opts']['label'] for row in out3['rawCalls']])
        self.seal('wf_third',out3)

    def test_completed_effect_without_accepted_ledger_write_cannot_repeat_after_prefix_miss(self):
        for decorated,tainted in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(decorated=decorated,tainted=tainted):
                self.ir['nodes'][2]['fresh']=decorated;self.write_graph()
                first=self.initial();original=self.execute(first)
                run='wf_unaccepted_'+str(decorated)+'_'+str(tainted)
                artifact,_=self.seal(run,original,foreign='ceremony' if tainted else None)
                # Model an outer result guard refusing a completed native
                # effect before it can become an accepted ledger write.
                artifact['summary']['ledger']=[]
                artifact['summary']['results']['ceremony']=None
                artifact['summary']['outcome']='WRONG-BASE'
                run_artifacts.write(self.root,run,artifact)
                (self.root/'.claude/fluxpoint/irreversible.jsonl').unlink()
                self.ir['nodes'][0]['prompt']+=' Force earlier prefix miss.'
                self.write_graph()
                second,_=self.assemble(run);out=self.execute(second)
                self.assertEqual(out['result']['outcome'],'LEDGER-CONFLICT')
                self.assertNotIn('ceremony',[row['opts']['label'] for row in out['rawCalls']])

    def test_native_completed_null_effect_still_prevents_automatic_repetition(self):
        first=self.initial();original=self.execute(first,body="if(opts.label==='ceremony') result=null;")
        self.assertEqual(original['result']['outcome'],'NODE-DEAD')
        self.assertEqual(original['result']['ledger'],[])
        self.seal('wf_null_effect',original)
        self.ir['nodes'][0]['prompt']+=' Force earlier prefix miss.';self.write_graph()
        second,_=self.assemble('wf_null_effect');out=self.execute(second)
        self.assertEqual(out['result']['outcome'],'LEDGER-CONFLICT')
        self.assertNotIn('ceremony',[row['opts']['label'] for row in out['rawCalls']])

    def test_conflicting_ledger_result_refuses_without_rearming(self):
        original=self.execute(self.initial());self.seal('wf_first',original)
        path=self.root/'.claude/fluxpoint/irreversible.jsonl'
        row=json.loads(path.read_text());row['result']['exit']=7;path.write_text(json.dumps(row)+'\n')
        second,_=self.assemble('wf_first')
        out=self.execute(second)
        self.assertEqual(out['result']['outcome'],'LEDGER-CONFLICT')
        self.assertEqual(out['rawCalls'],[])

    def test_exact_strings_order_prototype_keys_and_deduplication(self):
        ordered={'z':1,'a':2,'__proto__':{'own':True}}
        values=['plain text','123','{}','null','',False,0,ordered,ordered,{'a':2,'z':1,'__proto__':{'own':True}}]
        source=self.raw_source(values); state=self.build(source)
        restored=[replay.capsule_result(state,row) for row in state['_prefix']]
        self.assertEqual(restored,values)
        self.assertEqual([type(v) for v in restored],[type(v) for v in values])
        self.assertEqual(list(restored[7]),list(ordered))
        self.assertEqual(len(state['_capture']['results']),len(values)-1)
        self.assertNotEqual(state['_prefix'][7]['resultRef'],state['_prefix'][9]['resultRef'])
        ir=runtime.graph([runtime.node('unused',fresh=False)])
        js=compiler.emit(ir,runtime.CONTRACTS,replay_state=state)
        self.assertIn('const REPLAY_STATE = JSON.parse(',js)
        # Execute the literal capsule, including own __proto__, through Node.
        declaration=next(line for line in js.splitlines() if line.startswith('const REPLAY_STATE = '))
        encoded=state['_capture']['results'][state['_prefix'][7]['resultRef']]
        probe=declaration+'\nconst value=JSON.parse(REPLAY_STATE._capture.results['+json.dumps(state['_prefix'][7]['resultRef'])+']);console.log(JSON.stringify([Object.keys(value),Object.hasOwn(value,"__proto__"),value.__proto__.own]));'
        proc=subprocess.run(['node','-'],input=probe,text=True,encoding='utf-8',capture_output=True,check=True)
        self.assertEqual(json.loads(proc.stdout),[list(ordered),True,True])

    def test_taint_missing_and_failed_source_calls_fence_capture(self):
        state=self.build(self.raw_source(['safe','external','later'],foreign=1))
        self.assertEqual(len(state['_prefix']),1)
        self.assertIn('foreign-content',state['_capture']['stopReason'])
        source=self.raw_source(['safe',None,'later'])
        self.assertEqual(len(self.build(source)['_prefix']),1)
        (source[3]/'agent-value0.jsonl').unlink()
        with self.assertRaises(replay.ReplayError):self.build(source)

    def test_cap_preserves_complete_values_and_bounds_metadata(self):
        state=self.build(self.raw_source(['small','x'*(replay.CAPTURE_LIMIT+1),'later']))
        self.assertEqual(len(state['_prefix']),1)
        self.assertEqual(replay.capsule_result(state,state['_prefix'][0]),'small')
        self.assertLessEqual(len(json.dumps(state,ensure_ascii=True,separators=(',',':')).encode()),replay.CAPTURE_LIMIT)
        self.assertIn('limit',state['_capture']['stopReason'])
        state=self.build(self.raw_source(['same']*700))
        self.assertLessEqual(len(json.dumps(state,ensure_ascii=True,separators=(',',':')).encode()),replay.CAPTURE_LIMIT)
        self.assertEqual(len(state['_capture']['results']),1)
        self.assertLess(len(state['_prefix']),700)

    def test_source_and_capsule_tampering_refuse_before_launch(self):
        source=self.raw_source(['trusted']);state=self.build(source)
        for attack in ('value','missing','parent','version'):
            with self.subTest(attack=attack):
                changed=copy.deepcopy(state)
                if attack=='value':changed['_capture']['results'][changed['_prefix'][0]['resultRef']]='"forged"'
                elif attack=='missing':changed['_capture']['results']={}
                elif attack=='parent':changed['_prefix'][0]['callIndex']=1
                else:changed['_capture']['version']=99
                with self.assertRaises(ValueError):replay.verify_capture_state(self.root,changed,compiler.compiler_version(),self.projects)
                path=self.root/'.claude/forged-state.json';path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(changed))
                self.cli('compile-graph.py',self.graph,'--check','--gates-root',self.root,'--projects',self.projects,'--replay-state',path,want=1)
        source[3].joinpath('journal.jsonl').write_text('{"type":"started","agentId":"one","agentId":"two"}\n')
        with self.assertRaisesRegex(replay.ReplayError,'duplicate JSON key'):self.build(source)
        with self.assertRaises(ValueError):replay.strict_json('{"v":NaN}')
        with self.assertRaises(ValueError):replay.strict_json('{"v":1e999}')

    def test_fresh_gate_and_rejudge_are_not_locally_replayed(self):
        self.ir['nodes'][3]['fresh']=True;self.write_graph()
        original=self.execute(self.initial());self.seal('wf_first',original)
        second,_=self.assemble('wf_first');out=self.execute(second)
        labels=[row['opts']['label'] for row in out['rawCalls']]
        self.assertIn('tree-check:before gate',labels);self.assertIn('gate',labels);self.assertNotIn('ceremony',labels)
        self.seal('wf_second',out)
        request=self.root/'.claude/rejudge.json';request.write_text(json.dumps({'gate':{'nonce':'again','reason':'verify current state','note':'operator note'}}))
        third=json.loads(self.cli('resume.py','--root',self.root,'--graph',self.graph,'--run','wf_second','--projects',self.projects,'--set-json','rejudge=@'+str(request)).stdout)
        out=self.execute(third)
        self.assertIn('gate',[row['opts']['label'] for row in out['rawCalls']])
        self.assertNotIn('ceremony',[row['opts']['label'] for row in out['rawCalls']])

    def test_ledger_store_key_order_cannot_break_later_captured_prompts(self):
        original=self.execute(self.initial());self.seal('wf_first',original)
        path=self.root/'.claude/fluxpoint/irreversible.jsonl'
        row=json.loads(path.read_text());row['result']=dict(reversed(list(row['result'].items())))
        path.write_text(json.dumps(row)+'\n')
        second,_=self.assemble('wf_first');out=self.execute(second)
        self.assertEqual(out['rawCalls'],[])
        self.seal('wf_second',out)
        third,_=self.assemble('wf_second');self.assertEqual(self.execute(third)['rawCalls'],[])

    def test_missing_or_false_ledger_receipts_and_selected_parent_refuse_recording(self):
        original=self.execute(self.initial());self.seal('wf_first',original)
        second,_=self.assemble('wf_first');out=self.execute(second)
        # Create the legitimate empty current native journal first.
        directory=self.projects/'project/session/subagents/workflows/wf_second';directory.mkdir(parents=True)
        (directory/'journal.jsonl').write_text('{"type":"launched"}\n')
        for attack in ('missing-ledger','source-type','parent-type','same-run','ordinary-node','downgrade'):
            with self.subTest(attack=attack):
                self.ir['nodes'][2]['irreversible']=True;self.write_graph()
                summary=copy.deepcopy(out['result'])
                if attack=='missing-ledger':summary['ledgerReplays']=[]
                elif attack=='source-type':summary['callRecords'][0]['source']='bad'
                elif attack=='parent-type':summary['resumeSource']=['bad']
                elif attack=='same-run':summary['resumeSource']['runId']='wf_second'
                elif attack=='downgrade':summary.pop('resumeMode')
                else:
                    self.ir['nodes'][2]['irreversible']=False;self.write_graph()
                with self.assertRaises(ValueError):
                    validated=replay.validate_current(self.root,summary,'wf_second',self.projects,self.graph)
                    replay.verify_replays(self.root,summary,self.projects,self.graph,'wf_second',validated)

    def test_lost_ledger_and_capture_cap_never_drop_known_effect_protection(self):
        first=self.initial();original=self.execute(first);_,digest=self.seal('wf_first',original)
        (self.root/'.claude/fluxpoint/irreversible.jsonl').unlink()
        state=replay.build_memo(self.root,'wf_first',first['args']['_launch'],compiler.compiler_version(),self.projects,digest)
        with mock.patch.object(replay,'CAPTURE_LIMIT',3000):
            limited=replay.build_memo(self.root,'wf_first',first['args']['_launch'],compiler.compiler_version(),self.projects,digest)
        key=original['result']['ledger'][0]['key']
        self.assertIn(key,limited['_ledgerCalls'])
        self.assertLess(len(limited['_prefix']),len(original['result']['callRecords']))
        self.assertLessEqual(len(json.dumps(limited,ensure_ascii=True,separators=(',',':')).encode()),3000)
        spec_line=next(line for line in Path(first['scriptPath']).read_text().splitlines() if line.startswith('const SPECIFICATION = '))
        specification=json.loads(spec_line.removeprefix('const SPECIFICATION = '))
        for capsule in (state,limited):
            with self.subTest(limit=capsule['_capture']['limitBytes']):
                program=compiler.emit(self.ir,runtime.CONTRACTS,specification=specification,embedded_args=original['result']['inputs'],replay_state=capsule)
                out=runtime.execute(self.ir,args=dict(first['args'],_ledger={}),compiled_source=program)
                self.assertEqual(out['result']['outcome'],'LEDGER-CONFLICT')
                self.assertNotIn('ceremony',[row['opts']['label'] for row in out['rawCalls']])

    def test_legacy_source_degrades_to_fresh_suffix_and_retains_ledger_safety(self):
        first=self.initial();original=self.execute(first);artifact,_=self.seal('wf_first',original)
        artifact['summary'].pop('callRecords');artifact['summary'].pop('memoRecords')
        run_artifacts.write(self.root,'wf_first',artifact)
        second,diagnostic=self.assemble('wf_first')
        self.assertNotIn('resumeFromRunId',second)
        self.assertIn('legacy source',diagnostic.stderr)
        out=self.execute(second)
        self.assertNotIn('ceremony',[row['opts']['label'] for row in out['rawCalls']])
        self.assertTrue(any(row['status']=='LEGACY-LEDGER' for row in out['result']['provenance']))
        self.assertFalse(out['result']['ledgerReplays'][0]['verified'])
        self.seal('wf_second',out)

    def test_fork_closes_captured_prefix_and_whole_script_limit_remains_authoritative(self):
        self.ir['nodes']=[runtime.node('a',lane='one'),runtime.node('b',lane='two')]
        self.write_graph()
        first=self.initial();original=self.execute(first);self.seal('wf_first',original)
        second,_=self.assemble('wf_first');out=self.execute(second)
        self.assertIn('tree-check:fork 1',[row['opts']['label'] for row in out['rawCalls']])
        self.assertEqual(out['result']['callRecords'][0]['execution'],'captured')
        self.assertTrue(all(row['mode']=='live' for row in out['result']['callRecords'][1:]))
        result=self.cli('compile-graph.py',self.graph,'--check','--gates-root',self.root,'--max-script-bytes','100',want=1)
        self.assertIn('script rejected',result.stderr)


if __name__ == '__main__':
    unittest.main()
