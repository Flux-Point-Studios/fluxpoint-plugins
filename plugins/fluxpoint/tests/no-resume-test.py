#!/usr/bin/env python3
"""A node no resume may pass: "resume": "never" (#242).

A red-team fan-out halted by a dead worker could be resumed. Replay is the
longest unchanged prefix of calls, so every lens after the first dead one
ran again, a lens that had raised claims could come back clean, and the
review was rolled a second time under one record. Nothing in code refused
it: resume.py never looked at which nodes the recorded run had reached,
and the compiled script never read _resumeFromRunId.

A node can now declare "resume": "never". A run whose record shows that
node started, or any node that depends on it, cannot be resumed. Executed
on every path that can launch a resume: the validator, the rule over a
recorded summary, resume.py, compile-graph.py --plan, and the compiled
script's own check before its first call. A native resume launched by hand
names no source run and reaches none of those, so record-run.py holds the
record too: a later attempt of a run that had started the node is not filed.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import embedded_args as ea
import resume

_spec = importlib.util.spec_from_file_location('cg_no_resume', SCRIPTS / 'compile-graph.py')
cg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(PLUGIN / 'contracts'))
CODE = 'RESUME-PAST-NO-RESUME-NODE: lenses'
DESIGN = {'summary': 's', 'plan': [], 'files': [], 'risks': []}


def graph(flag=True, max_nodes=12, parked=False, reduced=False):
    """Build, three review lenses, then a verdict over them. The lenses are the review."""
    lenses = {'id': 'lenses', 'after': 'build', 'foreach': 'lens', 'prompt': 'review {{item.key}} of {{prev}}',
              'contract': 'FindingsV1', 'onRed': 'halt'}
    if flag:
        lenses['resume'] = 'never'
    nodes = [{'id': 'build', 'prompt': 'build it', 'contract': 'DesignV1'}, lenses,
             {'id': 'verdict', 'after': 'lenses', 'prompt': 'judge {{prev}}', 'contract': 'DesignV1'}]
    if reduced:
        # The lenses drop what the budget declines and a reduce sits between them and the verdict.
        lenses['onRed'] = 'drop+log'
        nodes.insert(2, {'id': 'distinct', 'reduce': {'from': 'lenses', 'over': 'findings', 'dedupeBy': ['claim']}})
        nodes[3]['after'] = 'distinct'
    if parked:
        # A person signs before the review, and work that does not wait for them goes on.
        nodes.insert(1, {'id': 'sign-off', 'after': 'build', 'actor': 'human', 'prompt': 'approve {{prev}}',
                         'contract': 'DesignV1', 'release': {'instructions': 'sign it', 'proofContract': 'DesignV1',
                                                             'whyNotAgent': 'a person signs'}})
        lenses['after'] = 'sign-off'
        nodes.append({'id': 'notes', 'after': 'build', 'prompt': 'write notes on {{prev}}', 'contract': 'DesignV1'})
    return {'version': 1, 'name': 'review-once', 'campaign': 'review once', 'treeGuard': False,
            'budget': {'maxNodes': max_nodes}, 'lists': {'lens': [{'key': 'a'}, {'key': 'b'}, {'key': 'c'}]},
            'nodes': nodes}


def run(ir, args=None, dead=(), replay_state=None):
    """Execute the emitted graph under stubs. A label in `dead` returns nothing."""
    js = cg.emit(ir, CONTRACTS, {}, replay_state=replay_state).replace('export const meta', 'const meta')
    with tempfile.TemporaryDirectory() as tmp:
        script, out = Path(tmp) / 'run.mjs', Path(tmp) / 'out.json'
        script.write_text('\n'.join([
            "import {writeFileSync} from 'node:fs';",
            'const calls=[],logs=[],dead=new Set(' + json.dumps(sorted(dead)) + ');',
            "const agent=async(p,o)=>{const label=String(o.label||'');calls.push(label);",
            '  if(dead.has(label))return null;',
            "  if(label.startsWith('lenses:'))return {findings:[{file:'a.py',line:1,claim:label,failure_path:'p',severity:'LOW'}]};",
            "  if(label.endsWith(':advice'))return {chosen:'wait',rationale:'r',alternatives:[],overturned_prior:false};",
            '  return ' + json.dumps(DESIGN) + '};',
            'const parallel=async(t)=>Promise.all(t.map(f=>f()));',
            'const pipeline=async()=>[],log=s=>logs.push(String(s)),phase=()=>{};',
            'const args=' + json.dumps(args or {}) + ';',
            'const budget={total:null,spent:()=>0,remaining:()=>1e9};',
            'const workflow=0;',
            '(async () => {', js,
            '})().then(r=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({r,calls,logs})))',
            '.catch(e=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({err:String(e&&e.message||e),calls})))',
        ]), encoding='utf-8')
        done = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
        if not out.exists():
            raise AssertionError(done.stderr or done.stdout)
        return json.loads(out.read_text(encoding='utf-8'))


def recorded(ir=None, **how):
    """The summary a run returned, which is what record-run files."""
    doc = run(ir or graph(), **how)
    if 'err' in doc:
        raise AssertionError(doc['err'])
    return doc['r']


class Declared(unittest.TestCase):
    def issues(self, ir):
        return [f for f in cg.validate(ir, CONTRACTS) if 'resume' in f]

    def test_the_field_takes_one_value(self):
        self.assertEqual(cg.validate(graph(), CONTRACTS), [])
        for value in ('always', 'Never', True, ['never'], None):
            with self.subTest(value=value):
                ir = graph()
                ir['nodes'][1]['resume'] = value
                found = self.issues(ir)
                self.assertEqual(len(found), 1, found)
                self.assertIn("'lenses'", found[0])

    def test_a_parked_node_cannot_declare_it(self):
        # A park exists to be resumed past. Declaring both is a contradiction, said at compile time.
        ir = graph(parked=True)
        self.assertEqual(cg.validate(ir, CONTRACTS), [])
        ir['nodes'][1]['resume'] = 'never'
        found = self.issues(ir)
        self.assertEqual(len(found), 1, found)
        self.assertIn("'sign-off'", found[0])

    def test_every_park_has_to_come_before_the_node(self):
        # A park after the review, or beside it, is continued by a resume, and
        # by then the review has started. The graph cannot deliver both, so it
        # is refused when it compiles instead of at the resume.
        after = graph(parked=True)
        after['nodes'][1]['after'], after['nodes'][2]['after'] = 'lenses', 'build'
        found = self.issues(after)
        self.assertEqual(len(found), 1, found)
        for part in ("'lenses'", "'sign-off'"):
            self.assertIn(part, found[0])
        beside = graph(parked=True)
        beside['nodes'][2]['after'] = 'build'
        self.assertEqual(len(self.issues(beside)), 1)
        # The review waits on the park: the run parks first, and resumes into a review that has not started.
        self.assertEqual(self.issues(graph(parked=True)), [])

    def test_a_repair_loop_cannot_declare_it(self):
        # A repair loop files its rounds under its checker and is continued by a resume.
        def loop(flagged):
            nodes = [{'id': 'fix', 'foreach': 'slices', 'mutates': True, 'prompt': 'fix {{item.key}}: {{lastCheck}} in {{roundDir}}',
                      'contract': 'SliceV1'},
                     {'id': 'check', 'foreach': 'slices', 'after': 'fix', 'independent': True, 'verifies': 'fix',
                      'isolation': 'measure', 'prompt': 'check {{prev}}', 'contract': 'HarnessCheckV1',
                      'repeat': {'until': 'exit == 0', 'maxRounds': 2, 'integrate': ['merge', 'review']}},
                     {'id': 'merge', 'after': 'check', 'mutates': True, 'prompt': 'merge {{prev}}', 'contract': 'SliceV1'},
                     {'id': 'review', 'after': 'merge', 'independent': True, 'verifies': 'merge', 'isolation': 'measure',
                      'prompt': 'review {{prev}}', 'contract': 'HarnessCheckV1', 'haltWhen': 'exit != 0'},
                     {'id': 'verdict', 'after': 'review', 'prompt': 'judge {{prev}}', 'contract': 'DesignV1'}]
            if flagged:
                next(n for n in nodes if n['id'] == flagged)['resume'] = 'never'
            return {'version': 1, 'name': 'repair', 'campaign': 'repair', 'treeGuard': False,
                    'budget': {'maxNodes': 40, 'cacheTtl': '1h'}, 'lists': {'slices': [{'key': 'parser'}]}, 'nodes': nodes}

        self.assertEqual(cg.validate(loop(None), CONTRACTS), [])
        for member in ('fix', 'check', 'merge', 'review'):
            with self.subTest(member=member):
                found = self.issues(loop(member))
                self.assertEqual(len(found), 1, found)
                self.assertIn(repr(member), found[0])
        # A node that follows the loop can declare it, and the loop is not in its reach.
        self.assertEqual(self.issues(loop('verdict')), [])
        self.assertEqual(cg.no_resume_reach(loop('verdict')), {'verdict': ['verdict']})

    def test_the_field_changes_nothing_about_a_first_launch(self):
        self.assertFalse(cg.replay_features(graph()))
        with_flag, without = run(graph()), run(graph(flag=False))
        self.assertEqual((with_flag['r']['outcome'], with_flag['calls']), (without['r']['outcome'], without['calls']))
        self.assertEqual(with_flag['r']['outcome'], 'COMPLETE')
        # The record names what the run was compiled to refuse, and a graph without the field carries nothing.
        self.assertEqual(with_flag['r']['noResume'], {'lenses': ['lenses', 'verdict']})
        self.assertNotIn('noResume', without['r'])
        self.assertNotIn('NO_RESUME', cg.emit(graph(flag=False), CONTRACTS, {}))

    def test_check_asks_a_red_team_fan_out_for_the_field(self):
        def lensed(flag):
            ir = graph(flag)
            ir['nodes'][1].update(contract='RedTeamV1')
            return [w for w in cg.warnings(ir) if '"resume": "never"' in w]

        found = lensed(False)
        self.assertEqual(len(found), 1, cg.warnings(graph(False)))
        self.assertIn("'lenses'", found[0])
        self.assertEqual(lensed(True), [])
        self.assertEqual([w for w in cg.warnings(graph(False)) if '"resume": "never"' in w], [])


class RecordedRun(unittest.TestCase):
    """Whether a recorded run may be resumed, read from its own record."""

    def refusal(self, summary, ir=None, labels=()):
        return cg.resume_refusal(ir or graph(), summary, labels)

    def test_a_run_that_died_inside_the_fan_out_cannot_be_resumed(self):
        summary = recorded(dead={'lenses:b'})
        self.assertEqual(summary['outcome'], 'NODE-DEAD')
        self.assertNotIn('verdict', summary['results'])
        found = self.refusal(summary)
        self.assertTrue(found and found.startswith(CODE), found)
        # Every lens dead is still a review that started.
        found = self.refusal(recorded(dead={'lenses:a', 'lenses:b', 'lenses:c'}))
        self.assertTrue(found and found.startswith(CODE), found)

    def test_a_run_that_completed_the_fan_out_cannot_be_resumed(self):
        for how in ({}, {'dead': {'verdict'}}):
            with self.subTest(how=how):
                found = self.refusal(recorded(**how))
                self.assertTrue(found and found.startswith(CODE), found)

    def test_a_run_that_stopped_before_the_fan_out_can_be_resumed(self):
        summary = recorded(dead={'build'})
        self.assertEqual(summary['outcome'], 'NODE-DEAD')
        self.assertIsNone(self.refusal(summary))

    def test_a_wait_is_not_a_start(self):
        # The review waits on a person. The run parks, the notes are written
        # meanwhile, and resuming once the person has signed is the point of a park.
        ir = graph(parked=True)
        summary = recorded(ir, args={'_releases': {}})
        self.assertIsNone(summary['results']['lenses'])
        self.assertEqual(summary['results']['notes'], DESIGN)
        self.assertIsNone(self.refusal(summary, ir))
        released = recorded(ir, args={'_releases': {'sign-off': {'proof': DESIGN, 'by': 'operator'}}})
        found = self.refusal(released, ir)
        self.assertTrue(found and found.startswith(CODE), found)

    def test_a_decline_is_not_a_start(self):
        # The node ceiling let the build run and declined every lens: nothing was reviewed.
        declined = recorded(graph(max_nodes=1))
        self.assertEqual(declined['outcome'], 'BUDGET-EXHAUSTED')
        self.assertIsNone(self.refusal(declined, graph(max_nodes=1)))
        # One lens ran before the ceiling. That is a review that started.
        partly = recorded(graph(max_nodes=2))
        found = self.refusal(partly, graph(max_nodes=2))
        self.assertTrue(found and found.startswith(CODE), found)

    def test_a_reduce_over_lenses_that_never_ran_is_not_a_start(self):
        # Every lens was declined and dropped, and the reduce after them still
        # files OK over nothing: it is code, it spawns no one, and no review ran.
        ir = graph(max_nodes=1, reduced=True)
        summary = recorded(ir)
        self.assertEqual(summary['results']['distinct'], [])
        self.assertIn(('distinct', 'OK'), [(row['node'], row['status']) for row in summary['provenance']])
        self.assertIsNone(self.refusal(summary, ir))
        # Once a lens has returned, the reduce holds what it found.
        ran = recorded(graph(reduced=True), dead={'verdict'})
        self.assertTrue(ran['results']['distinct'])
        found = self.refusal(ran, graph(reduced=True))
        self.assertTrue(found and found.startswith(CODE), found)

    def test_a_record_from_before_the_field_is_read_by_what_it_holds(self):
        # Compiled before the node declared the field, so the record names no declaration.
        old = recorded(graph(flag=False), dead={'lenses:b'})
        self.assertNotIn('noResume', old)
        found = self.refusal(old)
        self.assertTrue(found and found.startswith(CODE), found)
        self.assertIsNone(self.refusal(recorded(graph(flag=False), dead={'build'})))
        # Any one kind of evidence is enough, and a node that depends on the lenses counts for them.
        for holds in ({'results': {'lenses': [{'findings': []}]}},
                      {'results': {'verdict': DESIGN}},
                      {'results': {'lenses': []}, 'provenance': [{'node': 'lenses', 'status': 'DEAD', 'detail': '0/3 returned'}]},
                      {'provenance': [{'node': 'lenses:b', 'status': 'WRONG-BASE', 'detail': ''}]},
                      {'callRecords': [{'opts': {'label': 'lenses:a'}, 'mode': 'live'}]},
                      {'memoRecords': [{'node': 'lenses', 'mode': 'live', 'result': None, 'callIndex': 4}]},
                      {'memoRecords': [{'node': 'verdict', 'mode': 'memo', 'result': DESIGN}]},
                      {'callRecords': [{'opts': {'label': 'verdict'}, 'mode': 'live'}]}):
            with self.subTest(holds=holds):
                found = self.refusal(dict({'results': {}, 'provenance': []}, **holds))
                self.assertTrue(found and found.startswith(CODE), found)
        for holds in ({'results': {'build': DESIGN}},
                      {'results': {'lenses': None, 'verdict': None}},
                      {'results': {'lenses': []}, 'provenance': [{'node': 'lenses', 'status': 'SKIPPED', 'detail': 'budget floor'}]},
                      {'provenance': [{'node': 'lenses', 'status': 'BLOCKED', 'detail': 'blocked on sign-off'}]},
                      {'provenance': [{'node': 'lenses', 'status': 'NO-CANDIDATES', 'detail': 'no surviving design seat'}]},
                      {'provenance': [{'node': 'lenses', 'status': 'INVALID-CANDIDATES', 'detail': 'no unique keys'}]},
                      {'memoRecords': [{'node': 'lenses', 'mode': 'live', 'result': None, 'callIndex': None}]},
                      {'provenance': [{'node': 'verdict', 'status': 'DEAD', 'detail': 'node returned nothing'}]},
                      {'callRecords': [{'opts': {'label': 'tree-check:before lenses'}, 'mode': 'live'}]}):
            with self.subTest(holds=holds):
                self.assertIsNone(self.refusal(dict({'results': {}, 'provenance': []}, **holds)))

    def test_the_recorded_declaration_outlives_an_edit_to_the_graph(self):
        # Deleting the field from the graph does not make the old run resumable.
        summary = recorded(dead={'lenses:b'})
        found = self.refusal(summary, graph(flag=False))
        self.assertTrue(found and found.startswith(CODE), found)

    def test_calls_the_runtime_journal_holds_count_too(self):
        # The last recorded attempt stopped at the build, and a later attempt
        # under the same run id reached the lenses without being recorded.
        summary = recorded(dead={'build'})
        found = self.refusal(summary, labels=['build', 'lenses:b'])
        self.assertTrue(found and found.startswith(CODE), found)
        self.assertIsNone(self.refusal(summary, labels=['build', 'tree-check:before lenses']))


class ScheduledRuntime(unittest.TestCase):
    """A graph with lanes runs under the scheduled runtime, which files its own records."""

    def lanes(self, review):
        # An audit and a sweep run side by side; the review joins them, and a verdict follows it.
        nodes = [{'id': 'audit', 'lane': 'a', 'prompt': 'audit', 'contract': 'DesignV1'},
                 {'id': 'find', 'lane': 'b', 'prompt': 'find defects', 'contract': 'FindingsV1', 'verify': 'panel:3',
                  'verifyOver': 'findings', 'expectItems': 1},
                 dict(review, after=['find', 'audit'], resume='never'),
                 {'id': 'verdict', 'after': review['id'], 'prompt': 'judge {{prev}}', 'contract': 'DesignV1'}]
        return {'version': 1, 'name': 'once', 'campaign': 'review once', 'budget': {'maxNodes': 7 if 'foreach' not in review else 9},
                'lists': {'lens': [{'key': 'a'}, {'key': 'b'}, {'key': 'c'}]}, 'nodes': nodes}

    def scalar(self):
        return self.lanes({'id': 'review', 'prompt': 'review {{prev.find}} and {{prev.audit}}', 'contract': 'DesignV1'})

    def fan_out(self):
        return self.lanes({'id': 'review', 'foreach': 'lens', 'prompt': 'review {{item.key}} of {{prev.find}} and {{prev.audit}}',
                           'contract': 'FindingsV1', 'onRed': 'drop+log'})

    def run_lanes(self, ir, found, dead=()):
        self.assertEqual(cg.validate(ir, CONTRACTS), [])
        self.assertTrue(cg.replay_features(ir))
        js = cg.emit(ir, CONTRACTS, {}).replace('export const meta', 'const meta')
        args = {'_base': {'sha': 'a' * 40, 'branch': 'main', 'porcelain': '', 'root': '/campaign'},
                '_launch': {'since': '2026-11-11T00:00:00Z', 'nonce': 'nonce', 'root': '/campaign'},
                '_replayLaunchNonce': '1' * 64}
        with tempfile.TemporaryDirectory() as tmp:
            script, out = Path(tmp) / 'run.mjs', Path(tmp) / 'out.json'
            script.write_text('\n'.join([
                "import {writeFileSync} from 'node:fs';",
                'const calls=[],dead=new Set(' + json.dumps(sorted(dead)) + ');',
                "let agent=async(p,o)=>{const label=String(o.label||'');calls.push(label);",
                "  if(label.startsWith('tree-check:'))return {root:'/campaign',head:'a'.repeat(40),porcelain:''};",
                '  if(dead.has(label))return null;',
                "  if(label==='find')return {findings:Array.from({length:" + str(found) + "},(_,i)=>({file:'a.py',line:i+1,claim:'defect number '+i+' in the code',failure_path:'path '+i,severity:'LOW'}))};",
                "  if(/:refute\\d+$/.test(label))return {refuted:false,reason:'the claim stands on reproduction'};",
                "  if(label.startsWith('review:'))return {findings:[{file:'a.py',line:1,claim:label,failure_path:'p',severity:'LOW'}]};",
                '  return ' + json.dumps(DESIGN) + '};',
                'const parallel=async(t)=>Promise.all(t.map(f=>f()));',
                'const pipeline=async(xs,...ss)=>Promise.all(xs.map(async(x,i)=>{let p=x;for(const s of ss){p=await s(p,x,i)}return p}));',
                'const log=()=>{},phase=()=>{};', 'let args=' + json.dumps(args) + ';',
                'const budget={total:null,spent:()=>0,remaining:()=>1e9};', 'const workflow=0;',
                '(async () => {', js,
                '})().then(r=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({r,calls})))',
                '.catch(e=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({err:String(e&&e.stack||e),calls})))',
            ]), encoding='utf-8')
            done = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=120)
            if not out.exists():
                raise AssertionError(done.stderr or done.stdout)
            doc = json.loads(out.read_text(encoding='utf-8'))
        if 'err' in doc:
            raise AssertionError(doc['err'])
        return doc

    def test_a_review_the_budget_declined_did_not_start(self):
        # The sweep found six defects where the plan expected one, and its refuters
        # spent the node ceiling before the review was reached.
        for ir in (self.scalar(), self.fan_out()):
            with self.subTest(review='fan-out' if 'foreach' in ir['nodes'][2] else 'scalar'):
                doc = self.run_lanes(ir, found=6)
                self.assertFalse([label for label in doc['calls'] if label.split(':')[0] in ('review', 'verdict')])
                self.assertGreater(doc['r']['declined'], 0)
                self.assertIsNone(cg.resume_refusal(ir, doc['r']))

    def test_a_review_that_ran_is_refused_and_one_that_was_never_reached_is_not(self):
        ran = self.run_lanes(self.scalar(), found=1, dead={'verdict'})
        self.assertIn('review', ran['calls'])
        found = cg.resume_refusal(self.scalar(), ran['r'])
        self.assertTrue(found and found.startswith('RESUME-PAST-NO-RESUME-NODE: review'), found)
        before = self.run_lanes(self.scalar(), found=1, dead={'audit'})
        self.assertNotIn('review', before['calls'])
        self.assertIsNone(cg.resume_refusal(self.scalar(), before['r']))


class CompiledScript(unittest.TestCase):
    """The script refuses before its first call, whoever assembled the launch."""

    def refused(self, doc, code=CODE):
        self.assertIn('err', doc, doc.get('r'))
        self.assertIn(code, doc['err'])
        self.assertEqual(doc['calls'], [])

    def test_a_resume_whose_source_stopped_before_the_node_runs(self):
        doc = run(graph(), args={'_resumeFromRunId': 'wf_source', '_resumeReach': ['build']})
        self.assertEqual((doc.get('r') or {}).get('outcome'), 'COMPLETE', doc)
        self.assertEqual(doc['calls'], ['build', 'lenses:a', 'lenses:b', 'lenses:c', 'verdict'])

    def test_a_resume_whose_source_started_the_node_does_not_start(self):
        for reach in (['build', 'lenses'], ['build', 'verdict']):
            with self.subTest(reach=reach):
                self.refused(run(graph(), args={'_resumeFromRunId': 'wf_source', '_resumeReach': reach}))

    def test_a_resume_that_does_not_say_what_its_source_reached_does_not_start(self):
        # A launch assembled by hand names a source run and nothing else.
        # Nothing shows it stops before the node, so it is refused.
        self.refused(run(graph(), args={'_resumeFromRunId': 'wf_source'}), 'RESUME-REACH-UNKNOWN')
        self.refused(run(graph(), args={'_resumeFromRunId': 'wf_source', '_resumeReach': 'build'}), 'RESUME-REACH-UNKNOWN')

    def test_prior_results_and_the_embedded_prefix_are_read_as_well(self):
        fine = {'_resumeFromRunId': 'wf_source', '_resumeReach': ['build']}
        self.refused(run(graph(), args=dict(fine, _priorResults={'verdict': DESIGN})))
        ok = run(graph(), args=dict(fine, _priorResults={'build': DESIGN, 'lenses': None}))
        self.assertEqual((ok.get('r') or {}).get('outcome'), 'COMPLETE', ok)
        # The replay state is compiled into the script, so it is what this launch would replay.
        entry = {'label': 'lenses:a', 'promptSha256': '0' * 64, 'optsSha256': '0' * 64}
        self.refused(run(graph(), args=fine, replay_state={'_prefix': [entry], '_memo': {}}))
        self.refused(run(graph(), args=fine, replay_state={'_prefix': [], '_memo': {'k': {'node': 'verdict'}}}))
        before = run(graph(), args=fine, replay_state={'_prefix': [dict(entry, label='build')], '_memo': {}})
        self.assertEqual((before.get('r') or {}).get('outcome'), 'COMPLETE', before)
        # A launch that carries the replay state and names no source is the same resume.
        self.refused(run(graph(), replay_state={'_prefix': [entry], '_memo': {}}))

    def test_a_first_launch_and_an_undeclared_graph_are_untouched(self):
        self.assertEqual(run(graph())['r']['outcome'], 'COMPLETE')
        plain = run(graph(flag=False), args={'_resumeFromRunId': 'wf_source'})
        self.assertEqual((plain.get('r') or {}).get('outcome'), 'COMPLETE', plain)


class Recorder(unittest.TestCase):
    """record-run.py does not file a second roll of the review under one run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='no resume record ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for argv in (('init', '-q'), ('config', 'user.name', 'fixture'), ('config', 'user.email', 'fixture@example.invalid')):
            subprocess.run(['git', *argv], cwd=self.root, check=True, capture_output=True)
        (self.root / '.gitignore').write_text('.claude/\nresult.json\n', encoding='utf-8')
        (self.root / 'WORK.md').write_text('# Campaign\n\n```json graph-ir\n' + json.dumps(graph()) + '\n```\n\n## Evidence\n\n'
                                           '| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'base'], cwd=self.root, check=True)

    def record(self, summary, run_id='wf_review', want=0):
        (self.root / 'result.json').write_text(json.dumps(summary), encoding='utf-8')
        done = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'), '--root', str(self.root), '--graph',
                               str(self.root / 'WORK.md'), '--run-id', run_id, '--result', str(self.root / 'result.json')],
                              cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(done.returncode, want, done.stdout + done.stderr)
        return done

    def filed(self, run_id='wf_review'):
        return json.loads((self.root / '.claude/fluxpoint/runs' / (run_id + '.json')).read_text(encoding='utf-8'))

    def rows(self):
        return [line for line in (self.root / 'WORK.md').read_text(encoding='utf-8').splitlines() if line.startswith('| 20')]

    def test_a_later_attempt_under_one_run_id_is_not_filed(self):
        # The run died inside the review and was recorded. It was then resumed by
        # hand: the lenses after the dead one ran again and this time all returned.
        self.record(recorded(dead={'lenses:b'}))
        done = self.record(recorded(), want=2)
        self.assertIn(CODE, done.stderr)
        self.assertEqual(self.filed()['summary']['outcome'], 'NODE-DEAD')
        self.assertEqual(len(self.rows()), 1)

    def test_the_same_attempt_is_recorded_again_without_complaint(self):
        summary = recorded()
        self.record(summary)
        self.record(summary)
        self.assertEqual(self.filed()['summary']['outcome'], 'COMPLETE')

    def test_an_attempt_after_one_that_stopped_before_the_node_is_filed(self):
        self.record(recorded(dead={'build'}))
        self.record(recorded())
        self.assertEqual(self.filed()['summary']['outcome'], 'COMPLETE')

    def test_a_new_launch_has_its_own_run_id_and_its_own_record(self):
        self.record(recorded(dead={'lenses:b'}), run_id='wf_first')
        self.record(recorded(), run_id='wf_second')
        self.assertEqual(self.filed('wf_second')['summary']['outcome'], 'COMPLETE')

    def test_a_resume_that_names_its_source_is_held_to_the_source_record(self):
        self.record(recorded(dead={'lenses:b'}), run_id='wf_source')
        resumed = dict(recorded(), resumeMode='captured-prefix', resumeSource={'runId': 'wf_source', 'artifactSha256': 'f' * 64})
        done = self.record(resumed, run_id='wf_resumed', want=2)
        self.assertIn(CODE, done.stderr)
        self.assertFalse((self.root / '.claude/fluxpoint/runs/wf_resumed.json').exists())

    def test_a_graph_without_the_field_records_every_attempt(self):
        self.record(recorded(graph(flag=False), dead={'lenses:b'}))
        self.record(recorded(graph(flag=False)))
        self.assertEqual(self.filed()['summary']['outcome'], 'COMPLETE')


