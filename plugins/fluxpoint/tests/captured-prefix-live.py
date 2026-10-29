#!/usr/bin/env python3
"""Actual Workflow captured-prefix acceptance with a scripted localhost provider.

No model quality, paid provider, or real-world irreversible effect is asserted.
The effect control counts native worker dispatches for a harmless fixture node.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from claude_local import LocalProvider, run_cli, text_content, text_reply, tool_reply

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('captured_acceptance_fixture',HERE/'captured-prefix-test.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


def objects(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from objects(child)
    elif isinstance(value,list):
        for child in value:yield from objects(child)
    elif isinstance(value,str):
        decoder=json.JSONDecoder()
        for match in re.finditer(r'\{',value):
            try:child,_=decoder.raw_decode(value[match.start():])
            except ValueError:continue
            yield from objects(child)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',default=os.environ.get('FPL_CLAUDE_BINARY') or shutil.which('claude'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        if not args.binary:raise ValueError('set FPL_CLAUDE_BINARY to the installed CLI')
        binary=Path(args.binary).resolve()
        version=subprocess.check_output([str(binary),'--version'],text=True,encoding='utf-8',timeout=10).strip()
        if not version.startswith('2.1.312 '):raise ValueError('acceptance profile requires CLI2.1.312: '+version)
        output=args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='fpl-captured-native-'))
        output.mkdir(parents=True,exist_ok=True)
        case=fixture.CapturedPrefix();case.setUp()
        config=output/'config';case.projects=config/'projects';case.env.update(CLAUDE_CONFIG_DIR=str(config))
        case.ir['treeGuard']=False
        for node in case.ir['nodes']:
            node['prompt']='CAPTURED_PREFIX_NODE: '+node['id']+'. '+node['prompt']+' Return only the harmless scripted fixture object; perform no external action.'
        case.write_graph();case.git('add','WORK.md');case.git('commit','-qm','Native capture control')
        all_workers=[];reports=[]
        first=case.initial()
        original_launch=first['args']['_launch']
        prior=None
        workflow=first
        for attempt in (1,2,3):
            if attempt>1:
                workflow,_=case.assemble(prior,'changed fixture' if attempt==2 else None)
                if 'resumeFromRunId' in workflow:raise ValueError('captured launch asked native Workflow to resume its cache')
                if workflow['args']['_launch']!=original_launch:raise ValueError('captured resume replaced launch provenance')
            turn=[0];workers=[];before=set((config/'projects').glob('*/*/subagents/workflows/wf_*'))
            def respond(body,_index):
                names={tool.get('name') for tool in body.get('tools',[])}
                if 'StructuredOutput' not in names:
                    position=turn[0];turn[0]+=1
                    if position==0:
                        if 'Workflow' not in names:return text_reply('BLOCKED: native Workflow unavailable')
                        return tool_reply('Skill',{'skill':'workflow-authoring'},'main-skill')
                    if position==1:return tool_reply('Workflow',workflow,'main-workflow')
                    return text_reply('Native captures, not this scripted reply, determine acceptance.')
                text='\n'.join(text_content(message.get('content')) for message in body.get('messages',[]) if message.get('role')=='user')
                found=re.search(r'CAPTURED_PREFIX_NODE: ([a-z-]+)\.',text)
                if not found:raise ValueError('native worker did not receive an emitted fixture prompt')
                node=found[1];workers.append(node);all_workers.append(node)
                return tool_reply('StructuredOutput',{'outcome':'committed','exit':0,'sha':'b'*40},'worker-'+str(attempt)+'-'+node)
            artifacts=output/('attempt-'+str(attempt))
            with LocalProvider(respond) as provider:
                invocation=run_cli(binary,case.root,'Run this private compiled Workflow once. All worker replies are scripted localhost data. The ceremony is a harmless dispatch counter, never a real irreversible action. Use the emitted script unchanged and the supplied arguments; no permission bypass or real provider call is authorized.',provider,config,artifacts,['Skill(workflow-authoring)','Workflow','Read'],timeout=90)
            if invocation['exit']!=0:raise ValueError('native CLI failed; inspect '+str(artifacts/'cli.jsonl'))
            runs=set((config/'projects').glob('*/*/subagents/workflows/wf_*'))-before
            if len(runs)!=1:raise ValueError('expected exactly one new native Workflow run')
            run=runs.pop();prior=run.name
            summaries=[value for request in provider.requests for value in objects(request['body'].get('messages',[]))
                       if value.get('campaign')==case.ir['campaign'] and isinstance(value.get('callRecords'),list) and isinstance(value.get('results'),dict)]
            # Native completion notices truncate large summaries and XML-escape
            # prompt text. Read the exact output file named by the CLI's own
            # completion event; never reconstruct a result from that preview.
            for line in (artifacts/'cli.jsonl').read_text(encoding='utf-8').splitlines():
                if not line.startswith('{'):continue
                event=json.loads(line)
                if event.get('subtype')=='task_notification' and event.get('tool_use_id')=='main-workflow' and event.get('status')=='completed':
                    raw=Path(event['output_file']).read_bytes()
                    (artifacts/'workflow-output.json').write_bytes(raw)
                    summaries += [value for value in objects(json.loads(raw)) if value.get('campaign')==case.ir['campaign'] and isinstance(value.get('callRecords'),list) and isinstance(value.get('results'),dict)]
            if not summaries:raise ValueError('native Workflow did not return its complete compiled summary')
            summary=summaries[-1]
            if summary.get('outcome')!='COMPLETE':raise ValueError('native emitted graph did not complete: '+str(summary.get('outcome')))
            result=case.root/'.claude/native-result.json';result.write_text(json.dumps(summary),encoding='utf-8')
            case.cli('record-run.py','--root',case.root,'--graph',case.graph,'--run-id',run.name,'--result',result)
            recorded,_=fixture.run_artifacts.read(case.root,run.name)
            if recorded.get('replayProblems'):raise ValueError('native captured receipts failed independent recording')
            journal=[json.loads(line) for line in (run/'journal.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
            started=[row for row in journal if row.get('type')=='started']
            if len(started)!=len(workers):raise ValueError('provider worker count differs from native journal')
            expected=['prepare','check','ceremony','gate','repair'] if attempt==1 else ['repair'] if attempt==2 else []
            if workers!=expected:raise ValueError('unexpected native workers for attempt'+str(attempt)+': '+repr(workers))
            reports.append({'runId':run.name,'workers':workers,'nativeStarted':len(started),'workflowArgs':workflow,
                            'invocation':invocation,'journal':str(run/'journal.jsonl'),'recordedArtifactSha256':fixture.run_artifacts.read(case.root,run.name)[1]})
        if len({row['runId'] for row in reports})!=3 or all_workers.count('ceremony')!=1:raise ValueError('fresh run IDs or once-only worker dispatch not established')
        report={'version':version,'attempts':reports,'ceremonyNativeDispatches':all_workers.count('ceremony'),
                'dummyKeyOnly':True,'modelQualityOrProviderCostClaim':False,'artifacts':str(output)}
        (output/'acceptance.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        shutil.copytree(case.root/'.claude/fluxpoint',output/'recorded-evidence')
        print(json.dumps(report,sort_keys=True));return 0
    except (OSError,ValueError,AssertionError,StopIteration,KeyError,subprocess.SubprocessError) as exc:
        print('captured-prefix-live: '+str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
