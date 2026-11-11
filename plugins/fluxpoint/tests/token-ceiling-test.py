#!/usr/bin/env python3
"""budget.maxEstimatedTokens holds the run, and --check prices the node ceiling (#233).

The cap was compared with the planned estimate when the graph compiled and
never reached the compiled script. What the run was allowed to spend was
budget.maxNodes times whatever each call cost, and the plan leans on
expectItems, which is a guess: a sweep that found six times what it
expected ran six times the refuters, all of them inside the node ceiling
and none of them inside the cap.

The compiled script now prices every call it spawns at the figure the
estimate used for that node and declines a call that would pass the cap,
the way it declines one past the node ceiling. Executed: the emitted graph
under stubs, and the compiler's --check on a work file.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
_spec = importlib.util.spec_from_file_location('cg_token_ceiling', PLUGIN / 'scripts' / 'compile-graph.py')
cg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(PLUGIN / 'contracts'))
EXPECTED = 2


def sweep(cap=None, max_nodes=60):
    """One finder whose findings each go to three refuters, then a report."""
    budget = {'maxNodes': max_nodes}
    if cap is not None:
        budget['maxEstimatedTokens'] = cap
    return {'version': 1, 'campaign': 'sweep', 'treeGuard': False, 'budget': budget,
            'nodes': [{'id': 'find', 'prompt': 'find defects', 'contract': 'FindingsV1', 'verify': 'panel:3',
                       'verifyOver': 'findings', 'expectItems': EXPECTED},
                      {'id': 'report', 'after': 'find', 'prompt': 'report {{prev}}', 'contract': 'DesignV1'}]}


def planned():
    return cg.estimate_tokens(sweep(), CONTRACTS)


def run(ir, found):
    """Execute the emitted graph with a finder that returns `found` findings."""
    js = cg.emit(ir, CONTRACTS, {}).replace('export const meta', 'const meta')
    findings = [{'file': 'a.py', 'line': i + 1, 'claim': 'defect %d' % i, 'failure_path': 'p', 'severity': 'LOW'}
                for i in range(found)]
    with tempfile.TemporaryDirectory() as tmp:
        script, out = Path(tmp) / 'run.mjs', Path(tmp) / 'out.json'
        script.write_text('\n'.join([
            "import {writeFileSync} from 'node:fs';",
            'const calls=[],logs=[];',
            "const agent=async(p,o)=>{const label=String(o.label||'');calls.push(label);",
            "  if(label==='find')return {findings:" + json.dumps(findings) + '};',
            "  if(label.includes(':refute'))return {refuted:false,reason:'stands'};",
            "  return {summary:'s',plan:[],files:[],risks:[]}};",
            'const parallel=async(t)=>Promise.all(t.map(f=>f()));',
            'const pipeline=async()=>[],log=s=>logs.push(String(s)),phase=()=>{};',
            'const args={};',
            'const budget={total:null,spent:()=>0,remaining:()=>1e9};',
            'const workflow=0;',
            '(async () => {', js,
            '})().then(r=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({r,calls,logs})))',
            '.catch(e=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({err:String(e&&e.message||e)})))',
        ]), encoding='utf-8')
        done = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
        if not out.exists():
            raise AssertionError(done.stderr or done.stdout)
        doc = json.loads(out.read_text(encoding='utf-8'))
        if 'err' in doc:
            raise AssertionError(doc['err'])
        return doc


class RuntimeCap(unittest.TestCase):
    def test_a_run_that_follows_its_plan_spends_its_estimate_and_completes(self):
        # The plan is the sum of what its calls are priced at, so a cap set
        # to the estimate exactly must never decline a planned call.
        est = planned()
        doc = run(sweep(cap=est['total']), found=EXPECTED)
        self.assertEqual(doc['r']['outcome'], 'COMPLETE', doc['logs'])
        self.assertEqual((len(doc['calls']), doc['r']['declined']), (est['calls'], 0))
        self.assertLessEqual(abs(doc['r']['estimatedSpend'] - est['total']), 1)

    def test_a_run_that_outgrows_its_plan_stops_at_the_cap(self):
        est = planned()
        doc = run(sweep(cap=est['total']), found=12)
        # Twelve findings want thirty-six refuters. The plan priced six.
        self.assertNotEqual(doc['r']['outcome'], 'COMPLETE')
        self.assertGreater(doc['r']['declined'], 0)
        self.assertLess(len(doc['calls']), 1 + 36 + 1)
        self.assertLess(doc['r']['spawned'], 60)
        self.assertLessEqual(doc['r']['estimatedSpend'], est['total'])
        self.assertTrue(any('token ceiling' in line and 'NOT RUN' in line for line in doc['logs']), doc['logs'])
        # A call the cap declined is skipped work, the same as one past the node ceiling.
        self.assertFalse(any(row['status'] == 'DEAD' for row in doc['r']['provenance']), doc['r']['provenance'])

    def test_without_a_cap_only_the_node_ceiling_binds(self):
        doc = run(sweep(), found=12)
        self.assertEqual(len(doc['calls']), 1 + 36 + 1)
        self.assertEqual(doc['r']['declined'], 0)
        self.assertNotIn('estimatedSpend', {k for k, v in doc['r'].items() if v is None})

    def test_a_decline_names_the_ceiling_that_caused_it(self):
        # The node ceiling still declines as it did, in its own words.
        est = planned()
        by_nodes = run(sweep(max_nodes=est['calls']), found=12)
        self.assertTrue(any('budget ceiling' in line for line in by_nodes['logs']), by_nodes['logs'])
        self.assertFalse(any('token ceiling' in line for line in by_nodes['logs']))
        by_tokens = run(sweep(cap=est['total']), found=12)
        notes = ' '.join(str(row.get('detail')) for row in by_tokens['r']['provenance'])
        self.assertIn('token ceiling', notes)
        self.assertNotIn('node ceiling', notes)


class CheckPricesTheNodeCeiling(unittest.TestCase):
    def check(self, ir):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / 'WORK.md'
            work.write_text('# w\n\n```json graph-ir\n' + json.dumps(ir) + '\n```\n', encoding='utf-8')
            return subprocess.run([sys.executable, str(PLUGIN / 'scripts' / 'compile-graph.py'), str(work), '--check',
                                   '--gates-root', tmp], capture_output=True, text=True)

    def dearest(self):
        # The finder's own call, priced apart from its cheaper refuters.
        prices, _ = cg.call_prices(planned())
        self.assertGreater(prices['find']['work'], prices['find']['refute'])
        return max(price for row in prices.values() for price in row.values())

    def test_the_node_ceiling_is_priced_beside_the_plan(self):
        est = planned()
        allowed = int(round(60 * self.dearest()))
        done = self.check(sweep())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('node ceiling', done.stdout)
        self.assertIn(f'{allowed:,}', done.stdout)
        # With no cap, nothing but the node ceiling bounds the bill, and the line says so.
        self.assertIn('no budget.maxEstimatedTokens', done.stdout)
        capped = self.check(sweep(cap=est['total'] + 1000))
        self.assertEqual(capped.returncode, 0, capped.stderr)
        self.assertIn(f'{allowed:,}', capped.stdout)
        self.assertIn('the run stops at the cap', capped.stdout)

    def test_a_node_ceiling_inside_the_cap_says_nothing_more(self):
        est = planned()
        done = self.check(sweep(cap=est['total'] * 100, max_nodes=est['calls']))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('the run stops at the cap', done.stdout)
        self.assertNotIn('no budget.maxEstimatedTokens', done.stdout)


if __name__ == '__main__':
    unittest.main()
