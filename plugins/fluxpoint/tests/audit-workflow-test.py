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
  failurePath: 'a concrete failure path for: ' + title,
  evidence: 'read WORK.md line 12 and ran the check',
  rewire: 'rewire the node so that ' + title + ' cannot happen'});
const reduceItems = (p) => JSON.parse(p.slice(p.indexOf('FINDINGS:\n') + 'FINDINGS:\n'.length));
const singletons = (p) => ({clusters: reduceItems(p).map(f => ({members: [f.id], settled: ''}))});
const respond = __RESPOND__;
const agent = async (prompt, opts = {}) => {
  CALLS.push({prompt: String(prompt), label: String(opts.label || ''), phase: opts.phase || null,
              effort: opts.effort || null, schema: !!opts.schema});
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


def run(args, respond="() => ({findings: []})"):
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


BASE = {"graph": "WORK.md", "pluginRoot": "/plug",
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

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