class Launcher(unittest.TestCase):
    """resume.py --run refuses before it plans or compiles anything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='no resume ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for argv in (('init', '-q'), ('config', 'user.name', 'fixture'), ('config', 'user.email', 'fixture@example.invalid')):
            subprocess.run(['git', *argv], cwd=self.root, check=True, capture_output=True)
        (self.root / '.gitignore').write_text('.claude/\n', encoding='utf-8')
        self.write(graph())
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'base'], cwd=self.root, check=True)
        self.args = argparse.Namespace(root=str(self.root), graph='WORK.md', run='wf_previous', args=None, set=[],
                                       set_json=[], unpinned_ok=[], out=None, projects=str(self.root / 'projects'))
        self.compiled = []
        real_module, real_run = resume._module, resume._run
        self.addCleanup(mock.patch.stopall)

        def modules(name):
            if name == 'replay_memo':
                return types.SimpleNamespace(build_memo=mock.Mock(return_value={'_memo': {}, '_prefix': []}))
            return real_module(name)

        def commands(command, root):
            if str(command[0]) == sys.executable and str(command[1]).endswith('compile-graph.py'):
                self.compiled.append('plan' if '--plan' in command else 'compile')
                return 'fixture\n'
            return real_run(command, root)

        mock.patch.object(resume, '_module', side_effect=modules).start()
        mock.patch.object(resume, '_run', side_effect=commands).start()

    def write(self, ir):
        (self.root / 'WORK.md').write_text('```json graph-ir\n' + json.dumps(ir) + '\n```\n', encoding='utf-8')

    def record(self, summary):
        embedded = {}
        digest = ea.args_hash(embedded)
        source = ('// immutable executed source\nconst ARGS = ' + ea.canonical(embedded) +
                  ';\nconst ARGS_SHA256 = "' + digest + '";\n').encode('utf-8')
        archive = self.root / ea.ARCHIVES / (digest + '-' + hashlib.sha256(source).hexdigest() + '.graph.js')
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(source)
        summary = dict(copy.deepcopy(summary), argsSha256=digest, inputs={},
                       launch={'since': '2026-11-11T00:00:00Z', 'nonce': 'original', 'root': str(self.root)})
        store = self.root / '.claude/fluxpoint/runs'
        store.mkdir(parents=True, exist_ok=True)
        (store / 'wf_previous.json').write_text(json.dumps({'runId': 'wf_previous', 'summary': summary}), encoding='utf-8')

    def test_a_run_that_died_inside_the_fan_out_is_refused(self):
        self.record(recorded(dead={'lenses:b'}))
        with self.assertRaisesRegex(ValueError, '^' + CODE):
            resume.assemble(self.args)
        self.assertEqual(self.compiled, [])

    def test_a_run_that_completed_the_fan_out_is_refused(self):
        self.record(recorded(dead={'verdict'}))
        with self.assertRaisesRegex(ValueError, '^' + CODE):
            resume.assemble(self.args)
        self.assertEqual(self.compiled, [])

    def test_a_run_that_stopped_before_it_resumes_and_says_what_it_reached(self):
        self.record(recorded(dead={'build'}))
        workflow = resume.assemble(self.args)
        self.assertEqual(self.compiled, ['plan', 'compile', 'compile'])
        self.assertEqual(workflow['args']['_resumeFromRunId'], 'wf_previous')
        # The build died without a result, so the source run holds nothing the node waits on.
        self.assertEqual(workflow['args']['_resumeReach'], [])
        # The compiled script accepts exactly what the launcher assembled.
        doc = run(graph(), args={key: workflow['args'][key] for key in ('_resumeFromRunId', '_resumeReach')})
        self.assertEqual((doc.get('r') or {}).get('outcome'), 'COMPLETE', doc)

    def test_the_journal_of_an_unrecorded_attempt_is_read(self):
        self.record(recorded(dead={'build'}))
        journal = self.root / 'projects/session/subagents/workflows/wf_previous/journal.jsonl'
        journal.parent.mkdir(parents=True)
        rows = [{'type': 'started', 'agentId': 'a1', 'label': 'build'}, {'type': 'result', 'agentId': 'a1'},
                {'type': 'started', 'agentId': 'a2', 'opts': {'label': 'lenses:a'}}]
        journal.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '^' + CODE):
            resume.assemble(self.args)
        self.assertEqual(self.compiled, [])

    def test_a_graph_without_the_field_resumes_as_before(self):
        self.write(graph(flag=False))
        self.record(recorded(graph(flag=False), dead={'lenses:b'}))
        workflow = resume.assemble(self.args)
        self.assertNotIn('_resumeReach', workflow['args'])
        self.assertEqual(workflow['args']['_resumeFromRunId'], 'wf_previous')


class Plan(unittest.TestCase):
    """compile-graph.py --plan --resume refuses with the same code."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='no resume plan ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for argv in (('init', '-q'), ('config', 'user.name', 'fixture'), ('config', 'user.email', 'fixture@example.invalid')):
            subprocess.run(['git', *argv], cwd=self.root, check=True, capture_output=True)
        (self.root / 'WORK.md').write_text('```json graph-ir\n' + json.dumps(graph()) + '\n```\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'base'], cwd=self.root, check=True)

    def plan(self, summary, journal=None):
        summary = dict(copy.deepcopy(summary), launch={'since': '2026-11-11T00:00:00Z', 'nonce': 'original'}, inputs={})
        if journal is not None:
            path = self.root / 'projects/session/subagents/workflows/wf_previous/journal.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text(''.join(json.dumps(row) + '\n' for row in journal), encoding='utf-8')
        memo, artifacts = cg._sibling('replay_memo'), cg._sibling('run_artifacts')
        real = cg._sibling
        with mock.patch.object(cg, '_sibling', side_effect=lambda name: {'replay_memo': memo, 'run_artifacts': artifacts}.get(name) or real(name)), \
                mock.patch.object(artifacts, 'read', return_value=({'summary': summary}, 'f' * 64)), \
                mock.patch.object(memo, 'build_memo', return_value={'_memo': {}, '_prefix': []}), \
                mock.patch.object(memo, '_journal_calls', side_effect=memo.ReplayError('no transcripts')):
            return cg.rerun_plan(graph(), CONTRACTS, {}, str(self.root), 'wf_previous', projects=str(self.root / 'projects'))

    def test_a_run_that_reached_the_node_has_no_plan(self):
        for how in ({'dead': {'lenses:b'}}, {'dead': {'verdict'}}, {}):
            with self.subTest(how=how), self.assertRaisesRegex(ValueError, '^' + CODE):
                self.plan(recorded(**how))

    def test_a_run_that_stopped_before_it_is_planned(self):
        rows = self.plan(recorded(dead={'build'}))
        self.assertEqual([row['node'] for row in rows], ['build', 'lenses', 'verdict'])
        # An attempt nobody recorded reached the lenses: the runtime journal holds its calls.
        with self.assertRaisesRegex(ValueError, '^' + CODE):
            self.plan(recorded(dead={'build'}), journal=[{'type': 'started', 'agentId': 'a1', 'label': 'lenses:a'}])

    def test_the_command_line_names_the_code(self):
        store = self.root / '.claude/fluxpoint/runs'
        store.mkdir(parents=True)
        summary = dict(recorded(dead={'lenses:b'}), launch={'since': '2026-11-11T00:00:00Z', 'nonce': 'original'})
        (store / 'wf_previous.json').write_text(json.dumps({'runId': 'wf_previous', 'summary': summary}), encoding='utf-8')
        (self.root / 'args.json').write_text('{}', encoding='utf-8')
        done = subprocess.run([sys.executable, str(SCRIPTS / 'compile-graph.py'), '--plan', str(self.root / 'WORK.md'),
                               '--resume', 'wf_previous', '--args', str(self.root / 'args.json'), '--gates-root', str(self.root),
                               '--projects', str(self.root / 'projects')], capture_output=True, text=True, cwd=self.root)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('resume plan refused: ' + CODE, done.stderr)


if __name__ == '__main__':
    unittest.main()
