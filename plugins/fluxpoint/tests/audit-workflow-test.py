#!/usr/bin/env python3
"""The multi-lens graph audit does what its command says (issue #97).

One graph-auditor per round read as convergence and was not: three rounds
fell to six findings with nothing above MEDIUM, and the next round, run as
six lenses with refuting verifiers, found two real HIGHs and refuted nine
findings the single reader would have handed over as work. The shipped
workflow is that round. Everything below EXECUTES it under node with stub
agent/parallel/pipeline/phase/log/args and reads what came back:

  - the default round fans out one auditor per lens, all six, each told
    where the checklist lives and what the compile check said
  - the reduce sees every raw finding, merges only what it is told to, and
    cannot drop, invent or double-place a finding or lower its severity
  - HIGH and above get three refuters (reproduce, reachability, scope) and
    survive only when two of the three fail to refute; below HIGH gets one
  - a verifier or lens that returns nothing leaves the round UNVERIFIED or
    unaudited, never SOUND, and says so in the counts
  - the settled list reaches the lenses, the reduce and the matched
    finding's refuters, and every match is counted, not dropped
  - mode 'single' is one agent call and its findings stay unverified
  - meta is a pure literal and the body never reads a clock or randomness,
    which the runtime forbids because they break resume
  - the command's claims about the workflow match the script
"""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
SCRIPT = os.path.join(PLUGIN, "workflows", "graph-audit.js")
COMMAND = os.path.join(PLUGIN, "commands", "graph-audit.md")
passed = failed = 0

DEFAULT_LENSES = ["commands", "data-flow", "verdicts", "runtime", "merge-deploy", "walkthrough"]
PHASES_SEEN = set()


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<62} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


with open(SCRIPT, encoding="utf-8") as fh:
    SRC = fh.read()

# The stubs mirror the runtime's contract: parallel() is a barrier whose
# failed thunks resolve null, pipeline() runs each item through its stages
# with (prev, item, index) and drops a throwing item to null, agent() returns
# whatever the case's responder says (null is a dead agent). Date.now and
# Math.random throw, as they do under the runtime.
HARNESS = r"""
import {writeFileSync} from 'node:fs';
const OUT = __OUT__;
const CALLS = [], LOGS = [], PHASES = [];
Date.now = () => { throw new Error('Date.now is unavailable in a workflow'); };
Math.random = () => { throw new Error('Math.random is unavailable in a workflow'); };
const F = (severity, title) => ({severity, title,
  defectClass: 'fixture-class', classRewire: 'derive the invariant from its system of record',
  failurePath: 'a concrete failure path for: ' + title,
  evidence: 'read WORK.md line 12 and ran the check',
  rewire: 'rewire the node so that ' + title + ' cannot happen'});
const reduceItems = (p) => JSON.parse(p.slice(p.indexOf('FINDINGS:\n') + 'FINDINGS:\n'.length));
const singletons = (p) => ({clusters: reduceItems(p).map(f => ({members: [f.id], settled: ''}))});
const respond = __RESPOND__;
const agent = async (prompt, opts = {}) => {
  CALLS.push({prompt: String(prompt), label: String(opts.label || ''), phase: opts.phase || null,
              effort: opts.effort || null, schema: !!opts.schema, isolation: opts.isolation || null});
  const v = respond(String(prompt), opts);
  return v === undefined ? null : v;
};
const parallel = async (thunks) =>
  Promise.all(thunks.map(t => Promise.resolve().then(t).catch(() => null)));
const pipeline = async (items, ...stages) => Promise.all(items.map(async (item, i) => {
  let prev = item;
  for (const s of stages) {
    try { prev = await s(prev, item, i); } catch (e) { return null; }
  }
  return prev;
}));
const phase = (t) => { PHASES.push(t); };
const log = (m) => { LOGS.push(String(m)); };
const args = __ARGS__;
const budget = {total: null, spent: () => 0, remaining: () => Infinity};
const workflow = 0;
(async () => {
__SCRIPT__
})().then(
  r => writeFileSync(OUT, JSON.stringify({r, calls: CALLS, logs: LOGS, phases: PHASES})),
  e => writeFileSync(OUT, JSON.stringify({err: String(e && e.message || e), calls: CALLS, logs: LOGS, phases: PHASES})));
"""


