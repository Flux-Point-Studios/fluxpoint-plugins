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
import re
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


# ---------------------------------------------------------------------------
# A run that follows its plan is charged exactly its estimate. That holds only
# while every call the run makes is priced under the id the plan priced it at,
# and the labels a run spawns under are written far from the plan: the repair
# diff tier spawns as repair-diff:<item>:r<n>:attack:<lens>, and the plan
# prices it as <checker>:diff-attack. A label the lookup misses is charged at
# the dearest call, which stopped the shipped repair template in round one
# under its own cap. So every shipped template is run here on its plan.
REPO = PLUGIN.parents[1]
HEAD = 'a' * 40
LAUNCH = {'goal': 'ship it', 'target': 'src', 'findings': ['Original targeted finding'], '_seen': {},
          '_base': {'sha': HEAD, 'branch': 'main', 'porcelain': '', 'root': '/campaign'},
          '_launch': {'since': '2026-11-11T00:00:00Z', 'nonce': 'nonce', 'root': '/campaign'},
          '_replayLaunchNonce': '1' * 64}
PIPELINE = ('const pipeline=async(xs,...ss)=>Promise.all(xs.map(async(x,i)=>{let p=x;'
            'for(const s of ss){p=await s(p,x,i)}return p}));')


def execute(js, agent_body, args, pre=''):
    """Run emitted source with an agent whose body may read `label`, `p` and `o`."""
    with tempfile.TemporaryDirectory() as tmp:
        script, out = Path(tmp) / 'run.mjs', Path(tmp) / 'out.json'
        script.write_text('\n'.join([
            "import {writeFileSync} from 'node:fs';", 'const calls=[],logs=[];', pre,
            "const agent=async(p,o)=>{const label=String(o.label||'');calls.push(label);", agent_body, '};',
            'const parallel=async(t)=>Promise.all(t.map(f=>f()));', PIPELINE,
            'const log=s=>logs.push(String(s)),phase=()=>{};', 'const args=' + json.dumps(args) + ';',
            'const budget={total:null,spent:()=>0,remaining:()=>1e9};', 'const workflow=0;',
            '(async () => {', js.replace('export const meta', 'const meta'),
            '})().then(r=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({r,calls,logs})))',
            '.catch(e=>writeFileSync(' + json.dumps(str(out)) + ',JSON.stringify({err:String(e&&e.stack||e),calls,logs})))',
        ]), encoding='utf-8')
        done = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=300)
        if not out.exists():
            raise AssertionError((done.stderr or done.stdout)[-3000:])
        doc = json.loads(out.read_text(encoding='utf-8'))
        if 'err' in doc:
            raise AssertionError(doc['err'][:3000])
        return doc


def shipped(name, cap=None):
    """A shipped template compiled by the command line with its cap at its own estimate, or at `cap`.

    Returns (estimate, emitted source, the cap the template ships with).
    """
    template = PLUGIN / 'templates' / name
    source = template.read_text(encoding='utf-8')
    block = cg.IR_FENCE.search(source)
    ir = json.loads(block.group(1))
    declared = ir['budget'].get('maxEstimatedTokens')

    def cli(*argv):
        return subprocess.run([sys.executable, str(PLUGIN / 'scripts' / 'compile-graph.py'), *argv],
                              capture_output=True, text=True, encoding='utf-8', cwd=str(REPO))

    check = cli(str(template), '--check', '--gates-root', str(REPO))
    if check.returncode:
        raise AssertionError(name + ': ' + check.stderr)
    total = int(re.search(r'~([\d,]+) estimated tokens', check.stdout).group(1).replace(',', ''))
    ir['budget']['maxEstimatedTokens'] = total if cap is None else cap
    with tempfile.TemporaryDirectory() as tmp:
        copy, out = Path(tmp) / name, Path(tmp) / 'out.graph.js'
        copy.write_text(source[:block.start(1)] + json.dumps(ir, indent=2) + source[block.end(1):], encoding='utf-8')
        done = cli(str(copy), '--gates-root', str(REPO), '-o', str(out))
        if done.returncode:
            raise AssertionError(name + ': ' + done.stderr)
        return total, out.read_text(encoding='utf-8'), declared


