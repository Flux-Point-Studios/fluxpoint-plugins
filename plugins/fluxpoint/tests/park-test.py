#!/usr/bin/env python3
"""Park layer: a node nobody can run must not stop the ones that can.

Before this the engine had two answers for a node it could not complete —
halt the whole campaign, or drop the item and march on with a `null`. Real
deliveries guarantee the third case: 2-of-3 signing only humans can do, a
withdrawal only an external LP can perform, a 72h timelock. The property
under test is that such a node parks, says what would unblock it, marks the
run INCOMPLETE, and lets independent branches proceed — and that its
dependents inherit BLOCKED rather than being handed a null that reads like
a failure.

Executed, not inspected: every case runs the compiled graph under stubs and
counts the agent calls that actually happened.
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)

spec = importlib.util.spec_from_file_location(
    "compile_graph", os.path.join(PLUGIN, "scripts", "compile-graph.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(os.path.join(PLUGIN, "contracts"))
RELEASE_PY = os.path.join(PLUGIN, "scripts", "release.py")
INBOX_PY = os.path.join(PLUGIN, "scripts", "inbox.py")
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<54} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def case(name, mutate, want):
    ir = copy.deepcopy(IR)
    mutate(ir)
    errs = cg.validate(ir, CONTRACTS)
    if want is None:
        report(name, not errs, "accepted" if not errs else f"rejected: {errs[:1]}")
    else:
        hit = any(want in e for e in errs)
        report(name, hit, f"rejected on '{want}'" if hit else f"NOT rejected ({errs})")


IR = {
    "version": 1,
    "name": "ceremony",
    "campaign": "vault genesis with a human signing step",
    "budget": {"maxNodes": 12},
    "nodes": [
        {"id": "prepare", "phase": "Prep", "prompt": "build the unsigned tx",
         "contract": "HarnessCheckV1"},
        {"id": "sign", "phase": "Ceremony", "actor": "human",
         "prompt": "2-of-3 hardware signing; no agent holds these keys",
         "contract": "HarnessCheckV1",
         "release": {"instructions": "Sign with 2 of the 3 hardware keys and paste "
                                     "the HarnessCheckV1 from cardano-cli.",
                     "whyNotAgent": "the signing keys are on hardware devices held by "
                                    "three people and no agent may ever hold them",
                     "proofContract": "HarnessCheckV1"},
         "wake": {"check": "cardano-cli query tip --mainnet", "everyMinutes": 30,
                  "deadline": "2030-01-01T00:00:00Z"}},
        {"id": "submit", "phase": "Ceremony", "after": "sign",
         "prompt": "submit the signed tx from {{prev}}", "contract": "HarnessCheckV1"},
        {"id": "sidework", "phase": "Parallel", "prompt": "update the runbook docs",
         "contract": "HarnessCheckV1"},
    ],
}

# ==================== validation ==========================================
case("a human node with a release block is accepted", lambda ir: None, None)
case("actor must be a known role",
     lambda ir: ir["nodes"][1].update(actor="robot"), "actor must be one of")
case("a human node with no release block",
     lambda ir: ir["nodes"][1].pop("release"), "needs a release block")
case("a release with no instructions",
     lambda ir: ir["nodes"][1]["release"].update(instructions="  "),
     "release.instructions required")
case("a proofContract that is not a contract",
     lambda ir: ir["nodes"][1]["release"].update(proofContract="NopeV1"),
     "not a known contract")
case("a proofContract that contradicts the node contract",
     lambda ir: ir["nodes"][1]["release"].update(proofContract="RedTeamV1"),
     "differs from the node's contract")
case("wake on an agent node",
     lambda ir: ir["nodes"][0].update(wake={"check": "true", "everyMinutes": 5}),
     "only meaningful with actor human")
case("wake with no predicate",
     lambda ir: ir["nodes"][1]["wake"].update(check=""), "wake.check required")
case("wake with a zero interval",
     lambda ir: ir["nodes"][1]["wake"].update(everyMinutes=0),
     "everyMinutes must be an integer >= 1")
case("a human node that also mutates",
     lambda ir: ir["nodes"][1].update(mutates=True), "cannot be combined with")
case("a misspelled release field",
     lambda ir: ir["nodes"][1]["release"].update(instuctions="typo"),
     "unknown field 'instuctions'")

case("a human node with no stated reason an agent cannot do it",
     lambda ir: ir["nodes"][1]["release"].pop("whyNotAgent"),
     "release.whyNotAgent required")

# A parked node spawns no worker but does spawn one advisor, and the ceiling
# must say so — pricing it at zero would understate the run by one per park.
tight = copy.deepcopy(IR)
tight["budget"] = {"maxNodes": 3}
report("a parked node is priced for its advisor",
       bool(cg.validate(tight, CONTRACTS)) and cg.plan_node_count(tight) == 4,
       f"{cg.plan_node_count(tight)} planned, ceiling 3 rejected")


# ==================== execution ===========================================
def run(ir, args, resolved=None):
    js = cg.emit(ir, CONTRACTS, resolved)
    with tempfile.TemporaryDirectory() as d:
        out, spawned = os.path.join(d, "o.json"), os.path.join(d, "s.json")
        w = os.path.join(d, "w.mjs")
        with open(w, "w") as fh:
            fh.write(
                "import {writeFileSync} from 'node:fs';\n"
                "const SPAWNED=[];\n"
                "const agent=async(p)=>{SPAWNED.push(p);"
                "return p.includes('cannot be run by an agent')"
                "  ? {question:'can this be automated?',options:[],"
                "     chosen:'use cardano-cli with a watch-only wallet',"
                "     rationale:'the unsigned body can be built headlessly; only the "
                "signature needs a person',overturned_prior:false,frozen_by:'none',"
                "reversible:true,evidence:[]}"
                "  : {exit:0,command:'x',output:'ok'}};\n"
                "const parallel=async(t)=>Promise.all(t.map(f=>f()));\n"
                "const pipeline=async()=>[],log=()=>{},phase=()=>{};\n"
                f"const args={json.dumps(args)};\n"
                "const budget={total:null,spent:()=>0,remaining:()=>1e9};\n"
                "const workflow=0;\n"
                "(async () => {\n" + js.replace("export const meta", "const meta")
                + "\n})().then(r=>{"
                f"writeFileSync({json.dumps(out)},JSON.stringify(r||null));"
                f"writeFileSync({json.dumps(spawned)},JSON.stringify(SPAWNED))}})"
                ".catch(e=>{"
                f"writeFileSync({json.dumps(spawned)},JSON.stringify(SPAWNED));"
                "console.error(String(e&&e.message||e))})\n")
        p = subprocess.run(["node", w], capture_output=True, timeout=60, text=True)
        s = json.load(open(out)) if os.path.exists(out) else None
        c = json.load(open(spawned)) if os.path.exists(spawned) else []
        return s, c, p.stderr


summary, calls, err = run(IR, {"_releases": {}})
prov = {p["node"]: p["status"] for p in (summary or {}).get("provenance", [])}
advisory = [c for c in calls if "cannot be run by an agent" in c]
worker = [c for c in calls
          if "hardware signing" in c and "cannot be run by an agent" not in c]
report("the human node spawns no worker", not worker, f"{len(worker)} worker spawn(s)")
report("but it does ask for a recommendation first", len(advisory) == 1,
       f"{len(advisory)} advisory spawn(s)")
# The advisor is told to challenge the block before accepting it, because
# most steps that feel human-only are a CLI call away.
report("the advisor is told to challenge the block first",
       bool(advisory) and "headless browser" in advisory[0],
       "challenged" if advisory and "headless browser" in advisory[0] else "not asked")
report("the advisor is given the stated reason to attack",
       bool(advisory) and "hardware devices held by" in advisory[0], "supplied")
report("a recommendation comes back with the block",
       ((summary or {}).get("recommendations") or {}).get("sign", {}).get("chosen")
       is not None,
       str(((summary or {}).get("recommendations") or {}).get("sign"))[:40])
blocked_detail = [p.get("detail") for p in (summary or {}).get("provenance", [])
                  if p.get("node") == "sign"]
report("the block itself carries the recommendation",
       bool(blocked_detail) and "RECOMMENDED:" in (blocked_detail[0] or ""),
       (blocked_detail[0] or "")[-40:] if blocked_detail else "none")
report("the human node is reported BLOCKED", prov.get("sign") == "BLOCKED",
       prov.get("sign", f"absent; stderr={err[:40]}"))
report("its dependent inherits BLOCKED", prov.get("submit") == "BLOCKED",
       prov.get("submit", "absent"))
report("an independent branch still runs", prov.get("sidework") == "OK",
       prov.get("sidework", "absent"))
report("upstream work still ran", prov.get("prepare") == "OK",
       prov.get("prepare", "absent"))
report("the campaign does not halt", (summary or {}).get("outcome") == "INCOMPLETE",
       (summary or {}).get("outcome", "none"))
report("a blocked run can never read COMPLETE",
       (summary or {}).get("outcome") != "COMPLETE", (summary or {}).get("outcome"))
report("the wait is emitted with its predicate",
       bool([w for w in (summary or {}).get("waits", []) if w.get("check")]),
       str((summary or {}).get("waits"))[:60])
report("blocked ids ride out in the summary",
       set((summary or {}).get("blocked", [])) == {"sign", "submit"},
       str((summary or {}).get("blocked")))

# The instructions are the entire message the blocked human gets.
detail = [p.get("detail") for p in (summary or {}).get("provenance", [])
          if p.get("node") == "sign"]
report("the block carries its instructions",
       bool(detail) and "hardware keys" in (detail[0] or ""), str(detail)[:50])

# Released: the same graph, now with the proof on hand.
released = {"sign": {"node": "sign", "by": "operator",
                     "proof": {"exit": 0, "command": "cardano-cli", "output": "tx_ok"}}}
summary2, calls2, _ = run(IR, {"_releases": released})
prov2 = {p["node"]: p["status"] for p in (summary2 or {}).get("provenance", [])}
report("a released node reports RELEASED", prov2.get("sign") == "RELEASED",
       prov2.get("sign", "absent"))
report("the dependent runs once the block lifts", prov2.get("submit") == "OK",
       prov2.get("submit", "absent"))
report("the released campaign completes",
       (summary2 or {}).get("outcome") == "COMPLETE",
       (summary2 or {}).get("outcome"))
report("the pasted proof is what downstream consumes",
       ((summary2 or {}).get("results") or {}).get("sign", {}).get("output") == "tx_ok",
       str(((summary2 or {}).get("results") or {}).get("sign"))[:40])

# A missing releases map disarms the whole layer, so it must fail loudly.
summary3, calls3, err3 = run(IR, {})
report("no releases map -> the run throws",
       summary3 is None and "releases" in err3, (err3.strip()[:50] or "no error"))

# A graph with no parked nodes carries none of this machinery.
plain = copy.deepcopy(IR)
plain["nodes"] = [plain["nodes"][0], plain["nodes"][3]]
report("no park code without an actor", "BLOCKED" not in cg.emit(plain, CONTRACTS),
       "clean")

# ==================== release: proof, not an adjective ====================
with tempfile.TemporaryDirectory() as root:
    def rel(*a, stdin=""):
        return subprocess.run(
            [sys.executable, RELEASE_PY, "--root", root, "--plugin-root", PLUGIN, *a],
            input=stdin, capture_output=True, text=True)

    r = rel("--record", "--campaign", "c", "--node", "sign",
            "--contract", "HarnessCheckV1", stdin='"done"')
    report("a bare adjective is refused", r.returncode == 1,
           (r.stderr.strip().splitlines() or ["none"])[0][:50])

    r = rel("--record", "--campaign", "c", "--node", "sign",
            "--contract", "HarnessCheckV1", stdin='{"command":"x"}')
    report("a proof missing a required field is refused",
           r.returncode == 1 and "exit" in r.stderr, r.stderr.strip()[-40:])

    r = rel("--record", "--campaign", "c", "--node", "sign",
            "--contract", "HarnessCheckV1",
            stdin='{"exit":0,"command":"  ","tail":"x"}')
    report("a blank required string is refused",
           r.returncode == 1 and "blank proof" in r.stderr, r.stderr.strip()[-40:])

    r = rel("--record", "--campaign", "c", "--node", "sign",
            "--contract", "HarnessCheckV1",
            stdin='{"exit":0,"command":"cardano-cli submit","tail":"tx_abc"}')
    report("a well-formed proof is recorded", r.returncode == 0, r.stdout.strip()[:50])

    r = rel("--load", "--campaign", "c")
    loaded = json.loads(r.stdout)
    report("the release loads back keyed by node", "sign" in loaded,
           str(list(loaded))[:30])
    report("the recorded proof is the pasted document",
           loaded.get("sign", {}).get("proof", {}).get("tail") == "tx_abc",
           str(loaded.get("sign", {}).get("proof"))[:40])

    r = rel("--load", "--campaign", "a different campaign")
    report("another campaign's releases are not consulted", r.stdout.strip() == "{}",
           r.stdout.strip()[:20])

    # A graph compiled against its CONTRACTS: header parks on a proof
    # contract the plugin does not ship; the release must find it too.
    os.makedirs(os.path.join(root, ".local-contracts"))
    with open(os.path.join(root, ".local-contracts", "SignedV1.schema.json"), "w") as fh:
        json.dump({"$id": "SignedV1", "type": "object", "required": ["txHash"],
                   "properties": {"txHash": {"type": "string"}}}, fh)
    with open(os.path.join(root, "GRAPH.sign.md"), "w") as fh:
        fh.write("STATUS: READY\nCONTRACTS: .local-contracts\n")
    r = rel("--record", "--campaign", "c", "--node", "sign2", "--contract", "SignedV1",
            "--graph", "GRAPH.sign.md", stdin='{"txHash":"ab12"}')
    report("a repo-local proofContract resolves through the CONTRACTS: header",
           r.returncode == 0, (r.stderr or r.stdout).strip()[:50])
    r = rel("--record", "--campaign", "c", "--node", "sign2", "--contract", "SignedV1",
            "--graph", "GRAPH.sign.md", stdin='{"note":"signed"}')
    report("  and is still validated against it", r.returncode == 1 and "txHash" in r.stderr,
           r.stderr.strip()[-40:])

# ==================== inbox ==============================================
with tempfile.TemporaryDirectory() as root:
    def ib(*a):
        return subprocess.run([sys.executable, INBOX_PY, "--root", root, *a],
                              capture_output=True, text=True)

    report("an empty inbox counts zero", ib("--count").stdout.strip() == "0", "0")
    ib("--add", "--kind", "blocked", "--node", "sign", "--campaign", "c",
       "--detail", "2-of-3 signing")
    report("a block raises an item", ib("--count").stdout.strip() == "1", "1")
    ib("--add", "--kind", "blocked", "--node", "sign", "--campaign", "c",
       "--detail", "2-of-3 signing")
    report("the same block twice is one item, not two",
           ib("--count").stdout.strip() == "1", ib("--count").stdout.strip())
    r = ib("--list")
    report("--list says what is waiting and how to clear it",
           "2-of-3 signing" in r.stdout and "--resolve" in r.stdout,
           r.stdout.strip().splitlines()[0][:40])
    ib("--resolve", "blocked:c:sign")
    report("resolving closes it", ib("--count").stdout.strip() == "0", "0")
    report("a resolved item stays in the log",
           len([l for l in open(os.path.join(root, ".claude", "fluxpoint",
                                             "inbox.jsonl"))]) == 2, "2 rows")

# ==================== decisions: the choice, not the prose ================
# DesignV1 has summary/plan/files/risks and none of them is the choice, so a
# frozen decision had to be smuggled into free text and the rejected options
# had nowhere to go at all. The one that overturns the prior is exactly the
# one a fresh context re-decides the other way.
DEC_IR = {
    "version": 1, "name": "d", "campaign": "freeze the vault parameters",
    "budget": {"maxNodes": 8},
    "nodes": [
        {"id": "choose", "phase": "Council", "prompt": "pick the window and say why",
         "contract": "DecisionV1", "decides": "vault-window"},
        {"id": "build", "phase": "Build", "prompt":
            "implement under {{decisions.vault-window}}",
         "contract": "SliceV1", "honors": ["vault-window"]},
    ],
}
report("a decision graph compiles", not cg.validate(DEC_IR, CONTRACTS),
       str(cg.validate(DEC_IR, CONTRACTS))[:60])


def dcase(name, mutate, want):
    ir = copy.deepcopy(DEC_IR)
    mutate(ir)
    errs = cg.validate(ir, CONTRACTS)
    hit = any(want in e for e in errs)
    report(name, hit, f"rejected on '{want}'" if hit else f"NOT rejected ({errs})")


dcase("decides on a non-DecisionV1 node",
      lambda ir: ir["nodes"][0].update(contract="DesignV1"),
      "requires contract DecisionV1")
dcase("honors a decision nobody makes",
      lambda ir: ir["nodes"][1].update(honors=["nonexistent"],
                                       prompt="x {{decisions.nonexistent}}"),
      "neither decided by an earlier node nor listed under imports")
dcase("honors without reading it is a phantom edge",
      lambda ir: ir["nodes"][1].update(prompt="implement it"),
      "the prompt never uses")
dcase("reading a decision this node does not honor",
      lambda ir: ir["nodes"][1].update(honors=[],
                                       prompt="x {{decisions.vault-window}}"),
      "is not in this node's honors")
dcase("two nodes deciding the same thing",
      lambda ir: ir["nodes"].append(
          {"id": "again", "phase": "C", "prompt": "re-pick", "contract": "DecisionV1",
           "decides": "vault-window"}),
      "is already decided by node")
dcase("an import that is not a run reference",
      lambda ir: ir.update(imports={"vault-window": ""}),
      "must be a runId")
dcase("re-deciding an imported decision",
      lambda ir: ir.update(imports={"vault-window": "latest"}),
      "imported decision is frozen")

dec_js = cg.emit(copy.deepcopy(DEC_IR), CONTRACTS)
report("the decision is bound into the honoring prompt",
       'JSON.stringify(DECISIONS["vault-window"])' in dec_js, "bound")
report("overturning the prior is logged, not buried",
       "overturned the prior" in dec_js, "logged")
report("no decisions machinery without a decision",
       "DECISIONS" not in cg.emit(copy.deepcopy(IR), CONTRACTS), "clean")

# ==================== imports: resolved and embedded, never couriered =====
# The old shape emitted a launch-time throw against an args._decisions map
# the orchestrating agent assembled by hand — the most-protected artifact
# class with the least-protected loading path, since the throw checked only
# that *some* record arrived. Records now resolve from recorded runs at
# compile time and are embedded in the generated script.
IMP = copy.deepcopy(DEC_IR)
IMP["nodes"] = [IMP["nodes"][1]]
IMP["imports"] = {"vault-window": "latest"}

FROZEN = {
    "question": "how long should the vault unlock window stay open?",
    "options": [
        {"option": "24h", "argued_by": "ops",
         "strongest_objection": "a day may be too slow for incident response"},
        {"option": "72h", "argued_by": "gov",
         "strongest_objection": "three days widens the attack window"},
    ],
    "chosen": "72h",
    "rationale": "the governance timelock already binds operations to 72h, "
                 "so a shorter unlock window buys nothing and adds a race",
    "overturned_prior": False, "frozen_by": "genesis", "reversible": False,
    "evidence": ["scripts/harness.sh --full exit 0"],
}
NEWER = dict(FROZEN, chosen="24h",
             rationale="response drills showed 24h suffices and narrows "
                       "exposure by two thirds against the same ops load")


def _art(run_id, when, decisions):
    return {"runId": run_id, "when": when, "summary": {"decisions": decisions}}


with tempfile.TemporaryDirectory() as runs:
    def put(run_id, payload):
        with open(os.path.join(runs, run_id + ".json"), "w") as fh:
            json.dump(payload, fh)

    resolved, errs = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("an import with no recorded run fails the compile",
           not resolved and any("no recorded run" in e for e in errs),
           (errs or ["none"])[0][:50])

    put("wf-old", _art("wf-old", "2026-08-01 10:00", {"vault-window": FROZEN}))
    put("wf-new", _art("wf-new", "2026-08-07 09:00", {"vault-window": NEWER}))
    resolved, errs = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("latest is the newest run carrying the decision",
           not errs and resolved.get("vault-window", {}).get("runId") == "wf-new",
           str(resolved.get("vault-window", {}).get("runId")))
    # A run that honored an IMPORTED decision carries it as provenance; it
    # is not a newer ruling, however late the run was recorded.
    hon = _art("wf-hon", "2026-08-09 09:00", {"vault-window": FROZEN})
    hon["summary"]["decisionsImported"] = {"vault-window": "wf-old"}
    put("wf-hon", hon)
    resolved, errs = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("  and a run's imported copy is not a newer ruling",
           not errs and resolved.get("vault-window", {}).get("runId") == "wf-new",
           str(resolved.get("vault-window", {}).get("runId")))
    os.remove(os.path.join(runs, "wf-hon.json"))

    imp_js = cg.emit(copy.deepcopy(IMP), CONTRACTS, resolved)
    report("the resolved record is embedded, not couriered",
           '"chosen": "24h"' in imp_js and "_decisions" not in imp_js, "embedded")
    report("the source run is named next to the record",
           '"vault-window": "wf-new"' in imp_js, "named")

    pinned = copy.deepcopy(IMP)
    pinned["imports"] = {"vault-window": "wf-old"}
    resolved2, errs2 = cg.resolve_imports(pinned, CONTRACTS, runs)
    report("a pinned runId beats a newer run",
           not errs2 and resolved2.get("vault-window", {}).get("runId") == "wf-old"
           and resolved2["vault-window"]["record"]["chosen"] == "72h",
           str(resolved2.get("vault-window", {}).get("runId")))

    absent = copy.deepcopy(IMP)
    absent["imports"] = {"vault-window": "wf-nope"}
    _, errs3 = cg.resolve_imports(absent, CONTRACTS, runs)
    report("a pinned run that does not exist is a finding",
           any("not found" in e for e in errs3), (errs3 or ["none"])[0][:50])

    # A gutted record in the newest run must fail loudly, never silently
    # fall back to an older intact one — the artifact was tampered with.
    put("wf-cut", _art("wf-cut", "2026-08-08 09:00", {"vault-window": {"chosen": "12h"}}))
    _, errs4 = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("a hand-gutted record does not count as a decision",
           any("missing required DecisionV1" in e for e in errs4),
           (errs4 or ["none"])[0][:60])
    os.remove(os.path.join(runs, "wf-cut.json"))

    # Operator rulings are recorded with decision.py, never by a graph node,
    # so they never reach a run artifact. The store is the other source an
    # import resolves from (#98), and 'latest' spans both.
    store = os.path.join(runs, "decisions.jsonl")
    ruling = dict(FROZEN, chosen="48h", rationale="the operator ruled 48h after "
                  "the incident review, overturning the governance default",
                  options=FROZEN["options"] + [
                      {"option": "48h", "argued_by": "operator",
                       "strongest_objection": "splits the difference without new data"}])
    with open(store, "w") as fh:
        fh.write(json.dumps({"recordId": "dec_0123456789ab", "id": "vault-window",
                             "when": "2026-08-09 12:00:00", "record": ruling}) + "\n")
    resolved_s, errs_s = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs, store)
    # A record with every field name but the wrong types or floors is not a
    # decision either, wherever it was stored.
    bogus = dict(FROZEN, question=1, options=[], chosen="ghost", reversible="no")
    nested = dict(FROZEN, evidence=[1, 2], options=[
        {"option": "24h", "argued_by": 7, "strongest_objection": 12345678901234567890},
        FROZEN["options"][1]])
    with open(os.path.join(runs, "wf-nested.json"), "w") as fh:
        json.dump(_art("wf-nested", "2030-06-01 00:00", {"vault-window": nested}), fh)
    _, errs_n = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("a record with numeric option fields or evidence is not a valid DecisionV1",
           any("not a valid DecisionV1" in e for e in errs_n), (errs_n or ["none"])[0][:70])
    os.remove(os.path.join(runs, "wf-nested.json"))
    with open(os.path.join(runs, "wf-bogus.json"), "w") as fh:
        json.dump(_art("wf-bogus", "2030-01-01 00:00", {"vault-window": bogus}), fh)
    _, errs_b = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("a record that is not a valid DecisionV1 does not count as one",
           any("not a valid DecisionV1" in e for e in errs_b), (errs_b or ["none"])[0][:70])
    os.remove(os.path.join(runs, "wf-bogus.json"))
    # ...but a record a graph node froze is judged by the schema it was held
    # to, not decision.py's stricter chosen-among-options rule.
    worded = dict(FROZEN, chosen="72h, matching the governance timelock")
    with open(os.path.join(runs, "wf-worded.json"), "w") as fh:
        json.dump(_art("wf-worded", "2031-01-01 00:00", {"vault-window": worded}), fh)
    res_w, errs_w = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("  while an honest run's decision worded beside its options still imports",
           not errs_w and res_w.get("vault-window", {}).get("runId") == "wf-worded",
           str(errs_w or res_w.get("vault-window", {}).get("runId")))
    os.remove(os.path.join(runs, "wf-worded.json"))
    report("an import resolves a decision recorded with decision.py",
           not errs_s and resolved_s.get("vault-window", {}).get("runId") == "dec_0123456789ab"
           and resolved_s["vault-window"]["record"]["chosen"] == "48h",
           str(resolved_s.get("vault-window", {}).get("runId")))
    # Same minute, run recorded after the ruling: the run is the latest.
    # Compared as text, 'HH:MM:SS' outranked 'HH:MM' whatever came first.
    put("wf-late", dict(_art("wf-late", "2026-08-09 12:00", {"vault-window": NEWER}),
                        recordedAt="2026-08-09 12:00:45"))
    resolved_t, _ = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs, store)
    report("latest orders a run and a ruling from the same minute by the second",
           resolved_t.get("vault-window", {}).get("runId") == "wf-late",
           str(resolved_t.get("vault-window", {}).get("runId")))
    spec_d = importlib.util.spec_from_file_location(
        "decision", os.path.join(PLUGIN, "scripts", "decision.py"))
    dmod = importlib.util.module_from_spec(spec_d)
    spec_d.loader.exec_module(dmod)
    report("  and decision.py orders the two the same way",
           dmod.order_key("2026-08-09 12:00") < dmod.order_key("2026-08-09 12:00:30")
           < dmod.order_key("2026-08-09T12:00:45Z"), "normalized")
    os.remove(os.path.join(runs, "wf-late.json"))
    pinned_s = copy.deepcopy(IMP)
    pinned_s["imports"] = {"vault-window": "wf-new"}
    resolved_p, _ = cg.resolve_imports(pinned_s, CONTRACTS, runs, store)
    report("  and a pinned runId still names the run",
           resolved_p.get("vault-window", {}).get("runId") == "wf-new", "pinned")
    os.remove(store)

    with open(os.path.join(runs, "wf-bad.json"), "w") as fh:
        fh.write("{not json")
    _, errs5 = cg.resolve_imports(copy.deepcopy(IMP), CONTRACTS, runs)
    report("a malformed run artifact is a hard finding, never skipped",
           any("not readable JSON" in e for e in errs5),
           (errs5 or ["none"])[0][:60])
    os.remove(os.path.join(runs, "wf-bad.json"))

    # Executed: the frozen choice reaches the honoring prompt, and the
    # source run rides out in the summary for the recorder.
    summary_i, calls_i, err_i = run(copy.deepcopy(IMP), {}, resolved)
    honoring = [c for c in calls_i if "implement under" in c]
    report("the frozen choice reaches the honoring prompt",
           bool(honoring) and '"chosen":"24h"' in honoring[0],
           honoring[0][:60] if honoring else f"no spawn; stderr={err_i[:40]}")
    report("imported provenance rides out in the summary",
           (summary_i or {}).get("decisionsImported", {}).get("vault-window") == "wf-new",
           str((summary_i or {}).get("decisionsImported")))

# The refuters' arguments were collected and discarded; a survivor with its
# strongest objection recorded is worth more later than a vote count.
PANEL = {**copy.deepcopy(DEC_IR), "nodes": [
    {"id": "find", "phase": "F", "prompt": "find things", "contract": "FindingsV1",
     "verify": "panel:3", "verifyOver": "findings"}]}
report("refuter reasons survive the tally",
       "objections: cast.map(v => v.reason)" in cg.emit(PANEL, CONTRACTS), "carried")

# ==================== the parked node's own placeholders ==================
# A parked node's prompt and instructions used to be emitted through js_str,
# which is a double-quoted JS literal and interpolates nothing — so the
# advisor was handed the characters "{{A.branch}}" while being told to ground
# its recommendation in the repo, and the blocked human read the same. Every
# other prompt goes through js_template. The validator made it worse than a
# gap: its scope check walks SUBST over every non-reduce prompt, so the token
# was blessed as in-scope while the emitter rendered it literal — a binding
# promised and never made.
SUB = copy.deepcopy(IR)
SUB["argDefaults"] = {"branch": "claude/graph-x", "base": "main"}
SUB["nodes"][1]["prompt"] = "sign the tx on {{A.branch}} and compare it to {{A.base}}"
SUB["nodes"][1]["release"]["instructions"] = "check out {{A.branch}}, then merge to {{A.base}}"
emitted = cg.emit(SUB, CONTRACTS)

report("a parked prompt interpolates its args",
       "${tokenText(A.branch)}" in emitted and "{{A.branch}}" not in emitted,
       "interpolated" if "{{A.branch}}" not in emitted else "left literal")
report("and so do its release instructions",
       emitted.count("${tokenText(A.base)}") >= 2,
       f"{emitted.count('${tokenText(A.base)}')} site(s)")

# The emitted script must still parse: js_template escapes backticks and ${,
# and a release string is operator prose that can contain either.
EVIL = copy.deepcopy(SUB)
EVIL["nodes"][1]["release"]["instructions"] = "run `cmd` and mind ${notAVar} and a \\ backslash"
ev = cg.emit(EVIL, CONTRACTS)
# The emitted script is a workflow BODY, not a module: the runtime wraps it,
# so it carries top-level `return` and `node --check` rejects it on its own.
# Re-wrap it the way the runtime does before asking whether it parses.
wrapped = "async function _fpl_body() {\n" + \
    ev.replace("export const meta", "const meta", 1) + "\n}\n"
with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                 encoding="utf-8") as fh:
    fh.write(wrapped)
    evp = fh.name
rc = subprocess.run([os.environ.get("FPL_NODE", "node"), "--check", evp],
                    capture_output=True, text=True)
report("a release string with backticks and ${ still parses",
       rc.returncode == 0, "parses" if rc.returncode == 0 else rc.stderr.strip()[:60])
report("and the literal ${ is escaped, not interpolated",
       "\\${notAVar}" in ev, "escaped" if "\\${notAVar}" in ev else "LEAKED")
os.unlink(evp)

# ============ the other half of the interpolation promise ================
# Moving these two fields to js_template made them ${...} sites. The scope
# check walked only n["prompt"], so release.instructions was an interpolation
# surface no validator covered: {{process.env.X}} in operator prose compiled
# clean and read a secret into the advisor prompt, the provenance note and
# the run log. A validator that blesses what the emitter will not bind, and
# an emitter that binds what no validator checked, are the same defect from
# opposite ends -- both are covered below.

HOSTILE = copy.deepcopy(IR)
HOSTILE["nodes"][1]["release"]["instructions"] = (
    "Sign and paste. leak={{process.env.AWS_SECRET_ACCESS_KEY}}")
h_errs = cg.validate(HOSTILE, CONTRACTS)
report("an out-of-scope token in release.instructions is refused",
       any("release.instructions uses" in e and "not in scope" in e
           for e in h_errs),
       h_errs[0][:58] if h_errs else "ACCEPTED")

# The compiler must refuse the IR, not merely notice it: main() returns 1 on
# findings before emit() is ever called, which is what makes the rejection a
# gate rather than a remark.
report("and the same token in the prompt is refused as before",
       any("prompt uses" in e and "not in scope" in e
           for e in cg.validate(
               {**copy.deepcopy(IR), "nodes": [
                   {**copy.deepcopy(IR)["nodes"][0]},
                   {**copy.deepcopy(IR)["nodes"][1],
                    "prompt": "sign {{process.env.SECRET}}"},
                   *copy.deepcopy(IR)["nodes"][2:]]}, CONTRACTS)),
       "refused")

# A token the validator DOES bless must bind, or the graph compiles clean and
# throws ReferenceError at launch -- the failure the scope check exists to
# prevent, one field over.
BOUND = copy.deepcopy(IR)
BOUND["nodes"][1]["after"] = "prepare"
BOUND["nodes"][1]["prompt"] = "sign what {{prev}} produced"
BOUND["nodes"][1]["release"]["instructions"] = (
    "Sign the body from {{prev.exit}} and paste the result.")
b_errs = cg.validate(BOUND, CONTRACTS)
report("a parked node may consume its predecessor",
       not b_errs, str(b_errs)[:58] if b_errs else "accepted")
bj = cg.emit(BOUND, CONTRACTS)
report("{{prev}} in a parked prompt binds to RESULTS",
       "${JSON.stringify(RESULTS[\"prepare\"])}" in bj,
       "bound" if "${JSON.stringify(RESULTS[\"prepare\"])}" in bj else "UNBOUND")
report("{{prev.field}} in release instructions binds too",
       "${prev.exit}" not in bj and 'RESULTS["prepare"] || {})["exit"]' in bj,
       "bound" if "${prev.exit}" not in bj else "UNBOUND ${prev.exit}")

# The blessed tokens are exactly the ones that could go unbound, so assert on
# them by name rather than by scanning every ${...} the script legitimately
# writes. `${prev`, `${decisions.` and `${seen}` name no JS binding anywhere.
leaked = [t for t in ("${prev}", "${prev.", "${decisions.", "${seen}")
          if t in bj]
report("no blessed token reaches the script unbound", not leaked,
       "clean" if not leaked else f"unbound: {leaked}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
