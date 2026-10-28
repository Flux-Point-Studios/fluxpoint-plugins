#!/usr/bin/env python3
"""The proof-auditor's verdict reaches the record (issue #72).

"Treat WEAKENED as harness-red" was a sentence in a command file. Nothing
typed it, no graph node could bind the agent that says it, and the
Evidence row had no cell for it — so a feature campaign could run build ->
red-team -> green with no proof ever audited and the table read clean.

ProofV1 makes the verdict a contract; this pins what record-run.py does
with one, executed against the real script:
  - WEAKENED files the run BLOCKED-PROOF, and a flag cannot talk it back
  - UNPROVEN files it INCOMPLETE: the declared verification did not run
  - NOT-APPLICABLE leaves the outcome alone and says why in the Proof cell
  - SOUND over an empty surface is named as vacuous, never as reviewed
  - a run recorded before contracts were emitted still derives the verdict
  - the shipped feature template carries the unified header the row needs
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
RECORD = os.path.join(PLUGIN, "scripts", "record-run.py")
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<58} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def record(summary, run_id, *flags, template="WORK.md"):
    """Run record-run.py in a fresh repo carrying the named template.

    Returns (WORK.md text, run artifact dict, stderr)."""
    with tempfile.TemporaryDirectory() as d:
        work = os.path.join(d, "WORK.md")
        with open(os.path.join(PLUGIN, "templates", template), encoding="utf-8") as fh:
            src = fh.read()
        # These historical summaries predate requirement packets.
        src = src.replace("SPEC: .fluxpoint-spec.json\n", "")
        with open(work, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src)
        r = subprocess.run(
            [sys.executable, RECORD, "--run-id", run_id, "--graph", work,
             "--root", d, "--state-dir", os.path.join(d, ".claude", "fluxpoint", "runs"),
             *flags],
            input=json.dumps(summary), capture_output=True, text=True, cwd=d,
            env=dict(os.environ, FPL_MEMORY_INDEX="0"))
        text = open(work, encoding="utf-8").read()
        art_path = os.path.join(d, ".claude", "fluxpoint", "runs", f"{run_id}.json")
        art = json.load(open(art_path, encoding="utf-8")) if os.path.exists(art_path) else {}
        return text, art, r.stderr


def summary(verdict, surface, **extra):
    s = {"campaign": "c", "outcome": "COMPLETE",
         "contracts": {"gate": "HarnessCheckV1", "pa": "ProofV1", "rt": "RedTeamV1"},
         "results": {"gate": {"exit": 0, "command": "scripts/harness.sh --full", "tail": ""},
                     "pa": {"verdict": verdict, "surface": surface, "checker": "dafny verify .",
                            "findings": []},
                     "rt": {"verdict": "SHIP", "findings": [], "coverage": {
                         "subject": [{"repository": "fixture", "base": "a" * 40, "head": "b" * 40,
                                      "files": 1, "additions": 1, "deletions": 0}],
                         "attackers": 6, "lenses": ["authority", "value", "state", "input", "operations", "composition"],
                         "sampled": False}}},
         "provenance": [{"node": "gate", "status": "OK"}, {"node": "pa", "status": "OK"},
                        {"node": "rt", "status": "OK"}]}
    s.update(extra)
    return s


# ==================== WEAKENED is harness-red, mechanically ==============
text, art, _ = record(summary("WEAKENED", ["dafny:src/vault.dfy:Withdraw"]), "wf_p1")
report("WEAKENED files the run BLOCKED-PROOF", "| wf_p1 | BLOCKED-PROOF |" in text,
       "BLOCKED-PROOF" if "BLOCKED-PROOF" in text else "outcome untouched")
report("and names it in the claim", "proof-audit returned WEAKENED" in text, "named")
report("and in the Proof cell with the surface size",
       "proof-audit WEAKENED (1 surface item(s))" in text, "cell")
report("and in the run artifact", art.get("proofAudit") == "WEAKENED", str(art.get("proofAudit")))

text, art, _ = record(summary("WEAKENED", ["x"]), "wf_p2", "--proof-audit", "SOUND")
report("a SOUND flag cannot override a WEAKENED in the run",
       "| wf_p2 | BLOCKED-PROOF |" in text, "derived wins")

# A red-team BLOCK and a WEAKENED proof in one run: the red-team block is
# the outcome (it came first in the vocabulary), and both are in the claim.
s = summary("WEAKENED", ["x"])
s["results"]["rt"]["verdict"] = "BLOCK"
text, art, _ = record(s, "wf_p3")
report("BLOCK and WEAKENED together: outcome is BLOCKED-REDTEAM",
       "| wf_p3 | BLOCKED-REDTEAM |" in text, "red-team first")
report("  and the claim carries both",
       "red-team returned BLOCK" in text and "proof-audit returned WEAKENED" in text, "both")

s = summary("SOUND", ["access.py"])
s["results"]["rt"] = {"verdict": "BLOCK", "findings": [],
                      "review": {"blockers": ["No authority attack executed"], "coverage": []}}
text, art, _ = record(s, "wf_rt_incomplete", "--red-team", "SHIP")
report("incomplete red-team coverage blocks with no fabricated finding",
       art.get("outcome") == "BLOCKED-REDTEAM", "derived BLOCK wins over SHIP flag")
report("additional red-team review evidence survives recording",
       art.get("summary", {}).get("results", {}).get("rt") == s["results"]["rt"], "review retained")

# ==================== UNPROVEN is a verification that did not run ========
text, art, _ = record(summary("UNPROVEN", ["src/vault.dfy"]), "wf_p4")
report("UNPROVEN files the run INCOMPLETE", "| wf_p4 | INCOMPLETE |" in text, "INCOMPLETE")
report("and says no checker ran", "no checker ran" in text, "named")

# ==================== NOT-APPLICABLE is not a verdict about proofs ========
text, art, _ = record(summary("NOT-APPLICABLE", []), "wf_p5")
report("NOT-APPLICABLE leaves the outcome alone", "| wf_p5 | COMPLETE |" in text, "COMPLETE")
report("and the Proof cell says why", "proof-audit n/a (no proof surface)" in text, "cell")

# ==================== SOUND, and SOUND over nothing =======================
text, art, _ = record(summary("SOUND", ["a.dfy", "b.dfy", "aiken:lib/x.ak:prop"]), "wf_p6")
report("SOUND over a surface is filed as reviewed",
       "proof-audit SOUND (3 surface item(s))" in text and "| wf_p6 | COMPLETE |" in text,
       "3 items")
report("  with nothing suspicious in the claim", "vacuous" not in text, "clean")
text, art, _ = record(summary("SOUND", []), "wf_p7")
report("SOUND over an EMPTY surface is called vacuous",
       "SOUND over an EMPTY surface" in text and "vacuous" in text, "named")
report("  and still recorded as the verdict it was", art.get("proofAudit") == "SOUND", "SOUND")

# ==================== the worst of several proof nodes is the answer =====
s = summary("SOUND", ["a"])
s["contracts"]["pb"] = "ProofV1"
s["results"]["pb"] = {"verdict": "WEAKENED", "surface": ["b"], "findings": []}
text, art, _ = record(s, "wf_p8")
report("two proof nodes: WEAKENED beats SOUND", "| wf_p8 | BLOCKED-PROOF |" in text, "worst wins")

# ==================== a pre-contract run still derives from shape ========
old = {"campaign": "c", "outcome": "COMPLETE",
       "results": {"pa": {"verdict": "WEAKENED", "surface": ["a"], "findings": []}},
       "provenance": [{"node": "pa", "status": "OK"}]}
text, art, _ = record(old, "wf_p9")
report("no contracts map: the verdict is read from its shape",
       "| wf_p9 | BLOCKED-PROOF |" in text, "derived")

# ==================== no proof node: the flag is the fallback ============
plain = {"campaign": "c", "outcome": "COMPLETE", "results": {}, "provenance": []}
text, art, _ = record(plain, "wf_p10", "--proof-audit", "SOUND")
report("no proof node: the flag is used", "proof-audit SOUND;" in text, "flag")
text, art, _ = record(plain, "wf_p11")
report("no proof node, no flag: n/a", "proof-audit n/a;" in text, "n/a")

# ==================== the feature template carries the unified header ====
feat = open(os.path.join(PLUGIN, "templates", "WORK.feature.md"), encoding="utf-8").read()
report("WORK.feature.md uses the unified Evidence header",
       "| When (UTC) | Source | Outcome | Claim | Proof |" in feat
       and "Nodes OK/dead" not in feat, "unified")
text, art, _ = record(summary("WEAKENED", ["x"]), "wf_p12", template="WORK.feature.md")
report("and the row lands under it with the verdict",
       "| wf_p12 | BLOCKED-PROOF |" in text and "proof-audit WEAKENED" in text, "landed")

# ==================== the docs promise what the code does ================
cmd = open(os.path.join(PLUGIN, "commands", "proof-audit.md"), encoding="utf-8").read()
report("proof-audit.md names the graph node that runs it", "ProofV1" in cmd, "named")
agent = open(os.path.join(PLUGIN, "agents", "proof-auditor.md"), encoding="utf-8").read()
report("proof-auditor.md tells the agent NOT-APPLICABLE is not SOUND",
       "NOT-APPLICABLE" in agent and "vacuous" in agent, "stated")
report("and to experiment in a worktree, never the shared checkout",
       "worktree" in agent and "TREE-MOVED" in agent, "stated")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
