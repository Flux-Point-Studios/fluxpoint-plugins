#!/usr/bin/env python3
"""A SHIP over a CRITICAL finding cannot pass the gate (issue #90).

`haltWhen` compares one top-level field to a literal and cannot reach into
an array, and the shipped RedTeamV1 carried severity only inside
`findings[]`. So `{verdict: SHIP, findings: [{severity: CRITICAL, ...}]}`
was contract-valid, and a graph halting on `verdict == 'BLOCK'` sent it on
— typically to a person about to sign. The contract now carries
`worstSeverity` and `worstSeverityRank`, bound to the findings and to the
verdict so none of them can disagree; the compiled graph and record-run.py
re-derive the same answer from the findings, in case a validator skips the
conditional keywords.

Executed three ways: the schema against every small combination of
findings, verdict and declared severity (by a validator for exactly the
keywords the schema uses, checked against a reference predicate written
independently of it); the compiled graph under stubs; and record-run.py
over a real summary.
"""
import copy
import itertools
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "compile_graph", os.path.join(PLUGIN, "scripts", "compile-graph.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(os.path.join(PLUGIN, "contracts"))
SCHEMA = CONTRACTS["RedTeamV1"]
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<62} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


# ==================== a validator for exactly these keywords ==============
# Standard library only, like the rest of the plugin. It refuses a keyword
# it does not implement, so a schema edit that reaches for a new one fails
# here instead of being silently skipped — the same failure it guards.
KNOWN = {"$id", "description", "type", "required", "properties", "items",
         "contains", "maxItems", "enum", "const", "minimum", "maximum",
         "allOf", "if", "then"}
TYPES = {"object": dict, "array": list, "string": str}


def valid(inst, sch):
    unknown = set(sch) - KNOWN
    if unknown:
        raise ValueError(f"validator does not implement {sorted(unknown)}")
    t = sch.get("type")
    if t == "integer":
        if not (isinstance(inst, int) and not isinstance(inst, bool)):
            return False
    elif t and not isinstance(inst, TYPES[t]):
        return False
    if isinstance(inst, dict):
        if any(k not in inst for k in sch.get("required", [])):
            return False
        for k, sub in (sch.get("properties") or {}).items():
            if k in inst and not valid(inst[k], sub):
                return False
    if isinstance(inst, list):
        if "items" in sch and not all(valid(x, sch["items"]) for x in inst):
            return False
        if "contains" in sch and not any(valid(x, sch["contains"]) for x in inst):
            return False
        if "maxItems" in sch and len(inst) > sch["maxItems"]:
            return False
    if "enum" in sch and inst not in sch["enum"]:
        return False
    if "const" in sch and (inst != sch["const"] or type(inst) is not type(sch["const"])):
        return False
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in sch and inst < sch["minimum"]:
            return False
        if "maximum" in sch and inst > sch["maximum"]:
            return False
    if not all(valid(inst, s) for s in sch.get("allOf", [])):
        return False
    if "if" in sch and valid(inst, sch["if"]) and not valid(inst, sch.get("then", {})):
        return False
    return True


RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def finding(sev):
    return {"severity": sev, "finding": "f", "exploit_path": "p", "minimal_fix": "m"}


def coherent(inst):
    """The rule, written without reference to the schema."""
    worst = max((RANK[f["severity"]] for f in inst["findings"]), default=0)
    return (RANK[inst["worstSeverity"]] == worst and inst["worstSeverityRank"] == worst
            and not (inst["verdict"] == "SHIP" and worst >= 3))


# ==================== the contract ========================================
report("RedTeamV1 now requires worstSeverity and worstSeverityRank",
       {"worstSeverity", "worstSeverityRank"} <= set(SCHEMA["required"]),
       str(SCHEMA["required"]))
the_hole = {"verdict": "SHIP", "findings": [finding("CRITICAL")]}
report("the issue's object — SHIP over a CRITICAL — is no longer valid",
       not valid(the_hole, SCHEMA), "refused")
for ws in RANK:
    dressed = dict(the_hole, worstSeverity=ws, worstSeverityRank=RANK[ws])
    if valid(dressed, SCHEMA):
        report("  whatever severity it declares", False, f"accepted as {ws}")
        break
else:
    report("  whatever severity it declares", True, "refused under all five")
report("BLOCK over a CRITICAL, declared CRITICAL/4, is valid",
       valid({"verdict": "BLOCK", "findings": [finding("CRITICAL")],
              "worstSeverity": "CRITICAL", "worstSeverityRank": 4}, SCHEMA), "valid")
report("BLOCK with no findings (incomplete coverage) is valid as NONE/0",
       valid({"verdict": "BLOCK", "findings": [], "worstSeverity": "NONE",
              "worstSeverityRank": 0}, SCHEMA), "valid")
report("SHIP over a MEDIUM, declared MEDIUM/2, is valid",
       valid({"verdict": "SHIP", "findings": [finding("LOW"), finding("MEDIUM")],
              "worstSeverity": "MEDIUM", "worstSeverityRank": 2}, SCHEMA), "valid")
report("a rank that disagrees with worstSeverity is refused",
       not valid({"verdict": "BLOCK", "findings": [finding("HIGH")],
                  "worstSeverity": "HIGH", "worstSeverityRank": 2}, SCHEMA), "refused")
report("a severity that understates the findings is refused",
       not valid({"verdict": "BLOCK", "findings": [finding("HIGH"), finding("LOW")],
                  "worstSeverity": "LOW", "worstSeverityRank": 1}, SCHEMA), "refused")
report("a severity no finding carries is refused",
       not valid({"verdict": "BLOCK", "findings": [finding("LOW")],
                  "worstSeverity": "HIGH", "worstSeverityRank": 3}, SCHEMA), "refused")
