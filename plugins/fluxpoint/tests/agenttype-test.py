#!/usr/bin/env python3
"""agentType: the name that does not resolve, and the name that resolves wrong.

Two failure modes, reported from opposite ends, wanting opposite fixes.

An UNRESOLVABLE name fails at the worst possible node: `agent type not found`
is raised at the spawn, so a gate late in the graph pays for it only after
everything upstream has run. But the agent namespace is not closed from the
compiler's position -- agents resolve from every installed plugin and from
built-in types it cannot enumerate -- so rejecting on a partial view would
refuse valid IR. That one is a warning, emitted before the first spawn.

A name that RESOLVES to an agent whose answer shape contradicts the node's
contract is the expensive one: the agent is real, it runs, and it answers in
a schema the node cannot accept -- while possibly holding the campaign's only
haltWhen. That is provable from what the compiler can see, so it is rejected.

The line between them is the property under test, and the shipped templates
are the regression that keeps the rejection honest.
"""
import copy
import importlib.util
import shutil
import tempfile
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "compile_graph", os.path.join(PLUGIN, "scripts", "compile-graph.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(os.path.join(PLUGIN, "contracts"))
AGENTS = cg.load_agents(os.path.dirname(PLUGIN))
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<54} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def ir(**over):
    base = {"version": 1, "campaign": "c", "budget": {"maxNodes": 5},
            "nodes": [{"id": "gate", "prompt": "judge it", "contract": "RedTeamV1"}]}
    base["nodes"][0].update(over)
    return base


def errs(i):
    return cg.validate(i, CONTRACTS, None, AGENTS)


# ==================== the compiler can see the bundled agents ============
report("bundled agents resolve under their bare name",
       "red-team-reviewer" in AGENTS, f"{len(AGENTS)} name(s)")
report("and under their plugin-qualified name",
       "fluxpoint:graph-auditor" in AGENTS, "qualified")

# ==================== a resolved agent with the wrong shape ==============
# The reported incident: the shipped adversarial reviewer of GRAPHS, bound to
# a node contracted to a SHIP/BLOCK verdict. It exists, it resolves, it runs
# -- and it answers about the wrong subject in the wrong shape.
e = errs(ir(agentType="fluxpoint:graph-auditor"))
report("an agent that answers in prose cannot hold a contract",
       any("answers in a report" in x for x in e), e[0][:46] if e else "ACCEPTED")

e = errs(ir(agentType="red-team-reviewer", contract="HarnessCheckV1"))
report("a declared contract that differs is rejected",
       any("declares contract 'RedTeamV1'" in x for x in e),
       e[0][:46] if e else "ACCEPTED")

# ==================== and the binding that is actually right =============
# The guard against over-reach: red-team-reviewer's verdict vocabulary and
# findings table ARE RedTeamV1, which is why the shipped template binds them.
report("the matching agent and contract are accepted",
       not errs(ir(agentType="red-team-reviewer")), "accepted")

# ==================== an unresolvable name warns, never rejects ==========
unknown = ir(agentType="totally-made-up-agent-9000")
report("an unresolvable agentType is NOT rejected",
       not errs(unknown), "not rejected")
w = cg.warnings(unknown, AGENTS)
report("but it is warned about before the first spawn",
       any("does not resolve" in x for x in w), w[0][:46] if w else "SILENT")

# A built-in type the compiler cannot enumerate must not be refused.
report("a built-in agent type is not refused",
       not errs(ir(agentType="general-purpose")), "accepted")

# The same rule reached through a role, which is how the README writes it.
role_ir = ir(role="red-team")
role_ir["roles"] = {"red-team": {"agentType": "fluxpoint:graph-auditor"}}
report("a role's agentType is checked too",
       any("answers in a report" in x for x in errs(role_ir)), "checked")

# ==================== the proof-auditor is reachable from a graph =========
# Issue #72: security review was a node and verification review was not,
# because proof-auditor answered in prose. It now declares ProofV1, so a
# node contracted to ProofV1 may bind it and a node contracted to anything
# else is refused for the same reason red-team-reviewer would be.
report("proof-auditor declares ProofV1",
       AGENTS.get("proof-auditor", {}).get("contract") == "ProofV1",
       str(AGENTS.get("proof-auditor", {}).get("contract")))
e = errs(ir(agentType="proof-auditor"))
report("proof-auditor on a RedTeamV1 node is rejected",
       any("declares contract 'ProofV1'" in x for x in e), e[0][:46] if e else "ACCEPTED")
report("proof-auditor on a ProofV1 node halting on WEAKENED is accepted",
       not errs(ir(agentType="proof-auditor", contract="ProofV1",
                   haltWhen="verdict == 'WEAKENED'")), "accepted")
report("prover declares SliceV1 (its output is edits, not a report)",
       AGENTS.get("prover", {}).get("contract") == "SliceV1",
       str(AGENTS.get("prover", {}).get("contract")))

