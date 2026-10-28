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
                if mutation=='command':self.rows[0]['message']['content'][0]['input']['command']='printf other'
                if mutation=='output':self.rows[2]['message']['content'][0]['content']='condensed'
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
                if mutation=='background':self.rows[0]['message']['content'][0]['input']['run_in_background']=True
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
