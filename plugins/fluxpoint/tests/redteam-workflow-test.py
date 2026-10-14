#!/usr/bin/env python3
"""Execute the code red-team workflow and durable coverage gate (#127)."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / 'workflows/red-team.js'
SUBJECT = [{'repository': '/code/app', 'base': 'a' * 40, 'head': 'b' * 40,
            'files': 345, 'additions': 181038, 'deletions': 13590}]
BASE = {'pluginRoot': '/plug', 'subject': SUBJECT}
LENSES = ['authority', 'value', 'state', 'input', 'operations', 'composition']

HARNESS = r'''
import {writeFileSync} from 'node:fs';
const calls=[],logs=[],phases=[];
Date.now=()=>{throw new Error('clock unavailable')};
Math.random=()=>{throw new Error('random unavailable')};
const args=__ARGS__;
const subject=args.subject || args.repositories || [args];
const F=(severity,title)=>({severity,title,failurePath:'Executable failure path for '+title,
  evidence:'ran scratch/repro.py with a passing valid control',rewire:'Enforce the violated invariant at the reached boundary'});
const lens=(findings=[],extra={})=>({subject,completed:true,sampled:false,
  evidence:'Ran every applicable attack and valid control on the pinned state',blockers:[],findings,...extra});
const snapshot=(extra={})=>({stable:true,subject,evidence:'Resolved git objects and recomputed clean range counts',blockers:[],...extra});
const items=p=>JSON.parse(p.slice(p.indexOf('FINDINGS:\n')+'FINDINGS:\n'.length));
const singletons=p=>({clusters:items(p).map(f=>({members:[f.id],settled:''}))});
const respond=__RESPOND__;
const agent=async(p,o={})=>{calls.push({prompt:p,...o});return respond(p,o)};
const parallel=async ts=>Promise.all(ts.map(t=>Promise.resolve().then(t).catch(()=>null)));
const pipeline=async(xs,...ss)=>Promise.all(xs.map(async(x,i)=>{
 let p=x;for(const s of ss){try{p=await s(p,x,i)}catch(e){return null}}return p}));
const log=s=>logs.push(s),phase=s=>phases.push(s);
(async()=>{__SCRIPT__})().then(r=>writeFileSync(__OUT__,JSON.stringify({r,calls,logs,phases})),
 e=>writeFileSync(__OUT__,JSON.stringify({err:String(e.message||e),calls,logs,phases})));
'''
DEFAULT = r'''(p,o)=>o.label.startsWith('lens:')||o.label==='skeptic:coverage'?lens():o.label==='snapshot:final'?snapshot():null'''


def run(args=None, responder=DEFAULT):
    body = re.sub(r'(?m)^export const meta', 'const meta', SCRIPT.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        out, script = Path(tmp) / 'out.json', Path(tmp) / 'script.mjs'
        text = (HARNESS.replace('__ARGS__', json.dumps(BASE if args is None else args))
                .replace('__RESPOND__', responder).replace('__SCRIPT__', body)
                .replace('__OUT__', json.dumps(str(out))))
        script.write_text(text)
        result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
        if not out.exists():
            raise AssertionError(result.stderr or result.stdout)
        return json.loads(out.read_text())


def record(results, contracts=None, *flags):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        graph = root / 'WORK.md'
        graph.write_text('| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        summary = {'outcome': 'COMPLETE', 'contracts': contracts or {k: 'RedTeamV1' for k in results},
                   'results': results, 'provenance': []}
        p = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'), '--run-id', 'review',
                            '--graph', str(graph), '--root', str(root), '--state-dir', str(root / 'runs'), *flags],
                           input=json.dumps(summary), cwd=root, text=True, capture_output=True, timeout=30)
        if p.returncode:
            raise AssertionError(p.stderr)
        return json.loads((root / 'runs/review.json').read_text()), graph.read_text(), p.stderr


class WorkflowTests(unittest.TestCase):
    def test_six_lenses_and_fresh_identity_check(self):
        out = run()
        self.assertNotIn('err', out)
        result = out['r']
        self.assertEqual(result['verdict'], 'SHIP')
        self.assertEqual(result['coverage'], {'subject': SUBJECT, 'attackers': 6, 'lenses': LENSES, 'sampled': False})
        self.assertEqual((result['worstSeverity'], result['worstSeverityRank'], result['findings']), ('NONE', 0, []))
        self.assertEqual([c['label'] for c in out['calls']], ['lens:' + l for l in LENSES] + ['skeptic:coverage', 'snapshot:final'])
        for c in out['calls'][:6]:
            self.assertIn('/plug/agents/red-team-reviewer.md', c['prompt'])
            for s in SUBJECT:
                self.assertIn(s['base'], c['prompt'])
                self.assertIn(s['head'], c['prompt'])
            self.assertIn('valid controls', c['prompt'])

    def test_subject_aliases_multi_repository_and_immutability(self):
        args = copy.deepcopy(BASE)
        args['subject'].append(dict(SUBJECT[0], repository='/code/other', head='c' * 40))
        args['repositories'] = args.pop('subject')
        self.assertEqual(len(run(args)['r']['coverage']['subject']), 2)
        for key, value in [('head', 'main'), ('base', 'HEAD~3'), ('files', -1), ('additions', True), ('deletions', 1.2)]:
            invalid = copy.deepcopy(BASE)
            invalid['subject'][0][key] = value
            self.assertIn('err', run(invalid), key)

    def test_component_lenses_require_composition_and_unique_ids(self):
        args = dict(BASE, lenses=[{'id': 'payments', 'focus': 'Transfers across providers'}, 'composition'])
        self.assertEqual(run(args)['r']['coverage']['lenses'], ['payments', 'composition'])
        for lenses in [[], ['authority'], ['composition'], ['authority', 'authority', 'composition'], ['unknown', 'composition']]:
            self.assertIn('err', run(dict(BASE, lenses=lenses)))

    def test_missing_malformed_incomplete_or_mismatched_attacker_blocks(self):
        responses = ['null', '{}', 'lens([], {completed:false})', 'lens([], {evidence:""})',
                     'lens([], {subject:[]})', 'lens([], {sampled:null})',
                     'lens([], {blockers:["No build tools available"]})',
                     'lens([{severity:"HIGH",title:"missing exploit path"}])']
        for response in responses:
            out = run(responder=f'''(p,o)=>o.label==='lens:authority'?{response}:
                o.label.startsWith('lens:')?lens():snapshot()''')
            self.assertEqual(out['r']['verdict'], 'BLOCK', response)
            self.assertTrue(out['r']['review']['blockers'], response)

    def test_sampling_is_explicit_and_durable(self):
        out = run(responder='''(p,o)=>o.label.startsWith('lens:')?
            lens([], {sampled:o.label==='lens:authority'}):o.label==='skeptic:coverage'?lens():snapshot()''')
        self.assertEqual(out['r']['verdict'], 'SHIP')
        self.assertTrue(out['r']['coverage']['sampled'])
        art, row, _ = record({'rt': out['r']})
        self.assertTrue(art['redTeamCoverage'][0]['coverage']['sampled'])
        self.assertIn('SAMPLED', row)
        self.assertIn('345 files, 6 attacker(s), 6 lens(es)', row)

    def test_medium_findings_get_three_independent_refuters(self):
        out = run(responder='''(p,o)=>{
          if(o.label==='lens:authority')return lens([F('MEDIUM','replay permits duplicate debit')]);
          if(o.label.startsWith('lens:'))return lens();
          if(o.label==='reduce')return singletons(p);
          if(o.label.startsWith('refute:'))return {resolution:'CONFIRMED',evidence:'Ran the exploit and its valid control on the pinned head'};
          return snapshot();}''')
        result = out['r']
        refs = [c for c in out['calls'] if c['label'].startswith('refute:')]
        self.assertEqual(len(refs), 3)
        self.assertEqual({c['label'].split(':')[1] for c in refs}, {'reproduce', 'reachability', 'scope'})
        self.assertEqual((result['verdict'], result['worstSeverity'], result['worstSeverityRank']), ('BLOCK', 'MEDIUM', 2))
        self.assertEqual(result['findings'][0]['finding'], 'replay permits duplicate debit')

    def test_low_findings_get_one_combined_refuter(self):
        out = run(responder='''(p,o)=>o.label==='lens:input'?lens([F('LOW','small invalid input claim')]):
          o.label.startsWith('lens:')?lens():o.label==='reduce'?singletons(p):
          o.label.startsWith('refute:')?{resolution:'REFUTED',evidence:'Valid control passed and the boundary rejected the attack'}:snapshot()''')
        self.assertEqual(out['r']['verdict'], 'SHIP')
        self.assertEqual(out['r']['review']['counts']['verifiers'], 1)
        self.assertEqual(out['r']['review']['counts']['refuted'], 1)

    def test_uncertain_missing_malformed_or_divided_refuters_cannot_erase_a_claim(self):
        for vote in ['null', '{}', "{resolution:'REFUTED',evidence:''}",
                     "{resolution:'UNRESOLVED',evidence:'The required runtime tool is unavailable'}",
                     "{resolution:o.label.includes('reproduce')?'CONFIRMED':'REFUTED',evidence:'Independent contradictory evidence was recorded'}"]:
            out = run(responder='''(p,o)=>o.label==='lens:state'?lens([F('HIGH','forged receipt accepted by parser')]):
              o.label.startsWith('lens:')?lens():o.label==='reduce'?singletons(p):
              o.label.startsWith('refute:')?''' + vote + ''':snapshot()''')
            self.assertEqual(out['r']['verdict'], 'BLOCK', vote)
            self.assertEqual(out['r']['review']['counts']['unverified'], 1, vote)
            self.assertEqual(out['r']['worstSeverity'], 'HIGH')

    def test_reduce_cannot_drop_invent_duplicate_or_overmerge_claims(self):
        out = run(dict(BASE, settled=['Earlier receipt issue']), responder='''(p,o)=>{
          if(o.label==='lens:authority')return lens([F('HIGH','first independent authority bug'),F('MEDIUM','second independent authority bug')]);
          if(o.label==='lens:state')return lens([F('CRITICAL','same receipt failure under state lens')]);
          if(o.label.startsWith('lens:'))return lens();
          if(o.label==='reduce')return {clusters:[{members:['F1','F2','F3','invented'],settled:'Earlier receipt issue'},
            {members:['F1'],settled:'not a settled title'}]};
          if(o.label.startsWith('refute:'))return {resolution:'CONFIRMED',evidence:'The original attack and valid control both reproduce'};
          return snapshot();}''')
        r = out['r']
        self.assertEqual(r['review']['counts']['raw'], 3)
        self.assertEqual(r['review']['counts']['distinct'], 2)
        self.assertEqual(r['worstSeverity'], 'CRITICAL')
        self.assertEqual(r['review']['counts']['verifiers'], 6)
        self.assertIn('Earlier receipt issue', next(c['prompt'] for c in out['calls'] if c['label'].startswith('refute:')))
        self.assertIn('Every merged claim must be checked', next(c['prompt'] for c in out['calls'] if 'FINDING D1' in c['prompt']))

    def test_failed_reduce_preserves_every_original_claim(self):
        out = run(responder='''(p,o)=>o.label==='lens:input'?lens([F('LOW','first separate parser failure'),F('LOW','second separate parser failure')]):
          o.label.startsWith('lens:')?lens():o.label==='reduce'?null:
          o.label.startsWith('refute:')?{resolution:'CONFIRMED',evidence:'Reproduced the input fault independently'}:snapshot()''')
        self.assertEqual(out['r']['review']['counts']['distinct'], 2)
        self.assertEqual(len(out['r']['findings']), 2)

    def test_malformed_reduce_clusters_leave_claims_for_independent_refutation(self):
        out = run(responder='''(p,o)=>o.label==='lens:input'?lens([F('LOW','independent invalid input failure')]):
          o.label.startsWith('lens:')?lens():o.label==='reduce'?{clusters:[null,{},false]}:
          o.label.startsWith('refute:')?{resolution:'CONFIRMED',evidence:'Ran the attack and verified the claimed invalid result'}:snapshot()''')
        self.assertEqual(out['r']['verdict'], 'BLOCK')
        self.assertEqual(out['r']['review']['counts']['distinct'], 1)

    def test_final_drift_missing_recheck_and_mismatched_counts_block_ship(self):
        for snapshot in ['null', 'snapshot({stable:false})', 'snapshot({subject:[]})',
                         'snapshot({blockers:["Working tree changed during review"]})']:
            out = run(responder=f"(p,o)=>o.label.startsWith('lens:')||o.label==='skeptic:coverage'?lens():{snapshot}")
            self.assertEqual(out['r']['verdict'], 'BLOCK')
            self.assertTrue(out['r']['review']['blockers'])

    def test_coverage_counts_completed_attackers_and_retains_planned_scope_separately(self):
        out = run(responder='''(p,o)=>o.label==='lens:authority'?null:
            o.label==='lens:value'?lens([], {completed:false}):lens()''')
        result = out['r']
        self.assertEqual(result['verdict'], 'BLOCK')
        self.assertEqual(result['coverage']['attackers'], 4)
        self.assertEqual(result['coverage']['lenses'], LENSES[2:])
        self.assertEqual(result['review']['plannedLenses'], LENSES)
        self.assertEqual(result['review']['counts']['attackersCompleted'], 4)
        empty = run(responder='(p,o)=>null')['r']
        self.assertEqual(empty['verdict'], 'BLOCK')
        self.assertNotIn('coverage', empty)
        self.assertEqual(empty['review']['subject'], SUBJECT)

    def test_no_claims_still_requires_executed_independent_skeptic_sampling(self):
        for response in ['null', 'lens([], {completed:false})', 'lens([], {subject:[]})',
                         'lens([], {blockers:["Valid control did not build"]})']:
            out = run(responder=f"(p,o)=>o.label==='skeptic:coverage'?{response}:o.label.startsWith('lens:')?lens():snapshot()")
            self.assertEqual(out['r']['verdict'], 'BLOCK', response)
            self.assertTrue(out['r']['review']['blockers'])
            self.assertEqual(out['r']['coverage']['attackers'], 6)

    def test_skeptic_finds_missed_exploit_and_sends_it_to_fresh_refuters(self):
        out = run(responder='''(p,o)=>o.label.startsWith('lens:')?lens():
            o.label==='skeptic:coverage'?lens([F('HIGH','defended case rejected at wrong boundary')]):
            o.label==='reduce'?singletons(p):o.label.startsWith('refute:')?
            {resolution:'CONFIRMED',evidence:'Fresh reproduction proves the missed attack violates the invariant'}:snapshot()''')
        self.assertEqual(out['r']['verdict'], 'BLOCK')
        self.assertEqual(out['r']['review']['counts']['confirmed'], 1)
        self.assertEqual(out['r']['review']['counts']['verifiers'], 3)
        self.assertEqual(out['r']['coverage']['attackers'], 6)
        self.assertEqual(out['r']['review']['confirmed'][0]['sources'], ['coverage-skeptic'])


class RecordTests(unittest.TestCase):
    def ship(self):
        return {'verdict': 'SHIP', 'findings': [], 'worstSeverity': 'NONE', 'worstSeverityRank': 0,
                'coverage': {'subject': copy.deepcopy(SUBJECT), 'attackers': 6, 'lenses': LENSES, 'sampled': False}}

    def test_broad_ship_without_coverage_fails_even_if_schema_was_skipped(self):
        result = self.ship()
        del result['coverage']
        art, row, stderr = record({'rt': result})
        self.assertEqual(art['outcome'], 'BLOCKED-REDTEAM')
        self.assertEqual(art['redTeam'], 'BLOCK')
        self.assertIn('SHIP has no coverage object', stderr)
        self.assertIn('coverage missing', row)

    def test_bad_coverage_types_and_missing_fields_block(self):
        mutations = [lambda c: c.pop('subject'), lambda c: c.update(subject=[]),
                     lambda c: c['subject'][0].update(files=True), lambda c: c['subject'][0].update(additions=-1),
                     lambda c: c['subject'][0].pop('head'), lambda c: c.update(attackers=0),
                     lambda c: c.update(attackers=True), lambda c: c.update(lenses=[]),
                     lambda c: c.update(sampled='false')]
        for mutate in mutations:
            result = self.ship()
            mutate(result['coverage'])
            art, _, _ = record({'rt': result})
            self.assertEqual(art['outcome'], 'BLOCKED-REDTEAM', result)

    def test_coverage_is_preserved_per_gate_and_foreach_item(self):
        first, second = self.ship(), self.ship()
        second['coverage']['sampled'] = True
        art, row, _ = record({'rt': [first, second], 'other': first})
        records = art['redTeamCoverage']
        self.assertEqual(len(records), 3)
        self.assertEqual([r.get('index') for r in records[:2]], [0, 1])
        self.assertEqual(records[1]['coverage'], second['coverage'])
        self.assertEqual(art['outcome'], 'COMPLETE')
        self.assertIn('SAMPLED', row)

    def test_foreach_coverage_keeps_the_original_item_index_after_a_dead_item(self):
        result = self.ship()
        art, _, _ = record({'rt': [None, result]})
        self.assertEqual(art['redTeamCoverage'][0]['index'], 1)

    def test_ship_cannot_override_explicit_unresolved_review(self):
        result = self.ship()
        result['review'] = {'blockers': ['Missing composition attacks']}
        art, _, _ = record({'rt': result})
        self.assertEqual(art['outcome'], 'BLOCKED-REDTEAM')

    def test_cli_ship_cannot_replace_an_absent_review(self):
        art, _, _ = record({}, None, '--red-team', 'SHIP')
        self.assertEqual(art['outcome'], 'BLOCKED-REDTEAM')
        self.assertEqual(art['redTeam'], 'BLOCK')


if __name__ == '__main__':
    unittest.main(verbosity=2)
