#!/usr/bin/env python3
"""Two attested gates on one judged commit (#235, #236).

A repair graph ran a final gate, then a red-team verdict, then a second
gate that re-ran probes on the same commit. Two things were wrong with
that shape. Both `prove:` nodes had to use the contract named ExecutionV1,
so the second gate's return shape was folded into the first's as an arm of
one overloaded schema. And nothing tied the second gate's commit to the
first's: each resolved the branch when it ran, so a branch that moved in
between was certified at two heads and the run recorded COMPLETE.

The compiler now reads a prove: node's contract by its structure, and a
node can name the node whose commit it must share. Executed three ways:
the validator, the emitted graph under stubs, and the warnings.
"""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
_spec = importlib.util.spec_from_file_location('cg_prove_gates', PLUGIN / 'scripts' / 'compile-graph.py')
cg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cg)
SHIPPED = cg.load_contracts(str(PLUGIN / 'contracts'))
GATES = {'harness', 'reruns'}
HEAD, MOVED = 'a' * 40, 'b' * 40


def reruns():
    """The second gate's own contract: what the attest check reads, plus what only this gate returns."""
    return {'$id': 'RerunsV1', 'type': 'object', 'additionalProperties': False,
            'required': ['gate', 'exit', 'attestId', 'sha', 'probes'],
            'properties': {'gate': {'type': 'string', 'minLength': 1}, 'exit': {'type': 'integer'},
                           'attestId': {'type': 'string', 'minLength': 1},
                           'sha': {'type': 'string', 'pattern': '^[0-9a-f]{40}$'},
                           'probes': {'type': 'integer', 'minimum': 1}}}


def contracts(second=None):
    return dict(SHIPPED, RerunsV1=second if second is not None else reruns())


def graph(**second):
    nodes = [
        {'id': 'final', 'prompt': 'run the final gate', 'contract': 'ExecutionV1', 'verify': 'prove:harness',
         'haltWhen': 'exit != 0'},
        {'id': 'verdict', 'after': 'final', 'prompt': 'judge the commit the gate proved: {{prev}}', 'contract': 'DesignV1'},
        dict({'id': 'reruns', 'after': 'verdict', 'prompt': 'rerun the probes after {{prev}}', 'contract': 'RerunsV1',
              'verify': 'prove:reruns', 'haltWhen': 'exit != 0'}, **second),
        {'id': 'report', 'after': 'reruns', 'prompt': 'report {{prev}}', 'contract': 'DesignV1'},
    ]
    return {'version': 1, 'campaign': 'two gates', 'treeGuard': False, 'budget': {'maxNodes': 8}, 'nodes': nodes}


def findings(ir, known=None):
    return cg.validate(ir, known or contracts(), gates=GATES)


def run(ir, second_sha=HEAD, known=None, first=None):
    """Execute the emitted graph: both gates pass, and the second judged `second_sha`."""
    js = cg.emit(ir, known or contracts(), {}).replace('export const meta', 'const meta')
    first = {'gate': 'harness', 'exit': 0, 'attestId': 'att_first', 'sha': HEAD} if first is None else first
    second = {'gate': 'reruns', 'exit': 0, 'attestId': 'att_second', 'sha': second_sha, 'probes': 3}
    args = {'_launch': {'since': '2026-11-11T00:00:00Z', 'nonce': 'a1b2c3'}, '_ledger': {},
            '_replayLaunchNonce': 'prove-gates-current-attempt'}
    with tempfile.TemporaryDirectory() as tmp:
        script, out = Path(tmp) / 'run.mjs', Path(tmp) / 'out.json'
        script.write_text('\n'.join([
            "import {writeFileSync} from 'node:fs';",
            'const calls=[];',
            'const agent=async(p,o)=>{const label=String(o.label||\'\');calls.push(label);',
            '  if(label===\'final\')return ' + json.dumps(first) + ';',
            '  if(label===\'reruns\')return ' + json.dumps(second) + ';',
            "  return {summary:'s',plan:'p',files:[],risks:[]}};",
            'const parallel=async(t)=>Promise.all(t.map(f=>f()));',
            'const pipeline=async()=>[],log=()=>{},phase=()=>{};',
            'const args=' + json.dumps(args) + ';',
            'const budget={total:null,spent:()=>0,remaining:()=>1e9};',
            'const workflow=0;',
            '(async () => {', js,
            '})().then(r=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({r,calls})))',
            '.catch(e=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({err:String(e&&e.message||e)})))',
        ]), encoding='utf-8')
        done = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
        if not out.exists():
            raise AssertionError(done.stderr or done.stdout)
        return json.loads(out.read_text(encoding='utf-8'))