# Every node of the five non-repair templates, answering as the plan expects:
# each finder returns expectItems findings and each panel casts all its votes.
PLAN_PRE = r"""
const HEAD='a'.repeat(40), LENSES=['authority','value','state','input','operations','composition'];
const subject=[{repository:'/campaign',base:'b'.repeat(40),head:HEAD,files:3,additions:10,deletions:2}];
const cov=lens=>({subject:structuredClone(subject),attackers:lens?1:LENSES.length,lenses:lens?[lens]:LENSES,sampled:false});
let seq=0;
const finding=n=>({file:'src/f'+n+'.py',line:n,claim:'defect number '+n+' in the reviewed code path',failure_path:'input '+n+' reaches the unchecked branch',severity:'LOW',defectClass:'class-'+n});
const slice=()=>({baseSeen:HEAD,branch:'claude/graph-x',diffSummary:'d',testsAdded:['t'],harnessCommand:'scripts/harness.sh --full',harnessExit:0,evidence:['ran it: exit 0'],sharedResources:[],siblings:[],packetEdits:[]});
"""
PLAN_AGENT = r"""
if(label.startsWith('tree-check:'))return {root:'/campaign',head:HEAD,porcelain:''};
if(/:refute\d+$/.test(label))return {refuted:REFUTED,reason:'independent reproduction settles this claim'};
if(label.startsWith('find:'))return {findings:[finding(++seq),finding(++seq),finding(++seq)]};
if(label==='curate')return {findings:[finding(++seq),finding(++seq),finding(++seq),finding(++seq)]};
if(label.startsWith('hunt:'))return {findings:[finding(++seq),finding(++seq)]};
if(label.startsWith('design:'))return {summary:'s',plan:['p'],files:['f'],risks:['r'],baseSeen:HEAD};
if(label.startsWith('choose:judge')){const keys=['smallest-diff','risk-first','contract-first','none'];
  return {winner:'smallest-diff',ranking:keys,rationale:'the smallest diff ships observable value with the least blast radius',
    strongest_objection:Object.fromEntries(keys.map(k=>[k,'the strongest objection to '+k+' is its cost']))};}
if(label==='build'||label==='prove')return slice();
if(label==='gate')return {exit:0,command:'scripts/harness.sh --full',tail:'ok'};
if(label==='gate-proof')return {gate:'harness',exit:0,attestId:'att_1',sha:HEAD};
if(label==='proof-audit')return {verdict:'SOUND',surface:['a.dfy'],findings:[],evidence:['ran'],checks:[]};
if(label.startsWith('red-team-lenses:')){const lens=label.split(':')[1];
  const f=[1,2].map(i=>({severity:'HIGH',finding:lens+' defect '+i,exploit_path:'attack '+lens+' '+i+' with a valid control',minimal_fix:'guard it',target:'src/'+lens+'.py:'+i,class:'bypass-'+i}));
  return {verdict:'BLOCK',findings:f,worstSeverity:'HIGH',worstSeverityRank:3,coverage:cov(lens),baseSeen:HEAD};}
if(label==='red-team-refute')return {verdict:'BLOCK',findings:[],worstSeverity:'NONE',worstSeverityRank:0};
if(label==='red-team')return {verdict:'SHIP',findings:[],worstSeverity:'NONE',worstSeverityRank:0,coverage:cov()};
throw new Error('unexpected call '+label);
"""