report("the review object the reviewer adds is still allowed",
       valid({"verdict": "BLOCK", "findings": [], "worstSeverity": "NONE",
              "worstSeverityRank": 0, "review": {"coverage": []}}, SCHEMA), "valid")

wrong = []
total = 0
for n in range(3):
    for combo in itertools.product(["LOW", "MEDIUM", "HIGH", "CRITICAL"], repeat=n):
        for verdict in ("SHIP", "BLOCK"):
            for ws in RANK:
                for rank in range(5):
                    inst = {"verdict": verdict, "findings": [finding(c) for c in combo],
                            "worstSeverity": ws, "worstSeverityRank": rank}
                    total += 1
                    if valid(inst, SCHEMA) != coherent(inst):
                        wrong.append(inst)
report(f"the schema accepts exactly the coherent objects ({total} cases)",
       not wrong, f"{len(wrong)} disagree" + (f", first {wrong[0]}" if wrong else ""))

# ==================== the gate can read it ================================
gate = {"version": 1, "campaign": "c", "treeGuard": False, "budget": {"maxNodes": 4},
        "nodes": [{"id": "rt", "prompt": "red-team the diff", "contract": "RedTeamV1",
                   "haltWhen": "worstSeverityRank >= 3"}]}
report("haltWhen can threshold on worstSeverityRank",
       not cg.validate(gate, CONTRACTS), str(cg.validate(gate, CONTRACTS))[:60])


# ==================== the compiled graph re-derives it ====================
def run_graph(rt_result):
    ir = {"version": 1, "campaign": "c", "treeGuard": False, "budget": {"maxNodes": 4},
          "nodes": [{"id": "rt", "prompt": "red-team the diff", "contract": "RedTeamV1",
                     "haltWhen": "verdict == 'BLOCK'"},
                    {"id": "sign", "after": "rt", "prompt": "prepare the signature for {{prev.verdict}}",
                     "contract": "DesignV1"}]}
    js = cg.emit(ir, CONTRACTS, {})
    with tempfile.TemporaryDirectory() as d:
        out, w = os.path.join(d, "o.json"), os.path.join(d, "w.mjs")
        with open(w, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join([
                "import {writeFileSync} from 'node:fs';",
                "const LABELS=[];",
                f"const RT={json.dumps(rt_result)};",
                "const agent=async(p,o)=>{LABELS.push(String(o.label));",
                "  return String(o.label).includes('rt') ? RT : {summary:'s',plan:'p',files:[],risks:[]}};",
                "const parallel=async(t)=>Promise.all(t.map(f=>f()));",
                "const pipeline=async()=>[],log=()=>{},phase=()=>{};",
                "const args={};",
                "const budget={total:null,spent:()=>0,remaining:()=>1e9};",
                "const workflow=0;",
                "(async () => {", js.replace("export const meta", "const meta"),
                "})().then(r=>writeFileSync(" + json.dumps(out) + ",JSON.stringify({r, LABELS})))",
                ".catch(e=>writeFileSync(" + json.dumps(out)
                + ",JSON.stringify({err:String(e&&e.message||e)})))"]))
        subprocess.run(["node", w], capture_output=True, timeout=60)
        return json.load(open(out)) if os.path.exists(out) else {"err": "no output"}


lied = run_graph({"verdict": "SHIP", "findings": [finding("CRITICAL")],
                  "worstSeverity": "NONE", "worstSeverityRank": 0})
report("in the run, a SHIP over a CRITICAL halts before the next node",
       (lied.get("r") or {}).get("outcome") == "HALTED" and "sign" not in lied.get("LABELS", []),
       str(lied)[:80])
fine = run_graph({"verdict": "SHIP", "findings": [finding("LOW")],
                  "worstSeverity": "LOW", "worstSeverityRank": 1})
report("  while a coherent SHIP proceeds",
       (fine.get("r") or {}).get("outcome") == "COMPLETE" and "sign" in fine.get("LABELS", []),
       str(fine)[:80])
legacy = run_graph({"verdict": "SHIP", "findings": [finding("MEDIUM")]})
report("  and a repo-local RedTeamV1 without the ordinal is held to the SHIP rule only",
       (legacy.get("r") or {}).get("outcome") == "COMPLETE", str(legacy)[:80])

# ==================== record-run refuses to file it as shippable ==========
with tempfile.TemporaryDirectory() as d:
    work = os.path.join(d, "WORK.md")
    with open(os.path.join(PLUGIN, "templates", "WORK.md"), encoding="utf-8") as fh:
        src = fh.read().replace("SPEC: .fluxpoint-spec.json\n", "")
    with open(work, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src)
    summary = {"campaign": "c", "outcome": "COMPLETE", "contracts": {"rt": "RedTeamV1"},
               "results": {"rt": {"verdict": "SHIP", "findings": [finding("CRITICAL")],
                                  "worstSeverity": "NONE", "worstSeverityRank": 0}},
               "provenance": [{"node": "rt", "status": "OK"}]}
    r = subprocess.run([sys.executable, os.path.join(PLUGIN, "scripts", "record-run.py"),
                        "--run-id", "wf_rt", "--graph", work, "--root", d,
                        "--state-dir", os.path.join(d, ".claude", "fluxpoint", "runs")],
                       input=json.dumps(summary), capture_output=True, text=True, cwd=d,
                       env=dict(os.environ, FPL_MEMORY_INDEX="0"))
    text = open(work, encoding="utf-8").read()
report("record-run files a SHIP over a CRITICAL as BLOCKED-REDTEAM",
       "| wf_rt | BLOCKED-REDTEAM |" in text, (r.stderr or r.stdout).strip()[-80:])
report("  and says why in the row", "contradict its verdict" in text
       and "SHIP over a CRITICAL finding" in text, "named")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