class OwnContract(unittest.TestCase):
    """#235: a prove: node's contract is read by what it requires, not by its name."""

    def test_a_second_gate_carries_its_own_contract(self):
        self.assertEqual(findings(graph()), [])
        doc = run(graph())
        self.assertEqual((doc.get('r') or {}).get('outcome'), 'COMPLETE', doc)
        self.assertEqual(doc['r']['results']['reruns']['probes'], 3)
        # The recorder checks a prove: node by this map, whatever its contract is called.
        self.assertEqual(doc['r']['prove'], {'final': 'harness', 'reruns': 'reruns'})

    def test_a_contract_missing_what_the_attest_check_reads_is_refused(self):
        def without(field):
            schema = reruns()
            schema['required'].remove(field)
            return schema

        def retyped(field, **shape):
            schema = reruns()
            schema['properties'][field] = shape
            return schema

        weak = {'sha': without('sha'), 'attestId': without('attestId'), 'exit': retyped('exit', type='string'),
                'gate': retyped('gate', type='integer'),
                'the pattern': retyped('sha', type='string', pattern='^[0-9a-f]+$'),
                'an object': {'$id': 'RerunsV1', 'type': 'array'}}
        for names, schema in weak.items():
            with self.subTest(names=names):
                found = [f for f in findings(graph(), contracts(schema)) if 'prove:reruns' in f]
                self.assertEqual(len(found), 1, found)
                self.assertIn(names, found[0])
                self.assertIn('RerunsV1', found[0])

    def test_a_gate_field_may_be_an_enum_of_gate_names(self):
        schema = reruns()
        schema['properties']['gate'] = {'enum': ['harness', 'reruns']}
        self.assertEqual(findings(graph(), contracts(schema)), [])
        schema['properties']['gate'] = {'enum': ['reruns', 7]}
        self.assertEqual(len([f for f in findings(graph(), contracts(schema)) if 'prove:reruns' in f]), 1)

    def test_a_local_contract_named_ExecutionV1_is_held_to_the_same_fields(self):
        # The name used to be the whole check, so a repository's own copy
        # could drop the commit and still back a prove: node.
        local = copy.deepcopy(SHIPPED['ExecutionV1'])
        local['required'].remove('sha')
        found = [f for f in findings(graph(), dict(contracts(), ExecutionV1=local)) if 'prove:harness' in f]
        self.assertEqual(len(found), 1, found)

    def test_the_preamble_names_the_one_accepted_spelling(self):
        # An agent whose own spelling of the witness command was refused fell
        # back to a bare python and started the gate under the wrong shell (#244).
        js = cg.emit(graph(), contracts(), {})
        self.assertIn("'bash ' + quoteShell(PLUGIN.root + '/scripts/py.sh') + ' attest.py", js)
        self.assertIn('Run each of these commands exactly as written', js)
        self.assertIn('never start attest.py with python directly', js)

    def test_a_contract_without_those_fields_is_still_refused(self):
        ir = graph()
        ir['nodes'][2]['contract'] = 'DesignV1'
        found = [f for f in findings(ir) if 'prove:reruns' in f]
        self.assertEqual(len(found), 1, found)
        self.assertIn('attestId', found[0])


class OneCommit(unittest.TestCase):
    """#236: two gates that must judge one commit say so, and the run halts when they did not."""

    def test_the_later_gate_names_the_node_whose_commit_it_shares(self):
        self.assertEqual(findings(graph(sameShaAs='final')), [])
        doc = run(graph(sameShaAs='final'))
        self.assertEqual((doc['r']['outcome'], doc['calls']), ('COMPLETE', ['final', 'verdict', 'reruns', 'report']), doc)

    def test_a_branch_that_moved_between_the_gates_halts_the_run(self):
        doc = run(graph(sameShaAs='final'), second_sha=MOVED)
        self.assertEqual(doc['r']['outcome'], 'HALTED', doc)
        self.assertNotIn('report', doc['calls'])
        note = next(row for row in doc['r']['provenance'] if row['node'] == 'reruns' and row['status'] == 'HALTED')
        for part in (HEAD, MOVED, 'final'):
            self.assertIn(part, note['detail'])
        # Without the field the same run is recorded COMPLETE over two heads.
        self.assertEqual(run(graph(), second_sha=MOVED)['r']['outcome'], 'COMPLETE')

    def test_a_commit_that_cannot_be_compared_halts_too(self):
        # The earlier gate's result carries no sha to compare: UNPROVEN at
        # that node already, and never COMPLETE.
        doc = run(graph(sameShaAs='final'), first={'gate': 'harness', 'exit': 0, 'attestId': 'att_first'})
        self.assertNotEqual((doc.get('r') or {}).get('outcome'), 'COMPLETE', doc)
        self.assertNotIn('report', doc.get('calls', []))

    def test_the_field_is_checked_when_the_graph_compiles(self):
        def refused(ir, known=None):
            return [f for f in findings(ir, known) if 'sameShaAs' in f]

        self.assertEqual(len(refused(graph(sameShaAs='nowhere'))), 1)
        self.assertEqual(len(refused(graph(sameShaAs='reruns'))), 1)
        self.assertEqual(len(refused(graph(sameShaAs='report'))), 1)
        self.assertEqual(len(refused(graph(sameShaAs=['final']))), 1)
        # Both nodes have to carry an exact commit.
        self.assertEqual(len(refused(graph(sameShaAs='verdict'))), 1)
        on_design = graph()
        on_design['nodes'][3]['sameShaAs'] = 'final'
        self.assertEqual(len(refused(on_design)), 1)
        fanned = graph(sameShaAs='final')
        fanned['lists'] = {'targets': [{'key': 'a'}, {'key': 'b'}]}
        fanned['nodes'][2].update(foreach='targets', prompt='rerun {{item.key}} after {{prev}}')
        self.assertEqual(len(refused(fanned)), 1)

    def test_check_warns_about_a_second_gate_that_names_no_commit(self):
        def about(ir):
            return [w for w in cg.warnings(ir, gates=GATES) if 'sameShaAs' in w]

        found = about(graph())
        self.assertEqual(len(found), 1, cg.warnings(graph(), gates=GATES))
        for part in ("'reruns'", "'final'"):
            self.assertIn(part, found[0])
        self.assertEqual(about(graph(sameShaAs='final')), [])
        # Two gates on separate branches of the graph judge separate things.
        apart = graph()
        apart['nodes'][2].pop('after')
        apart['nodes'][2]['prompt'] = 'rerun the probes'
        self.assertEqual(about(apart), [])


if __name__ == '__main__':
    unittest.main()