# The repair template on its plan: every slice passes its checker, each diff
# attacker raises one claim, three refuters test each claim, and the claims an
# integration review raises stay open, so the slices reopen for the next round.
REPAIR_PRE = r"""
const HEAD='a'.repeat(40), MANIFEST=__MANIFEST__, MSHA=__MSHA__;
const snaps={}; let tok=0;
const ORIGIN='python repro.py', CTRL='python valid.py';
const seatObs=(base,sha)=>({base,patchSha256:sha,attack:{command:ORIGIN,result:'exit 0: defended'},control:{command:CTRL,result:'exit 0: valid'},reproduces:false});
const observed=reproduces=>({command:ORIGIN,result:reproduces?'exit 1: harm':'exit 0: defended',controlCommand:CTRL,controlResult:'exit 0: valid',controlPassed:true,reproduces});
const sliceResult=()=>({baseSeen:HEAD,branch:'repair',patchSha256:MANIFEST.patchSha256,diffSummary:'complete patch',testsAdded:['t'],harnessCommand:'pytest',harnessExit:0,evidence:['ran: exit 0'],sharedResources:[],siblings:[],packetEdits:[]});
const ctx=p=>JSON.parse(p.split('ORIGIN REPLAY CONTEXT: ')[1].split('\n')[0]);
"""
REPAIR_AGENT = r"""
const a=Object.fromEntries([...p.matchAll(/--([a-z0-9-]+) '([^']*)'/g)].map(x=>[x[1],x[2]]));
if(label.startsWith('tree-check:'))return {root:'/campaign',head:HEAD,porcelain:''};
if(label.startsWith('repair-witness:read:'))return {status:'READ',base:a.base,patch:a.patch,item:a.item,round:Number(a.round),manifest:MANIFEST,manifestSha256:MSHA};
if(label.startsWith('repair-witness:materialize:')){const snapshot='/fixture/'+(++tok)+'/'+label;
  snaps[snapshot]={base:a.base,patchSha256:a.sha256,obs:[seatObs(a.base,a.sha256)]};
  return {status:'READY',base:a.base,patchSha256:a.sha256,snapshot,token:tok.toString(16).padStart(64,'0')};}
if(label.startsWith('repair-witness:check:')){const s=snaps[a.snapshot];
  const pc={kind:'packet-check',status:'CHECKED',exit:0,spec:a.spec,base:s.base,patchSha256:s.patchSha256};s.obs.push(pc);return pc;}
if(label.startsWith('repair-witness:cleanup:'))return {status:'CLEANED',snapshot:a.snapshot,token:a.token,observations:snaps[a.snapshot].obs};
if(label.startsWith('fix:')||label.startsWith('integrate:'))return sliceResult();
if(label.startsWith('check:')||label.startsWith('integration-check:')){
  const c=ctx(p), m=JSON.parse(p.split('HARNESS PATCH MANIFEST (derived from raw patch bytes, never your own inventory): ')[1].split('\n')[0]);
  const snapshot=JSON.parse(p.split('SNAPSHOT IDENTITY: ')[1].split('\n')[0]);
  const executions=[];
  const runCase=command=>{const o={command,result:'executed and defended'};executions.push({base:c.base,patchSha256:m.patchSha256,attack:o,control:o,reproduces:false});return o};
  const siblings=c.targetClasses.flatMap(finding=>['field','encoding','caller','network','role','path'].map(axis=>{const o=runCase('sibling '+finding+' '+axis);
    return {finding,invariant:'inv',axis,surface:'unlisted-'+axis,attack:o,control:o,outcome:'DEFENDED',reason:'executed'}}));
  const hunks=m.hunks.map(h=>{const o=runCase('hunk '+h.path);return {id:h.id,surface:'other',attack:o,control:o}});
  snaps[snapshot.snapshot].obs=executions;
  return {verdict:'PASS',findings:[],blockers:[],evidence:['independent checker executed'],targetClasses:c.targetClasses,siblings,diffCoverage:{patchSha256:m.patchSha256,hunks}};
}
if(label.startsWith('repair-diff:')&&label.includes(':attack:')){
  const m=JSON.parse(p.split('PINNED PATCH SUBJECT: ')[1].split('\n')[0]), c=ctx(p), lens=label.split(':').pop();
  const r={base:c.base,patchSha256:m.patchSha256,completed:true,evidence:['executed attacks with valid controls'],blockers:[],findings:[]};
  if(lens!=='coverage-skeptic'){
    const cur=JSON.stringify([(c.integrated||c.current).item,(c.integrated||c.current).round,(c.integrated||c.current).patch]);
    const rounds=c.patches.map(x=>({...x,...observed(!c.integrated&&JSON.stringify([x.item,x.round,x.patch])===cur)}));
    r.findings=[{finding:'claim by '+lens+' r'+c.round,severity:'HIGH',defectClass:'cls-'+lens+'-r'+c.round,invariant:'inv',classRewire:'rewire',measurement:'measured',
      origin:c.integrated?'cross-slice':'fix-introduced',...(c.integrated?{}:{introducedIn:c.round}),
      originEvidence:{base:{snapshot:c.base,...observed(false)},targetClasses:c.targetClasses,rounds,...(c.integrated?{integrated:{...c.integrated,...observed(true)}}:{})}}];
  }
  return r;
}
if(label.startsWith('repair-diff:')&&label.includes(':refute:')){
  const c=ctx(p);
  return {resolution:c.integrated?'CONFIRMED':'REFUTED',attack:{command:ORIGIN,result:'exit 0: defended'},control:{command:CTRL,result:'exit 0: valid'},contradiction:'executed case contradicts the claim'};
}
throw new Error('unexpected call '+label);
"""


