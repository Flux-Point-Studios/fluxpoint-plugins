#!/usr/bin/env python3
"""Executable regressions for compiler, park and execution evidence cleanup."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'
sys.path.insert(0, str(SCRIPTS))

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

cg = module('cleanup_compile', SCRIPTS / 'compile-graph.py')
rr = module('cleanup_record', SCRIPTS / 'record-run.py')
import replay_memo
import attest
CONTRACTS = cg.load_contracts(str(PLUGIN / 'contracts'))

class Cleanup(unittest.TestCase):
    def test_wait_names_preserve_identity(self):
        campaign = 'campaign/' + 'long-' * 40
        names = [rr.wait_filename(campaign, n) for n in ['park-a', 'park-b', 'park/a']]
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(len(n) < 180 for n in names))
        self.assertNotEqual(names[0], rr.wait_filename(campaign + 'other', 'park-a'))

    def test_blocked_park_does_not_request_another_release(self):
        release = {'instructions': 'Read the receipt', 'whyNotAgent': 'Only the operator has the physical token', 'proofContract': 'HarnessCheckV1'}
        ir = {'version': 1, 'name': 'park', 'campaign': 'two parks', 'treeGuard': False, 'budget': {'maxNodes': 20},
              'nodes': [{'id': 'one', 'actor': 'human', 'contract': 'HarnessCheckV1', 'prompt': 'first', 'release': release, 'wake': {'check': 'true', 'everyMinutes': 5}},
                        {'id': 'two', 'actor': 'human', 'after': 'one', 'contract': 'HarnessCheckV1', 'prompt': 'second {{prev}}', 'release': release, 'wake': {'check': 'true', 'everyMinutes': 5}}]}
        script = cg.emit(ir, CONTRACTS).replace('export const meta', 'const meta')
        wrapper = "const args={_releases:{}}, budget={total:null,spent:()=>0,remaining:()=>1e9}; let calls=0; const agent=async()=>{calls++;return {chosen:'wait',rationale:'physical token'}}; const log=()=>{},phase=()=>{},parallel=async ts=>Promise.all(ts.map(t=>t())),pipeline=async()=>[]; const workflow=0; (async()=>{" + script + "})().then(r=>process.stdout.write(JSON.stringify({r,calls})))"
        result = subprocess.run(['node', '-'], input=wrapper, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['calls'], 1)
        self.assertEqual([w['node'] for w in value['r']['waits']], ['one'])
        self.assertEqual(set(value['r']['blocked']), {'one', 'two'})

    def test_irreversible_prompt_is_emitted_once(self):
        marker = 'UNIQUE CEREMONY PROMPT ebc9304301'
        ir = {'version': 1, 'name': 'once', 'campaign': 'once', 'treeGuard': False, 'budget': {'maxNodes': 20},
              'nodes': [{'id': 'one', 'irreversible': True, 'contract': 'HarnessCheckV1', 'prompt': marker}]}
        self.assertEqual(cg.emit(ir, CONTRACTS).count(marker), 1)

    def test_reducer_is_an_ancestry_edge_and_redteam_terminal(self):
        ir = cg.extract_ir((PLUGIN / 'templates/WORK.verified.md').read_text())
        by_id = {n['id']: n for n in ir['nodes']}
        by_id['red-team-lenses']['after'] = ['proof-audit', 'gate-proof']
        by_id['red-team-lenses']['prompt'] += ' Inputs {{prev.proof-audit}} and {{prev.gate-proof}}.'
        self.assertIn('red-team-lenses', cg.ancestors(by_id['red-team'], by_id))
        script = cg.emit(ir, CONTRACTS)
        line = next(l for l in script.splitlines() if l.startswith('const RED_TEAM_TERMINALS ='))
        terminals = json.loads(line.split('=', 1)[1])
        self.assertIn('red-team-lenses', terminals['red-team']['sources'])
        self.assertIn('red-team-distinct', terminals['red-team']['stages'])

    def test_emit_refuses_undeclared_probe_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / 'WORK.md'
            ir = {'version': 1, 'name': 'probe', 'campaign': 'probe', 'treeGuard': False, 'budget': {'maxNodes': 20},
                  'nodes': [{'id': 'one', 'isolation': 'worktree', 'contract': 'DesignV1', 'prompt': 'Run `python scripts/build.py`.'}]}
            graph.write_text('```json graph-ir\n' + json.dumps(ir) + '\n```\n')
            output = Path(tmp) / 'out.js'
            run = subprocess.run([sys.executable, str(SCRIPTS / 'compile-graph.py'), str(graph), '--gates-root', tmp, '-o', str(output)], text=True, capture_output=True)
            self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('no probe declaration', run.stderr)
            self.assertFalse(output.exists())

    def test_await_command_in_emitted_preamble_parses(self):
        ir = {'version': 1, 'name': 'gate', 'campaign': 'gate', 'treeGuard': False, 'budget': {'maxNodes': 20},
              'nodes': [{'id': 'gate', 'contract': 'ExecutionV1', 'verify': 'prove:harness', 'prompt': 'Run gate'}]}
        script = cg.emit(ir, CONTRACTS)
        self.assertIn('${ATTEST} --await <token>${ROOT_ARG}', script)
        declarations = '\n'.join(line for line in script.splitlines()
                                 if line.startswith(('const PLUGIN =', 'const quoteShell =', 'const ATTEST =')))
        emitted = subprocess.run(['node', '-e', declarations + '\nconsole.log(ATTEST)'],
                                 capture_output=True, text=True, check=True).stdout.strip()
        for root in ['', " --root '/tmp/project with space'"]:
            import shlex
            command = shlex.split(emitted + ' --await bg_0123' + root)
            self.assertEqual(command[0], 'bash')
            self.assertTrue(command[1].endswith('/scripts/py.sh'))
            self.assertEqual(command[2], 'attest.py')
            argv = command[3:]
            parser = "import argparse,json,runpy,sys; p=argparse.ArgumentParser.parse_args; argparse.ArgumentParser.parse_args=lambda self,*a,**k:(print(json.dumps(vars(p(self,*a,**k)))),sys.exit(0))[0]; runpy.run_path(sys.argv.pop(1),run_name='__main__')"
            run = subprocess.run([sys.executable, '-c', parser, str(SCRIPTS / 'attest.py'), *argv], text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            parsed = json.loads(run.stdout)
            self.assertEqual(parsed['await_'], 'bg_0123')
            self.assertEqual(json.loads(parsed['plugin_pin'])['root'], str(SCRIPTS.parent))

    def test_local_citation_must_match_judged_commit(self):
        head, tree = 'a' * 40, 'b' * 40
        row = dict(attestId='att_test', gate='harness', exit=0, dirty=False, witness='wrapper', headSha=head, treeSha=tree, started='2026-10-28T01:00:00Z', nonce='n')
        launch = dict(since='2026-10-28T00:00:00Z', nonce='n')
        for sha, wanted in [(tree, 'ATTESTED'), (head, 'MISMATCH'), (tree[:7], 'MISMATCH')]:
            result = attest._check_cited([row], 'gate', 'harness', dict(attestId='att_test', exit=0, sha=sha), launch, head, {})
            self.assertEqual(result['status'], wanted, result)

    def test_prestart_failure_with_no_agent_is_not_a_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'project/session/subagents/workflows/wf_test'
            directory.mkdir(parents=True)
            rows = [dict(type='failed', key='v2:ab', agentId=''), dict(type='started', key='v2:ab', agentId='agent1'), dict(type='failed', key='v2:ab', agentId='agent1')]
            (directory / 'journal.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in rows))
            calls = replay_memo._journal_calls('wf_test', tmp)
            self.assertEqual([c['agentId'] for c in calls], ['agent1'])
            rows[0]['type'] = 'result'
            (directory / 'journal.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in rows))
            with self.assertRaises(replay_memo.ReplayError):
                replay_memo._journal_calls('wf_test', tmp)

if __name__ == '__main__': unittest.main()
