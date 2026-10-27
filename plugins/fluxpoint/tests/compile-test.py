#!/usr/bin/env python3
"""Invariant tests for the fluxpoint graph compiler.

Each case asserts that a specific class of unsound graph is REJECTED at
compile time, and that the canonical sound graph compiles. Run:
  python3 plugins/fluxpoint/tests/compile-test.py
"""
import copy
import json
import os
import sys
from repair_proof_fixture import wire as proof_fixture

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "compile_graph", os.path.join(PLUGIN, "scripts", "compile-graph.py")
)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

CONTRACTS = cg.load_contracts(os.path.join(PLUGIN, "contracts"))

BASE = {
    "version": 1,
    "name": "t",
    "campaign": "a campaign that exists to be validated",
    # The canonical sound graph declares the prompt-cache TTL its fan-out
    # needs; without one the compiler (rightly) says the 5-minute default
    # expires while the parent blocks, and the silence cases below would
    # be measuring that warning instead of their own.
    "budget": {"maxNodes": 32, "cacheTtl": "1h"},
    "lists": {"dims": [{"key": "a", "brief": "x"}]},
    "nodes": [
        {
            "id": "find",
            "phase": "Find",
            "foreach": "dims",
            "prompt": "review {{item.brief}}",
            "contract": "FindingsV1",
            "verify": "panel:3",
            "verifyOver": "findings",
            "expectItems": 2,
        }
    ],
}

passed = failed = 0