class ShippedTemplatesOnPlan(unittest.TestCase):
    def test_every_other_template_on_its_plan_spends_its_estimate_and_declines_nothing(self):
        names = sorted(path.name for path in (PLUGIN / 'templates').glob('WORK*.md')
                       if cg.IR_FENCE.search(path.read_text(encoding='utf-8')) and path.name != 'WORK.repair.md')
        self.assertGreaterEqual(len(names), 5, names)
        for name in names:
            total, js, _ = shipped(name)
            for refuted in ('true', 'false'):
                with self.subTest(template=name, refuted=refuted):
                    doc = execute(js, PLAN_AGENT.replace('REFUTED', refuted), LAUNCH, PLAN_PRE)
                    declined = [line for line in doc['logs'] if 'token ceiling' in line]
                    self.assertEqual((doc['r']['declinedByTokens'], declined), (0, []))
                    self.assertLessEqual(abs(doc['r']['estimatedSpend'] - total), 1)
                    self.assertEqual(doc['r']['unpriced'], [])

    def test_the_repair_template_follows_its_plan_under_its_own_cap(self):
        from repair_diff import compact_manifest, manifest_digest
        manifest = compact_manifest(b'diff --git a/fixture b/fixture\n--- a/fixture\n+++ b/fixture\n@@ -0,0 +1 @@\n+fixture\n')
        pre = REPAIR_PRE.replace('__MANIFEST__', json.dumps(manifest)).replace('__MSHA__', json.dumps(manifest_digest(manifest)))
        args = {key: LAUNCH[key] for key in ('findings', '_base', '_launch', '_replayLaunchNonce')}
        total, js, declared = shipped('WORK.repair.md')
        self.assertGreater(declared, total)
        for cap in (None, declared):
            with self.subTest(cap=cap or 'the estimate'):
                if cap is not None:
                    _, js, _ = shipped('WORK.repair.md', cap)
                doc = execute(js, REPAIR_AGENT, args, pre)
                declined = [line for line in doc['logs'] if 'NOT RUN' in line]
                self.assertEqual((doc['r']['declinedByTokens'], declined), (0, []))
                self.assertNotEqual(doc['r']['outcome'], 'BUDGET-EXHAUSTED')
                # The diff tier ran in every planned round, priced as the plan priced it.
                rounds = {label.split(':')[2] for label in doc['calls'] if label.startswith('repair-diff:')}
                self.assertEqual(rounds, {'r1', 'r2', 'r3'})
                self.assertLessEqual(doc['r']['estimatedSpend'], total)
                self.assertEqual(doc['r']['unpriced'], [])


