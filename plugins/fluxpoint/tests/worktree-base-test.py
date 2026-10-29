#!/usr/bin/env python3
"""Exact-base compiler controls (#188); actual CLI probe is a separate fixture."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from worktree_base_fixture import cg, CONTRACTS, commands, graph, preamble, SCRIPTS
sys.path.insert(0, str(SCRIPTS))
BASE = {'sha': 'a' * 40, 'root': '/campaign', 'branch': 'main', 'porcelain': ''}


def execute(ir, results, args=None, replay_state=None):
    source = cg.emit(ir, CONTRACTS, replay_state=replay_state).replace('export const meta =', 'const meta =')
    program = '''const fs=require('fs'),input=JSON.parse(fs.readFileSync(0,'utf8')),calls=[],logs=[];
const agent=async(p,o)=>{calls.push({prompt:p,options:o});return input.results[o.label]===undefined?null:input.results[o.label]};
const parallel=async fs=>Promise.all(fs.map(f=>f()));
const pipeline=async(items,...stages)=>Promise.all(items.map(async(item,i)=>{let value=item;for(const stage of stages)value=await stage(value,item,i);return value}));
const Fn=Object.getPrototypeOf(async function(){}).constructor;
new Fn('args','agent','budget','parallel','pipeline','phase','log',input.source)(input.args,agent,{total:null,remaining:()=>1e9,spent:()=>0},parallel,pipeline,()=>{},s=>logs.push(s)).then(summary=>console.log(JSON.stringify({summary,calls,logs}))).catch(e=>{console.error(e.stack);process.exitCode=1});'''
    launch = {'_base': BASE, '_launch': {'nonce': 'launch', 'root': '/campaign'}, '_replayLaunchNonce': 'fresh'}
    launch.update(args or {})
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / 'run.cjs'; script.write_text(program, encoding='utf-8')
        run = subprocess.run(['node', str(script)], input=json.dumps({'source': source, 'args': launch, 'results': results}), capture_output=True, text=True, encoding='utf-8', timeout=30)
    if run.returncode: raise AssertionError(run.stderr[-1800:])
    return json.loads(run.stdout)


def git(root, *args, check=True):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, encoding='utf-8', check=check)
    return result.stdout.strip() if check else result

class WorktreeBase(unittest.TestCase):
    def test_emitted_preamble_uses_plain_fast_forward_and_bound_schema(self):
        text, function = preamble(BASE)
        self.assertNotIn('-C', function); self.assertNotIn('$(', function)
        self.assertIn('git merge --ff-only ' + BASE['sha'], text)
        self.assertIn('baseSeen', text)
        self.assertIn('BEFORE doing the requested work', text)
        result = execute(graph(), {'worker': {'summary': 'checked', 'plan': 'done', 'files': [], 'risks': [], 'baseSeen': BASE['sha']}})
        self.assertEqual(result['summary']['outcome'], 'COMPLETE')
        schema = result['calls'][0]['options']['schema']
        self.assertIn('baseSeen', schema['required'])
        self.assertEqual(schema['properties']['baseSeen']['pattern'], '^[0-9a-f]{40}$')

    def test_actual_emitted_commands_refuse_unsafe_trees_and_keep_the_worker_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); launch = root / 'launch'; launch.mkdir()
            git(launch, 'init', '-q', '-b', 'main'); git(launch, 'config', 'user.name', 'Base test'); git(launch, 'config', 'user.email', 'fixture@example.invalid')
            (launch / 'tracked').write_text('original\n'); git(launch, 'add', '.'); git(launch, 'commit', '-qm', 'fresh')
            fresh = git(launch, 'rev-parse', 'HEAD')
            (launch / 'tool').write_text('campaign tool\n'); git(launch, 'add', '.'); git(launch, 'commit', '-qm', 'campaign base')
            base = git(launch, 'rev-parse', 'HEAD')
            for case in ('behind', 'exact', 'ahead', 'diverged', 'dirty-behind', 'dirty-exact', 'detached', 'missing', 'shared'):
                with self.subTest(case=case):
                    target = launch if case == 'shared' else root / case
                    if target != launch:
                        git(launch, 'worktree', 'add', '-q', '-b', case, str(target), base if case in ('exact', 'ahead', 'dirty-exact') else fresh)
                    if case in ('ahead', 'diverged'):
                        (target / 'other').write_text('other commit\n'); git(target, 'add', '.'); git(target, 'commit', '-qm', case)
                    if case.startswith('dirty'): (target / 'uncommitted').write_text('do not discard\n')
                    if case == 'detached': git(target, 'checkout', '--detach', '-q', 'HEAD')
                    before, branch = git(target, 'rev-parse', 'HEAD'), git(target, 'symbolic-ref', '--short', 'HEAD', check=False)
                    expected = '0' * 40 if case == 'missing' else base
                    emitted = commands({'sha': expected, 'root': str(launch), 'branch': 'main'})
                    history = []
                    def run(index):
                        value = subprocess.run(shlex.split(emitted[index]), cwd=target, capture_output=True, text=True, encoding='utf-8')
                        history.append((emitted[index], value.returncode)); return value
                    head, own = run(0), run(1)
                    accepted = own.returncode == 0 and os.path.normcase(os.path.realpath(own.stdout.strip())) != os.path.normcase(os.path.realpath(launch))
                    if accepted:
                        clean = run(2); accepted = clean.returncode == 0 and not clean.stdout.strip()
                    if accepted: accepted = run(3).returncode == 0
                    if accepted and head.stdout.strip() != expected: accepted = run(4).returncode == 0
                    if accepted: accepted = run(5).stdout.strip() == expected
                    self.assertEqual(accepted, case in ('behind', 'exact'), history)
                    self.assertEqual(git(target, 'rev-parse', 'HEAD'), expected if accepted else before)
                    after_branch = git(target, 'symbolic-ref', '--short', 'HEAD', check=False)
                    self.assertEqual((after_branch.returncode, after_branch.stdout), (branch.returncode, branch.stdout))
                    if case == 'behind':
                        git(target, 'commit', '--allow-empty', '-qm', 'later worker commit')
                        self.assertIn(branch.stdout.strip(), git(target, 'branch', '--contains', 'HEAD'))
                    if case in ('shared', 'dirty-behind', 'dirty-exact', 'detached'): self.assertNotIn(emitted[4], [row[0] for row in history])
                    if case == 'ahead': self.assertIn((emitted[4], 0), history)

    def test_launch_root_and_full_sha_refuse_before_spawning(self):
        requirement = next(row for row in cg.launch_requirements(graph())['args'] if row['name'] == '_base')
        self.assertIn('root', requirement['fields'])
        for field, value in [('sha', '--help'), ('sha', 'main; false'), ('sha', 'abc123'), ('root', ''), ('root', None)]:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(AssertionError, r'args\._base'): execute(graph(), {}, args={'_base': {**BASE, field: value}})

    def test_wrong_base_scalar_and_fanout_never_contribute_findings(self):
        node = {'id': 'worker', 'contract': 'FindingsV1', 'prompt': 'Inspect.', 'isolation': 'worktree', 'onRed': 'drop+log'}
        for observed in (None, 'abc', 'b' * 40):
            with self.subTest(observed=observed):
                report = {'findings': [{'claim': 'wrong-base claim'}], 'baseSeen': observed}
                result = execute(graph([node]), {'worker': report})
                self.assertEqual(result['summary']['outcome'], 'WRONG-BASE')
                self.assertIsNone(result['summary']['results']['worker'])
        missing = execute(graph([node]), {'worker': {'findings': ['missing observation']}})
        self.assertEqual(missing['summary']['outcome'], 'WRONG-BASE')
        self.assertNotIn('missing observation', json.dumps(missing['summary']['results']))
        for malformed in (False, 0, ''):
            with self.subTest(malformed=malformed):
                self.assertEqual(execute(graph([node]), {'worker': malformed})['summary']['outcome'], 'WRONG-BASE')
        ir = graph([{**node, 'foreach': 'tasks'}]); ir['lists'] = {'tasks': [{'key': 'bad'}, {'key': 'good'}]}
        result = execute(ir, {'worker:bad': {'findings': [{'claim': 'must disappear'}], 'baseSeen': 'b' * 40},
                              'worker:good': {'findings': [], 'baseSeen': BASE['sha']}})
        self.assertEqual(result['summary']['outcome'], 'WRONG-BASE')
        self.assertNotIn('must disappear', json.dumps(result['summary']['results']))

    def test_isolated_judge_ballots_keep_schema_extension_without_counting_bad_votes(self):
        nodes = [{'id': 'design', 'contract': 'DesignV1', 'foreach': 'choices', 'prompt': 'Design.'},
                 {'id': 'choose', 'contract': 'DecisionV1', 'after': 'design', 'decides': 'approach', 'verify': 'judges:3', 'isolation': 'worktree', 'prompt': 'Choose.'}]
        ir = graph(nodes); ir['lists'] = {'choices': [{'key': 'one'}, {'key': 'two'}]}
        ballot = {'winner': 'one', 'ranking': ['one', 'two', 'none'], 'rationale': 'The selected option preserves the independently examined authoritative invariant.',
                  'strongest_objection': {key: 'A substantive independently checked objection for ' + key for key in ('one', 'two', 'none')}, 'baseSeen': BASE['sha']}
        results = {'design:one': {'summary': 'one'}, 'design:two': {'summary': 'two'}, **{'choose:judge' + str(i): copy.deepcopy(ballot) for i in (1, 2, 3)}}
        self.assertEqual(execute(ir, results)['summary']['outcome'], 'COMPLETE')
        results['choose:judge2']['baseSeen'] = 'b' * 40
        rejected = execute(ir, results)
        self.assertEqual(rejected['summary']['outcome'], 'WRONG-BASE')
        decisions = rejected['summary'].get('decisions', {}).get('approach', {}).get('council', {}).get('judgments', [])
        self.assertTrue(all(row['vote']['baseSeen'] == BASE['sha'] for row in decisions))

    def test_restored_memo_and_ledger_results_are_checked_again(self):
        worker = {'id': 'worker', 'contract': 'DesignV1', 'prompt': 'Inspect.', 'isolation': 'worktree', 'replay': 'inputs'}
        first = execute(graph([worker]), {'worker': {'baseSeen': BASE['sha']}})
        record = first['summary']['memoRecords'][0]
        entry = {**record, 'runId': 'wf_source', 'artifactSha256': '1' * 64, 'agentId': 'source', 'payloadSha256': record['key'],
                 'promptSha256': hashlib.sha256(record['prompt'].encode()).hexdigest(), 'result': {'baseSeen': 'b' * 40, 'findings': ['unjudged']}}
        restored = execute(graph([worker]), {}, replay_state={'_memo': {record['key']: entry}})
        self.assertEqual(restored['summary']['outcome'], 'WRONG-BASE'); self.assertEqual(restored['calls'], [])
        effect = {**worker, 'id': 'effect', 'irreversible': True}; effect.pop('replay')
        first = execute(graph([effect]), {'effect': {'baseSeen': BASE['sha']}}, args={'_ledger': {}, 'confirm': 'effect'})
        row = first['summary']['ledger'][0]
        restored = execute(graph([effect]), {}, args={'_ledger': {row['key']: {'result': {'baseSeen': 'b' * 40, 'findings': ['unjudged']}}}})
        self.assertEqual(restored['summary']['outcome'], 'WRONG-BASE'); self.assertEqual(restored['calls'], [])
        self.assertNotIn('unjudged', json.dumps(restored['summary']['results']))

    def test_repair_fixer_and_checker_wrong_base_stop_before_findings_count(self):
        # An emitter backstop also covers an isolated checker extension. The
        # shipped FixCheck measure checker keeps its snapshot schema instead.
        fixer = {'id': 'fix', 'contract': 'DesignV1', 'mutates': True, 'foreach': 'tasks', 'prompt': 'Fix.'}
        checker = {'id': 'check', 'contract': 'HarnessCheckV1', 'isolation': 'worktree', 'foreach': 'tasks', 'after': 'fix', 'verifies': 'fix', 'prompt': 'Check.', 'repeat': {'until': 'exit == 0', 'maxRounds': 1}}
        ir = graph([fixer, checker]); ir['lists'] = {'tasks': [{'key': 'one'}]}
        for label in ('fix:one:r1', 'check:one:r1'):
            results = {'fix:one:r1': {'baseSeen': BASE['sha'], 'patchSha256': '1' * 64}, 'check:one:r1': {'baseSeen': BASE['sha'], 'exit': 0}}
            results[label] = {'baseSeen': 'b' * 40, 'findings': ['unjudged']}
            value = execute(ir, results)
            self.assertEqual(value['summary']['outcome'], 'WRONG-BASE')
            self.assertNotIn('unjudged', json.dumps(value['summary']['results']))

    def test_measurement_snapshot_preamble_and_launch_compatibility_stay_intact(self):
        node = {'id': 'measure', 'contract': 'DesignV1', 'prompt': 'Measure.', 'isolation': 'measure'}
        source = cg.emit(graph([node]), CONTRACTS)
        self.assertIn('$(mktemp -d)', source)
        result = execute(graph([node]), {'measure': {'summary': 'measured'}}, args={'_base': {'sha': 'old-reading', 'porcelain': ''}})
        self.assertEqual(result['summary']['outcome'], 'COMPLETE')
        self.assertNotIn('baseSeen', result['calls'][0]['options']['schema'].get('required', []))

    def test_settings_preflight_is_scoped_and_advisory_even_when_unknown(self):
        import worktree_settings
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / '.claude').mkdir()
            self.assertEqual(worktree_settings.report(root)['state'], 'fresh/absent')
            shared = root / '.claude/settings.json'; local = root / '.claude/settings.local.json'
            shared.write_text('{"worktree":{"baseRef":"fresh"}}')
            local.write_text('{"worktree":{"baseRef":"head"}}')
            self.assertEqual(worktree_settings.report(root)['state'], 'head')
            local.write_text('{malformed')
            self.assertEqual(worktree_settings.report(root)['state'], 'unknown')
            with patch.object(Path, 'read_text', side_effect=PermissionError('unreadable')):
                self.assertEqual(worktree_settings.report(root)['state'], 'unknown')
            run = subprocess.run([sys.executable, str(SCRIPTS / 'worktree-base.py'), '--root', str(root), '--json'], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0)
            value = json.loads(run.stdout); self.assertTrue(value['advisory']); self.assertEqual(value['scope'], 'project/local')
            self.assertIn('managed', value['caveat']); self.assertIn('CLAUDE_CONFIG_DIR', value['caveat'])

if __name__ == '__main__': unittest.main()