def case(name, mutate, want_reject_substr):
    """want_reject_substr=None means the IR must be ACCEPTED."""
    global passed, failed
    ir = copy.deepcopy(BASE)
    mutate(ir)
    findings = cg.validate(ir, CONTRACTS)
    if want_reject_substr is None:
        ok = not findings
        detail = "accepted" if ok else f"rejected: {findings}"
    else:
        ok = any(want_reject_substr in f for f in findings)
        detail = f"rejected on '{want_reject_substr}'" if ok else f"NOT rejected (got {findings})"
    print(f"{'PASS' if ok else 'FAIL'}  {name:<54} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


# The canonical sound graph must compile and emit valid-looking JS.
case("sound graph accepted", lambda ir: None, None)

# Contract layer.
case("node without a contract", lambda ir: ir["nodes"][0].pop("contract"), "contract required")
case("unknown contract name", lambda ir: ir["nodes"][0].update(contract="NopeV9"), "unknown contract")
case(
    "verifyOver names a non-field",
    lambda ir: ir["nodes"][0].update(verifyOver="nope"),
    "is not a field",
)

# Verification layer — the bug that shipped in v0.1.
def add_mutator(ir):
    ir["nodes"].append(
        {
            "id": "build",
            "phase": "Build",
            "prompt": "implement it",
            "contract": "SliceV1",
            "mutates": True,
        }
    )


case("mutator with no independent verifier", add_mutator, "may not certify its own work")


def add_verified_mutator(ir):
    add_mutator(ir)
    ir["nodes"].append(
        {
            "id": "gate",
            "phase": "Gate",
            "after": "build",
            "prompt": "re-run the harness yourself against this slice: {{prev}}",
            "contract": "HarnessCheckV1",
            "independent": True,
            "verifies": "build",
            "haltWhen": "exit != 0",
        }
    )


case("mutator WITH independent verifier", add_verified_mutator, None)


def self_verify(ir):
    add_mutator(ir)
    ir["nodes"][1].update(independent=True, verifies="build")


case("node verifying itself", self_verify, "cannot verify itself")


def verifier_not_independent(ir):
    add_verified_mutator(ir)
    ir["nodes"][2].pop("independent")


case("verifier not marked independent", verifier_not_independent, "not marked independent")

# Panel sanity.
case("even panel (majority undefined)", lambda ir: ir["nodes"][0].update(verify="panel:4"), "even")
case("bogus tier", lambda ir: ir["nodes"][0].update(verify="vibes"), "verify must be")
case(
    "panel without verifyOver",
    lambda ir: ir["nodes"][0].pop("verifyOver"),
    "needs verifyOver",
)

# Edge layer.
case(
    "after references unknown node",
    lambda ir: ir["nodes"][0].update(after="ghost"),
    "is not a node defined earlier",
)


def phantom_after(ir):
    ir["nodes"].append(
        {"id": "next", "phase": "P", "after": "find", "prompt": "carry on",
         "contract": "DesignV1"})


case("after whose prompt never consumes {{prev}}", phantom_after, "never uses")


def prev_without_after(ir):
    ir["nodes"].append(
        {"id": "next", "phase": "P", "prompt": "use {{prev}} anyway",
         "contract": "DesignV1"})


case("{{prev}} with no after edge", prev_without_after, "hidden coupling")

# Budget as cost, not calls (issue #66). maxNodes is a fan-out guardrail;
# the estimate prices effort, the prompt cache and the model, and the
# ceiling on it is the one the bill answers to.
case("maxEstimatedTokens below the estimate", lambda ir: ir["budget"].update(maxEstimatedTokens=1000),
     "estimated at")
case("maxEstimatedTokens above the estimate", lambda ir: ir["budget"].update(maxEstimatedTokens=10_000_000), None)
case("maxEstimatedTokens that is not a positive integer", lambda ir: ir["budget"].update(maxEstimatedTokens="lots"),
     "positive integer")
case("cacheTtl the runtime does not offer", lambda ir: ir["budget"].update(cacheTtl="2h"), "cacheTtl must be")
case("cacheTtl of an hour", lambda ir: ir["budget"].update(cacheTtl="1h"), None)

# --- {{prev.<field>}} projection ------------------------------------------
# The trap this closes: a dotted prev token used to pass validation on its
# root and emit `${prev.<field>}` with no JS binding — a graph that compiled
# clean and died at launch with a ReferenceError.


def projector(prompt, pred=None):
    def m(ir):
        ir["nodes"].append(pred or {
            "id": "gate", "phase": "G", "prompt": "run it",
            "contract": "HarnessCheckV1"})
        ir["nodes"].append({
            "id": "reader", "phase": "G", "after": ir["nodes"][-1]["id"],
            "prompt": prompt, "contract": "DesignV1"})
    return m


case("prev projection of a real field accepted",
     projector("the exit was {{prev.exit}}"), None)
case("prev projection of an unknown field",
     projector("the exit was {{prev.nope}}"), "is not a field of")
case("prev projection more than one hop deep",
     projector("{{prev.a.b}}"), "single-hop")
case("prev projection against an item-array predecessor",
     projector("{{prev.findings}}", pred={
         "id": "hunt", "phase": "H", "prompt": "hunt", "contract": "FindingsV1",
         "verify": "skeptic:1", "verifyOver": "findings", "expectItems": 2}),
     "consume {{prev}} whole")

proj = copy.deepcopy(BASE)
projector("the exit was {{prev.exit}} of {{prev.command}}")(proj)
proj_js = cg.emit(proj, CONTRACTS)
for needle, why in [
    ('(RESULTS["gate"] || {})["exit"] ?? null', "field is bound via bracket access, not a bare identifier"),
    ('["command"] ?? null', "every projected field gets its own binding"),
]:
    ok = needle in proj_js
    print(f"{'PASS' if ok else 'FAIL'}  projection: {why:<44} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

# --- onRed is a closed registry and real on fan-out -----------------------
case("onRed outside the registry",
     lambda ir: ir["nodes"][0].update(onRed="Halt"), "onRed must be one of")

halt_fan = copy.deepcopy(BASE)
halt_fan["nodes"][0].update(onRed="halt")
halt_js = cg.emit(halt_fan, CONTRACTS)
drop_js = cg.emit(BASE, CONTRACTS)
for src, name, needle, want in [
    (halt_js, "onRed=halt on a fan-out emits the halt", "halting per onRed=halt", True),
    (halt_js, "a dead fan-out worker ends in NODE-DEAD", "summary('NODE-DEAD')", True),
    (drop_js, "onRed=drop+log fan-out does not halt", "halting per onRed=halt", False),
    # A ceiling-declined spawn is exhaustion, not death: it must file
    # SKIPPED and, under halt, end BUDGET-EXHAUSTED — never NODE-DEAD.
    (halt_js, "ceiling declines file SKIPPED, not DEAD", "declined by the node ceiling", True),
    (halt_js, "ceiling declines halt as exhaustion", "halting as exhausted, not as dead", True),
    (halt_js, "declined spawns are counted apart from deaths", "DECLINED_LABELS", True),
]:
    ok = (needle in src) == want
    print(f"{'PASS' if ok else 'FAIL'}  onRed: {name:<48} -> {'as declared' if ok else 'WRONG'}")
    passed, failed = (passed + ok, failed + (not ok))

# A dead discovery round must never read as a dry one — a finder that
# keeps crashing would end the sweep looking converged.
dead_round = copy.deepcopy(BASE)
dead_round["nodes"][0]["repeat"] = {
    "untilDryRounds": 2, "maxRounds": 4, "dedupeBy": ["file", "line"]}
dead_round["nodes"][0]["prompt"] = "hunt, seen: {{seen}}"
dead_round["budget"]["maxNodes"] = 200
dead_js = cg.emit(dead_round, CONTRACTS)
for needle, why in [
    ("a dead round is never a dry round", "dead rounds are named, not counted as converged"),
    ("if (!raw_n_find.length) continue", "an all-dead round skips the dry counter"),
    ("NOT counted dry", "a PARTIALLY dead round skips the dry counter too"),
    ("perWorker_n_find[wk] = null", "a dead worker reads null, keyed by identity"),
    ("perWorker", "per-worker unique-new counts recorded for fan-out efficiency"),
]:
    ok = needle in dead_js
    print(f"{'PASS' if ok else 'FAIL'}  discovery: {why:<45} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))
case(
    "foreach references unknown list",
    lambda ir: ir["nodes"][0].update(foreach="ghosts"),
    "has no entry under lists",
)
case(
    "role references undeclared role",
    lambda ir: ir["nodes"][0].update(role="ghost"),
    "is not declared under roles",
)

# Budget layer.
case("fan-out exceeds budget", lambda ir: ir["budget"].update(maxNodes=3), "budget.maxNodes is 3")
case("no budget ceiling", lambda ir: ir.pop("budget"), "budget.maxNodes: required")

# Halt grammar.
case(
    "malformed haltWhen",
    lambda ir: ir["nodes"][0].update(haltWhen="if it feels wrong"),
    "haltWhen must be",
)

# --- discovery loops (repeat) -------------------------------------------
def make_repeat(**over):
    def m(ir):
        r = {"untilDryRounds": 2, "maxRounds": 4, "dedupeBy": ["file", "line"]}
        r.update(over)
        for k, v in list(r.items()):
            if v is None:
                del r[k]
        ir["nodes"][0]["repeat"] = r
    return m


case("repeat: sound discovery loop accepted", make_repeat(), None)
case("repeat: no maxRounds (unbounded)", make_repeat(maxRounds=None), "maxRounds must be an integer >= 1")
case("repeat: no dry rule", make_repeat(untilDryRounds=None), "untilDryRounds must be an integer >= 1")
case("repeat: dry rule can never fire", make_repeat(untilDryRounds=9), "can never fire")
case("repeat: no dedupe key", make_repeat(dedupeBy=None), "dedupeBy must be a non-empty list")
case("repeat: dedupe key not in contract", make_repeat(dedupeBy=["nope"]), "is not a field of")


def seen_without_repeat(ir):
    ir["nodes"][0]["prompt"] = "find things, skip these: {{seen}}"


case("repeat: {{seen}} without a repeat block", seen_without_repeat, "declares no 'repeat'")


def rounds_blow_budget(ir):
    make_repeat(maxRounds=4)(ir)
    ir["budget"]["maxNodes"] = 20  # fine for one round, not for four


case("repeat: rounds priced into the ceiling", rounds_blow_budget, "budget.maxNodes is 20")

# --- warnings: legal shapes that will predictably disappoint -------------
def warn_case(name, mutate, want_substr):
    """want_substr=None means the IR must produce NO warnings."""
    global passed, failed
    ir = copy.deepcopy(BASE)
    mutate(ir)
    ws = cg.warnings(ir)
    if want_substr is None:
        ok, detail = (not ws, "silent" if not ws else f"warned: {ws}")
    else:
        ok = any(want_substr in w for w in ws)
        detail = "warned" if ok else f"NOT warned (got {ws})"
    print(f"{'PASS' if ok else 'FAIL'}  warn: {name:<48} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


warn_case("healthy ceiling stays silent", make_repeat(untilDryRounds=2, maxRounds=6), None)
warn_case("ceiling too tight to ever go dry",
          make_repeat(untilDryRounds=2, maxRounds=3), "will end the sweep before the dry rule")
warn_case("ceiling equal to dry rule",
          make_repeat(untilDryRounds=2, maxRounds=2), "leaves only 0 round(s)")
warn_case("non-discovery graph stays silent", lambda ir: None, None)


def unverified_discovery(ir):
    make_repeat()(ir)
    ir["nodes"][0]["verify"] = "schema-only"
    ir["nodes"][0].pop("verifyOver", None)


warn_case("discovery with no verification tier", unverified_discovery, "no verification tier")

# The discovery loop's emitted shape carries its own guarantees.
disc = copy.deepcopy(BASE)
make_repeat()(disc)
disc["nodes"][0]["prompt"] = "hunt, already seen: {{seen}}"
disc["budget"]["maxNodes"] = 200
disc_js = cg.emit(disc, CONTRACTS)
for needle, why in [
    ("while (dry_n_find < 2 && round_n_find < 4)", "bounded by both dry rule and ceiling"),
    ("seen_n_find.add", "everything seen is remembered"),
    ("!seen_n_find.has(key_n_find(it))", "dedup happens before verification"),
    ("dry_n_find = 0", "a productive round resets the dry counter"),
    ("discovery INCOMPLETE, not exhausted", "hitting the ceiling is never called exhaustion"),
    ("INCOMPLETE = true", "a ceilinged sweep marks the whole campaign incomplete"),
    ("outcome === 'COMPLETE' && INCOMPLETE ? 'INCOMPLETE'", "a partial run cannot report COMPLETE"),
    ("seenList_n_find.join", "later rounds are told what earlier rounds found"),
]:
    ok = needle in disc_js
    print(f"{'PASS' if ok else 'FAIL'}  discovery: {why:<45} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

# --- run-time budget floor on work nodes, not just verification ----------
for needle, why in [
    ("const NODE_FLOOR", "work nodes have their own floor"),
    ("function affordable(", "affordability is checked, not assumed"),
    ("NOT RUN", "declined work is announced"),
    ("'SKIPPED'", "declined work is recorded as SKIPPED"),
]:
    ok = needle in disc_js
    print(f"{'PASS' if ok else 'FAIL'}  budget floor: {why:<43} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

single_floor = cg.emit(
    {**copy.deepcopy(BASE), "nodes": [
        {"id": "solo", "phase": "S", "prompt": "do it", "contract": "HarnessCheckV1"}]},
    CONTRACTS,
)
for needle, why in [
    ("affordable(\"node solo\")", "single nodes check the floor too"),
    ("BUDGET-EXHAUSTED", "a halt-on-red node stops rather than silently skipping"),
]:
    ok = needle in single_floor
    print(f"{'PASS' if ok else 'FAIL'}  budget floor: {why:<43} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

# A declared verification tier must actually run on a single (non-foreach)
# node too. It silently did not before v1.0.0 — the tier was emitted into the
# prelude and never called, so the claims went unverified while the spec said
# they were checked.
single = copy.deepcopy(BASE)
single["nodes"] = [
    {
        "id": "solo",
        "phase": "Solo",
        "prompt": "find things",
        "contract": "FindingsV1",
        "verify": "skeptic:1",
        "verifyOver": "findings",
        "expectItems": 2,
    }
]
single_js = cg.emit(single, CONTRACTS)
for needle, why in [
    ("verifyItems(", "single node calls the verifier"),
    ('"findings"', "verifier is pointed at the contract field"),
]:
    ok = needle in single_js
    print(f"{'PASS' if ok else 'FAIL'}  single-node tier: {why:<38} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

# Irreversible effects. `mutates` bought worktree isolation and nothing else,
# so the same marker covered editing a test file and minting a one-shot NFT.
# The failure that matters is not the first run — it is resume, which this
# plugin actively prescribes and which re-fires everything after the repair.
IRREV = copy.deepcopy(BASE)
IRREV["requiredArgs"] = ["confirm"]
IRREV["nodes"] = [
    {"id": "dry-run", "phase": "Rehearse", "prompt": "build the tx without submitting",
     "contract": "HarnessCheckV1"},
    {"id": "check", "phase": "Rehearse", "prompt": "independently re-derive: {{prev}}",
     "after": "dry-run", "contract": "HarnessCheckV1", "independent": True,
     "verifies": "dry-run", "haltWhen": "exit != 0"},
    {"id": "genesis", "phase": "Ceremony", "prompt": "submit the one-shot mint",
     "contract": "HarnessCheckV1", "irreversible": True},
]


def irrev_case(name, mutate, want):
    ir = copy.deepcopy(IRREV)
    mutate(ir)
    case(name, lambda _ir: _ir.update(ir), want)


case("irreversible graph accepted", lambda ir: ir.update(copy.deepcopy(IRREV)), None)
irrev_case("irreversible without a confirm arg",
           lambda ir: ir.pop("requiredArgs"), "requires 'confirm' in requiredArgs")
irrev_case("irreversible with no gate before it",
           lambda ir: ir["nodes"].__setitem__(1, {**ir["nodes"][1], "haltWhen": None}),
           "gate must be ordered before the effect")
irrev_case("irreversible with the gate ordered after it",
           lambda ir: ir["nodes"].reverse(), "gate must be ordered before the effect")
irrev_case("irreversible fan-out",
           lambda ir: ir["nodes"][2].update(foreach="dims",
                                            prompt="mint for {{item.brief}}"),
           "cannot be combined with foreach")
irrev_case("irreversible discovery loop",
           lambda ir: ir["nodes"][2].update(
               contract="FindingsV1", verify="skeptic:1", verifyOver="findings",
               repeat={"untilDryRounds": 2, "maxRounds": 4, "dedupeBy": ["file"]}),
           "cannot be combined with repeat")

irrev_js = cg.emit(copy.deepcopy(IRREV), CONTRACTS)
for needle, why in [
    ("which needs args._ledger",
     "an absent ledger fails loudly rather than disarming the guard"),
    ("in LEDGER", "the ledger is consulted before the effect"),
    ("REPLAYED-FROM-LEDGER", "a hit is reported, not silently skipped"),
    ("if (!confirmed(\"genesis\"))", "the node refuses unless a human named it"),
    ("CONFIRM-REQUIRED", "refusal is an outcome, not a warning"),
    ("LEDGER_WRITES.push(", "a performed effect is recorded for the next run"),
    ("ledger: LEDGER_WRITES", "the records ride out in the summary"),
]:
    ok = needle in irrev_js
    print(f"{'PASS' if ok else 'FAIL'}  irreversible: {why:<42} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

# The guard must not leak into graphs that declare no irreversible node.
plain_js = cg.emit(copy.deepcopy(BASE), CONTRACTS)
ok = "LEDGER" not in plain_js
print(f"{'PASS' if ok else 'FAIL'}  irreversible: {'no ledger code without the field':<42} "
      f"-> {'clean' if ok else 'LEAKED'}")
passed, failed = (passed + ok, failed + (not ok))

# --- reduce: deterministic code between agents ----------------------------
# Models for ambiguity, code for plumbing: dedupe/rank/cut must not cost a
# spawn, and a reduce that silently dropped work would read as coverage.
REDUCE = {"id": "cut", "phase": "Reduce",
          "reduce": {"from": "find", "dedupeBy": ["file", "line"],
                     "sortBy": "severity", "order": "desc", "topK": 5}}


def with_reduce(**over):
    def m(ir):
        node = copy.deepcopy(REDUCE)
        node["reduce"].update(over)
        for k, v in list(node["reduce"].items()):
            if v is None:
                del node["reduce"][k]
        ir["nodes"].append(node)
    return m


case("reduce: sound reduce accepted", with_reduce(), None)
case("reduce: from must exist", with_reduce(**{"from": "ghost"}),
     "must name a node defined earlier")
case("reduce: over on an item-array source", with_reduce(over="findings"),
     "reduce.over is not allowed")
case("reduce: no operation declared",
     with_reduce(dedupeBy=None, sortBy=None, order=None, topK=None),
     "declares no operation")
case("reduce: dedupe key outside the item schema",
     with_reduce(dedupeBy=["nope"]), "not a field of the items")
case("reduce: order without sortBy",
     with_reduce(sortBy=None, topK=None), "orders nothing")
case("reduce: topK without a ranking",
     with_reduce(sortBy=None, order=None), "name the ranking")
case("reduce: unknown op is an error",
     with_reduce(groupBy="file"), "unknown field")


def reduce_with_prompt(ir):
    with_reduce()(ir)
    ir["nodes"][-1]["prompt"] = "also do this"


case("reduce: cannot also be an agent", reduce_with_prompt,
     "cannot be combined with")


def reduce_over_object(ir):
    ir["nodes"].append({"id": "scan", "phase": "B", "prompt": "scan the repo",
                        "contract": "FindingsV1"})
    ir["nodes"].append({"id": "shortlist", "phase": "R",
                        "reduce": {"from": "scan", "over": "findings",
                                   "dedupeBy": ["file"]}})


case("reduce: object source requires over (accepted with it)",
     reduce_over_object, None)


def reduce_object_no_over(ir):
    reduce_over_object(ir)
    del ir["nodes"][-1]["reduce"]["over"]


case("reduce: object source without over", reduce_object_no_over,
     "reduce.over required")


# Keys over items with no declared fields would read String(undefined) at
# run time: dedupe would collapse N distinct items to one and file the
# destruction as deduplication. Rejected outright, never waved through.
def reduce_schemaless(**ops):
    def m(ir):
        ir["nodes"].append({"id": "slice", "phase": "B", "prompt": "build",
                            "contract": "SliceV1"})
        ir["nodes"].append({"id": "tests", "phase": "R",
                            "reduce": {"from": "slice", "over": "testsAdded",
                                       **ops}})
    return m


case("reduce: dedupe key over schema-less items",
     reduce_schemaless(dedupeBy=["x"]), "declare no fields")
case("reduce: sort key over schema-less items",
     reduce_schemaless(sortBy="x", topK=1), "declare no fields")


def parked_on_red(ir):
    ir["nodes"].append({
        "id": "sign", "phase": "P", "prompt": "sign it", "actor": "human",
        "contract": "HarnessCheckV1", "onRed": "halt",
        "release": {"instructions": "sign with the hardware key",
                    "whyNotAgent": "key material an agent must not hold",
                    "proofContract": "HarnessCheckV1"}})


case("onRed on a parked node is inert, so it is rejected", parked_on_red,
     "cannot be combined with 'onRed'")

red_ir = copy.deepcopy(BASE)
with_reduce()(red_ir)
red_ir["nodes"].append({"id": "synth", "phase": "S", "after": "cut",
                        "prompt": "synthesize {{prev}}", "contract": "DesignV1"})
ok = cg.plan_node_count(red_ir) == cg.plan_node_count(BASE) + 1  # synth only
print(f"{'PASS' if ok else 'FAIL'}  reduce: {'costs zero planned agent calls':<47} -> "
      f"{'yes' if ok else cg.plan_node_count(red_ir)}")
passed, failed = (passed + ok, failed + (not ok))

red_js = cg.emit(red_ir, CONTRACTS)
for needle, why in [
    ("const seen = new Set()", "dedupe is a Set, not a model"),
    ("no silent caps", "a topK cut names how many it dropped"),
    ("before: before_n_cut, after: n_cut.length", "compression is recorded as structured data"),
    ('"cut": "FindingsV1"', "reduce node carries its source's contract"),
    ('reducers: ["cut"]', "reducers are named in the summary"),
]:
    ok = needle in red_js
    print(f"{'PASS' if ok else 'FAIL'}  reduce: {why:<47} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

# Emission smoke: the sound graph produces JS containing its guarantees.
js = cg.emit(BASE, CONTRACTS)
for needle, why in [
    ("typeof args === 'object'", "args normalizer present"),
    ("VERIFY_FLOOR", "budget floor present"),
    ("Attempt to REFUTE", "refuters attack, never confirm"),
    ("Default to refuted=true when uncertain", "uncertainty kills, never carries"),
    ("v.kills < (codeReview ? n : need)", "discovery uses majority; code refutation requires unanimity"),
    ('"Find", 3, 2)', "panel:3 passes need=2, a true majority"),
    ("PROVENANCE", "provenance recorded"),
    ("const MAX_NODES", "the ceiling exists at run time, not only at compile time"),
    ("async function spawn(", "agents are spawned through one counted helper"),
    # A consumer that has to infer a result's type from its shape will
    # eventually file a harness exit code as a red-team verdict. The
    # compiler knows the declared contract; it says so.
    ("const CONTRACTS = {", "nodeId -> contract is emitted, not left to be sniffed"),
    ("contracts: CONTRACTS", "the contract map rides out in the summary"),
]:
    ok = needle in js
    print(f"{'PASS' if ok else 'FAIL'}  emitted JS: {why:<42} -> {'found' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

# Every agent call in generated code must go through spawn(), or the runtime
# ceiling counts less than actually ran.
import re as _re
for name, src in [("fan-out+panel", js), ("discovery", disc_js), ("single", single_js)]:
    bypass = [l for l in src.splitlines()
              if _re.search(r'(?<![a-zA-Z])agent\(', l) and 'return agent(prompt, opts)' not in l
              # Comments are not calls.
              and not l.strip().startswith('//')
              # The tree sentinel is a guard rail, not a node: it must not
              # spend the node budget and must still run after the ceiling is
              # reached, so its direct agent() call is exempt BY DESIGN — and
              # asserted separately below, so the exemption cannot widen into
              # a loophole.
              and 'tree-check' not in l]
    ok = not bypass
    print(f"{'PASS' if ok else 'FAIL'}  ceiling: {name+' routes every agent via spawn':<44} -> "
          f"{'yes' if ok else bypass[0].strip()[:50]}")
    passed, failed = (passed + ok, failed + (not ok))


# ---------------------------------------------------------------------------
# Tree integrity: the tree a node acts on is declared, injected, and checked.
# Three filed failures, one theme — a worktree cut from the wrong base read as
# a true report (#16), a fan-out measuring a shared tree corrupted its own
# numbers silently (#20), and a node's leftover dirt let a green gate describe
# a tree that no longer existed (#21).
# ---------------------------------------------------------------------------
case("isolation is a closed registry",
     lambda ir: ir["nodes"][0].update(isolation="worktreee"),
     "isolation must be one of")
case("isolation: 'measure' is accepted",
     lambda ir: ir["nodes"][0].update(isolation="measure"), None)


def measure_mutator(ir):
    add_verified_mutator(ir)
    ir["nodes"][1]["isolation"] = "measure"


case("a mutator cannot be a throwaway measurement",
     measure_mutator, "cannot combine with isolation:'measure'")
case("treeGuard must be boolean",
     lambda ir: ir.update(treeGuard="yes"), "treeGuard must be a boolean")

tree_ir = copy.deepcopy(BASE)
tree_ir["nodes"][0]["isolation"] = "measure"
add_verified_mutator(tree_ir)
tree_js = cg.emit(tree_ir, CONTRACTS)
off_js = cg.emit({**copy.deepcopy(tree_ir), "treeGuard": False}, CONTRACTS)
plain_js = cg.emit(BASE, CONTRACTS)
for name, ok in [
    # #20: a measure node makes its own frozen snapshot instead of taking the
    # runtime's worktree — only the mutator gets `isolation: 'worktree'`.
    ("measure node self-snapshots, no runtime worktree",
     tree_js.count("isolation: 'worktree'") == 1),
    ("the snapshot command reaches the prompt, executable not prose",
     "git archive HEAD" in tree_js and "measurePreamble() +" in tree_js),
    # #16: the campaign base is loaded at launch, refused if absent, and the
    # worktree node's first act is asserting it.
    ("isolated nodes refuse to start without args._base",
     "A._base" in tree_js and "no base was passed" in tree_js),
    ("the worktree preamble asserts the base, with the sha supplied",
     "basePreamble() +" in tree_js and "merge-base --is-ancestor" in tree_js
     and "${BASE.sha}" in tree_js),
    # It may READ one — the tree guard compares a launch reading when
    # there is one (#94) — but never refuses to start without it.
    ("a graph with no isolated nodes demands no base",
     'A["_base"] !== undefined' in plain_js),
    # #21: the sentinel brackets the campaign and guards every verdict-minting
    # node; drift halts rather than advancing.
    ("sentinel at start, before the verdict node, and at end",
     "treeCheck('campaign-start'" in tree_js
     and "treeCheck(\"before gate\"" in tree_js
     and "treeCheck('campaign-end'" in tree_js),
    ("tree drift is a halt, not a log line",
     "TREE.unknown ? 'TREE-UNKNOWN' : 'TREE-MOVED'" in tree_js),
    ("the sentinel record rides out in the summary",
     "tree: TREE" in tree_js),
    ("the sentinel is schema-forced",
     "TreeCheckV1" in tree_js),
    ("the sentinel bypasses the node budget by design",
     "tree-check:${point}" in tree_js),
    ("treeGuard: false removes the sentinel, on the record",
     "treeCheck" not in off_js and "TreeCheckV1" not in off_js),
]:
    print(f"{'PASS' if ok else 'FAIL'}  tree: {name:<50} -> {'yes' if ok else 'MISSING'}")
    passed, failed = (passed + ok, failed + (not ok))

warn_case("fan-out that measures a shared tree",
          lambda ir: ir["nodes"][0].update(
              prompt="measure the byte size of {{item.brief}}"),
          "share ONE working tree")
warn_case("measuring fan-out with isolation stays silent",
          lambda ir: ir["nodes"][0].update(
              prompt="measure the byte size of {{item.brief}}",
              isolation="measure"),
          None)

# ---------------------------------------------------------------------------
# Emission is BYTES, not just content. An open(path, "w") without encoding and
# newline writes the platform default: on Windows that is cp1252 with CRLF, and
# both halves are invisible to a content assertion because the script still says
# all the right things. The file was simply unusable — a non-UTF-8 script cannot
# be read as text downstream, and the Workflow permission handler rejects the
# carriage returns as "control characters that would be hidden in the approval
# dialog", which is correct of it. graph-run was blocked end-to-end on the
# platform. CI is ubuntu-only, where both halves pass by default; that is
# precisely why this shipped, so the check has to be on bytes.
# ---------------------------------------------------------------------------

def emission_bytes_case():
    global passed, failed
    import tempfile

    ir = copy.deepcopy(BASE)
    # Non-ASCII in the IR is the only thing that exercises the encoding half.
    ir["campaign"] = "en dash – em dash — accented café, all must survive"
    ir["nodes"][0]["prompt"] = "review {{item.brief}} — report precisely"

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "WORK.md")
        out = os.path.join(d, "campaign.graph.js")
        with open(src, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# t\n\n```json graph-ir\n" + json.dumps(ir) + "\n```\n")

        import subprocess
        r = subprocess.run(
            [sys.executable,
             os.path.join(PLUGIN, "scripts", "compile-graph.py"), src, "-o", out],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("FAIL emission-bytes: compiler exited", r.returncode, r.stderr[-300:])
            failed += 1
            return

        with open(out, "rb") as fh:
            raw = fh.read()

        # The two emit paths are separately fallible, so a test on one proves
        # nothing about the other. `--out` opens with newline="\n"; stdout is
        # a TextIOWrapper that translated every \n to os.linesep, so the same
        # compiler shipped LF through one path and CRLF through the other, and
        # the Workflow tool refused the result naming neither CRLF nor the
        # compiler. Run it again with no -o and compare the bytes.
        #
        # PYTHONIOENCODING is forced to a CRLF-hostile setting here on purpose:
        # it pins the ENCODING and says nothing about line endings, which is
        # precisely why fixing the encoding half left this half broken.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r2 = subprocess.run(
            [sys.executable,
             os.path.join(PLUGIN, "scripts", "compile-graph.py"), src],
            capture_output=True, env=env)

        problems = []
        if r2.returncode != 0:
            problems.append("stdout emit exited %d" % r2.returncode)
        elif r2.stdout != raw:
            problems.append(
                "the stdout and --out paths disagree: %d vs %d bytes, %d vs %d CR"
                % (len(r2.stdout), len(raw),
                   r2.stdout.count(b"\r"), raw.count(b"\r")))
        crs = raw.count(b"\r") + r2.stdout.count(b"\r")
        if crs:
            problems.append(
                "emitted script contains %d CR byte(s) across the two emit paths; "
                "it must be LF-only or the Workflow permission handler rejects it "
                "as control characters" % crs)
        text = ""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append("emitted script is not valid UTF-8: %s" % e)
        if text and "—" not in text:
            problems.append("non-ASCII from the IR did not survive emission")

        if problems:
            for m in problems:
                print("FAIL emission-bytes:", m)
            failed += 1
        else:
            print("ok   emission-bytes: utf-8, LF-only on BOTH emit paths, "
                  "non-ASCII preserved")
            passed += 1


emission_bytes_case()


def tree_sentinel_executed_case():
    """Executed, not grepped: a sentinel that observes drift HALTS the run.

    The emitted `if (!await treeCheck(...)) return summary('TREE-MOVED')` is
    exactly the kind of line a substring assertion proves present and never
    proves live. Run the compiled graph under stubs twice — a steady tree and
    a drifting one — and read what actually came back: the verdict node must
    never have spawned after the drift, because a gate that runs anyway is
    the whole bug (#21).
    """
    global passed, failed
    import subprocess
    import tempfile

    ir = copy.deepcopy(BASE)
    add_verified_mutator(ir)
    js = cg.emit(ir, CONTRACTS)

    def run(drift):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            w = os.path.join(d, "w.mjs")
            with open(w, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join([
                    "import {writeFileSync} from 'node:fs';",
                    "const PROMPTS=[]; let checks=0;",
                    "const agent=async(p,o)=>{PROMPTS.push(String(p));",
                    "  if(String(o.label||'').includes('tree-check'))",
                    f"    return {{head:'abc123', porcelain: (++checks > 1 && {json.dumps(drift)}) ? ' M src/app.py' : ''}};",
                    "  if(String(o.label||'').includes('refute'))",
                    "    return {refuted:false, reason:'no'};",
                    "  if(String(o.label||'').includes('gate'))",
                    "    return {exit:0, command:'scripts/harness.sh --full', tail:''};",
                    "  if(String(o.label||'').includes('build'))",
                    "    return {summary:'done', plan:'p', files:[], risks:[]};",
                    "  return {findings:[]}};",
                    "const parallel=async(t)=>Promise.all(t.map(f=>f()));",
                    "const pipeline=async()=>[],log=()=>{},phase=()=>{};",
                    "const args={_base:{sha:'abc123',branch:'main',porcelain:''}};",
                    "const budget={total:null,spent:()=>0,remaining:()=>1e9};",
                    "const workflow=0;",
                    "(async () => {",
                    js.replace("export const meta", "const meta"),
                    "})().then(r=>writeFileSync(" + json.dumps(out)
                    + ",JSON.stringify({r, gateRan: PROMPTS.some(p=>p.includes('re-run the harness'))})))",
                    ".catch(e=>writeFileSync(" + json.dumps(out)
                    + ",JSON.stringify({err:String(e&&e.message||e)})))",
                ]))
            subprocess.run(["node", w], capture_output=True, timeout=90)
            return json.load(open(out)) if os.path.exists(out) else {"err": "no output"}

    steady = run(drift=False)
    moved = run(drift=True)
    for name, ok in [
        ("steady tree: campaign completes with the checks on record",
         (steady.get("r") or {}).get("outcome") in ("COMPLETE", "INCOMPLETE")
         and len(((steady.get("r") or {}).get("tree") or {}).get("checks", [])) == 3),
        ("drifting tree: the run halts as TREE-MOVED",
         (moved.get("r") or {}).get("outcome") == "TREE-MOVED"),
        ("and the verdict node never spawned after the drift",
         moved.get("gateRan") is False),
    ]:
        detail = "yes" if ok else f"got steady={steady} moved={moved}"
        print(f"{'PASS' if ok else 'FAIL'}  tree-executed: {name:<48} -> {detail[:90]}")
        passed, failed = (passed + ok, failed + (not ok))


tree_sentinel_executed_case()


def tree_own_writes_and_launch_case():
    """The guard ignores the plugin's own writes and re-reads at launch (#94).

    The Stop hook appends an Evidence row to the state file whenever the
    orchestrator ends a turn, which a live graph's sentinel read as an
    undeclared mutation: TREE-MOVED, and every verdict discarded. And the
    sentinel is a memoized agent() call, so a resume replayed its first
    reading instead of taking one — the launcher's own fresh reading in
    args._base is what a resume is now held to.
    """
    global passed, failed
    import subprocess
    import tempfile

    ir = copy.deepcopy(BASE)
    add_verified_mutator(ir)
    js = cg.emit(ir, CONTRACTS, graph_file="GRAPH.rollout.md")

    plain_js = cg.emit(copy.deepcopy(BASE), CONTRACTS)

    def run(readings, base, js=js):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            w = os.path.join(d, "w.mjs")
            with open(w, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join([
                    "import {writeFileSync} from 'node:fs';",
                    "const PROMPTS=[], LOGS=[]; let checks=0;",
                    f"const READINGS={json.dumps(readings)};",
                    "const agent=async(p,o)=>{PROMPTS.push(String(p));",
                    "  if(String(o.label||'').includes('tree-check')) {",
                    "    const r = READINGS[Math.min(checks, READINGS.length - 1)]; checks++;",
                    "    return {head: r[0], porcelain: r[1], ...(r[2] ? {root:r[2]} : {})} }",
                    "  if(String(o.label||'').includes('refute'))",
                    "    return {refuted:false, reason:'no'};",
                    "  if(String(o.label||'').includes('gate'))",
                    "    return {exit:0, command:'scripts/harness.sh --full', tail:''};",
                    "  if(String(o.label||'').includes('build'))",
                    "    return {summary:'done', plan:'p', files:[], risks:[]};",
                    "  return {findings:[]}};",
                    "const parallel=async(t)=>Promise.all(t.map(f=>f()));",
                    "const pipeline=async()=>[],phase=()=>{};",
                    "const log=(m)=>LOGS.push(String(m));",
                    f"const args={json.dumps({'_base': base} if base else {})};",
                    "const budget={total:null,spent:()=>0,remaining:()=>1e9};",
                    "const workflow=0;",
                    "(async () => {",
                    js.replace("export const meta", "const meta"),
                    "})().then(r=>writeFileSync(" + json.dumps(out)
                    + ",JSON.stringify({r, LOGS, gateRan: PROMPTS.some(p=>p.includes('re-run the harness'))})))",
                    ".catch(e=>writeFileSync(" + json.dumps(out)
                    + ",JSON.stringify({err:String(e&&e.message||e)})))",
                ]))
            subprocess.run(["node", w], capture_output=True, timeout=90)
            return json.load(open(out)) if os.path.exists(out) else {"err": "no output"}

    base = {"sha": "abc123", "branch": "main", "porcelain": ""}
    hook_row = run([["abc123", ""], ["abc123", " M WORK.md"]], base)
    own = run([["abc123", ""], ["abc123", "?? .claude/\n M GRAPH.rollout.md"]], base)
    code = run([["abc123", ""], ["abc123", " M WORK.md\n M src/app.py"]], base)
    moved_head = run([["abc123", ""]], {"sha": "fff999", "branch": "main", "porcelain": ""})
    moved_dirt = run([["abc123", ""]], {"sha": "abc123", "porcelain": " M src/app.py"})
    resumed_dirty_state = run([["abc123", ""]], {"sha": "abc123", "porcelain": " M WORK.md"})
    no_launch = run([["abc123", ""]], None, js=plain_js)
    wrong_root = run([["abc123", "", "/other-checkout"]], dict(base, root="/campaign"))
    wrong_root_and_head = run([["other123", "", "/other-checkout"]], dict(base, root="/campaign"))
    normalized_root = run([["abc123", "", "C:/WORK/Campaign/"]], dict(base, root="c:\\work\\campaign"))
    no_root = run([["abc123", "", "/other-checkout"]], base)
    old_shape = run([["abc123", ""]], {"sha": "abc123", "branch": "main"})

    def outcome(x):
        return (x.get("r") or {}).get("outcome")

    # A project below a monorepo's top level: porcelain names paths from the
    # repository root, so the ignore list carries the project's prefix. And
    # git octal-escapes a non-ASCII graph name, which must still match.
    mono_js = cg.emit(ir, CONTRACTS, graph_file="GRAPH-\u00e9.md",
                      project_prefix="services/api/")
    mono_hook = run([["abc123", ""], ["abc123", " M services/api/WORK.md\n"
                                                 "?? services/api/.claude/"]], base, js=mono_js)
    mono_launch = run([["abc123", " M services/api/WORK.md"]], base, js=mono_js)
    mono_quoted = run([["abc123", ' M "services/api/GRAPH-\\303\\251.md"']], base, js=mono_js)
    mono_code = run([["abc123", ""], ["abc123", " M services/api/src/app.py"]], base, js=mono_js)
    mono_other = run([["abc123", ""], ["abc123", " M WORK.md"]], base, js=mono_js)
    # Only the plugin's own directories under .claude/ are its writes: a
    # tracked settings file or project agent edited mid-run is a mutation.
    settings = run([["abc123", ""], ["abc123", " M .claude/settings.json"]], base)
    agent_def = run([["abc123", ""], ["abc123", " M .claude/agents/red-team-reviewer.md"]], base)
    # git quotes a path with a tab and, under core.quotePath=false, leaves an
    # astral character raw: decoding it must not throw out of the guard.
    astral = run([["abc123", ""], ["abc123", '?? "x\\t\U0001F600.txt"']], base)
    plugin_dirs = run([["abc123", ""], ["abc123", "?? .claude/fluxpoint/\n"
                                                   " M .claude/workflows/g.js\n"
                                                   "?? .claude/worktrees/"]], base)

    for name, ok, got in [
        ("different checkouts with identical HEAD/dirt are WRONG-ROOT before any node",
         outcome(wrong_root) == "WRONG-ROOT" and wrong_root.get("gateRan") is False
         and any("/cd" in m and "/campaign" in m and "/other-checkout" in m for m in wrong_root.get("LOGS", [])), wrong_root),
        ("checkout identity takes precedence over different HEADs",
         outcome(wrong_root_and_head) == "WRONG-ROOT", wrong_root_and_head),
        ("Windows root separators, trailing slash and drive-path case normalize",
         outcome(normalized_root) in ("COMPLETE", "INCOMPLETE"), normalized_root),
        ("an omitted root retains existing compatibility",
         outcome(no_root) in ("COMPLETE", "INCOMPLETE"), no_root),
        ("an old _base without porcelain refuses before sentinel or nodes",
         "git status --porcelain" in old_shape.get("err", ""), old_shape),
        ("the Stop hook's Evidence row is not TREE-MOVED",
         outcome(hook_row) in ("COMPLETE", "INCOMPLETE"), hook_row),
        ("  nor in a project below a monorepo's top level",
         outcome(mono_hook) in ("COMPLETE", "INCOMPLETE"), mono_hook),
        ("  whose launch-time STATUS edit does not halt a fresh launch",
         outcome(mono_launch) in ("COMPLETE", "INCOMPLETE"), mono_launch),
        ("  nor does a graph file git prints octal-escaped",
         outcome(mono_quoted) in ("COMPLETE", "INCOMPLETE"), mono_quoted),
        ("  while its code dirt still halts",
         outcome(mono_code) == "TREE-MOVED", mono_code),
        ("  and so does a WORK.md that is not the project's",
         outcome(mono_other) == "TREE-MOVED", mono_other),
        ("a tracked .claude/settings.json edited mid-run halts",
         outcome(settings) == "TREE-MOVED", settings),
        ("  and so does a project agent definition",
         outcome(agent_def) == "TREE-MOVED", agent_def),
        ("  but the plugin's own .claude/ directories do not",
         outcome(plugin_dirs) in ("COMPLETE", "INCOMPLETE"), plugin_dirs),
        ("a quoted path with an astral character is read, not thrown on",
         outcome(astral) == "TREE-MOVED" and "err" not in astral, astral),
        ("nor are .claude/ state and the compiled graph file",
         outcome(own) in ("COMPLETE", "INCOMPLETE"), own),
        ("real code dirt beside them still halts the run",
         outcome(code) == "TREE-MOVED", code),
        ("a launch on a different HEAD than the run's first reading halts",
         outcome(moved_head) == "TREE-MOVED" and moved_head.get("gateRan") is False, moved_head),
        ("  and so does launch-time code dirt the first reading lacked",
         outcome(moved_dirt) == "TREE-MOVED", moved_dirt),
        ("  but a launch whose only difference is the state file proceeds",
         outcome(resumed_dirty_state) in ("COMPLETE", "INCOMPLETE"), resumed_dirty_state),
        ("with no launch reading the run proceeds and says what it cannot check",
         outcome(no_launch) in ("COMPLETE", "INCOMPLETE")
         and any("no launch reading" in m for m in no_launch.get("LOGS", [])), no_launch),
    ]:
        detail = "yes" if ok else f"got {str(got)[:160]}"
        print(f"{'PASS' if ok else 'FAIL'}  tree-own-writes: {name:<52} -> {detail[:120]}")
        passed, failed = (passed + ok, failed + (not ok))


tree_own_writes_and_launch_case()


def tree_prefix_from_main_case():
    """main() derives the project prefix from git, not from the caller."""
    global passed, failed
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        repo = os.path.join(d, "mono")
        proj = os.path.join(repo, "services", "api")
        os.makedirs(proj)
        subprocess.run(["git", "init", "-q", repo], check=True)
        work = os.path.join(proj, "WORK.md")
        with open(work, "w", encoding="utf-8") as fh:
            fh.write("# w\n\n```json graph-ir\n" + json.dumps(BASE) + "\n```\n")
        out = os.path.join(d, "g.js")
        r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "compile-graph.py"),
                            work, "--gates-root", proj, "-o", out],
                           capture_output=True, text=True)
        js = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        line = next((x for x in js.splitlines() if x.startswith("const TREE_IGNORE")), "")
        ignore = json.loads(line.split("=", 1)[1]) if line else {}
        outside = cg.git_prefix(d)
    for name, ok, got in [
        ("the compiled ignore list carries the project's prefix",
         "services/api/WORK.md" in ignore.get("files", [])
         and "services/api/.claude/fluxpoint/" in ignore.get("prefixes", []),
         line or r.stderr),
        ("  and a directory outside any repository has none",
         outside == "", outside),
    ]:
        detail = "yes" if ok else f"got {str(got)[:160]}"
        print(f"{'PASS' if ok else 'FAIL'}  tree-prefix: {name:<55} -> {detail[:120]}")
        passed, failed = (passed + ok, failed + (not ok))


tree_prefix_from_main_case()


def duplicate_contract_id_case():
    """Two files claiming one $id: which binds must not depend on the loader."""
    global passed, failed
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for fn, req in (("A-old.schema.json", ["txHash"]), ("SigV1.schema.json", ["txHash", "signer"])):
            with open(os.path.join(d, fn), "w", encoding="utf-8") as fh:
                json.dump({"$id": "SigV1", "type": "object", "required": req}, fh)
        try:
            cg.load_contracts(d)
            got = "loaded"
        except cg.GraphError as e:
            got = str(e)
    ok = "both declare $id SigV1" in got
    print(f"{'PASS' if ok else 'FAIL'}  contracts: two files claiming one $id are refused      -> {got[:100]}")
    passed, failed = (passed + ok, failed + (not ok))


duplicate_contract_id_case()


def contracts_header_case():
    """A graph file's CONTRACTS: header is honored (issue #95).

    Contracts came only from --contracts, which defaults to the plugin's
    own directory, so a repo-local set declared in the header produced a
    page of false findings that read as defects in the IR — and nothing
    said which directory had been used.
    """
    global passed, failed
    import subprocess
    import tempfile
    stricter = copy.deepcopy(CONTRACTS["SliceV1"])
    stricter["properties"]["outcome"] = {"enum": ["shipped", "parked"]}
    local = {"$id": "RuntimeEvidenceV1", "type": "object", "required": ["observed"],
             "properties": {"observed": {"type": "string"}}}
    ir = {"version": 1, "campaign": "c", "treeGuard": False, "budget": {"maxNodes": 4},
          "nodes": [{"id": "slice", "prompt": "do it", "contract": "SliceV1",
                     "haltWhen": "outcome == 'parked'"},
                    {"id": "watch", "after": "slice", "prompt": "observe {{prev}}",
                     "contract": "RuntimeEvidenceV1"}]}

    def run(d, header, *extra):
        with open(os.path.join(d, "GRAPH.x.md"), "w", encoding="utf-8") as fh:
            fh.write(header + "\n```json graph-ir\n" + json.dumps(ir) + "\n```\n")
        return subprocess.run(
            [sys.executable, os.path.join(PLUGIN, "scripts", "compile-graph.py"),
             "GRAPH.x.md", "--check", *extra], cwd=d, capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".fluxpoint-contracts"))
        for c in (stricter, local):
            with open(os.path.join(d, ".fluxpoint-contracts", f"{c['$id']}.schema.json"),
                      "w", encoding="utf-8") as fh:
                json.dump(c, fh)
        plain = run(d, "STATUS: DESIGN\n")
        declared = run(d, "STATUS: DESIGN\nCONTRACTS: .fluxpoint-contracts\n")
        missing = run(d, "CONTRACTS: .nowhere\n")
        flag = run(d, "CONTRACTS: .fluxpoint-contracts\n", "--contracts",
                   os.path.join(PLUGIN, "contracts"))
    for name, ok, detail in [
        ("without the header, repo-local contracts are unknown",
         plain.returncode == 1 and "RuntimeEvidenceV1" in plain.stderr, plain.stderr[-80:]),
        ("CONTRACTS: overlays the repo's set on the shipped one",
         declared.returncode == 0, declared.stderr[-120:]),
        ("  and --check names the directory it used",
         ".fluxpoint-contracts" in declared.stdout and "contracts" in declared.stdout,
         declared.stdout.strip()[-100:]),
        ("a CONTRACTS: directory that does not exist is an error, not a fallback",
         missing.returncode == 1 and ".nowhere" in missing.stderr, missing.stderr[-80:]),
        ("an explicit --contracts still wins over the header",
         flag.returncode == 1 and "RuntimeEvidenceV1" in flag.stderr, flag.stderr[-80:]),
    ]:
        print(f"{'PASS' if ok else 'FAIL'}  contracts-header: {name:<52} -> {detail[:90]}")
        passed, failed = (passed + ok, failed + (not ok))


contracts_header_case()


def root_arg_literal(js):
    """The emitted ROOT_ARG, fed a hostile root, reaches bash as that root."""
    import subprocess
    import tempfile
    line = next((x for x in js.splitlines() if x.startswith("const ROOT_ARG = ")), "")
    if not line:
        return False
    hostile = "/srv/build$dir/pr'oj`touch pwned`"
    with tempfile.TemporaryDirectory() as d:
        probe = ("const LAUNCH = {root: " + json.dumps(hostile) + "};\n" + line +
                 "\nprocess.stdout.write(ROOT_ARG)\n")
        arg = subprocess.run(["node", "-e", probe], capture_output=True, text=True).stdout
        out = subprocess.run(["bash", "-c", "set -- " + arg + "; printf '%s|%s' \"$1\" \"$2\""],
                             capture_output=True, text=True, cwd=d).stdout
        return out == "--root|" + hostile and not os.path.exists(os.path.join(d, "pwned"))


def prove_ci_and_launch_case():
    """CI's statuses guard an irreversible node; citations carry the launch.

    The two strongest attestations a merge has — CI's own commit statuses
    and a gate longer than one tool call — could not guard an irreversible
    node, so the terminal merge had to be parked on a person (#96). And a
    cited attestation was never bound to the run that cited it (#91).
    """
    global passed, failed
    import subprocess
    import tempfile

    ir = {"version": 1, "campaign": "ship it", "treeGuard": False,
          "budget": {"maxNodes": 6}, "requiredArgs": ["confirm"],
          "nodes": [{"id": "push", "prompt": "push the tip", "contract": "HarnessCheckV1"},
                    {"id": "ci-gate", "prompt": "cite CI's verdict on the tip",
                     "contract": "ExecutionV1", "verify": "prove:ci",
                     "independent": True, "verifies": "push", "haltWhen": "exit != 0"},
                    {"id": "merge", "prompt": "merge the tip",
                     "contract": "HarnessCheckV1", "irreversible": True}]}
    with_ci = cg.validate(ir, CONTRACTS, gates={"harness", "ci"})
    without = cg.validate(ir, CONTRACTS, gates={"harness"})
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, ".fluxpoint-gates.json"), "w") as fh:
            json.dump({"version": 1, "gates": {"harness": "scripts/harness.sh"},
                       "ci": {"forge": "github", "repository": "fixture/repo", "contexts": ["harness"], "witness": {"statusCreators": ["ci-bot"], "checkRunApps": []}}}, fh)
        loaded = cg.load_gates(d)
        # The witness's own rules, at compile time: a manifest attest.py
        # refuses mints no rows, so a prove: node over it cannot be proved.
        with open(os.path.join(d, ".fluxpoint-gates.json"), "w") as fh:
            json.dump({"version": 1, "gates": {"harness": "scripts/harness.sh", "ci": "make ci"}}, fh)
        refused = cg.validate(ir, CONTRACTS, gates=cg.load_gates(d))
        with open(os.path.join(d, ".fluxpoint-gates.json"), "w") as fh:
            json.dump({"version": 1, "gates": {}, "ci": {"forge": "github", "repository": "fixture/repo", "witness": {"statusCreators": ["ci-bot"], "checkRunApps": []}}}, fh)
        ci_only = cg.load_gates(d)
    js = cg.emit(ir, CONTRACTS, {})

    def run(args):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            w = os.path.join(d, "w.mjs")
            with open(w, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join([
                    "import {writeFileSync} from 'node:fs';",
                    "const agent=async(p,o)=>{",
                    "  if(String(o.label||'').includes('ci-gate')) return {gate:'ci', exit:0, attestId:'att_x'};",
                    "  return {exit:0, command:'git', tail:''}};",
                    "const parallel=async(t)=>Promise.all(t.map(f=>f()));",
                    "const pipeline=async()=>[],log=()=>{},phase=()=>{};",
                    f"const args={json.dumps(args)};",
                    "const budget={total:null,spent:()=>0,remaining:()=>1e9};",
                    "const workflow=0;",
                    "(async () => {",
                    js.replace("export const meta", "const meta"),
                    "})().then(r=>writeFileSync(" + json.dumps(out) + ",JSON.stringify({r})))",
                    ".catch(e=>writeFileSync(" + json.dumps(out)
                    + ",JSON.stringify({err:String(e&&e.message||e)})))",
                ]))
            subprocess.run(["node", w], capture_output=True, timeout=90)
            return json.load(open(out)) if os.path.exists(out) else {"err": "no output"}

    stamped = run({"confirm": "merge", "_ledger": {},
                   "_launch": {"since": "2026-09-23T00:00:00Z", "nonce": "a1b2c3"}})
    unstamped = run({"confirm": "merge", "_ledger": {}})
    no_nonce = run({"confirm": "merge", "_ledger": {},
                    "_launch": {"since": "2026-09-23T00:00:00Z"}})
    bad_nonce = run({"confirm": "merge", "_ledger": {},
                     "_launch": {"since": "2026-09-23T00:00:00Z", "nonce": "x; rm -rf ."}})
    for name, ok, got in [
        ("prove:ci resolves when the manifest declares a ci section",
         not with_ci, with_ci),
        ("prove:ci without a ci section is rejected, and says what to add",
         any("prove:ci" in f and "'ci' section" in f for f in without), without),
        ("load_gates lists ci beside the declared gates",
         loaded == {"harness", "ci"}, loaded),
        ("a manifest the witness refuses fails a prove: node at compile time",
         any("which the witness refuses" in f for f in refused), refused),
        ("  while a CI-only manifest is a manifest",
         ci_only == {"ci"} and not ci_only.findings, (ci_only, ci_only.findings)),
        ("a prove: graph refuses to start without a launch stamp",
         "attest.py --stamp" in str(unstamped.get("err")), unstamped),
        ("  and carries the stamp into the summary it is recorded from",
         ((stamped.get("r") or {}).get("launch") or {}).get("since") == "2026-09-23T00:00:00Z",
         stamped),
        ("a stamp without a run nonce is refused (overlapping runs, #91 review)",
         "attest.py --stamp" in str(no_nonce.get("err")), no_nonce),
        ("  and so is a nonce that is not a plain token",
         "attest.py --stamp" in str(bad_nonce.get("err")), bad_nonce),
        ("the prove node is told to bind its gate run to this run's nonce",
         "FPL_ATTEST_NONCE=${LAUNCH.nonce}" in js and "provePreamble(\"ci\") +" in js
         and "--match-head-commit" in js, "preamble"),
        ("  and to name the stamped project root to attest.py",
         "const ROOT_ARG = " in js and "attest.py --ci${ROOT_ARG}" in js
         and "--last ${gate}${ROOT_ARG}" in js, "root"),
        ("  single-quoted, so no path can expand or run in the shell",
         root_arg_literal(js), "literal"),
        ("a ci citation that names no commit is UNPROVEN in the run",
         "gate === 'ci' && !r.sha" in js, "checked"),
    ]:
        detail = "yes" if ok else f"got {str(got)[:150]}"
        print(f"{'PASS' if ok else 'FAIL'}  prove-ci: {name:<58} -> {detail[:120]}")
        passed, failed = (passed + ok, failed + (not ok))


prove_ci_and_launch_case()


# Halt reasons are executable interpolation sites, validated before launch (#107).
def halt_tokens_case():
    import subprocess
    import tempfile
    global passed, failed
    ir = {"version": 1, "campaign": "halt tokens", "treeGuard": False, "budget": {"maxNodes": 8},
          "nodes": [{"id": "check", "prompt": "check {{A.target}}", "contract": "HarnessCheckV1",
                     "haltWhen": "exit != 0", "haltReason": "red {{A.target}}: {{prev.exit}}"}]}
    checks = [("halt reason current-result projection is accepted", not cg.validate(ir, CONTRACTS))]
    js = cg.emit(ir, CONTRACTS)
    checks.append(("halt reason renders args and current result", "{{" not in js[js.index("HALT at check"):]
                   and 'tokenText((n_check || {})["exit"] ?? null)' in js))
    for token in ["process.env.SECRET", "prev.nope", "prev.exit.deep", "item.key", "A.target + secret", "A..target", "A[target]", "campaign.nope"]:
        bad = copy.deepcopy(ir); bad["nodes"][0]["haltReason"] = "bad {{" + token + "}}"
        checks.append(("halt reason rejects " + token, any("haltReason" in f for f in cg.validate(bad, CONTRACTS))))
    with tempfile.TemporaryDirectory() as tmp:
        graph = os.path.join(tmp, "WORK.md")
        # Non-ASCII output is measured as UTF-8 bytes, not Python characters.
        ir["nodes"][0]["prompt"] = "é" * 1000
        open(graph, "w", encoding="utf-8").write("```json graph-ir\n" + json.dumps(ir) + "\n```\n")
        script = os.path.join(tmp, "launch.js")
        cli = [sys.executable, cg.__file__, graph, "--gates-root", tmp]
        built = subprocess.run(cli + ["-o", script], capture_output=True, text=True)
        size = os.path.getsize(script)
        exact = subprocess.run(cli + ["--check", "--max-script-bytes", str(size)], capture_output=True, text=True)
        over = subprocess.run(cli + ["--check", "--max-script-bytes", str(size-1)], capture_output=True, text=True)
        open(script, "ab").write(b"x")
        changed = subprocess.run(cli + ["--check", "--max-script-bytes", str(size), "--check-script", script], capture_output=True, text=True)
        checks.extend([
            ("--check measures exact UTF-8 output and permits limit", built.returncode == exact.returncode == 0 and f"script {size:,} bytes" in exact.stdout and "headroom 0" in exact.stdout),
            ("--check rejects one byte over launcher limit", over.returncode == 1 and "script rejected" in over.stderr),
            ("launch preflight rechecks the actual output file", changed.returncode == 1 and "launch script rejected" in changed.stderr),
        ])
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        passed, failed = passed + ok, failed + (not ok)

halt_tokens_case()

for scope, want_warning in [("docs/binding.json", True), ("The generation-1 settlement must remain supported.", False)]:
    diagnostics = cg.warnings(BASE, specification={"packet": {"checks": [{"id": "scope-test", "scope": scope}]}})
    ok = any("bare path" in w and "scope-test" in w for w in diagnostics) == want_warning
    print(f"{'PASS' if ok else 'FAIL'}  compiler surfaces opaque scope path: {scope}")
    passed, failed = passed + ok, failed + (not ok)


# Execute a repair graph: dropout, feedback, immutable paths and integration (#126).
def repair_loop_case():
    import subprocess
    import tempfile
    global passed, failed
    ir = {"version": 1, "campaign": "repair", "treeGuard": False,
          "budget": {"maxNodes": 40, "cacheTtl": "1h"},
          "lists": {"slices": [{"key": "a"}, {"key": "b"}]},
          "nodes": [
              {"id": "fix", "foreach": "slices", "mutates": True, "prompt": "fix {{item.key}} in {{roundDir}} after {{lastCheck}}", "contract": "SliceV1"},
              {"id": "check", "foreach": "slices", "after": "fix", "independent": True, "verifies": "fix", "isolation": "measure", "prompt": "check {{prev}} from {{roundDir}}", "contract": "HarnessCheckV1", "repeat": {"until": "exit == 0", "maxRounds": 3, "integrate": ["merge", "review"]}},
              {"id": "merge", "after": "check", "mutates": True, "prompt": "merge {{prev}} in {{roundDir}}; previous {{lastCheck}}", "contract": "SliceV1"},
              {"id": "review", "after": "merge", "independent": True, "verifies": "merge", "isolation": "measure", "prompt": "review {{prev}} from {{roundDir}}", "contract": "HarnessCheckV1", "haltWhen": "exit != 0"},
          ]}
    checks = [("repair graph accepted", not cg.validate(ir, CONTRACTS)),
              ("repair budget includes both workers and integration every round", cg.plan_node_count(ir) == 18)]
    for name, change, needle in [
        ("invalid round cap", lambda x: x["nodes"][1]["repeat"].update(maxRounds="forever"), "integer >= 1"),
        ("invalid integration", lambda x: x["nodes"][1]["repeat"].update(integrate=[{}, []]), "must name"),
        ("checker isolation", lambda x: x["nodes"][1].pop("isolation"), "fresh frozen tree"),
        ("checker independence", lambda x: x["nodes"][1].pop("independent"), "independent checker"),
        ("until field", lambda x: x["nodes"][1]["repeat"].update(until="missing == 0"), "not a field"),
        ("unique output paths", lambda x: x["lists"]["slices"].append({"key": "a"}), "unique lowercase"),
        ("unsafe path key", lambda x: x["lists"]["slices"][0].update(key="../../escape"), "unique lowercase"),
        ("integration independent", lambda x: x["nodes"][3].pop("independent"), "integration review"),
        ("discovery/repair ambiguity", lambda x: x["nodes"][1]["repeat"].update(untilDryRounds=1), "do not combine"),
    ]:
        bad = copy.deepcopy(ir); change(bad)
        checks.append(("repair rejects " + name, any(needle in f for f in cg.validate(bad, CONTRACTS))))

    def run(mode="normal", rounds=3):
        run_ir = copy.deepcopy(ir); run_ir["nodes"][1]["repeat"]["maxRounds"] = rounds
        js = cg.emit(run_ir, CONTRACTS).replace("export const meta", "const meta")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "result.json"); script = os.path.join(tmp, "run.mjs")
            prefix = '''import {writeFileSync} from 'node:fs';
const calls=[]; const mode=MODE;
const args={_base:{sha:'base',porcelain:''},_replayLaunchNonce:'1111111111111111111111111111111111111111111111111111111111111111',_launch:{root:'/campaign',nonce:'launch1'}};
const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};
const parallel=async(xs)=>Promise.all(xs.map(f=>f()));
const agent=async(p,o)=>{const label=String(o.label); calls.push({label,p});
 if (label.startsWith('check:')) return {exit: mode==='cap' || (label.includes(':b:') && label.endsWith('r1')) ? 1 : 0, command:'check', tail:'finding'};
 if (label.startsWith('review:')) return {exit: mode==='conflict' && label.endsWith('r1') ? 1 : 0, command:'review',tail:'composition failure'};
 return {branch:'branch',diffSummary:'patch',testsAdded:[],sharedResources:[],harnessCommand:'test',harnessExit:0,evidence:['test']};};
'''.replace("MODE", json.dumps(mode))
            open(script, "w").write(prefix + '\n(async()=>{\n' + js + '\n})().then(r=>writeFileSync(' + json.dumps(out) + ',JSON.stringify({r,calls}))).catch(e=>writeFileSync(' + json.dumps(out) + ',JSON.stringify({error:String(e)})));')
            proc = subprocess.run(["node", script], capture_output=True, text=True, timeout=20)
            return json.load(open(out)) if os.path.exists(out) else {"error": proc.stderr}

    normal = run(); cap = run("cap", 2); conflict = run("conflict")
    calls = normal.get("calls", [])
    checks.extend([
        ("repair executes until every slice passes", normal.get("r", {}).get("outcome") == "COMPLETE"),
        ("passing slice drops out of later rounds", [x["label"] for x in calls if x["label"].startswith("fix:")] == ["fix:a:r1", "fix:b:r1", "fix:b:r2"]),
        ("failed check reaches next fix", any(x["label"] == "fix:b:r2" and '"exit":1' in x["p"] for x in calls)),
        ("round output paths are versioned", any('/launch1/b/r2/' in x["p"] for x in calls if x["label"] == "fix:b:r2")),
        ("fresh-tree checker receives patch artifact directory", any("git archive" in x["p"] and "/launch1/b/r2/" in x["p"] for x in calls if x["label"] == "check:b:r2")),
        ("integration runs each round over cumulative passing items", [x["label"] for x in calls if x["label"].startswith("merge:")] == ["merge:r1", "merge:r2"]),
        ("cap is INCOMPLETE with pending item identities", cap.get("r", {}).get("outcome") == "INCOMPLETE" and any(x.get("data", {}).get("pending") == ["a", "b"] for x in cap.get("r", {}).get("provenance", []))),
        ("failed integration reopens passed items", any(x["label"] == "fix:a:r2" and "composition failure" in x["p"] for x in conflict.get("calls", []))),
    ])
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -> {normal}" if not ok else ""))
        passed, failed = passed + ok, failed + (not ok)

repair_loop_case()


# A recurring repair mechanism needs an independently measured reframe (#125).
def repair_reframe_case():
    import subprocess
    import tempfile
    global passed, failed
    ir = {"version": 1, "campaign": "reframe", "treeGuard": False,
          "budget": {"maxNodes": 1000, "cacheTtl": "1h"},
          "lists": {"slices": [{"key": "parser", "targetClasses": ["decode"]}]},
          "nodes": [
              {"id": "fix", "foreach": "slices", "mutates": True, "prompt": "fix {{item.key}}: {{lastCheck}} in {{roundDir}}", "contract": "SliceV1"},
              {"id": "check", "foreach": "slices", "after": "fix", "independent": True, "verifies": "fix", "isolation": "measure", "prompt": "check {{prev}}", "contract": "FixCheckV1", "verifyOver": "findings", "repeat": {"until": "verdict == 'PASS'", "maxRounds": 4, "classBy": ["defectClass"], "reframeAfter": 2}},
          ]}
    checks = [("FixCheck repair class configuration accepted", not cg.validate(ir, CONTRACTS))]
    for key, value in [("classBy", ["missing"]), ("reframeAfter", 1), ("reframeAfter", True)]:
        bad = copy.deepcopy(ir); bad["nodes"][1]["repeat"][key] = value
        checks.append(("repair rejects invalid " + key + str(value), bool(cg.validate(bad, CONTRACTS))))
    def run(mode):
        run_ir = copy.deepcopy(ir)
        if mode.startswith("sentinel"):
            run_ir["treeGuard"] = True
        if mode.startswith("integration"):
            run_ir["nodes"][1]["repeat"]["integrate"] = ["merge", "review"]
            run_ir["nodes"].extend([
                {"id": "merge", "after": "check", "mutates": True, "prompt": "merge {{prev}}", "contract": "SliceV1"},
                {"id": "review", "after": "merge", "independent": True, "verifies": "merge", "isolation": "measure", "prompt": "review {{prev}}", "contract": "FixCheckV1", "haltWhen": "verdict != 'PASS'"},
            ])
        js = cg.emit(run_ir, CONTRACTS).replace("export const meta", "const meta")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.json"); script = os.path.join(tmp, "run.mjs")
            prefix = r'''import {writeFileSync} from 'node:fs';
const args={_replayLaunchNonce:'freshlaunch',_base:{sha:'base',porcelain:''},_launch:{root:'/campaign',nonce:'nonce'}},calls=[],mode=MODE;
const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};
const parallel=async(xs)=>Promise.all(xs.map(f=>f()));
const reframe={classes:['decode'],invariant:'ledger admits only bounded rows',classRewire:'enforce item decoding budget at ingress',disposition:'adopt',measurement:{command:'pytest test_decode_budget',result:'all outsider rows bounded'}};
const origin=(p,defectClass)=>{const line=p.split('ORIGIN REPLAY CONTEXT: ')[1].split('\n')[0],c=JSON.parse(line);
 const observation={command:'pytest replay',result:'failure reproduced',controlCommand:'pytest valid',controlResult:'control passed',controlPassed:true,reproduces:true};
 return {origin:defectClass==='decode'?'incomplete-fix':'pre-existing',originEvidence:{base:{snapshot:c.base,...observation},targetClasses:['decode'],rounds:c.patches.map(x=>({...x,...observation})),...(c.integrated?{integrated:{...c.integrated,...observation}}:{})}};};

const agent=async(p,o)=>{const label=String(o.label); calls.push({label,p});
 if(label.startsWith('tree-check:'))return (mode==='sentinel-start'&&label.includes('campaign-start'))||(mode==='sentinel-after'&&label.includes('after repair'))?null:{head:'base',porcelain:''};
 const round=Number(label.match(/:r(\d+)$/)?.[1]||1);
 if(label.startsWith('fix:'))return {branch:'patch',diffSummary:'patch',testsAdded:[],sharedResources:[],harnessCommand:'pytest',harnessExit:0,evidence:['pytest exit0'],...(round>=3&&['measured','unchecked','integration-measured','integration-merge-narrow'].includes(mode)?{reframe}: {})};
 if(label.startsWith('check:')){const pass=mode==='contradictory'||mode.startsWith('integration')||round>=3&&['measured','unchecked','different'].includes(mode);
  const defectClass=mode==='different'&&round===2?'resource':'decode';
  return {verdict:pass?'PASS':'FAIL',findings:pass&&mode!=='contradictory'?[]:[{defectClass,finding:'bad outsider row',severity:'HIGH',invariant:'bounded rows',classRewire:'bound parser',measurement:'pytest reproduced',...origin(p,defectClass)},...(mode==='duplicates'?[{defectClass,finding:'same mechanism another path',severity:'HIGH',invariant:'bounded',classRewire:'bound',measurement:'pytest reproduced',...origin(p,defectClass)}]:[])],blockers:[],evidence:['pytest observed'],...(['measured','integration-measured','integration-merge-narrow'].includes(mode)&&round>=3?{reframe:{...reframe,validated:true,measurement:{command:'pytest test_independent_budget',result:'independently checked bounds'}}}: {})};}
 if(label.startsWith('merge:'))return {branch:'integrated',diffSummary:'patch',testsAdded:[],sharedResources:[],harnessCommand:'pytest',harnessExit:0,evidence:['pytest'],...(mode==='integration-measured'&&round>=3?{reframe}: {})};
 if(label.startsWith('review:'))return {verdict:round>=3?'PASS':'FAIL',findings:round>=3?[]:[{defectClass:'decode',finding:'composition failure',severity:'HIGH',invariant:'bounded rows',classRewire:'enforce whole budget',measurement:'pytest compose',...origin(p,'decode')}],blockers:[],evidence:['pytest compose'],...(mode==='integration-measured'&&round>=3?{reframe:{...reframe,validated:true}}: {})};
 return null;};
'''.replace("MODE", json.dumps(mode))
            open(script, "w").write(proof_fixture(prefix) + '\n(async()=>{\n' + js + '\n})().then(r=>writeFileSync(' + json.dumps(out) + ',JSON.stringify({r,calls}))).catch(e=>writeFileSync(' + json.dumps(out) + ',JSON.stringify({error:String(e)})));')
            proc = subprocess.run(["node", script], capture_output=True, text=True, timeout=20)
            return json.load(open(out)) if os.path.exists(out) else {"error": proc.stderr}
    narrow = run("narrow"); measured = run("measured"); unchecked = run("unchecked"); different = run("different"); duplicates = run("duplicates")
    checks += [
        ("third narrow patch refused before checker/integration", narrow.get("r", {}).get("outcome") == "REFRAME-REQUIRED" and not any(c["label"] == "check:parser:r3" for c in narrow.get("calls", []))),
        ("two same-class failures force next fixer to reframe", any(c["label"] == "fix:parser:r3" and c["p"].startswith("REFRAME REQUIRED") for c in narrow.get("calls", []))),
        ("recurring class and rounds are recorded", any(x.get("class") == "decode" and x.get("rounds") == [1, 2] for x in narrow.get("r", {}).get("recurringClasses", []))),
        ("independently measured reframe can complete", measured.get("r", {}).get("outcome") == "COMPLETE"),
        ("fixer's own measurement cannot stand for checker", unchecked.get("r", {}).get("outcome") == "REFRAME-REQUIRED"),
        ("different failure class resets consecutive-round count", different.get("r", {}).get("outcome") == "COMPLETE"),
        ("same-class duplicate findings count as one failed round", not any(c["label"] == "fix:parser:r2" and c["p"].startswith("REFRAME REQUIRED") for c in duplicates.get("calls", []))),
    ]
    for mode, outcome in [("integration-narrow", "REFRAME-REQUIRED"), ("integration-merge-narrow", "REFRAME-REQUIRED"), ("integration-measured", "COMPLETE"), ("contradictory", "HALTED")]:
        result = run(mode)
        checks.append(("integration/class gate " + mode, result.get("r", {}).get("outcome") == outcome))
    for mode in ["sentinel-start", "sentinel-after"]:
        result = run(mode)
        checks.append(("failed tree sentinel stops immediately " + mode, result.get("r", {}).get("outcome") == "TREE-UNKNOWN" and (mode != "sentinel-start" or len(result.get("calls", [])) == 1)))
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -> {narrow}" if not ok else ""))
        passed, failed = passed + ok, failed + (not ok)

repair_reframe_case()


# Exact-key councils elect a candidate, never a judge's paraphrase (#144).
def council_case():
    import subprocess
    import tempfile
    global passed, failed
    ir = {"version": 1, "campaign": "elect a real candidate", "treeGuard": False,
          "budget": {"maxNodes": 20, "cacheTtl": "1h"},
          "lists": {"angles": [{"key": "removal"}, {"key": "keep"}, {"key": "trim"}]},
          "nodes": [
              {"id": "design", "foreach": "angles", "prompt": "design {{item.key}}", "contract": "DesignV1", "onRed": "drop+log"},
              {"id": "choose", "after": "design", "prompt": "Compare every candidate side by side: {{prev}}", "contract": "DecisionV1", "decides": "approach", "verify": "judges:3"},
              {"id": "build", "after": "choose", "prompt": "Implement the raw design {{winner}}. Decision {{prev.chosen}}.", "contract": "DesignV1"},
          ]}
    raw_only = copy.deepcopy(ir); raw_only["nodes"][2]["prompt"] = "Implement {{winner}}"
    checks = [("raw winner consumption satisfies its declared after edge", not cg.validate(raw_only, CONTRACTS)),
              ("keyed council is valid", not cg.validate(ir, CONTRACTS)),
              ("judge seats are priced without an extra synthesis agent", cg.plan_node_count(ir) == 7 and cg.estimate_tokens(ir, CONTRACTS)["perNode"]["choose"]["calls"] == 3)]
    for name, mutate, needle in [
        ("even seats", lambda x: x["nodes"][1].update(verify="judges:4"), "positive odd"),
        ("zero seats", lambda x: x["nodes"][1].update(verify="judges:0"), "positive odd"),
        ("missing decision", lambda x: x["nodes"][1].pop("decides"), "requires decides"),
        ("wrong decision contract", lambda x: x["nodes"][1].update(contract="DesignV1"), "contract DecisionV1"),
        ("duplicate candidate keys", lambda x: x["lists"]["angles"][1].update(key="trim"), "unique lowercase"),
        ("unkeyed candidate", lambda x: x["lists"]["angles"][1].pop("key"), "unique lowercase"),
        ("reserved none key", lambda x: x["lists"]["angles"][1].update(key="none"), "reserved"),
        ("scalar predecessor", lambda x: x["nodes"][0].pop("foreach"), "keyed candidate"),
        ("panel candidate predecessor", lambda x: x["nodes"][0].update(verify="panel:3", verifyOver="findings", contract="FindingsV1"), "keyed candidate"),
        ("refuter-only field", lambda x: x["nodes"][1].update(verifyOver="options"), "cannot combine with verifyOver"),
        ("drop policy cannot bypass election", lambda x: x["nodes"][1].update(onRed="drop+log"), "requires onRed"),
        ("winner outside a judged edge", lambda x: x["nodes"][1].update(verify="schema-only"), "not in scope"),
        ("winner projection", lambda x: x["nodes"][2].update(prompt="{{prev}} {{winner.plan}}"), "not projections"),
    ]:
        bad = copy.deepcopy(ir); mutate(bad)
        checks.append(("council rejects " + name, any(needle in f for f in cg.validate(bad, CONTRACTS))))
    def run(mode):
        js = cg.emit(ir, CONTRACTS).replace("export const meta", "const meta")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.json"); script = os.path.join(tmp, "run.mjs")
            prefix = r'''import {writeFileSync} from 'node:fs';
const args={},calls=[],mode=MODE;
const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};
const parallel=async(xs)=>Promise.all(xs.map(f=>f()));
const agent=async(p,o)=>{const label=String(o.label);calls.push({label,p,schema:o.schema});
 if(label.startsWith('design:')){const key=label.split(':')[1];
  if(mode==='dead-design'&&key==='removal'||mode==='one-design'&&key!=='trim'||mode==='no-designs')return null;
  return {summary:'full independent candidate '+key,plan:['RAW PLAN '+key],files:[key+'.py'],risks:['measured risk for '+key],key:'forged-agent-key'};}
 if(label.startsWith('choose:judge')){const seat=Number(label.slice(-1)),keys=o.schema.properties.winner.enum;
  if((mode==='dead-split'||mode==='dead-majority')&&seat===3)return null;
  let winner=mode==='split'?['removal','keep','trim'][seat-1]:mode==='dead-split'?(seat===1?'keep':'trim'):mode==='none'?'none':'trim';
  if(mode==='prose')winner='Shape-only follow (design 3): trim';
  const ranking=[winner,...keys.filter(k=>k!==winner)];
  if(mode==='bad-ranking')ranking[1]=winner;
  const strongest_objection=Object.fromEntries(keys.map(k=>[k,'A substantive independent objection to candidate '+k]));
  if(mode==='missing-objection')delete strongest_objection.none;
  return {winner,ranking,rationale:'This candidate preserves the measured invariant with the smallest supported change.',strongest_objection};}
 return {summary:'builder completed the elected design',plan:['built the elected raw design'],files:[],risks:['the implementation still needs its independent gate']};};
'''.replace("MODE", json.dumps(mode))
            open(script, "w").write(prefix+'\n(async()=>{\n'+js+'\n})().then(r=>writeFileSync('+json.dumps(out)+',JSON.stringify({r,calls}))).catch(e=>writeFileSync('+json.dumps(out)+',JSON.stringify({error:String(e)})));')
            proc = subprocess.run(["node", script], capture_output=True, text=True, timeout=20)
            return json.load(open(out)) if os.path.exists(out) else {"error": proc.stderr}
    normal=run("normal"); dead=run("dead-design")
    checks += [
        ("strict majority elects the exact key", normal.get("r", {}).get("results", {}).get("choose", {}).get("chosen") == "trim"),
        ("builder gets original winning design bytes", any(c["label"] == "build" and '"plan":["RAW PLAN trim"]' in c["p"] and "RAW PLAN removal" not in c["p"] for c in normal.get("calls", []))),
        ("producer cannot forge its candidate key", normal.get("r", {}).get("results", {}).get("design", [])[0].get("key") == "removal"),
        ("dead design does not shift selected candidate identity", dead.get("r", {}).get("outcome") == "COMPLETE" and dead.get("r", {}).get("winners", {}).get("choose", {}).get("plan") == ["RAW PLAN trim"]),
        ("dead candidates are absent from ballot schema", all(c["schema"]["properties"]["winner"]["enum"] == ["keep","trim","none"] for c in dead.get("calls", []) if c["label"].startswith("choose:judge"))),
        ("no candidate falls back after every design dies", run("no-designs").get("r", {}).get("outcome") == "NO-CANDIDATES"),
        ("two surviving votes can reach majority of three seats", run("dead-majority").get("r", {}).get("outcome") == "COMPLETE"),
        ("one surviving design remains a choice against none", run("one-design").get("r", {}).get("outcome") == "COMPLETE"),
        ("prose winner is excluded by every narrowed schema", all("Shape-only follow (design 3): trim" not in c["schema"]["properties"]["winner"]["enum"] for c in normal.get("calls", []) if c["label"].startswith("choose:judge"))),
    ]
    for mode in ["split", "dead-split", "none", "prose", "bad-ranking", "missing-objection"]:
        result=run(mode)
        checks.append(("council blocks without an exact-key majority: "+mode, result.get("r", {}).get("outcome") == "NO-MAJORITY" and not any(c["label"] == "build" for c in result.get("calls", []))))
    decision=normal.get("r", {}).get("results", {}).get("choose")
    checks.append(("tallied decision satisfies chosen-among-options validation", bool(decision) and not cg._sibling("decision").validate(decision, CONTRACTS["DecisionV1"], choice_among_options=True)))
    with tempfile.TemporaryDirectory() as tmp:
        broken = copy.deepcopy(decision); broken["chosen"] = "a prose choice outside the candidate keys"
        for strict in [True, False]:
            stored = copy.deepcopy(broken)
            if not strict:
                stored.pop("council")
            open(os.path.join(tmp, "source.json"), "w").write(json.dumps({"summary": {"decisions": {"approach": stored}}}))
            _, errors = cg.resolve_imports({"imports": {"approach": "source"}, "nodes": []}, CONTRACTS, tmp, os.path.join(tmp, "missing.jsonl"))
            checks.append(("judged import retains exact-key rule" if strict else "legacy free-text import stays supported", bool(errors) == strict))
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}"+(f" -> {normal}" if not ok else ""))
        passed, failed = passed + ok, failed + (not ok)

council_case()


def probe_declarations_case():
    """--check validates probe declarations with the audit's own grammar (#192).

    A builder that runs a script left its isolated probe incomplete in every
    audit round, so the graph could never read SOUND. Script commands now
    need a reviewed `probe` declaration, and --check refuses what the audit
    would refuse before a round is bought.
    """
    global passed, failed
    import subprocess
    import tempfile
    runner = 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --log build.log -- python scripts/build.py --all'
    awaited = 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py await --timeout 240 <token>'
    tool = 'python scripts/tool.py report --json'

    def node(nid, prompt, probe=None, **extra):
        n = {"id": nid, "prompt": prompt, "contract": "DesignV1", "isolation": "worktree", **extra}
        if probe is not None:
            n["probe"] = probe
        return n

    def shape(command, form, **extra):
        return {"commands": [{"command": command, "kind": "shape", "form": form, **extra}]}

    def help_form(command):
        return shape(command, command + " --help")

    def read_only(command):
        return {"commands": [{"command": command, "kind": "read-only"}]}

    def run(nid, command, probe, **extra):
        return node(nid, "Run " + command, probe, **extra)

    later = "python scripts/check.py --key {{item.key}} --strict"
    remainder = 'longrun.py start --log build.log python child.py'
    clean = [
        node("read-only", "Run " + tool, {"commands": [{"command": tool, "kind": "read-only"}]}),
        node("remainder", "Run " + runner,
             shape(runner, 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --log build.log --help')),
        node("subcommand", "Run " + runner, shape(runner, 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --help')),
        node("data", "Then `" + awaited + "`", shape(awaited, awaited + " --help", data={"token": "token"})),
        node("template", "Use `cd <dir> && python ...` there.",
             {"templates": [{"template": "cd <dir> && python ...", "instances": ["cd {{A._launch.root}} && " + tool]}],
              "commands": [{"command": "cd {{A._launch.root}} && " + tool, "kind": "read-only"}]}),
        node("grammar", "Run `git -C {{A._launch.root}} fetch origin && git status | cat`"),
        node("later-binding", "Run `cd {{item.key}} && python tool.py`", foreach="dims"),
        node("unbound", "Run `python {{A.tool}} --check`",
             {"commands": [{"command": "python {{A.tool}} --check", "kind": "read-only"}]}),
        run("rule-ok-python-options", "python3 -I -u scripts/tool.py report",
            shape("python3 -I -u scripts/tool.py report", "python3 -I -u scripts/tool.py --help")),
        run("rule-ok-python-module", "python -m pytest -q tests", shape("python -m pytest -q tests", "python -m pytest --help")),
        run("rule-ok-node-script", "node scripts/build.js --all", shape("node scripts/build.js --all", "node scripts/build.js --help")),
        run("rule-ok-windows-python", "C:/Python311/python.exe scripts/tool.py report",
            shape("C:/Python311/python.exe scripts/tool.py report", "C:/Python311/python.exe scripts/tool.py --help")),
        run("rule-ok-rm-options", "rm -rf build", shape("rm -rf build", "rm -rf --help")),
        run("git-id-plain-read", "git status --porcelain", read_only("git status --porcelain")),
        run("win-forward-python", '"C:/Program Files/Python/python.exe" scripts/a.py --check',
            shape('"C:/Program Files/Python/python.exe" scripts/a.py --check',
                  '"C:/Program Files/Python/python.exe" scripts/a.py --help')),
        node("rule-f-typed-item", "Run `" + later + "`",
             shape(later, "python scripts/check.py --key {{item.key}} --help", data={"item.key": "token"}), foreach="dims"),
    ]
    refused = {
        "undeclared": (node("undeclared", "Run " + tool), "no probe declaration for " + tool),
        "unbound-undeclared": (node("unbound-undeclared", "Run `python {{A.tool}} --check`"),
                               "no probe declaration for python A.tool --check"),
        "stray": (node("stray", "Run " + tool, {"commands": [{"command": tool, "kind": "read-only"},
                                                             {"command": "python other.py", "kind": "read-only"}]}),
                  "matches no inventoried command"),
        "stray-template": (node("stray-template", "Run " + tool, {
            "commands": [{"command": tool, "kind": "read-only"}],
            "templates": [{"template": "git -C <dir> status", "instances": ["git -C /x status"]}]}),
            "matches no inventoried command"),
        "kind": (node("kind", "Run " + tool, {"commands": [{"command": tool, "kind": "trusted"}]}), "kind must be"),
        "key": (node("key", "Run " + tool, {"commands": [{"command": tool, "kind": "read-only"}], "allow": ["python"]}),
                "unknown field 'allow'"),
        "after-separator": (node("after-separator", "Run " + runner, shape(
            runner, 'bash "{{A._launch.root}}/scripts/py.sh" longrun.py start --log build.log -- --help')), "-- separator"),
        "blind-append": (node("blind-append", "Run " + runner, shape(runner, runner + " --help")), "-- separator"),
        "names-child": (node("names-child", "Run `" + remainder + "`", shape(
            remainder, "longrun.py start --log build.log python --help")), "names another program"),
        "not-prefix": (node("not-prefix", "Run " + tool, shape(tool, "python scripts/tool.py status --help")), "not a prefix"),
        "no-help": (node("no-help", "Run " + tool, shape(tool, tool)), "ends a program part in --help"),
        "git-help": (node("git-help", "Run `git push origin main`", shape("git push origin main", "git push --help")),
                     "ends a program part in --help"),
        "read-only-git": (node("read-only-git", "Run `git push origin main`",
                               {"commands": [{"command": "git push origin main", "kind": "read-only"}]}), "not a git read"),
        "read-only-data": (node("read-only-data", "Then `" + awaited + "`",
                                {"commands": [{"command": awaited, "kind": "read-only"}]}), "placeholder"),
        "untyped-data": (node("untyped-data", "Then `" + awaited + "`", shape(awaited, awaited + " --help")),
                         "<token> has no declared type"),
        "data-type": (node("data-type", "Then `" + awaited + "`", shape(awaited, awaited + " --help", data={"token": "path"})),
                      "data type"),
        "instance": (node("instance", "Use `cd <dir> && python ...` there.",
                          {"templates": [{"template": "cd <dir> && python ...", "instances": ["git status"]}]}),
                     "does not instantiate"),
        "no-placeholder": (node("no-placeholder", "Run " + tool,
                                {"templates": [{"template": tool, "instances": [tool]}]}), "no <placeholder>"),
        # A: -h is human-readable or a host in du, df, sort, ls and docker run.
        "rule-a-dash-h": (run("rule-a-dash-h", "python3 scripts/tool.py report",
                              shape("python3 scripts/tool.py report", "python3 scripts/tool.py -h")), "-h is not a help flag"),
        "rule-a-ls": (run("rule-a-ls", "ls -la build", shape("ls -la build", "ls -h")), "-h is not a help flag"),
        # B: an interpreter's options before its script cannot carry code.
        "rule-b-python-c": (run("rule-b-python-c", 'python3 -c "import shutil; shutil.rmtree(\'x\')"',
                                help_form('python3 -c "import shutil; shutil.rmtree(\'x\')"')), "the option -c"),
        "rule-b-node-e": (run("rule-b-node-e", 'node -e "require(\'fs\').rmSync(\'x\',{recursive:true})"',
                              help_form('node -e "require(\'fs\').rmSync(\'x\',{recursive:true})"')), "the option -e"),
        "rule-b-bash-c": (run("rule-b-bash-c", "bash -c 'for f in a b; do rm -f $f; done'",
                              help_form("bash -c 'for f in a b; do rm -f $f; done'")), "the option -c"),
        "rule-b-sh-c": (run("rule-b-sh-c", "sh -c 'exec python x.py'", help_form("sh -c 'exec python x.py'")), "the option -c"),
        "rule-b-python-ic": (run("rule-b-python-ic", "python -Ic 'import os'", help_form("python -Ic 'import os'")),
                             "the option -Ic"),
        "rule-b-node-eval": (run("rule-b-node-eval", 'node --eval "1"', help_form('node --eval "1"')), "the option --eval"),
        "rule-b-node-print": (run("rule-b-node-print", 'node -p "1"', help_form('node -p "1"')), "the option -p"),
        "rule-b-python-stdin": (run("rule-b-python-stdin", "python3 -", help_form("python3 -")), "the option - before"),
        "rule-b-windows-python": (run("rule-b-windows-python", 'C:/Python311/python3.11.exe -c "import os"',
                                      help_form('C:/Python311/python3.11.exe -c "import os"')), "the option -c"),
        "rule-b-py-launcher": (run("rule-b-py-launcher", 'py -c "import os"', help_form('py -c "import os"')), "the option -c"),
        "rule-b-expansion": (run("rule-b-expansion", 'bash "$ROOT/scripts/run.sh" check',
                                 shape('bash "$ROOT/scripts/run.sh" check', 'bash "$ROOT/scripts/run.sh" --help')),
                             "shell expansion"),
        # C: a launcher hands its trailing words to another program or host.
        "rule-c-npx": (run("rule-c-npx", "npx some-package", help_form("npx some-package")), "forwards its trailing words"),
        "rule-c-docker": (run("rule-c-docker", "docker run --rm img", help_form("docker run --rm img")),
                          "forwards its trailing words"),
        "rule-c-sudo": (run("rule-c-sudo", "sudo python3 scripts/tool.py", shape("sudo python3 scripts/tool.py", "sudo --help")),
                        "forwards its trailing words"),
        "rule-c-env": (run("rule-c-env", "/usr/bin/env python3 scripts/tool.py",
                           shape("/usr/bin/env python3 scripts/tool.py", "/usr/bin/env --help")), "forwards its trailing words"),
        "rule-c-timeout": (run("rule-c-timeout", "timeout 60 python3 scripts/tool.py",
                               shape("timeout 60 python3 scripts/tool.py", "timeout --help")), "forwards its trailing words"),
        # D: a read-only declaration never runs a writer, a launcher or inline code.
        "rule-d-rm": (run("rule-d-rm", "rm -rf build", read_only("rm -rf build")), "writes files or reaches the network"),
        "rule-d-curl": (run("rule-d-curl", "curl -X POST https://example.invalid/x",
                            read_only("curl -X POST https://example.invalid/x")), "writes files or reaches the network"),
        "rule-d-python-c": (run("rule-d-python-c", 'python3 -c "import os"', read_only('python3 -c "import os"')),
                            "the option -c"),
        "rule-d-sudo": (run("rule-d-sudo", "sudo cat /etc/hosts", read_only("sudo cat /etc/hosts")),
                        "forwards its trailing words"),
        "rule-d-assignment": (run("rule-d-assignment", "FOO=1 python3 scripts/tool.py", read_only("FOO=1 python3 scripts/tool.py")),
                              "variable assignment"),
        "rule-d-expansion": (run("rule-d-expansion", '"$TOOL" report', read_only('"$TOOL" report')), "shell expansion"),
        "rule-d-second-part": (run("rule-d-second-part", "cd build && rm -rf out", read_only("cd build && rm -rf out")),
                               "writes files or reaches the network"),
        # Also refused: --help after a writer's operand, which a POSIXLY_CORRECT
        # build acts on first, and a quoted -- separator.
        "rule-x-rm-operand": (run("rule-x-rm-operand", "rm -rf out", shape("rm -rf out", "rm -rf out --help")),
                              "after an operand of rm"),
        "rule-x-quoted-separator": (run("rule-x-quoted-separator", 'longrun.py start --log L "--" python child.py',
                                        shape('longrun.py start --log L "--" python child.py',
                                              'longrun.py start --log L "--" --help')), "-- separator"),
        # Git is checked by its subcommand, so only the plain word git is git.
        "git-id-path": (run("git-id-path", "/usr/bin/git reset --hard", read_only("/usr/bin/git reset --hard")),
                        "write it git"),
        "git-id-quoted": (run("git-id-quoted", "'git' reset --hard", read_only("'git' reset --hard")), "write it git"),
        "git-id-exe": (run("git-id-exe", "git.exe push origin main", read_only("git.exe push origin main")), "write it git"),
        "git-id-path-c": (run("git-id-path-c", "/usr/bin/git -C /scratch/repo reset --hard",
                              read_only("/usr/bin/git -C /scratch/repo reset --hard")), "write it git"),
        "git-id-shape": (run("git-id-shape", "/usr/bin/git push origin main",
                             shape("/usr/bin/git push origin main", "/usr/bin/git push --help")), "write it git"),
        "git-id-undeclared": (run("git-id-undeclared", "/usr/bin/git status", None), "write it git"),
        "git-id-plain-reset": (run("git-id-plain-reset", "git reset --hard", read_only("git reset --hard")), "not a git read"),
        # A backslash in a program word is an escape to a shell and a separator
        # to Windows, so a declaration may not name its program that way.
        "win-git-double": (run("win-git-double", r'"C:\Tools\git.exe" reset --hard',
                               read_only(r'"C:\Tools\git.exe" reset --hard')), "forward slashes"),
        "win-git-single": (run("win-git-single", r"'C:\Program Files\Git\cmd\git.exe' reset --hard",
                               read_only(r"'C:\Program Files\Git\cmd\git.exe' reset --hard")), "forward slashes"),
        "win-git-bare": (run("win-git-bare", r"C:\Tools\git.exe reset --hard", read_only(r"C:\Tools\git.exe reset --hard")),
                         "forward slashes"),
        "win-rm": (run("win-rm", r"C:\Tools\rm.exe -rf build", read_only(r"C:\Tools\rm.exe -rf build")), "forward slashes"),
        "win-python-c": (run("win-python-c", r'"C:\Py\python.exe" -c "import os"',
                             help_form(r'"C:\Py\python.exe" -c "import os"')), "forward slashes"),
        "win-docker": (run("win-docker", r"C:\Tools\docker.exe run --rm img", help_form(r"C:\Tools\docker.exe run --rm img")),
                       "forward slashes"),
        "win-git-inline": (node("win-git-inline", r'Then `"C:\Tools\git.exe" status` reports the tree.'), "forward slashes"),
        "escaped-rm": (run("escaped-rm", r"r\m -rf build", read_only(r"r\m -rf build")),
                       r"probe.commands[0]: r\m -rf build: "),
        # F: a later-bound token is data, typed only inside a shape form.
        "rule-f-read-only": (node("rule-f-read-only", "Run `" + later + "`", read_only(later), foreach="dims"),
                             "cannot carry {{item.key}}"),
        "rule-f-untyped": (node("rule-f-untyped", "Run `" + later + "`",
                                shape(later, "python scripts/check.py --key {{item.key}} --help"), foreach="dims"),
                           "{{item.key}} has no declared type"),
        "rule-f-launch-key": (node("rule-f-launch-key", "Run `python {{A.tool}} --check`",
                                   shape("python {{A.tool}} --check", "python {{A.tool}} --help", data={"A.tool": "token"})),
                              "launch bindings"),
        "rule-f-form-token": (node("rule-f-form-token", "Run `python scripts/check.py --strict`",
                                   shape("python scripts/check.py --strict", "python scripts/check.py {{item.key}} --help"),
                                   foreach="dims"),
                              "form carries"),
    }

    def check(nodes, env=None):
        ir = {"version": 1, "campaign": "probe declarations", "budget": {"maxNodes": 256},
              "lists": {"dims": [{"key": "a"}]}, "nodes": nodes}
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "WORK.md"), "w", encoding="utf-8") as fh:
                fh.write("STATUS: DESIGN\n\n```json graph-ir\n" + json.dumps(ir) + "\n```\n")
            return subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "compile-graph.py"), "WORK.md", "--check"],
                                  cwd=d, capture_output=True, text=True, encoding="utf-8", env=env)

    good = check(clean)
    mixed = check(clean + [n for n, _ in refused.values()])
    lines = mixed.stderr.splitlines()
    checks = [("--check accepts declared script, shape, data, template, grammar and later-bound commands",
               good.returncode == 0, good.stderr[-300:])]
    for nid, (_, needle) in refused.items():
        mine = [x for x in lines if f"node '{nid}'" in x]
        checks.append((f"--check refuses {nid}", any(needle in x for x in mine), mine or lines[-3:]))
    for n in clean:
        mine = [x for x in lines if f"node '{n['id']}'" in x]
        checks.append((f"--check raises nothing on {n['id']} beside refused nodes", mixed.returncode == 1 and not mine, mine))
    nodeless = check(clean, env=dict(os.environ, PATH=tempfile.gettempdir()))
    checks.append(("--check without Node.js refuses rather than skipping the probe grammar",
                   nodeless.returncode == 1 and "Node.js" in nodeless.stderr, nodeless.stderr[-200:]))
    plain = copy.deepcopy(BASE)
    plain["nodes"][0]["probe"] = {"commands": []}
    measured = copy.deepcopy(BASE)
    measured["nodes"][0].update(isolation="measure", probe={"commands": []})
    probed = copy.deepcopy(BASE)
    probed["nodes"][0].update(isolation="worktree", probe={"commands": []})
    for name, ir, want in [("probe on a node the audit never probes is refused", plain, True),
                           ("probe on a measure node is refused", measured, True),
                           ("probe on an isolated node is a known field", probed, False)]:
        got = [x for x in cg.validate(ir, CONTRACTS) if "probe" in x]
        checks.append((name, bool(got) == want and all("audit does not probe" in x for x in got), got))
    for name, ok, got in checks:
        print(f"{'PASS' if ok else 'FAIL'}  probe: {name:<70} -> {'yes' if ok else str(got)[:160]}")
        passed, failed = passed + ok, failed + (not ok)


probe_declarations_case()


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