def run(args, respond="(p,o) => o.label === 'probe-plan' ? {nodes:[], argDefaults:{}} : {findings: []}"):
    """Execute the workflow once. Returns {r|err, calls, logs, phases}."""
    body = re.sub(r"(?m)^export const meta", "const meta", SRC)
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.json")
        w = os.path.join(d, "w.mjs")
        text = (HARNESS.replace("__OUT__", json.dumps(out))
                .replace("__RESPOND__", respond)
                .replace("__ARGS__", json.dumps(args))
                .replace("__SCRIPT__", body))
        with open(w, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        p = subprocess.run(["node", w], capture_output=True, text=True, timeout=120)
        if not os.path.exists(out):
            return {"err": "no output: " + (p.stderr or p.stdout)[-400:], "calls": [], "logs": [], "phases": []}
        with open(out, encoding="utf-8") as fh:
            res = json.load(fh)
    PHASES_SEEN.update(res.get("phases") or [])
    for c in res.get("calls") or []:
        if c.get("phase"):
            PHASES_SEEN.add(c["phase"])
    return res


BASE = {"graph": "WORK.md", "pluginRoot": "/plug", "ir": {"nodes": [], "argDefaults": {}},
        "check": "graph-compile: WORK.md ok, 10 node(s), ~900,000 estimated tokens"}


def labels(res, prefix):
    return [c for c in res["calls"] if c["label"].startswith(prefix)]


def find(items, title):
    return next((x for x in items if x["title"] == title), None)


# ============================================ default fan-out, nothing found
res = run(BASE)
r = res.get("r") or {}
lens_calls = labels(res, "lens:")
report("default round runs without error", "err" not in res, res.get("err", "ok")[:80])
report("one auditor per lens, six lenses", len(res["calls"]) == 6 and len(lens_calls) == 6,
       f"{len(lens_calls)} lens call(s) of {len(res['calls'])}")
report("the lenses are the six the command names",
       sorted(c["label"][5:] for c in lens_calls) == sorted(DEFAULT_LENSES),
       ",".join(c["label"][5:] for c in lens_calls))
report("every lens is pointed at the checklist, graph and check line",
       all("/plug/agents/graph-auditor.md" in c["prompt"] and "WORK.md" in c["prompt"]
           and BASE["check"] in c["prompt"] and c["schema"] for c in lens_calls),
       "all six" if lens_calls else "no lens calls")
report("zero raw findings skips the reduce and every verifier",
       not labels(res, "reduce") and not labels(res, "refute:"), "skipped")
c = r.get("counts") or {}
report("zero findings over six returned lenses is SOUND",
       r.get("verdict") == "SOUND" and c.get("raw") == 0 and c.get("distinct") == 0
       and c.get("confirmed") == 0 and c.get("unverified") == 0 and c.get("lensesReturned") == 6,
       f"{r.get('verdict')} {c}"[:90])
report("per-stage counts are logged", any(l.startswith("counts:") for l in res["logs"]),
       "logged" if any(l.startswith("counts:") for l in res["logs"]) else "MISSING")

# ============== reduce merges, HIGH gets three refuters, MEDIUM gets one
STALE = "terminal merge reads stale local refs"
STALE2 = "merge step trusts the local clone's refs"
MARKER = "regression marker filtered out by every gate"
RESPOND_MAIN = r"""(p, o) => {
  const l = String(o.label || '');
  if (l === 'lens:commands') return {findings: [F('HIGH', '%s')]};
  if (l === 'lens:verdicts') return {findings: [F('MEDIUM', '%s')]};
  if (l === 'lens:merge-deploy') return {findings: [F('MEDIUM', "%s")]};
  if (l.startsWith('lens:')) return {findings: []};
  if (l === 'reduce') {
    const items = reduceItems(p);
    // The MEDIUM restatement is listed first, so severity must come from
    // the members rather than from whichever the reduce put first.
    const merge = items.filter(f => /refs/.test(f.title)).map(f => f.id).reverse();
    const rest = items.filter(f => !/refs/.test(f.title)).map(f => ({members: [f.id], settled: ''}));
    return {clusters: [{members: merge, settled: ''}, ...rest]};
  }
  if (l.startsWith('refute:scope:')) return {refuted: true, reason: 'scope says this is out of bounds'};
  if (l.startsWith('refute:combined:')) return {refuted: true, reason: 'the gate already counts the marker'};
  if (l.startsWith('refute:')) return {refuted: false, reason: 'reproduced against the repository'};
  return null;
}""" % (STALE, MARKER, STALE2)
res = run(BASE, RESPOND_MAIN)
r = res.get("r") or {}
c = r.get("counts") or {}
red = labels(res, "reduce")
report("the reduce runs once, at low effort", len(red) == 1 and red[0]["effort"] == "low",
       f"{len(red)} call(s), effort {red[0]['effort'] if red else None}")
rin = red[0]["prompt"] if red else ""
report("the reduce sees every raw finding with its lens",
       all(t in rin for t in (STALE, STALE2, MARKER))
       and all(x in rin for x in ('"commands"', '"verdicts"', '"merge-deploy"')),
       "3 of 3" if all(t in rin for t in (STALE, STALE2, MARKER)) else "MISSING")
report("its clusters become the distinct findings", c.get("raw") == 3 and c.get("distinct") == 2,
       f"raw {c.get('raw')} -> distinct {c.get('distinct')}")
d_stale = find(r.get("confirmed") or [], STALE)
report("a merged cluster keeps its highest severity and every lens",
       bool(d_stale) and d_stale["severity"] == "HIGH"
       and sorted(d_stale["sources"]) == ["commands", "merge-deploy"],
       f"{d_stale and d_stale['severity']} {d_stale and d_stale['sources']}")
sid = d_stale["id"] if d_stale else "?"
high_refs = labels(res, "refute:")
on_high = [x for x in high_refs if x["label"].endswith(":" + sid)]
report("a HIGH gets three refuters: reproduce, reachability, scope",
       sorted(x["label"].split(":")[1] for x in on_high) == ["reachability", "reproduce", "scope"],
       ",".join(x["label"] for x in on_high))
on_med = [x for x in high_refs if not x["label"].endswith(":" + sid)]
report("a MEDIUM gets one combined refuter",
       len(on_med) == 1 and on_med[0]["label"].startswith("refute:combined:"),
       ",".join(x["label"] for x in on_med))
report("every refuter defaults to refuted when uncertain",
       all("Default to refuted: true" in x["prompt"] for x in high_refs), f"{len(high_refs)} prompt(s)")
report("two of three not refuted confirms the HIGH",
       bool(d_stale) and c.get("confirmed") == 1 and r.get("verdict") == "REWIRE",
       f"confirmed {c.get('confirmed')}, {r.get('verdict')}")
report("the refuted MEDIUM comes back as a title for the settled list",
       r.get("refuted") == [MARKER] and c.get("refuted") == 1, f"{r.get('refuted')}")
bs = c.get("bySeverity") or {}
report("counts split by severity",
       bs.get("HIGH") == {"distinct": 1, "confirmed": 1, "refuted": 0, "unverified": 0}
       and bs.get("MEDIUM") == {"distinct": 1, "confirmed": 0, "refuted": 1, "unverified": 0}
       and c.get("verifiers") == 4, f"{bs.get('HIGH')} {bs.get('MEDIUM')} verifiers={c.get('verifiers')}")

# ================ an over-merge cannot drop a finding (review of #97)
# The reduce folds a CRITICAL and a different HIGH into one cluster. The
# refuters used to see only the lead's failure path, refute it, and the
# round came back SOUND with the HIGH nowhere in the result.
SKIPPED = "release gate passes on a skipped CI job"
RESPOND_OVER = r"""(p, o) => {
  const l = String(o.label || '');
  if (l === 'lens:merge-deploy') return {findings: [F('CRITICAL', '%s')]};
  if (l === 'lens:verdicts') return {findings: [F('HIGH', '%s'), F('MEDIUM', 'verdict schema drops a field')]};
  if (l.startsWith('lens:')) return {findings: []};
  if (l === 'reduce') return {clusters: [{members: reduceItems(p).map(f => f.id), settled: ''}]};
  if (l.startsWith('refute:')) return p.includes('a concrete failure path for: %s')
    ? {refuted: false, reason: 'the skipped job really passes the gate'}
    : {refuted: true, reason: 'stale refs are refreshed first'};
  return null;
}""" % (STALE, SKIPPED, SKIPPED)
res = run(BASE, RESPOND_OVER)
r = res.get("r") or {}
c = r.get("counts") or {}
report("the refuters see every merged member's failure path",
       any("a concrete failure path for: " + SKIPPED in x["prompt"] for x in labels(res, "refute:")),
       "carried" if labels(res, "refute:") else "no refuters")
report("  so an over-merged distinct finding is not refuted on its lead alone",
       r.get("verdict") == "REWIRE" and c.get("confirmed", 0) >= 1, f"{r.get('verdict')} {c.get('confirmed')}")
merged = [m["title"] for d in (r.get("confirmed") or []) for m in d.get("merged", [])]
report("  and the absorbed member is named in the result", SKIPPED in merged, str(merged)[:80])
report("two findings from one lens never share a cluster",
       c.get("distinct") == 2 and any("one lens" in l for l in res["logs"]),
       f"distinct {c.get('distinct')}")

# ======================= one of three is not a majority; all refuted = SOUND
RESPOND_ONE = r"""(p, o) => {
  const l = String(o.label || '');
  if (l === 'lens:runtime') return {findings: [F('CRITICAL', 'resume replays the irreversible node')]};
  if (l.startsWith('lens:')) return {findings: []};
  if (l === 'reduce') return singletons(p);
  if (l.startsWith('refute:reproduce:')) return {refuted: false, reason: 'reproduced it by reading the script'};
  if (l.startsWith('refute:')) return {refuted: true, reason: 'the ledger replays it instead of firing'};
  return null;
}"""
res = run(BASE, RESPOND_ONE)
r = res.get("r") or {}
c = r.get("counts") or {}
report("a CRITICAL with one of three standing is refuted",
       c.get("refuted") == 1 and c.get("confirmed") == 0 and len(labels(res, "refute:")) == 3,
       f"refuted {c.get('refuted')}, confirmed {c.get('confirmed')}")
report("and a round whose every finding is refuted is SOUND", r.get("verdict") == "SOUND",
       f"{r.get('verdict')}: {r.get('why')}"[:90])

# ==================================== a dead verifier is never a verdict
RESPOND_DEAD = r"""(p, o) => {
  const l = String(o.label || '');
  if (l === 'lens:verdicts') return {findings: [F('MEDIUM', 'gate reads the wrong exit code'),
                                                F('HIGH', 'green tests carry a filtered marker')]};
  if (l.startsWith('lens:')) return {findings: []};
  if (l === 'reduce') return singletons(p);
  if (l.startsWith('refute:combined:')) return null;
  if (l.startsWith('refute:scope:')) return null;
  if (l.startsWith('refute:')) return {refuted: false, reason: 'reproduced against the repository'};
  return null;
}"""
res = run(BASE, RESPOND_DEAD)
r = res.get("r") or {}
c = r.get("counts") or {}
unv = r.get("unverified") or []
report("a MEDIUM whose only verifier died is UNVERIFIED",
       any(u["severity"] == "MEDIUM" for u in unv), f"{[u['severity'] for u in unv]}")
report("a HIGH missing one of three votes is UNVERIFIED, not confirmed",
       any(u["severity"] == "HIGH" for u in unv) and c.get("confirmed") == 0,
       f"confirmed {c.get('confirmed')}, unverified {c.get('unverified')}")
report("unverified findings make the round REWIRE",
       r.get("verdict") == "REWIRE" and c.get("unverified") == 2 and c.get("refuted") == 0,
       f"{r.get('verdict')}: {r.get('why')}"[:90])
report("the dead verifiers are counted and named",
       c.get("deadVerifiers") == 2 and any("returned nothing" in l and "UNVERIFIED" in l for l in res["logs"])
       and all(any(v["refuted"] is None for v in u["votes"]) for u in unv),
       f"deadVerifiers {c.get('deadVerifiers')}")

# ============================== the settled list reaches every stage
SET1 = "terminal merge reads stale local refs"
SET2 = "Marker hides regression tests"
RESPOND_SETTLED = r"""(p, o) => {
  const l = String(o.label || '');
  if (l === 'lens:commands') return {findings: [F('HIGH', 'merge command uses refs fetched hours ago'),
                                                F('LOW', 'marker HIDES   regression tests'),
                                                F('MEDIUM', 'walkthrough node has no halt condition')]};
  if (l.startsWith('lens:')) return {findings: []};
  if (l === 'reduce') {
    const items = reduceItems(p);
    return {clusters: items.map(f => ({members: [f.id],
      settled: /fetched hours ago/.test(f.title) ? '%s'
             : /halt condition/.test(f.title) ? 'a title nobody settled' : ''}))};
  }
  if (l.startsWith('refute:')) return {refuted: true, reason: 'nothing new since it was settled'};
  return null;
}""" % SET1
res = run(dict(BASE, settled=[SET1, SET2, {"title": SET1.upper()}]), RESPOND_SETTLED)
r = res.get("r") or {}
c = r.get("counts") or {}
lens_calls = labels(res, "lens:")
report("every lens prompt carries the settled titles and the rule",
       len(lens_calls) == 6 and all(SET1 in x["prompt"] and SET2 in x["prompt"]
                                    and "new measured evidence" in x["prompt"] for x in lens_calls),
       f"{len(lens_calls)} lens prompt(s)")
report("the settled list is deduplicated and echoed back", r.get("settled") == [SET1, SET2],
       f"{r.get('settled')}")
red = labels(res, "reduce")
report("the reduce is asked to flag settled matches",
       bool(red) and SET1 in red[0]["prompt"] and SET2 in red[0]["prompt"], "flagging asked")
matched = {m["settled"] for m in r.get("settledMatched") or []}
report("semantic and exact-title matches are both flagged, not dropped",
       c.get("settledMatched") == 2 and matched == {SET1, SET2} and c.get("distinct") == 3,
       f"settledMatched {c.get('settledMatched')}, distinct {c.get('distinct')}")
refs = [x for x in labels(res, "refute:") if f'settled item "{SET1}"' in x["prompt"]]
report("a matched finding's refuters are told what settled it", len(refs) == 3,
       f"{len(refs)} refuter prompt(s) name it")
report("a settled title the reduce invented is ignored and logged",
       any("not on the settled list" in l for l in res["logs"]) and not any(
           m["settled"] == "a title nobody settled" for m in r.get("settledMatched") or []), "logged")

# ============================================================ single mode
RESPOND_SINGLE = r"""(p, o) => (String(o.label) === 'auditor'
  ? {findings: [F('HIGH', 'gate reads stale refs'), F('LOW', 'effort bump on a schema-only node')]}
  : null)"""
res = run(dict(BASE, mode="single"), RESPOND_SINGLE)
r = res.get("r") or {}
c = r.get("counts") or {}
report("mode single spawns exactly one auditor",
       len(res["calls"]) == 1 and res["calls"][0]["label"] == "auditor"
       and res["calls"][0]["phase"] == "Single", f"{[x['label'] for x in res['calls']]}")
report("its findings stay unverified and the round is REWIRE",
       r.get("mode") == "single" and c.get("unverified") == 2 and c.get("confirmed") == 0
       and c.get("verifiers") == 0 and r.get("verdict") == "REWIRE", f"{r.get('verdict')} {c.get('unverified')}")
res = run(dict(BASE, mode="single"))
r = res.get("r") or {}
report("mode single with nothing found is SOUND and says single",
       len(res["calls"]) == 1 and r.get("verdict") == "SOUND" and r.get("mode") == "single",
       f"{len(res['calls'])} call(s), {r.get('verdict')}")

# ====================================== a dead lens is unaudited ground
RESPOND_DEADLENS = r"""(p, o) => (String(o.label) === 'lens:runtime' ? null : {findings: []})"""
res = run(BASE, RESPOND_DEADLENS)
r = res.get("r") or {}
report("a lens that returned nothing blocks SOUND and is named",
       r.get("verdict") == "REWIRE" and r.get("deadLenses") == ["runtime"]
       and "runtime" in (r.get("why") or "") and (r.get("counts") or {}).get("lensesReturned") == 5,
       f"{r.get('verdict')}: {r.get('why')}"[:90])

# ================== a dead or careless reduce loses nothing, and says so
RESPOND_NOREDUCE = r"""(p, o) => {
  const l = String(o.label || '');
  if (l === 'lens:commands') return {findings: [F('LOW', 'first finding here'), F('LOW', 'second finding here')]};
  if (l.startsWith('lens:')) return {findings: []};
  if (l === 'reduce') return null;
  return {refuted: true, reason: 'refuted by reading the IR'};
}"""
res = run(BASE, RESPOND_NOREDUCE)
c = (res.get("r") or {}).get("counts") or {}
report("a dead reduce verifies every raw finding unmerged",
       c.get("distinct") == 2 and any("unmerged" in l for l in res["logs"]), f"distinct {c.get('distinct')}")

RESPOND_SLOPPY = r"""(p, o) => {
  const l = String(o.label || '');
  if (l === 'lens:commands') return {findings: [F('LOW', 'finding number one'), F('HIGH', 'finding number two')]};
  if (l === 'lens:runtime') return {findings: [F('MEDIUM', 'finding number three')]};
  if (l.startsWith('lens:')) return {findings: []};
  if (l === 'reduce') return {clusters: [{members: ['F1', 'F99'], settled: ''},
                                         {members: ['F1', 'F2'], settled: ''}]};
  return {refuted: true, reason: 'refuted by reading the IR'};
}"""
res = run(BASE, RESPOND_SLOPPY)
r = res.get("r") or {}
c = r.get("counts") or {}
logs = " | ".join(res["logs"])
report("an invented, repeated or omitted id is repaired, never lost",
       c.get("raw") == 3 and c.get("distinct") == 3 and c.get("refuted") == 3,
       f"raw {c.get('raw')} distinct {c.get('distinct')} refuted {c.get('refuted')}")
report("and each repair is logged",
       all(s in logs for s in ("no lens produced", "two clusters", "unclustered")), "logged")

# ============================================================ lens override
RESPOND_OVR = r"""(p, o) => ({findings: []})"""
res = run(dict(BASE, lenses=["commands", {"id": "oracle", "focus": "whether the price oracle can be stale"}]),
          RESPOND_OVR)
report("a lens override replaces the fan-out",
       [x["label"] for x in res["calls"]] == ["lens:commands", "lens:oracle"]
       and "price oracle" in res["calls"][1]["prompt"], f"{[x['label'] for x in res['calls']]}")
res = run(dict(BASE, lenses=["comands"]))
report("a misspelt lens id fails loudly instead of auditing less",
       "comands" in res.get("err", "") and not res["calls"], res.get("err", "NO ERROR")[:80])
res = run(dict(BASE, lenses=["commands", "commands"]))
report("a duplicated lens fails loudly", "twice" in res.get("err", ""), res.get("err", "NO ERROR")[:80])

# ============================================================== bad args
for name, a, needle in (("no graph", {"pluginRoot": "/plug"}, "args.graph"),
                        ("no plugin root", {"graph": "WORK.md"}, "args.pluginRoot"),
                        ("an unknown mode", dict(BASE, mode="fast"), "args.mode"),
                        ("a settled string", dict(BASE, settled="x"), "args.settled")):
    res = run(a)
    report(f"{name} fails before any agent runs",
           needle in res.get("err", "") and not res["calls"], res.get("err", "NO ERROR")[:80])

# ======================================== isolated command-shape probes (#142)
PROBE_IR = {"argDefaults": {"launch": "/launch-checkout"}, "nodes": [
    {"id": "boolean", "isolation": True, "prompt": "git -C {{A.launch}} fetch origin; git status | cat"},
    {"id": "worktree", "isolation": "worktree", "prompt": 'test -z "$(git rev-list HEAD)" && git status'},
    {"id": "mutator", "mutates": True, "prompt": "cd /another-worktree && git status; git log | cat"},
    {"id": "measure", "isolation": "measure", "prompt": "git status"},
    {"id": "plain", "prompt": "git status"},
]}
PROBE_OK = r"""(p,o) => {
 if(!o.label.startsWith('probe:'))return {findings:[]};
 const originals=JSON.parse(p.split('PRESCRIBED COMMANDS:\n')[1].split('\n\n')[0]);
 return {completed:true,commands:originals.map(original=>({original,
   safe:original.replace('fetch origin','status'),outcome:'ACCEPTED',evidence:'runtime accepted the reached command'})),
   findings:[],blockers:[]};}"""
probed = run(dict(BASE, ir=PROBE_IR), PROBE_OK)
probes = labels(probed, "probe:")
report("every worktree/boolean/mutator node gets its own isolated Probe before Lenses",
       [c['label'] for c in probes] == ['probe:boolean','probe:worktree','probe:mutator']
       and all(c['isolation'] == 'worktree' and c['phase'] == 'Probe' for c in probes)
       and probed['calls'][0]['label'].startswith('probe:'), "three isolated probes, no measure/plain probe")
report("safe probe keeps prescribed -C, substitution and compound shell shapes",
       all(token in '\n'.join(c['prompt'] for c in probes)
           for token in ['git -C /launch-checkout fetch origin; git status | cat', '$(git rev-list HEAD)',
                         'cd /another-worktree && git status; git log | cat', 'non-mutating', 'Never fetch']),
       "defaults expanded, shell syntax preserved, writes forbidden")
report("accepted completed command probes contribute explicit coverage",
       probed.get('r',{}).get('verdict') == 'SOUND' and probed['r']['counts']['probesReturned'] == 3
       and probed['r']['counts']['probeCommands'] == 3, "3/3 probes, three measured commands")
ONE_PROBE = {"nodes": [PROBE_IR['nodes'][0]], "argDefaults": PROBE_IR['argDefaults']}
PROBE_REFUSED = r"""(p,o)=>{
 if(o.label.startsWith('probe:'))return {completed:true,commands:[{original:'git -C /launch-checkout fetch origin; git status | cat',
   safe:'git -C /launch-checkout status; git status | cat',outcome:'REFUSED',evidence:'redirects git to the shared checkout via -C'}],
   findings:[F('HIGH','runtime isolation refuses the prescribed git shape')],blockers:[]};
 if(o.label.startsWith('lens:'))return {findings:[]};
 if(o.label==='reduce')return singletons(p);
 if(o.label.startsWith('refute:'))return {refuted:false,reproduced:true,reason:'Measured the same hook refusal in this isolated worktree'};
 return null;}"""
refused = run(dict(BASE, ir=ONE_PROBE), PROBE_REFUSED)
refs = labels(refused,'refute:')
report("probe refusals become findings with isolated refuters under the same boundary",
       refused.get('r',{}).get('counts',{}).get('confirmed') == 1 and len(refs) == 3
       and all(c['isolation'] == 'worktree' and 'UNVERIFIED' in c['prompt'] for c in refs)
       and refused['r']['confirmed'][0]['probeNodes'] == ['boolean'], "one confirmed refusal, three isolated refuters")
merged_probe = run(dict(BASE, ir=ONE_PROBE), PROBE_REFUSED.replace(
    "if(o.label.startsWith('lens:'))return {findings:[]};",
    "if(o.label==='lens:commands')return {findings:[F('CRITICAL','normal lens raises the same isolated refusal')]};if(o.label.startsWith('lens:'))return {findings:[]};").replace(
    "if(o.label==='reduce')return singletons(p);", "if(o.label==='reduce')return {clusters:[{members:reduceItems(p).map(f=>f.id),settled:''}]};"))
report("reduce retains isolation when a higher severity ordinary lens leads a probe cluster",
       merged_probe.get('r',{}).get('confirmed',[{}])[0].get('severity') == 'CRITICAL'
       and all(c['isolation'] == 'worktree' for c in labels(merged_probe,'refute:')),
       "probe origin survives deduplication")
unavailable = run(dict(BASE, ir=ONE_PROBE), PROBE_REFUSED.replace('reproduced:true','reproduced:false'))
report("inability to reproduce an isolated refusal remains UNVERIFIED, never refuted",
       unavailable.get('r',{}).get('counts',{}).get('unverified') == 1
       and unavailable['r']['counts']['refuted'] == 0 and unavailable['r']['verdict'] == 'REWIRE', "unavailable boundary blocks SOUND")
for missing in ('null', '{completed:false,findings:[],commands:[],blockers:["tool unavailable"]}',
                '{completed:true,findings:[],commands:[],blockers:[]}',
                "{completed:true,findings:[],commands:[{original:'git status',safe:'git status',outcome:'UNRESOLVED',evidence:'chain short-circuited'}],blockers:[]}"):
    broken = run(dict(BASE, ir=ONE_PROBE), "(p,o)=>o.label.startsWith('probe:')?" + missing + ":{findings:[]}")
    report("missing/unexecuted probe coverage blocks SOUND: " + missing[:35],
           broken.get('r',{}).get('verdict') == 'REWIRE' and 'boolean' in broken['r']['deadProbes'], "probe incomplete")
discovered = run({"graph":"WORK.md","pluginRoot":"/plug"},
                 "(p,o)=>o.label==='probe-plan'?" + json.dumps(ONE_PROBE) + ":(" + PROBE_OK + ")(p,o)")
report("an older caller without args.ir discovers the plan instead of skipping probes",
       len(labels(discovered,'probe-plan')) == 1 and len(labels(discovered,'probe:')) == 1
       and discovered.get('r',{}).get('verdict') == 'SOUND', "discovery plus isolated execution")
unknown_plan = run({"graph":"WORK.md","pluginRoot":"/plug"}, "(p,o)=>o.label==='probe-plan'?null:{findings:[]}")
report("a missing probe plan cannot produce SOUND from clear lenses",
       unknown_plan.get('r',{}).get('verdict') == 'REWIRE' and unknown_plan['r']['deadProbes'] == ['plan'], "plan unknown")

# Probe completion is bound to the actual command, not the agent's boolean.
def command_probe(prompt, commands, reason=None):
    ir = {"nodes": [{"id": "shape", "isolation": True, "prompt": prompt}], "argDefaults": {}}
    response = {"completed": True, "findings": [], "commands": commands, "blockers": []}
    if reason:
        response['noCommandsReason'] = reason
    return run(dict(BASE, ir=ir), "(p,o)=>o.label.startsWith('probe:')?" + json.dumps(response) + ":{findings:[]}")

def receipt(original, safe=None):
    return dict(original=original, safe=safe if safe is not None else original,
                outcome='ACCEPTED', evidence='runtime accepted the entire measured shell command')

compound = 'git -C "$(git rev-parse --show-toplevel)" fetch origin && git -C /target status | cat'
compound_safe = compound.replace('fetch origin', 'status')
positive = command_probe('Run ' + compound, [receipt(compound, compound_safe)])
report('safe read conversion retains nested substitution, both -C targets, && and pipe',
       positive.get('r', {}).get('verdict') == 'SOUND', 'full command receipt accepted')
for name, original, safe in [
        ('unrelated command', 'git status', 'git status'),
        ('dropped compound branch', compound, 'git -C "$(git rev-parse --show-toplevel)" status'),
        ('changed -C operand', compound, compound_safe.replace('/target', '/other')),
        ('changed substitution', compound, compound_safe.replace('--show-toplevel', '--git-dir')),
        ('changed compound operator', compound, compound_safe.replace('&&', ';')),
        ('mutation kept as safe', compound, compound),
        ('hidden mutating substitution', compound, compound_safe.replace('git rev-parse --show-toplevel', 'git fetch origin')),
        ('unsafe read flag', compound, compound_safe.replace('status |', 'diff --output=/tmp/shared |'))]:
    bad = command_probe('Run ' + compound, [receipt(original, safe)])
    report('probe blocks false completion: ' + name,
           bad.get('r', {}).get('verdict') == 'REWIRE' and bad['r']['deadProbes'] == ['shape'], 'coverage rejected')
for name, prompt, rows in [
        ('second prescribed command omitted', '```sh\ngit status\ngit rev-parse HEAD\n```', [receipt('git status')]),
        ('extra invented receipt', '`git status`', [receipt('git status'), receipt('git log')]),
        ('duplicate receipt', '`git status`', [receipt('git status'), receipt('git status')]),
        ('changed cd target', 'cd /target && git status', [receipt('cd /target && git status', 'cd /other && git status')]),
        ('changed read-only target', 'git -C /target rev-parse HEAD', [receipt('git -C /target rev-parse HEAD', 'git -C /target rev-parse OTHER')]),
        ('replaced prescribed read', 'git -C /target rev-parse HEAD', [receipt('git -C /target rev-parse HEAD', 'git -C /target status')]),
        ('unsupported executable', 'Run python3 tool.py', [receipt('python3 tool.py')]),
        ('hidden backtick mutation', 'git -C "`git fetch origin`" status', [receipt('git -C "`git fetch origin`" status')]),
        ('redirection', 'git status > /tmp/shared', [receipt('git status > /tmp/shared')])]:
    bad = command_probe(prompt, rows)
    report('probe requires complete safe inventory: ' + name,
           bad.get('r', {}).get('verdict') == 'REWIRE' and bad['r']['deadProbes'] == ['shape'], 'coverage rejected')
no_command = command_probe('Read the target and report risks.', [], 'The prompt asks for inspection and prescribes no shell commands.')
report('a node with no prescribed command retains explicit no-command coverage',
       no_command.get('r', {}).get('verdict') == 'SOUND', 'concrete noCommandsReason accepted')

# ============================ typed probe receipts for script commands (#192)
# A builder that runs a script could never complete its probe: the grammar
# admits no interpreter. A node now declares, in the IR, how each script
# command is probed: `read-only` runs the exact command, `shape` runs a
# reviewed --help form that must stop before the program forwards it.
LAUNCH = {"_launch": {"root": "/plugin"}, "_base": {"sha": "abc123"}}


def resolved(text):
    return text.replace('{{A._launch.root}}', LAUNCH["_launch"]["root"])


TOOL = 'python scripts/tool.py report --json'
RUNNER = 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --log build.log -- python scripts/build.py --all'
RUNNER_RESOLVED = resolved(RUNNER)
RUNNER_FORM = 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --log build.log --help'
AWAIT = 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py await --timeout 240 <token>'
AWAIT_RESOLVED = resolved(AWAIT)
AWAIT_FORM = AWAIT + ' --help'
AWAIT_RUN = AWAIT_RESOLVED.replace('<token>', 'fpl-probe-token') + ' --help'


def finding(title):
    return dict(severity='HIGH', title=title, defectClass='isolation-shape',
                classRewire='derive the probe form from the reviewed declaration in the IR',
                failurePath='the isolation hook refuses the declared shape, so the builder dies at launch',
                evidence='hook text: command refused in this worktree', rewire='rewire the node so that ' + title)


def declared_probe(prompt, declaration, rows, findings=(), bindings=LAUNCH, refute=None, plan=False):
    node = {"id": "builder", "mutates": True, "prompt": prompt}
    if declaration is not None:
        node["probe"] = declaration
    ir = {"nodes": [node], "argDefaults": {}}
    response = json.dumps({"completed": True, "findings": list(findings), "commands": rows, "blockers": []})
    respond = ("(p,o)=>o.label.startsWith('probe:')?" + response +
               (":o.label==='reduce'?singletons(p):o.label.startsWith('refute:')?" + refute if refute else "") +
               (":o.label==='probe-plan'?" + json.dumps(ir) if plan else "") + ":{findings:[]}")
    args = dict(BASE, bindings=bindings)
    if plan:
        args.pop("ir")
    else:
        args["ir"] = ir
    return run(args, respond)


def probe_report(res):
    reports = (res.get('r') or {}).get('probes') or []
    return reports[0] if reports else {}


def declared_forms(res):
    sent = labels(res, 'probe:')
    if not sent or 'DECLARED FORMS:\n' not in sent[0]['prompt']:
        return None
    return json.loads(sent[0]['prompt'].split('DECLARED FORMS:\n')[1].split('\n\n')[0])


def sound(res):
    return (res.get('r') or {}).get('verdict') == 'SOUND'


def incomplete(res):
    r = res.get('r') or {}
    return r.get('verdict') == 'REWIRE' and r.get('deadProbes') == ['builder']


def blocked(res, needle):
    return incomplete(res) and any(needle in b for b in probe_report(res).get('blockers', []))


def requires(res, command):
    return command in (probe_report(res).get('required') or [])


READ_ONLY = {"commands": [{"command": TOOL, "kind": "read-only"}]}
ok = declared_probe('Run ' + TOOL, READ_ONLY, [receipt(TOOL)])
report('#192 a declared read-only script command receipts and completes the probe',
       sound(ok) and probe_report(ok).get('completed') is True
       and [c.get('kind') for c in probe_report(ok).get('commands', [])] == ['read-only'],
       f"{(ok.get('r') or {}).get('verdict')} {probe_report(ok).get('blockers')}"[:90])
bare = declared_probe('Run ' + TOOL, None, [receipt(TOOL)])
report('#192 an undeclared script command stays incomplete and the blocker names it',
       incomplete(bare) and any(TOOL in b for b in probe_report(bare).get('blockers', [])),
       f"{probe_report(bare).get('blockers')}"[:90])
wrong = declared_probe('Run ' + TOOL, READ_ONLY, [receipt(TOOL, TOOL + ' --verbose')])
report('#192 a read-only declaration accepts only the exact command as its receipt',
       incomplete(wrong) and requires(wrong, TOOL), 'a changed argv is not the declared command')
PUSH = 'git push origin main'
for name, decl, needle in [
        ('a declaration matching nothing', {"commands": [{"command": TOOL, "kind": "read-only"},
                                                         {"command": 'python scripts/other.py', "kind": "read-only"}]},
         'matches no inventoried command'),
        ('an unknown kind', {"commands": [{"command": TOOL, "kind": "trusted"}]}, 'kind must be'),
        ('an unknown key', {"commands": [{"command": TOOL, "kind": "read-only"}], "allow": ["python"]}, "unknown field 'allow'"),
        ('a read-only mutating git command', {"commands": [{"command": TOOL, "kind": "read-only"},
                                                           {"command": PUSH, "kind": "read-only"}]}, 'not a git read')]:
    res = declared_probe('Run ' + TOOL + '\n`' + PUSH + '`', decl, [receipt(TOOL), receipt(PUSH, 'git status')])
    report('#192 the audit refuses ' + name, blocked(res, needle), f"{probe_report(res).get('blockers')}"[:90])

SHAPE = {"commands": [{"command": RUNNER, "kind": "shape", "form": RUNNER_FORM}]}
shaped = declared_probe('Run ' + RUNNER, SHAPE, [receipt(RUNNER_RESOLVED, resolved(RUNNER_FORM))])
shape_why = (shaped.get('r') or {}).get('why') or ''
evidence = (shaped.get('r') or {}).get('probeEvidence') or {}
report('#192 a REMAINDER CLI shape stopping before -- completes as shape-only',
       sound(shaped) and [c.get('kind') for c in probe_report(shaped).get('commands', [])] == ['shape-only'],
       f"{(shaped.get('r') or {}).get('verdict')} {probe_report(shaped).get('blockers')}"[:90])
report('#192 the verdict names each shape-only receipt and leaves semantics to the gates',
       'shape-only' in shape_why and RUNNER_RESOLVED in shape_why
       and evidence.get('shapeOnly') == ['builder: ' + RUNNER_RESOLVED] and evidence.get('readOnly') == []
       and 'attested gates' in str(evidence.get('semantic')), shape_why[-90:])
read_why = (ok.get('r') or {}).get('why') or ''
report('#192 an exact read-only receipt is reported as read-only, not shape-only',
       'shape-only' not in read_why and ((ok.get('r') or {}).get('probeEvidence') or {}).get('readOnly') == ['builder: ' + TOOL],
       read_why[-90:])
report('#192 the probe agent is told the exact declared form to send',
       declared_forms(shaped) == {RUNNER_RESOLVED: {"kind": "shape", "run": resolved(RUNNER_FORM)}},
       str(declared_forms(shaped))[:90])
ran_original = declared_probe('Run ' + RUNNER, SHAPE, [receipt(RUNNER_RESOLVED)])
report('#192 a shape receipt that ran the original command is not coverage',
       incomplete(ran_original) and requires(ran_original, RUNNER_RESOLVED), 'the receipt must be the declared form')
for name, form, needle in [
        ('--help after the -- separator', RUNNER_FORM.replace('--log build.log --help', '--log build.log -- --help'),
         '-- separator'),
        ('--help appended after the child argv', RUNNER + ' --help', '-- separator'),
        ('a form that is not a prefix', RUNNER_FORM.replace('build.log', 'other.log'), 'not a prefix'),
        ('a form that changes the cd target', 'cd /elsewhere && ' + RUNNER_FORM, 'changes an operator')]:
    res = declared_probe('Run ' + RUNNER, {"commands": [{"command": RUNNER, "kind": "shape", "form": form}]},
                         [receipt(RUNNER_RESOLVED, resolved(form))])
    report('#192 a shape form is refused: ' + name, blocked(res, needle), f"{probe_report(res).get('blockers')}"[:90])

refused_rows = [dict(receipt(RUNNER_RESOLVED, resolved(RUNNER_FORM)), outcome='REFUSED',
                     evidence='hook: python launcher refused in this worktree')]
REPRODUCED = "{refuted:false,reproduced:true,reason:'Measured the same hook refusal in this isolated worktree'}"
hook = declared_probe('Run ' + RUNNER, SHAPE, refused_rows, [finding('isolation refuses the declared longrun shape')],
                      refute=REPRODUCED)
hr = hook.get('r') or {}
report('#192 a refused shape becomes a probe finding that reaches the refuters',
       hr.get('verdict') == 'REWIRE' and (hr.get('counts') or {}).get('confirmed') == 1
       and hr['confirmed'][0]['probeNodes'] == ['builder'] and probe_report(hook).get('completed') is True
       and not (hr.get('probeEvidence') or {}).get('shapeOnly'),
       f"{hr.get('verdict')} {hr.get('why')}"[:90])
silent = declared_probe('Run ' + RUNNER, SHAPE, refused_rows)
report('#192 a refused shape without its finding leaves the probe incomplete',
       incomplete(silent) and requires(silent, RUNNER_RESOLVED), 'refusal kept')

TEMPLATE_PROMPT = 'Use `git -C <dir> <subcommand> ...` and `cd <dir> && python ...` from the launch checkout.'
CD_TOOL = 'cd {{A._launch.root}} && ' + TOOL
TEMPLATES = {"templates": [
    {"template": 'git -C <dir> <subcommand> ...',
     "instances": ['git -C {{A._launch.root}} status --porcelain', 'git -C {{A._launch.root}} fetch origin']},
    {"template": 'cd <dir> && python ...', "instances": [CD_TOOL]}],
    "commands": [{"command": CD_TOOL, "kind": "read-only"}]}
TEMPLATE_ROWS = [receipt('git -C /plugin status --porcelain'),
                 receipt('git -C /plugin fetch origin', 'git -C /plugin status'),
                 receipt(resolved(CD_TOOL))]
templated = declared_probe(TEMPLATE_PROMPT, TEMPLATES, TEMPLATE_ROWS)
report('#192 a template instantiated by receipted concrete commands completes',
       sound(templated) and probe_report(templated).get('required') == [r['original'] for r in TEMPLATE_ROWS],
       f"{(templated.get('r') or {}).get('verdict')} {probe_report(templated).get('blockers')}"[:90])
missing = declared_probe(TEMPLATE_PROMPT, TEMPLATES, TEMPLATE_ROWS[:2])
report('#192 a template with an unreceipted instantiation stays incomplete',
       incomplete(missing) and requires(missing, resolved(CD_TOOL)), 'instance unreceipted')
stray = copy.deepcopy(TEMPLATES)
stray["templates"][0]["instances"].append('git status')
mismatched = declared_probe(TEMPLATE_PROMPT, stray, TEMPLATE_ROWS + [receipt('git status')])
report('#192 an instance that does not instantiate its template is refused',
       blocked(mismatched, 'does not instantiate'), f"{probe_report(mismatched).get('blockers')}"[:90])
undeclared_template = declared_probe(TEMPLATE_PROMPT, None, [receipt('git -C <dir> <subcommand> ...'),
                                                              receipt('cd <dir> && python ...')])
report('#192 an undeclared illustrative template stays incomplete',
       blocked(undeclared_template, 'no probe declaration for git -C <dir> <subcommand> ...'),
       f"{probe_report(undeclared_template).get('blockers')}"[:90])

nested = declared_probe('Run `git -C {{A._launch.root}} rev-parse {{A._base.sha}}`', None,
                        [receipt('git -C /plugin rev-parse abc123')])
report('#192 nested bindings {{A._launch.root}} and {{A._base.sha}} resolve before matching',
       sound(nested) and probe_report(nested).get('prescribed') == ['git -C /plugin rev-parse abc123'],
       f"{probe_report(nested).get('prescribed')}"[:90])
quoted = declared_probe('Run `cd "{{A.unbound.path}}" && git status`', None,
                        [receipt('cd "{{A.unbound.path}}" && git status')])
report('#192 an unresolved binding leaves the probe incomplete even inside quotes',
       incomplete(quoted) and any('unresolved' in b for b in probe_report(quoted).get('blockers', [])),
       f"{probe_report(quoted).get('blockers')}"[:90])

DATA = {"commands": [{"command": AWAIT, "kind": "shape", "form": AWAIT_FORM, "data": {"token": "token"}}]}
awaited = declared_probe('Then `' + AWAIT + '`', DATA, [receipt(AWAIT_RESOLVED, AWAIT_RUN)])
report('#192 a typed data placeholder takes its dummy only inside the declared shape',
       sound(awaited) and declared_forms(awaited) == {AWAIT_RESOLVED: {"kind": "shape", "run": AWAIT_RUN}},
       f"{(awaited.get('r') or {}).get('verdict')} {probe_report(awaited).get('blockers')}"[:90])
real_token = declared_probe('Then `' + AWAIT + '`', DATA,
                            [receipt(AWAIT_RESOLVED, AWAIT_RUN.replace('fpl-probe-token', 'tok-from-a-live-run'))])
report('#192 the probe cannot choose its own placeholder value',
       incomplete(real_token) and requires(real_token, AWAIT_RESOLVED), 'dummy is fixed by type')
for name, decl, needle in [
        ('read-only', {"commands": [{"command": AWAIT, "kind": "read-only"}]}, 'placeholder'),
        ('untyped', {"commands": [{"command": AWAIT, "kind": "shape", "form": AWAIT_FORM}]}, '<token> has no declared type'),
        ('unknown type', {"commands": [{"command": AWAIT, "kind": "shape", "form": AWAIT_FORM, "data": {"token": "path"}}]},
         'data type'),
        ('undeclared', None, 'no probe declaration for ' + AWAIT_RESOLVED)]:
    res = declared_probe('Then `' + AWAIT + '`', decl, [receipt(AWAIT_RESOLVED, AWAIT_RUN)])
    report('#192 a data placeholder outside a typed shape is refused: ' + name, blocked(res, needle),
           f"{probe_report(res).get('blockers')}"[:90])

INLINE = 'python3 -c "import shutil; shutil.rmtree(\'x\')"'
for name, prompt, decl, rows, needle in [
        ('rule A: -h is not a help flag', 'Run ' + RUNNER,
         {"commands": [{"command": RUNNER, "kind": "shape", "form": RUNNER_FORM.replace('--help', '-h')}]},
         [receipt(RUNNER_RESOLVED, resolved(RUNNER_FORM.replace('--help', '-h')))], '-h is not a help flag'),
        ('rule B: an interpreter option that runs inline code', 'Run ' + INLINE,
         {"commands": [{"command": INLINE, "kind": "shape", "form": INLINE + ' --help'}]},
         [receipt(INLINE, INLINE + ' --help')], 'the option -c'),
        ('rule C: a launcher that forwards --help', 'Run npx some-package',
         {"commands": [{"command": 'npx some-package', "kind": "shape", "form": 'npx some-package --help'}]},
         [receipt('npx some-package', 'npx some-package --help')], 'forwards its trailing words'),
        ('rule D: a read-only declaration of a writer', 'Run rm -rf build',
         {"commands": [{"command": 'rm -rf build', "kind": "read-only"}]},
         [receipt('rm -rf build')], 'writes files or reaches the network')]:
    res = declared_probe(prompt, decl, rows)
    report('#192 ' + name + ' leaves the probe incomplete', blocked(res, needle) and not declared_forms(res),
           f"{probe_report(res).get('blockers')}"[:90])

# A later-bound token ({{prev.sha}}, {{item.id}}) is data an earlier output
# fills in, typed like <token> and given its dummy only inside a shape form.
BUILD = ('bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --log build.log -- '
         'python scripts/build.py g2 --suites-sha {{prev.sha}}')
BUILD_FORM = 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --log build.log --help'
CHECK = 'python scripts/check.py --sha {{prev.sha}} --strict'
CHECK_FORM = 'python scripts/check.py --sha {{prev.sha}} --help'
typed = declared_probe('Run ' + BUILD, {"commands": [{"command": BUILD, "kind": "shape", "form": BUILD_FORM,
                                                      "data": {"prev.sha": "sha"}}]},
                       [receipt(resolved(BUILD), resolved(BUILD_FORM))])
report('#192 rule F: a typed {{prev.sha}} command completes with its shape form',
       sound(typed) and requires(typed, resolved(BUILD))
       and declared_forms(typed) == {resolved(BUILD): {"kind": "shape", "run": resolved(BUILD_FORM)}},
       f"{(typed.get('r') or {}).get('verdict')} {probe_report(typed).get('blockers')}"[:90])
carried = declared_probe('Run ' + CHECK, {"commands": [{"command": CHECK, "kind": "shape", "form": CHECK_FORM,
                                                        "data": {"prev.sha": "sha"}}]},
                         [receipt(CHECK, 'python scripts/check.py --sha ' + '0' * 40 + ' --help')])
report('#192 rule F: a form that carries {{prev.sha}} runs with the sha dummy',
       sound(carried) and declared_forms(carried) == {
           CHECK: {"kind": "shape", "run": 'python scripts/check.py --sha ' + '0' * 40 + ' --help'}},
       f"{probe_report(carried).get('blockers')}"[:90])
for name, decl, needle in [
        ('an untyped {{prev.sha}} stays unresolved', {"commands": [{"command": BUILD, "kind": "shape", "form": BUILD_FORM}]},
         'unresolved binding in ' + resolved(BUILD)),
        ('an undeclared {{prev.sha}} stays unresolved', None, 'unresolved binding in ' + resolved(BUILD)),
        ('a read-only declaration carrying {{prev.sha}} is refused',
         {"commands": [{"command": BUILD, "kind": "read-only"}]}, 'cannot carry {{prev.sha}}'),
        ('data keyed A.x is refused', {"commands": [{"command": BUILD, "kind": "shape", "form": BUILD_FORM,
                                                     "data": {"prev.sha": "sha", "A.x": "token"}}]}, 'launch bindings'),
        ('a form carrying a token the command lacks is refused',
         {"commands": [{"command": BUILD, "kind": "shape", "form": BUILD_FORM.replace('--help', '{{prev.id}} --help'),
                        "data": {"prev.sha": "sha"}}]}, 'form carries')]:
    res = declared_probe('Run ' + BUILD, decl, [receipt(resolved(BUILD), resolved(BUILD_FORM))])
    report('#192 rule F: ' + name, blocked(res, needle), f"{probe_report(res).get('blockers')}"[:90])

# Git is checked by its subcommand, so only the plain word git counts as git:
# a path, quotes or .exe would carry reset --hard past the read check.
def read_only_evidence(res):
    return ((res.get('r') or {}).get('probeEvidence') or {}).get('readOnly')


for spelling, command in [('a path', '/usr/bin/git reset --hard'), ('quotes', "'git' reset --hard"),
                          ('.exe', 'git.exe push origin main'),
                          ('a path and -C', '/usr/bin/git -C /scratch/repo reset --hard')]:
    res = declared_probe('Run ' + command, {"commands": [{"command": command, "kind": "read-only"}]}, [receipt(command)])
    report('#192 git identity: a read-only git spelled with ' + spelling + ' is refused',
           blocked(res, 'write it git') and not declared_forms(res) and read_only_evidence(res) == [],
           f"{(res.get('r') or {}).get('verdict')} {probe_report(res).get('blockers')}"[:90])
plain_reset = declared_probe('Run git reset --hard', {"commands": [{"command": 'git reset --hard', "kind": "read-only"}]},
                             [receipt('git reset --hard')])
report('#192 git identity: a read-only plain git reset --hard is refused', blocked(plain_reset, 'not a git read'),
       f"{probe_report(plain_reset).get('blockers')}"[:90])
plain_read = declared_probe('Run git status --porcelain',
                            {"commands": [{"command": 'git status --porcelain', "kind": "read-only"}]},
                            [receipt('git status --porcelain')])
report('#192 git identity: a read-only plain git status --porcelain is accepted',
       sound(plain_read) and read_only_evidence(plain_read) == ['builder: git status --porcelain'],
       f"{probe_report(plain_read).get('blockers')}"[:90])
for name, prompt, decl, rows in [
        ('an undeclared grammar part', 'Run /usr/bin/git status', None, [receipt('/usr/bin/git status')]),
        ('a shape form', 'Run /usr/bin/git push origin main',
         {"commands": [{"command": '/usr/bin/git push origin main', "kind": "shape", "form": '/usr/bin/git push --help'}]},
         [receipt('/usr/bin/git push origin main', '/usr/bin/git push --help')]),
        ('a template instance', 'Run `<git> -C <dir> status`',
         {"templates": [{"template": '<git> -C <dir> status', "instances": ['/usr/bin/git -C /x status']}]},
         [receipt('/usr/bin/git -C /x status')])]:
    res = declared_probe(prompt, decl, rows)
    report('#192 git identity: git spelled as a path is refused in ' + name, blocked(res, 'write it git'),
           f"{probe_report(res).get('blockers')}"[:90])

# A backslash in a program word is an escape to a shell and a separator to
# Windows, so neither reading can name the program safely.
for name, command, decl in [
        ('a double-quoted Windows git, read-only', r'"C:\Tools\git.exe" reset --hard', "read-only"),
        ('a single-quoted Windows git, read-only', r"'C:\Program Files\Git\cmd\git.exe' reset --hard", "read-only"),
        ('an unquoted Windows git, read-only', r'C:\Tools\git.exe reset --hard', "read-only"),
        ('a Windows rm.exe, read-only', r'C:\Tools\rm.exe -rf build', "read-only"),
        ('a Windows python with -c, as a shape', r'"C:\Py\python.exe" -c "import os"', "shape"),
        ('a Windows docker, as a shape', r'C:\Tools\docker.exe run --rm img', "shape")]:
    entry = {"command": command, "kind": decl}
    if decl == "shape":
        entry["form"] = command + ' --help'
    res = declared_probe('Run ' + command, {"commands": [entry]},
                         [receipt(command, entry.get("form", command))])
    report('#192 backslash program: ' + name + ' is refused', blocked(res, 'forward slashes') and not declared_forms(res),
           f"{(res.get('r') or {}).get('verdict')} {probe_report(res).get('blockers')}"[:90])
escaped = declared_probe(r'Run r\m -rf build', {"commands": [{"command": r'r\m -rf build', "kind": "read-only"}]},
                         [receipt(r'r\m -rf build')])
report('#192 backslash program: an escaped r\\m stays refused as read-only',
       incomplete(escaped) and not declared_forms(escaped), f"{probe_report(escaped).get('blockers')}"[:90])
FORWARD = '"C:/Program Files/Python/python.exe" scripts/a.py'
forward = declared_probe('Run ' + FORWARD + ' --check',
                         {"commands": [{"command": FORWARD + ' --check', "kind": "shape", "form": FORWARD + ' --help'}]},
                         [receipt(FORWARD + ' --check', FORWARD + ' --help')])
report('#192 backslash program: a forward-slash Windows python shape is accepted', sound(forward),
       f"{probe_report(forward).get('blockers')}"[:90])

# The inventory must not lose an explicit instruction to run something, nor
# invent a command out of the punctuation left beside an inline one.
def inventoried(prompt, reason='The prompt describes work and prescribes no command.'):
    return command_probe(prompt, [], reason)


for name, prompt, expected in [
        ('an execution verb mid-line', 'Before approving, execute `aiken check`.', ['aiken check']),
        ('invoke', 'Then invoke `aiken fmt --check` on the tree.', ['aiken fmt --check']),
        ('a quoted Windows interpreter', 'Then `"C:/Py/python.exe" x.py` writes the report.', ['"C:/Py/python.exe" x.py'])]:
    res = inventoried(prompt)
    got = probe_report(res).get('prescribed')
    report('#192 inventory: ' + name + ' is inventoried, not read as no command',
           got == expected and (res.get('r') or {}).get('verdict') == 'REWIRE', f"{got}"[:90])
for name, prompt, expected in [
        ('Run `git status`. adds no "." command', 'Run `git status`.', ['git status']),
        ('runs does not count as run', 'The nightly job runs `aiken check` for us.', [])]:
    got = probe_report(inventoried(prompt)).get('prescribed')
    report('#192 inventory: ' + name, got == expected, f"{got}"[:90])
BUILDER_PROMPT = '\n'.join([
    '1. Read the packet first.',
    '2. Run `python scripts/x.py`. Keep its report.',
    '3. Start the build in the background: `bash "/p/scripts/py.sh" longrun.py start --log L -- python scripts/b.py g2 '
    '--suites-sha {{prev.sha}}`. Keep the token it prints.',
    '4. In the foreground, run `bash "/p/scripts/py.sh" longrun.py await --timeout 240 <token>`, again on exit 3, '
    'until it completes.',
    'If the build must stop, cancel it with `bash "/p/scripts/py.sh" longrun.py cancel <token>` and say why.',
    'Read the last line of `/w/g2-build.log` with the file tool.',
    'When nothing is left to do, return outcome `stopped`.'])
builder = probe_report(inventoried(BUILDER_PROMPT)).get('prescribed')
report('#192 inventory: a builder prompt inventories exactly the four commands it names',
       builder == ['python scripts/x.py',
                   'bash "/p/scripts/py.sh" longrun.py start --log L -- python scripts/b.py g2 --suites-sha {{prev.sha}}',
                   'bash "/p/scripts/py.sh" longrun.py await --timeout 240 <token>',
                   'bash "/p/scripts/py.sh" longrun.py cancel <token>'], f"{builder}"[:90])
windows_inline = probe_report(inventoried(r'Then `"C:\Tools\git.exe" status` reports the tree.')).get('prescribed')
report('#192 backslash program: an inline Windows git with no verb is inventoried',
       windows_inline == [r'"C:\Tools\git.exe" status'], f"{windows_inline}"[:90])

discovered = declared_probe('Run ' + TOOL, READ_ONLY, [receipt(TOOL)], plan=True)
report('#192 a discovered probe plan carries the node declarations',
       sound(discovered) and len(labels(discovered, 'probe-plan')) == 1
       and 'mutates, probe and foreach' in labels(discovered, 'probe-plan')[0]['prompt'],
       f"{(discovered.get('r') or {}).get('why')}"[:90])

# ======================================== meta is a pure literal
m = re.search(r"^export const meta = (\{.*?^\})", SRC, re.S | re.M)
meta = m.group(1) if m else ""
STRING = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")
rest = STRING.sub('""', meta)
TOKEN = re.compile(r'\s+|""|[{}\[\],:]|[A-Za-z_$][\w$]*(?=\s*:)|-?\d+(?:\.\d+)?|true|false|null')
pos, bad = 0, ""
while pos < len(rest):
    t = TOKEN.match(rest, pos)
    if not t or t.end() == pos:
        bad = rest[pos:pos + 30]
        break
    pos = t.end()
report("meta is a pure literal (keys, strings, numbers only)", bool(meta) and not bad,
       "pure" if meta and not bad else f"not a literal near {bad!r}" if meta else "meta NOT FOUND")
titles = set(re.findall(r"title:\s*'([^']+)'", meta))
report("every phase the runs used is declared in meta.phases",
       bool(PHASES_SEEN) and PHASES_SEEN <= titles, f"used {sorted(PHASES_SEEN)} of {sorted(titles)}")

# ============================ no clock, no randomness, no host APIs
code = re.sub(r"(?m)^\s*//.*$", "", SRC)
code = re.sub(r"(?<![:'\"\w])//[^\n]*", "", code)
banned = [p for p in (r"\bDate\.now\b", r"\bMath\.random\b", r"\bnew Date\s*\(", r"\bperformance\.now\b",
                      r"\brequire\s*\(", r"^\s*import\b", r"\bprocess\.", r"\bfetch\s*\(")
          if re.search(p, code, re.M)]
report("the body reads no clock, randomness or host API", not banned, "clean" if not banned else f"{banned}")

# ================== what the command says about the workflow is true
with open(COMMAND, encoding="utf-8") as fh:
    CMD = fh.read()
FLAT = re.sub(r"\s+", " ", CMD)
WORDS = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
for name, ok in (
        ("the command invokes the shipped script by plugin root",
         "${CLAUDE_PLUGIN_ROOT}/workflows/graph-audit.js" in CMD and os.path.isfile(SCRIPT)),
        ("the command's lens count matches the default fan-out",
         f"{WORDS[len(DEFAULT_LENSES)]} lenses" in FLAT.lower()),
        ("it names every default lens", all(f"`{x}`" in CMD for x in DEFAULT_LENSES)),
        ("it names the three refuter lenses and the 2-of-3 rule",
         all(f"`{x}`" in CMD for x in ("reproduce", "reachability", "scope")) and "2 of 3" in FLAT),
        ("--quick maps to the script's single mode", "--quick" in CMD and 'mode: "single"' in CMD),
        ("it persists rounds under .claude/fluxpoint/audits/", ".claude/fluxpoint/audits/" in CMD),
        ("it prints the per-stage counts the script returns",
         all(k in CMD for k in ("raw", "distinct", "settledMatched", "confirmed", "refuted",
                                "unverified", "bySeverity"))),
        ("the fallback points at constants the script defines",
         "`LENSES`" in CMD and "`REFUTERS`" in CMD
         and re.search(r"^const LENSES = \[", SRC, re.M) and re.search(r"^const REFUTERS = \[", SRC, re.M))):
    report(name, bool(ok), "stated" if ok else "MISSING OR STALE")

# The documented probe declaration completes a probe over the commands it names.
example = re.search(r'```json\n\s*"probe": (\{.*?\n\s*\})\n\s*```', CMD, re.S)
documented = json.loads(example.group(1)) if example else {}
doc_commands = [c["command"] for c in documented.get("commands", [])]
doc_prompt = "\n".join(["Run " + doc_commands[0]] + ["Then `" + c + "`" for c in doc_commands[1:]]
                       + ["Use `" + t["template"] + "`." for t in documented.get("templates", [])]) if doc_commands else ""
doc_rows = [receipt(resolved(c["command"]), resolved(c["command"]) if c["kind"] == "read-only"
                    else resolved(c["form"]).replace("<token>", "fpl-probe-token"))
            for c in documented.get("commands", [])]
doc_rows += [receipt(resolved(i)) for t in documented.get("templates", []) for i in t["instances"]]
doc_probe = declared_probe(doc_prompt, documented, doc_rows) if doc_commands else {}
report("the command's probe declaration example completes a probe", bool(doc_commands) and sound(doc_probe),
       f"{probe_report(doc_probe).get('blockers') if doc_probe else 'example NOT FOUND'}"[:90])

history = [{"mode": "multi", "counts": {"confirmed": n}, "confirmed": [
    {"title": "variant " + str(i), "defectClass": "unbounded-reader"}]} for i, n in enumerate([3, 2])]
stopped = run({"graph": "WORK.md", "pluginRoot": "/plugin", "history": history})
report("a repeated class stops the next audit before spawning",
       stopped.get("r", {}).get("plateau") and not stopped["calls"]
       and "unbounded-reader" in stopped["r"]["why"], "class persisted across rounds")
history[0]["confirmed"][0]["defectClass"] = "other"
history[1]["counts"]["confirmed"] = 3
stopped = run({"graph": "WORK.md", "pluginRoot": "/plugin", "history": history})
report("flat confirmed counts require reframe before another audit",
       stopped.get("r", {}).get("plateau") and not stopped["calls"], "plateau before spawn")
history[1]["counts"]["confirmed"] = 1
continued = run({"graph": "WORK.md", "pluginRoot": "/plugin", "history": history})
report("falling counts of different classes continue auditing",
       continued.get("r", {}).get("verdict") == "SOUND" and bool(continued["calls"]), "new audit ran")

for invalid in ({"status": "UNVERIFIED"}, {"status": "REFUTED"}, {"unverified": True},
                {"votes": [{"refuted": None}]}, {"defectClass": None}, {"defectClasses": [None, 1, ""]}):
    invalid_history = [{"mode": "multi", "counts": {"confirmed": n}, "confirmed": [
        {"title": "variant", "defectClass": "same-class", **invalid}]} for n in (3, 2)]
    result = run({"graph": "WORK.md", "pluginRoot": "/plugin", "history": invalid_history})
    report("unresolved or missing historical class cannot trigger recurrence: " + str(invalid),
           not result.get("err") and not result.get("r", {}).get("plateau") and bool(result["calls"]),
           "a fresh audit ran")

retained_history = [{"mode": "multi", "counts": {"confirmed": 1, "unverified": 1},
                     "confirmed": [{"title": "variant", "defectClass": "same-class"}],
                     "unverified": [{"title": "unresolved finding"}], "refuted": ["old claim"],
                     "deadLenses": ["runtime"], "lenses": ["runtime", "commands"]} for _ in range(2)]
result = run({"graph": "WORK.md", "pluginRoot": "/plugin", "history": retained_history})
report("plateau before another audit retains unresolved historical evidence",
       result.get("r", {}).get("plateau") and not result["calls"]
       and result["r"]["unverified"] == retained_history[-1]["unverified"]
       and result["r"]["deadLenses"] == ["runtime"] and result["r"]["refuted"] == ["old claim"],
       "no unresolved claim was erased")

# Regressions for #221–#223: reductions are deterministic and future git
# operands can be probed without creating the future branch or commit.
reduce_ir = {"nodes": [
    {"id": "builder", "mutates": True, "prompt": "Run git status"},
    {"id": "reduced", "reduce": {"from": "builder", "op": "distinct"}}], "argDefaults": {}}
reduce_result = run(dict(BASE, ir=reduce_ir),
    "(p,o)=>o.label.startsWith('probe:')?" + json.dumps({"completed": True,
        "commands": [receipt('git status')], "findings": [], "blockers": []}) + ":{findings:[]}")
report('#222 a valid reduce has no prompt and does not kill the probe plan',
       sound(reduce_result) and len(labels(reduce_result, 'probe:')) == 1,
       str((reduce_result.get('r') or {}).get('deadProbes')))
bad_probed_ir = {"nodes": [{"id": "builder", "mutates": True}], "argDefaults": {}}
bad_probed = run(dict(BASE, ir=bad_probed_ir))
report('#222 an actual probed node still requires its prompt',
       (bad_probed.get('r') or {}).get('deadProbes') == ['plan'], 'missing probe prompt refused')

for command, form, data in [
        ('git switch --detach {{prev.sha}}', 'git status', {'prev.sha': 'sha'}),
        ('git merge-base --is-ancestor {{prev.gate.sha}} HEAD', 'git status', {'prev.gate.sha': 'sha'}),
        ('git -C /repo rev-parse <future-ref>', 'git -C /repo status', {'future-ref': 'token'}),
        ('git update-ref refs/heads/<branch> HEAD {{prev.sha}}', 'git status',
         {'branch': 'token', 'prev.sha': 'sha'})]:
    result = declared_probe('Run `' + command + '`',
        {'commands': [{'command': command, 'kind': 'shape', 'form': form, 'data': data}]},
        [receipt(command, form)])
    report('#221/#223 typed future git operands accept a read in the same frame: ' + command,
           sound(result) and probe_report(result).get('commands', [{}])[0].get('kind') == 'shape-only',
           str(probe_report(result).get('blockers')))

future = 'git -C /repo switch {{prev.sha}}'
for name, form, data in [('untyped', 'git -C /repo status', {}),
                         ('different cwd', 'git -C /other status', {'prev.sha': 'sha'}),
                         ('writer form', 'git -C /repo reset --hard HEAD', {'prev.sha': 'sha'})]:
    result = declared_probe('Run `' + future + '`',
        {'commands': [{'command': future, 'kind': 'shape', 'form': form, 'data': data}]},
        [receipt(future, form)])
    report('#223 future git declaration refuses ' + name, incomplete(result),
           str(probe_report(result).get('blockers')))

compound = 'cd /repo && git -C /repo switch task; git update-ref refs/heads/task HEAD'
canonical = 'cd /repo && git -C /repo status ; git status'
generated = declared_probe('Run `' + compound + '`', None, [receipt(compound, canonical)])
report('#223 git write probes receive deterministic safe reads in the declared forms',
       sound(generated) and declared_forms(generated) == {compound: {'kind': 'shape', 'run': canonical}},
       str(declared_forms(generated)))
report('#221 probe advice names generated reads and contains no dry-run suggestion',
       bool(labels(generated, 'probe:')) and '--dry-run' not in labels(generated, 'probe:')[0]['prompt'],
       'probe prompt follows the accepted grammar')

# The compiler executes this same grammar for --check, so declarations do
# not pass only at runtime while being rejected before a graph can run.
import importlib.util
compiler_spec = importlib.util.spec_from_file_location('probe_compiler_regression', os.path.join(PLUGIN, 'scripts', 'compile-graph.py'))
compiler = importlib.util.module_from_spec(compiler_spec)
compiler_spec.loader.exec_module(compiler)
compiler_node = {'id': 'builder', 'mutates': True, 'prompt': 'Run `git switch {{prev.sha}}`',
    'probe': {'commands': [{'command': 'git switch {{prev.sha}}', 'kind': 'shape',
                            'form': 'git status', 'data': {'prev.sha': 'sha'}}]}}
report('#223 compiler check accepts the same fully typed future git declaration',
       compiler.probe_findings({'nodes': [compiler_node]}) == [], 'shared grammar accepts the declaration')
compiler_node['probe']['commands'][0]['form'] = 'git reset --hard HEAD'
report('#223 compiler check refuses a future git declaration whose form writes',
       bool(compiler.probe_findings({'nodes': [compiler_node]})), 'writer form refused by the shared grammar')

rooted = run(dict(BASE, root='/scratch/revision'),
    "(p,o)=>o.label==='lens:commands'?{findings:[F('MEDIUM','root fixture')]}:"
    "o.label==='reduce'?singletons(p):o.label.startsWith('refute:')?{refuted:true,reason:'not reproduced in this revision'}:{findings:[]}")
root_calls = [c for c in rooted['calls'] if c['label'].startswith(('lens:', 'refute:')) or c['label']=='reduce']
report('#158 every lens, reducer and refuter receives the explicit repository root',
       bool(root_calls) and all('/scratch/revision' in c['prompt'] and 'relative path' in c['prompt'] for c in root_calls)
       and (rooted.get('r') or {}).get('root')=='/scratch/revision', 'audit rooted in the requested tree')
report('#158 refuters receive the existing structural check summary',
       bool(labels(rooted,'refute:')) and all(BASE['check'] in c['prompt'] for c in labels(rooted,'refute:')),
       'structural check reused')
invalid_root = run(dict(BASE, root=17))
report('#158 a malformed root is rejected before dispatch', bool(invalid_root.get('err')) and not invalid_root['calls'],
       invalid_root.get('err','accepted'))

report('#197 completed probe output stays pending until native binding',
       (probed.get('r') or {}).get('nativeCommandBinding', {}).get('status') == 'PENDING'
       and (probed.get('r') or {}).get('ready') is False
       and 'bind-native.py' in CMD,
       'raw SOUND cannot make the graph READY')

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