# ==================== silence when nothing is bound ======================
report("a node with no agentType is silent",
       not errs(ir()) and not cg.warnings(ir(), AGENTS), "silent")

# ==================== the shipped templates still compile ================
# This is the regression that keeps the rejection from becoming over-reach:
# the plugin's own feature template binds red-team-reviewer by bare name.
import glob
import json
import re
# Gates resolve against the repo root's manifest, the way harness.sh
# compiles the templates: a template using prove:<gate> is legal only there.
REPO = os.path.dirname(os.path.dirname(PLUGIN))
GATES = cg.load_gates(REPO)
TEMPLATES = {}
for t in sorted(glob.glob(os.path.join(PLUGIN, "templates", "WORK*.md"))):
    src = open(t, encoding="utf-8").read()
    m = cg.IR_FENCE.search(src)
    if not m:
        continue
    tir = json.loads(m.group(1))
    TEMPLATES[os.path.basename(t)] = tir
    te = cg.validate(tir, CONTRACTS, GATES, AGENTS)
    report(f"shipped {os.path.basename(t)} still compiles",
           not te, "valid" if not te else te[0][:44])


def _bound(tir, agent):
    """Nodes in an IR that resolve (via role or agentType) to `agent`."""
    roles = tir.get("roles") or {}
    out = []
    for n in tir.get("nodes") or []:
        want = n.get("agentType") or (roles.get(n.get("role")) or {}).get("agentType")
        if want == agent:
            out.append(n)
    return out


# The failure mode itself, pinned: a template that binds the security
# reviewer and leaves the verification reviewer unbound is #72 again.
for name, tir in TEMPLATES.items():
    # The terminal shipping gate needs the proof gate. Repair-loop checkers
    # bind the reviewer to independently reproduce a slice between fixes.
    rt = [n for n in _bound(tir, "red-team-reviewer") if n.get("id") == "red-team"]
    if not rt:
        continue
    pa = _bound(tir, "proof-auditor")
    report(f"{name}: red-team bound implies proof-audit bound", bool(pa),
           f"{len(pa)} proof node(s)")
    report(f"{name}: the proof node is contracted to ProofV1 and halts on WEAKENED",
           all(n.get("contract") == "ProofV1" and n.get("haltWhen") == "verdict == 'WEAKENED'"
               for n in pa) and bool(pa), "halts on WEAKENED")
    order = [n["id"] for n in tir["nodes"]]
    report(f"{name}: proof-audit runs before red-team closes",
           all(order.index(p["id"]) < order.index(r["id"]) for p in pa for r in rt),
           "ordered")

verified = TEMPLATES.get("WORK.verified.md") or {}
pv = _bound(verified, "prover")
report("WORK.verified.md binds a prover node that mutates",
       bool(pv) and all(n.get("mutates") for n in pv), f"{len(pv)} prover node(s)")
report("and an independent node verifies it",
       bool(pv) and all(any(o.get("independent") and o.get("verifies") == n["id"]
                            for o in verified.get("nodes", [])) for n in pv), "verified")

# ===== the plugin namespace survives a real install ======================
# load_agents took the qualified prefix from basename(plugin_dir). In this repo
# that is "fluxpoint" and every assertion above passes. A real install lives at
# ~/.claude/plugins/cache/fluxpoint/fluxpoint/<VERSION>/, so the prefix became
# the VERSION STRING -- and `fluxpoint:graph-auditor`, the exact binding the
# reported incident used, resolved to nothing and was accepted in silence. The
# feature was green in the repo and inert everywhere it ships.
with tempfile.TemporaryDirectory() as _d:
    _inst = os.path.join(_d, "cache", "fluxpoint", "fluxpoint", "9.9.9")
    shutil.copytree(PLUGIN, _inst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    _spec = importlib.util.spec_from_file_location(
        "cg_installed", os.path.join(_inst, "scripts", "compile-graph.py"))
    _cg = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_cg)
    _agents = _cg.load_agents(os.path.dirname(_inst))
    _ns = sorted({k.split(":")[0] for k in _agents if ":" in k})
    report("an installed plugin still qualifies agents as 'fluxpoint'",
           _ns == ["fluxpoint"], ",".join(_ns) or "NONE")

    _ir = {"version": 1, "campaign": "c", "budget": {"maxNodes": 5},
           "nodes": [{"id": "gate", "agentType": "fluxpoint:graph-auditor",
                      "contract": "RedTeamV1", "prompt": "judge"}]}
    _hit = [f for f in _cg.validate(_ir, CONTRACTS, None, _agents)
            if "answers in a report" in f]
    report("and the qualified binding is still rejected from an install",
           bool(_hit), "rejected" if _hit else "ACCEPTED — the gate never fires")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
