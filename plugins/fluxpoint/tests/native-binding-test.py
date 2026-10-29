#!/usr/bin/env python3
"""Actual capture files bind fixed command output independently of the worker."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class NativeBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / 'projects'
        self.run, self.agent = 'wf_native', 'worker1'
        self.directory = self.projects / 'project/session/subagents/workflows' / self.run
        self.directory.mkdir(parents=True)
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        self.report = {'commands': ['printf data'], 'readings': [{'exit': 0, 'output': 'data\n'}]}
        self.events = [dict(type='started', agentId=self.agent, label='fixed-check'),
                       dict(type='result', agentId=self.agent, result=self.report)]
        self.rows = [dict(type='assistant', timestamp='2026-10-28T12:00:00Z', cwd=str(self.worktree),
                         message={'role':'assistant','content':[dict(type='tool_use', id='bash1', name='Bash', input={'command':'printf data'})]}),
                     dict(type='assistant', message={'role':'assistant','content':[dict(type='tool_use', id='bash1', name='Bash', input={'command':'printf data'})]}),
                     dict(type='user', message={'role':'user','content':[dict(type='tool_result',tool_use_id='bash1',content='data\n')]}),
                     dict(type='user', message={'role':'user','content':[dict(type='tool_result',tool_use_id='bash1',content='data\n')]}),
                     dict(type='assistant', cwd=str(self.worktree), message={'role':'assistant','content':[dict(type='tool_use',id='final',name='StructuredOutput',input=self.report)]})]
        self.save()

    def save(self):
        (self.directory / 'journal.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in self.events))
        (self.directory / ('agent-'+self.agent+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in self.rows))
        (self.directory / ('agent-'+self.agent+'.meta.json')).write_text(json.dumps({'worktreePath':str(self.worktree)}))

    def bind(self):
        import native_binding
        self.save()
        return native_binding.bind(self.run, self.agent, projects=self.projects)

    def test_first_tool_occurrence_keeps_timestamped_cwd_and_ids(self):
        result=self.bind()
        self.assertTrue(result['bound'])
        self.assertEqual(result['commands'][0]['toolUseId'],'bash1')
        self.assertTrue(result['commands'][0]['strictEqual'])

    def test_fixed_line_end_exit_and_empty_normalizations_are_visible(self):
        self.report['readings'][0]={'exit':7,'output':'data\n'}
        for row in self.rows:
            for item in row['message']['content']:
                if item['type']=='tool_result':item.update(content='Exit code 7\r\ndata\r\n',is_error=True)
        result=self.bind()
        self.assertTrue(result['bound'])
        self.assertFalse(result['commands'][0]['strictEqual'])
        self.assertIn('exit-prefix', result['commands'][0]['normalizations'])
        self.report['readings'][0]={'exit':0,'output':''}
        for row in self.rows:
            for item in row['message']['content']:
                if item['type']=='tool_result':item.update(content='(Bash completed with no output)',is_error=False)
        self.assertTrue(self.bind()['bound'])

    def test_changed_command_output_exit_cwd_or_duplicate_refuses(self):
        import native_binding
        for mutation in ('command','output','exit','cwd','duplicate'):
            with self.subTest(mutation=mutation):
                rows=copy.deepcopy(self.rows)
                if mutation=='command':
                    for row in self.rows:
                        for item in row['message']['content']:
                            if item.get('type')=='tool_use' and item.get('name')=='Bash':item['input']['command']='printf other'
                if mutation=='output':
                    for row in self.rows:
                        for item in row['message']['content']:
                            if item.get('type')=='tool_result':item['content']='condensed'
                if mutation=='exit':self.report['readings'][0]['exit']=2
                if mutation=='cwd':self.rows[0]['cwd']=str(self.root)
                if mutation=='duplicate':self.rows[1]['message']['content'][0]['input']['command']='printf other'
                with self.assertRaises(native_binding.BindingError):self.bind()
                self.rows=rows;self.report['readings'][0]['exit']=0

    def test_unlisted_tool_background_or_missing_result_refuses(self):
        import native_binding
        for mutation in ('tool','background','missing'):
            with self.subTest(mutation=mutation):
                rows=copy.deepcopy(self.rows)
                if mutation=='tool':self.rows.insert(0,dict(cwd=str(self.worktree),message={'content':[dict(type='tool_use',id='read',name='Read',input={'file_path':'x'})]}))
                if mutation=='background':
                    for row in self.rows:
                        for item in row['message']['content']:
                            if item.get('type')=='tool_use' and item.get('name')=='Bash':item['input']['run_in_background']=True
                if mutation=='missing':self.rows=[r for r in self.rows if r['message']['content'][0]['type']!='tool_result']
                with self.assertRaises(native_binding.BindingError):self.bind()
                self.rows=rows

    def test_artifact_marker_and_bytes_must_both_match(self):
        import native_binding
        data=b'{"rows":[1,2,3]}\n';artifact=self.worktree/'output.json';artifact.write_bytes(data)
        digest=hashlib.sha256(data).hexdigest()
        self.report['readings'][0]={'exit':0,'artifact':{'path':'output.json','sha256':digest}}
        for row in self.rows:
            for item in row['message']['content']:
                if item['type']=='tool_result':item['content']=digest+'  output.json\n'
        self.assertTrue(self.bind()['bound'])
        artifact.write_bytes(b'changed')
        with self.assertRaisesRegex(native_binding.BindingError,'artifact'):self.bind()

    def test_probe_binding_does_not_accept_a_retyped_audit_receipt(self):
        import native_binding
        receipt=dict(original='printf data',safe='printf data',outcome='ACCEPTED',evidence='data\n',exit=0)
        self.report.clear();self.report.update(completed=True,commands=[receipt],findings=[],blockers=[])
        self.events[0]['label']='probe:one'
        audit=dict(mode='multi',verdict='SOUND',probes=[dict(node='one',completed=True,commands=[dict(receipt,kind='read-only')])])
        self.save()
        bound=native_binding.bind_audit(self.run,audit,self.projects)
        self.assertTrue(bound['ready'])
        self.assertTrue(bound['nativeCommandBinding']['bound'])
        audit['probes'][0]['commands'][0]['evidence']='shortened'
        with self.assertRaisesRegex(native_binding.BindingError,'receipts differ'):
            native_binding.bind_audit(self.run,audit,self.projects)

    def test_scalar_fixed_preflight_recording_uses_native_result(self):
        import native_binding
        summary=dict(contracts={'fixed-check':'NativeReadingsV1'},results={'fixed-check':copy.deepcopy(self.report)})
        self.assertEqual(len(native_binding.verify_fixed(summary,self.run,self.projects)),1)
        summary['results']['fixed-check']['readings'][0]['output']='retyped'
        with self.assertRaisesRegex(native_binding.BindingError,'caller report'):
            native_binding.verify_fixed(summary,self.run,self.projects)

    def test_compiler_base_extension_stays_exact_and_closed(self):
        import native_binding
        self.report['baseSeen'] = 'a' * 40
        self.save()
        summary = dict(contracts={'fixed-check':'NativeReadingsV1'}, results={'fixed-check':copy.deepcopy(self.report)}, worktreeBase={'sha':'a' * 40})
        self.assertEqual(native_binding.verify_fixed(summary,self.run,self.projects)[0]['baseSeen'], 'a' * 40)
        summary['worktreeBase']['sha'] = 'b' * 40
        with self.assertRaisesRegex(native_binding.BindingError,'baseSeen'): native_binding.verify_fixed(summary,self.run,self.projects)
        with self.assertRaisesRegex(native_binding.BindingError,'baseSeen'): self.bind()
        self.report['unexpected'] = True; self.save()
        with self.assertRaisesRegex(native_binding.BindingError,'closed'): native_binding.bind(self.run,self.agent,self.projects, expected_base='a' * 40)

    def test_domain_adapter_receipts_keep_exact_command_and_source_ids(self):
        import native_binding
        self.rows.insert(0,dict(type='assistant',cwd=str(self.worktree),message={'role':'assistant','content':[dict(type='tool_use',id='inspection',name='Read',input={'file_path':'source.py'})]}))
        self.save()
        receipts=native_binding.tool_receipts(self.run,self.agent,self.projects)
        self.assertEqual(receipts['calls'][0]['command'],'printf data')
        self.assertEqual(receipts['calls'][0]['toolUseId'],'bash1')
        self.assertEqual(receipts['calls'][0]['output'],'data\n')
        self.assertEqual(receipts['report'],self.report)
        with self.assertRaisesRegex(native_binding.BindingError,'unlisted native tool'):
            native_binding.bind(self.run,self.agent,self.projects)

    def test_unfinished_capture_and_explicit_truncation_refuse(self):
        import native_binding
        self.events.pop();self.save()
        with self.assertRaisesRegex(native_binding.BindingError,'completed'):
            native_binding.bind(self.run,self.agent,self.projects)
        self.events.append(dict(type='result',agentId=self.agent,result=self.report))
        for row in self.rows:
            for item in row['message']['content']:
                if item['type']=='tool_result':item['content']='[output truncated]'
        self.save()
        with self.assertRaisesRegex(native_binding.BindingError,'truncated'):
            native_binding.tool_receipts(self.run,self.agent,self.projects)

    def test_compiler_and_record_run_require_native_fixed_preflight_evidence(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('native_compiler',SCRIPTS/'compile-graph.py')
        compiler=importlib.util.module_from_spec(spec);spec.loader.exec_module(compiler)
        contracts=compiler.load_contracts(str(SCRIPTS.parent/'contracts'))
        ir=dict(version=1,campaign='fixed',treeGuard=False,budget={'maxNodes':4},nodes=[dict(id='fixed-check',isolation='worktree',contract='NativeReadingsV1',prompt='Run the fixed status command.')])
        self.assertEqual(compiler.validate(ir,contracts),[])
        self.assertIn('FIXED NATIVE READINGS',compiler.emit(ir,contracts))
        ir['nodes'][0]['isolation']=False
        self.assertTrue(any('NativeReadingsV1' in value for value in compiler.validate(ir,contracts)))
        graph=self.worktree/'WORK.md'
        graph.write_text('## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        self.report['baseSeen'] = 'a' * 40
        self.save()
        ir['nodes'][0]['isolation'] = 'worktree'
        compiled = compiler.emit(ir, contracts)
        runner = self.root / 'compiled-native.mjs'
        runner.write_text('const args=' + json.dumps({'_base': {'sha':'a' * 40, 'root':str(self.root), 'porcelain':'', 'branch':'main'}}) + ';\n'
                          + 'const agent=async(p,o)=>(' + json.dumps(self.report) + ');\n'
                          + 'const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};\n'
                          + 'const result=await(async()=>{\n' + compiled.replace('export const meta =','const meta =') + '\n})();\nconsole.log(JSON.stringify(result));\n', encoding='utf-8')
        executed = subprocess.run(['node',str(runner)],capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(executed.returncode,0,executed.stderr)
        summary=json.loads(executed.stdout)
        self.assertEqual(summary['outcome'],'COMPLETE')
        self.assertEqual(summary['results']['fixed-check']['baseSeen'],'a' * 40)
        command=[sys.executable,str(SCRIPTS/'record-run.py'),'--root',str(self.worktree),'--graph',str(graph),'--run-id',self.run]
        result=subprocess.run(command,input=json.dumps(summary),capture_output=True,text=True,
                              env=dict(os.environ,CLAUDE_CONFIG_DIR=str(self.root),FPL_MEMORY_INDEX='0'),timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        artifact=json.loads((self.worktree/'.claude/fluxpoint/runs'/(self.run+'.json')).read_text())
        self.assertEqual(artifact['outcome'],'COMPLETE')
        self.assertTrue(artifact['nativeCommandBindings'][0]['bound'])
        self.assertIn('NATIVE-COMMAND-BOUND',(self.worktree/'WORK.md').read_text())

    def test_completion_order_and_boolean_integer_aliases_refuse(self):
        import native_binding
        self.events.reverse();self.save()
        with self.assertRaisesRegex(native_binding.BindingError,'earlier start'):
            native_binding.bind(self.run,self.agent,self.projects)
        self.events.reverse()
        self.rows[-1]['message']['content'][0]['input']=copy.deepcopy(self.report)
        self.rows[-1]['message']['content'][0]['input']['readings'][0]['exit']=False
        self.save()
        with self.assertRaisesRegex(native_binding.BindingError,'StructuredOutput'):
            native_binding.bind(self.run,self.agent,self.projects)

    def test_native_journal_without_labels_uses_the_actual_task_marker(self):
        import native_binding
        self.events[0].pop('label')
        prompt='[Workflow harness — computed task]\nThe computed task text follows:\n\n  Fluxpoint-Native-Seat: fixed-check\n  Run the fixed command.\n'
        self.rows.insert(0,dict(type='user',message={'role':'user','content':prompt}))
        self.save()
        summary=dict(contracts={'fixed-check':'NativeReadingsV1'},results={'fixed-check':self.report})
        self.assertEqual(len(native_binding.verify_fixed(summary,self.run,self.projects)),1)

    def test_cli_reads_source_report_and_refuses_an_external_retyped_report(self):
        self.save()
        cmd=[sys.executable,str(SCRIPTS/'bind-native.py'),'--run-id',self.run,'--agent-id',self.agent,'--projects',str(self.projects)]
        result=subprocess.run(cmd,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['bound'])
        report=self.root/'claimed.json';report.write_text(json.dumps({'commands':['made up'],'readings':[]}))
        result=subprocess.run(cmd+['--report',str(report)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)


if __name__=='__main__':unittest.main()
