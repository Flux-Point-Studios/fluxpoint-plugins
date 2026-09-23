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
    ("throw new Error('this graph has irreversible nodes but no ledger",
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
    ("kills < need", "survival is decided by the majority threshold"),
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
     "no base was passed" not in plain_js),
    # #21: the sentinel brackets the campaign and guards every verdict-minting
    # node; drift halts rather than advancing.
    ("sentinel at start, before the verdict node, and at end",
     "treeCheck('campaign-start'" in tree_js
     and "treeCheck(\"before gate\"" in tree_js
     and "treeCheck('campaign-end'" in tree_js),
    ("tree drift is a halt, not a log line",
     "return summary('TREE-MOVED')" in tree_js),
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
                    "const args={_base:{sha:'abc123',branch:'main'}};",
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
                    "    return {head: r[0], porcelain: r[1]} }",
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

    def outcome(x):
        return (x.get("r") or {}).get("outcome")

    for name, ok, got in [
        ("the Stop hook's Evidence row is not TREE-MOVED",
         outcome(hook_row) in ("COMPLETE", "INCOMPLETE"), hook_row),
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
                       "ci": {"forge": "github", "contexts": ["harness"]}}, fh)
        loaded = cg.load_gates(d)
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
        ("a prove: graph refuses to start without a launch stamp",
         "launch stamp" in str(unstamped.get("err")), unstamped),
        ("  and carries the stamp into the summary it is recorded from",
         ((stamped.get("r") or {}).get("launch") or {}).get("since") == "2026-09-23T00:00:00Z",
         stamped),
        ("a stamp without a run nonce is refused (overlapping runs, #91 review)",
         "launch stamp" in str(no_nonce.get("err")), no_nonce),
        ("  and so is a nonce that is not a plain token",
         "launch stamp" in str(bad_nonce.get("err")), bad_nonce),
        ("the prove node is told to bind its gate run to this run's nonce",
         "FPL_ATTEST_NONCE=${LAUNCH.nonce}" in js and "provePreamble(\"ci\") +" in js
         and "--match-head-commit" in js, "preamble"),
        ("a ci citation that names no commit is UNPROVEN in the run",
         "gate === 'ci' && !r.sha" in js, "checked"),
    ]:
        detail = "yes" if ok else f"got {str(got)[:150]}"
        print(f"{'PASS' if ok else 'FAIL'}  prove-ci: {name:<58} -> {detail[:120]}")
        passed, failed = (passed + ok, failed + (not ok))


prove_ci_and_launch_case()


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