class LabelsThePlanDidNotWrite(unittest.TestCase):
    """The price of a call is found from its label, so a label that reads like another kind of call is a mispricing."""

    def on_plan(self, ir, finders, extra=None):
        est = cg.estimate_tokens(ir, CONTRACTS)
        capped = json.loads(json.dumps(ir))
        capped['budget']['maxEstimatedTokens'] = est['total']
        self.assertEqual(cg.validate(capped, CONTRACTS), [])
        ids = sorted((n['id'] for n in ir['nodes']), key=len, reverse=True)
        pre = ('let seq=0;const FINDERS=%s,IDS=%s;const OWNER=l=>IDS.find(i=>l===i||l.startsWith(i+":"));'
               % (json.dumps(finders), json.dumps(ids)))
        agent = r"""
if(label.startsWith('tree-check:'))return {root:'/campaign',head:'a'.repeat(40),porcelain:''};
if(/:\d+:refute\d+$/.test(label))return {refuted:false,reason:'the claim stands on reproduction'};
const own=OWNER(label);
if(FINDERS[own]!==undefined)return {findings:Array.from({length:FINDERS[own]},()=>({file:'f'+(++seq)+'.py',line:seq,claim:'defect number '+seq+' in the code',failure_path:'path '+seq,severity:'LOW'}))};
return {summary:'s',plan:[],files:[],risks:[]};
"""
        doc = execute(cg.emit(capped, CONTRACTS, {}), agent, dict(LAUNCH, **(extra or {})), pre)
        return est, doc

    def test_a_node_named_like_the_tree_sentinel_is_priced_as_a_node(self):
        ir = {'version': 1, 'campaign': 'c', 'budget': {'maxNodes': 6, 'cacheTtl': '1h'}, 'nodes': [
            {'id': 'build', 'prompt': 'build', 'contract': 'DesignV1'},
            {'id': 'tree-check', 'after': 'build', 'prompt': 'check the tree of {{prev}}', 'contract': 'DesignV1'},
            {'id': 'report', 'after': 'tree-check', 'prompt': 'report {{prev}}', 'contract': 'DesignV1'}]}
        est, doc = self.on_plan(ir, {})
        self.assertEqual(doc['r']['outcome'], 'COMPLETE', doc['logs'])
        self.assertEqual(doc['r']['declinedByTokens'], 0)
        self.assertLessEqual(abs(doc['r']['estimatedSpend'] - est['total']), 1)

    def test_a_list_key_that_reads_like_a_refuter_or_an_advisor_is_still_the_node_s_own_work(self):
        for keys in (['docs', 'tests'], ['advice', 'tests'], ['refute1', 'tests']):
            with self.subTest(keys=keys):
                ir = {'version': 1, 'campaign': 'c', 'treeGuard': False, 'budget': {'maxNodes': 20, 'cacheTtl': '1h'},
                      'lists': {'dims': [{'key': key} for key in keys]},
                      'nodes': [{'id': 'find', 'foreach': 'dims', 'prompt': 'find {{item.key}}', 'contract': 'FindingsV1',
                                 'verify': 'panel:3', 'verifyOver': 'findings', 'expectItems': 2, 'effort': 'low',
                                 'model': 'claude-haiku'}]}
                est, doc = self.on_plan(ir, {'find': 2})
                self.assertEqual(doc['r']['declinedByTokens'], 0, doc['logs'])
                self.assertLessEqual(abs(doc['r']['estimatedSpend'] - est['total']), 1)

    def test_a_call_the_plan_did_not_price_is_named_in_the_summary(self):
        # Charged at the dearest call, as before, and no longer in silence.
        ir = sweep(cap=10 ** 9)
        emitted = cg.emit(ir, CONTRACTS, {})
        self.assertEqual(emitted.count('label: "report"'), 1)
        js = emitted.replace('label: "report"', 'label: "somewhere-else"')
        doc = execute(js, "if(label==='find')return {findings:[]};return {summary:'s',plan:[],files:[],risks:[]};", {})
        self.assertEqual(doc['r']['unpriced'], ['somewhere-else'])
        self.assertTrue(any('somewhere-else' in line and 'not priced' in line for line in doc['logs']), doc['logs'])


if __name__ == '__main__':
    unittest.main()
