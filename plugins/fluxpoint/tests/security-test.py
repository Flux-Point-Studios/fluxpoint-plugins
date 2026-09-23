#!/usr/bin/env python3
"""Regression tests for the red-team findings against v1.3.0.

The compiler turns user-authored JSON into JavaScript that is then executed,
which makes every interpolated field an injection surface. Two of these were
driven to real code execution before they were fixed: `haltReason` was pasted
straight into a template literal, and a `lists` key became a bare JS
identifier. `js_template` already escaped prompts, which is what made the gap
so easy to miss — the authors clearly knew WORK.md was untrusted input and
still left two fields raw.

Each case below re-runs an exploit or failure path that was confirmed real.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "compile_graph", os.path.join(PLUGIN, "scripts", "compile-graph.py")
)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(os.path.join(PLUGIN, "contracts"))

passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def rejected(name, ir, needle):
    errs = cg.validate(ir, CONTRACTS)
    hit = any(needle in e for e in errs)
    report(name, hit, "rejected at compile" if hit else f"ACCEPTED (got {errs})")


def node(**kw):
    n = {"id": "n", "phase": "P", "prompt": "go", "contract": "FindingsV1"}
    n.update(kw)
    return {"version": 1, "name": "t", "campaign": "a campaign for security probes",
            "budget": {"maxNodes": 8}, "nodes": [n]}


def emits_inert(name, ir, payload_marker, resolved=None):
    """Compile, then run the emitted JS under stubs; the payload must not fire."""
    with tempfile.TemporaryDirectory() as d:
        js = cg.emit(ir, CONTRACTS, resolved)
        canary = os.path.join(d, "PWNED")
        js = js.replace("__CANARY__", canary)
        wrapped = os.path.join(d, "w.mjs")
        with open(wrapped, "w") as fh:
            fh.write(
                "const agent=async()=>({exit:1,command:'x',findings:[]}),"
                "parallel=async()=>[],pipeline=async()=>[],log=()=>{},phase=()=>{},"
                "args={},budget={total:null,remaining:()=>1e9,spent:()=>0},workflow=0;\n"
                "(async () => {\n"
                + js.replace("export const meta", "const meta")
                + "\n})()\n"
            )
        syntax = subprocess.run(["node", "--check", wrapped], capture_output=True)
        if syntax.returncode != 0:
            report(name, False, "emitted JS does not even parse")
            return
        subprocess.run(["node", wrapped], capture_output=True, timeout=30)
        fired = os.path.exists(canary)
        report(name, not fired, "payload inert" if not fired else "PAYLOAD EXECUTED")


# --- HIGH: haltReason was pasted raw into a template literal (proven RCE) ---
emits_inert(
    "haltReason cannot break out of its string",
    node(contract="HarnessCheckV1", haltWhen="exit != 0",
         haltReason="`);await import('node:fs').then(m=>m.writeFileSync('__CANARY__','x'));log(`"),
    "__CANARY__",
)

# --- HIGH: a lists key became a bare JS identifier (proven RCE) -------------
evil_key = "unused = 0; await import('node:fs').then(m=>m.writeFileSync('__CANARY__','x')); const dummy"
ir = node()
ir["lists"] = {evil_key: [{"key": "a"}]}
rejected("lists key must be an identifier", ir, "emitted as a JS identifier")
for bad in ("Upper", "has space", "semi;colon", "1leading", "dot.key"):
    ir = node()
    ir["lists"] = {bad: [{"key": "a"}]}
    rejected(f"lists key rejected: {bad!r}", ir, "must be lowercase kebab-case")

# --- MEDIUM: a crafted haltWhen literal emitted unparseable JS -------------
emits_inert(
    "crafted haltWhen literal stays inside its string",
    node(contract="HarnessCheckV1", haltWhen="command == 'abc\\'"),
    "__CANARY__",
)
emits_inert(
    "backtick in a haltWhen literal is escaped",
    node(contract="HarnessCheckV1", haltWhen="command == '`+x+`'"),
    "__CANARY__",
)

# --- LOW: an unbound {{token}} compiled clean and threw at launch ----------
rejected("unbound {{item}} without foreach", node(prompt="audit {{item.brief}}"),
         "not in scope for this node")
rejected("unbound {{seen}} without repeat", node(prompt="skip {{seen}}"),
         "declares no 'repeat'")

# Bindings that ARE in scope must still compile.
ok_ir = node(foreach="dims", prompt="audit {{item.brief}} for {{A.target}}")
ok_ir["lists"] = {"dims": [{"key": "a", "brief": "b"}]}
errs = cg.validate(ok_ir, CONTRACTS)
report("in-scope tokens still compile", not errs, "accepted" if not errs else str(errs))

# --- MEDIUM: ceiling-declined refuters were laundered into "survived" ------
disc = node(verify="panel:3", verifyOver="findings", expectItems=2)
disc["budget"] = {"maxNodes": 40}
js = cg.emit(disc, CONTRACTS)
report("a short panel marks the item unverified",
       "unverified: cast.length < n," in js,
       "tagged" if "unverified: cast.length < n," in js else "MISSING")
report("survival is decided by the majority alone",
       "filter(v => v.kills < need)" in js and "v.unverified || v.kills" not in js,
       "kills-only" if "v.unverified || v.kills" not in js else "unverified still grants survival")

# --- Prompts were already escaped; keep it that way ------------------------
emits_inert(
    "prompt cannot break out of its template literal",
    node(prompt="hi `);await import('node:fs').then(m=>m.writeFileSync('__CANARY__','x'));log(`"),
    "__CANARY__",
)
emits_inert(
    "list item values cannot break out",
    (lambda: (lambda i: (i.update(lists={"dims": [
        {"key": "a", "brief": "`);await import('node:fs').then(m=>m.writeFileSync('__CANARY__','x'));log(`"}]}),
        i["nodes"][0].update(foreach="dims", prompt="do {{item.brief}}"), i)[-1])(node()))(),
    "__CANARY__",
)

# --- Imported decision records are embedded at compile time ----------------
# The record comes from a run artifact on disk — repo-writable state, so a
# crafted one is untrusted input to codegen exactly like WORK.md is. It is
# embedded via json.dumps (ASCII-only, JS-compatible escaping); this pins
# that a hostile record stays data in the object literal and in the prompt.
_evil = "`);await import('node:fs').then(m=>m.writeFileSync('__CANARY__','x'));(`"
with tempfile.TemporaryDirectory() as _runs:
    _rec = {
        "question": "a question long enough to satisfy the schema floor?",
        # A valid DecisionV1 (two options, the floors met) carrying the
        # payload in every free-text field: imports validate the record, so
        # the hostile one must still be a decision to reach emission.
        "options": [{"option": _evil, "argued_by": _evil,
                     "strongest_objection": "an objection with real length"},
                    {"option": "${process.exit(2)}", "argued_by": _evil,
                     "strongest_objection": "a second objection with real length"}],
        "chosen": _evil,
        "rationale": "a rationale long enough that a lazy output cannot "
                     "satisfy it, carrying the payload elsewhere",
        "overturned_prior": False, "frozen_by": _evil, "reversible": False,
        "evidence": [_evil, "${process.exit(1)}", "  */ // <!--"],
    }
    with open(os.path.join(_runs, "wf-evil.json"), "w", encoding="utf-8") as fh:
        json.dump({"runId": "wf-evil", "when": "2026-08-08 00:00",
                   "summary": {"decisions": {"vault-params": _rec}}}, fh)
    _ir = node(honors=["vault-params"],
               prompt="build under the frozen {{decisions.vault-params}}")
    _ir["imports"] = {"vault-params": "latest"}
    _resolved, _errs = cg.resolve_imports(_ir, CONTRACTS, _runs)
    report("the hostile record still resolves (it is data, not policy)",
           not _errs and "vault-params" in _resolved,
           "resolved" if not _errs else str(_errs)[:40])
    emits_inert("a hostile decision record embeds inert", _ir, "__CANARY__",
                resolved=_resolved)

# --- reduce ops are validated before they are an interpolation surface ------
# dedupeBy/sortBy must name declared item fields, so an arbitrary string can
# only reach emission by BEING one — repo-committed contract schemas, not
# WORK.md. Over schema-less items (SliceV1.testsAdded, bare strings) any key
# is rejected outright: it could not be validated and would collapse
# distinct items to one at run time.
_evil_key = "\"+(()=>{require('fs').writeFileSync('__CANARY__','x')})()+\""
_evil_tpl = "`);require('fs').writeFileSync('__CANARY__','x');(`"
_red_ir = {
    "version": 1, "name": "t", "campaign": "a campaign for security probes",
    "budget": {"maxNodes": 8},
    "nodes": [
        {"id": "src", "phase": "P", "prompt": "go", "contract": "SliceV1"},
        {"id": "cut", "phase": "R",
         "reduce": {"from": "src", "over": "testsAdded",
                    "dedupeBy": [_evil_key, _evil_tpl],
                    "sortBy": _evil_tpl, "order": "desc", "topK": 1}},
    ],
}
rejected("hostile reduce keys never reach emission (schema-less items)",
         _red_ir, "declare no fields")

_red_ir2 = {
    "version": 1, "name": "t", "campaign": "a campaign for security probes",
    "budget": {"maxNodes": 8},
    "nodes": [
        {"id": "src", "phase": "P", "prompt": "go", "contract": "FindingsV1"},
        {"id": "cut", "phase": "R",
         "reduce": {"from": "src", "over": "findings",
                    "dedupeBy": [_evil_key], "sortBy": _evil_tpl}},
    ],
}
rejected("hostile reduce keys never reach emission (schema'd items)",
         _red_ir2, "is not a field")

rejected(
    "a hostile reduce.over never reaches emission",
    {**_red_ir, "nodes": [_red_ir["nodes"][0],
                          {"id": "cut", "phase": "R",
                           "reduce": {"from": "src",
                                      "over": "x\"];require('fs')//",
                                      "dedupeBy": ["k"]}}]},
    "is not a field",
)


# A haltWhen literal is regex-checked and json.dumps'd, which closes quote
# termination -- but emit_halt_any drops it inside a BACKTICK template, where
# ${ interpolates, so the literal reached executable position through the very
# log line that reported the halt. This probe printed EXECUTED before js_tmpl.
def halt_literal_cannot_interpolate():
    name = "haltWhen literal cannot interpolate into the halt log"
    nl = chr(10)
    n = {"id": "probe", "haltReason": "x",
         "haltWhen": "exit != '${(globalThis.__PWN=1)}'"}
    js = cg.emit_halt_any(n, "v")
    wrapper = nl.join([
        "const log=()=>{},note=()=>{},summary=()=>0,RESULTS={};",
        "const v=[{exit:1}];", "function run(){", js, "}",
        "try{run()}catch(e){}",
        "console.log(globalThis.__PWN?'EXECUTED':'INERT')", ""])
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "p.mjs")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(wrapper)
        if subprocess.run(["node", "--check", f], capture_output=True).returncode:
            report(name, False, "emitted JS does not parse")
            return
        out = subprocess.run(["node", f], capture_output=True, text=True).stdout
    report(name, "INERT" in out, "payload inert" if "INERT" in out
           else "PAYLOAD EXECUTED -- template-literal injection")


halt_literal_cannot_interpolate()


# memory.priors routes RUNTIME data — killed-lesson claims and objections
# loaded from args._seen — through the PRIORS template literal and into
# refuter prompts. Unlike the compile-time fields above, this payload
# arrives after codegen, so the property under test is different: the data
# must flow through String() inside an already-built literal, never through
# source. A store row is agent-influenced (a finder's claim text becomes a
# lesson), which makes this the same trust boundary WORK.md is.
def priors_payload_stays_data():
    name = "hostile killed-lesson text cannot escape the priors literal"
    ir = {
        "version": 1, "name": "probe",
        "campaign": "probe hostile priors data staying inert",
        "budget": {"maxNodes": 80},
        "nodes": [{
            "id": "find", "phase": "Find", "contract": "FindingsV1",
            "prompt": "find; seen: {{seen}}",
            "verify": "panel:1", "verifyOver": "findings",
            "repeat": {"untilDryRounds": 1, "maxRounds": 2,
                       "dedupeBy": ["file", "claim"]},
            "memory": {"seed": "audit", "emit": "audit", "priors": True,
                       "classBy": None},
        }],
    }
    errs = cg.validate(ir, CONTRACTS)
    if errs:
        report(name, False, f"probe IR rejected: {errs[:1]}")
        return
    js = cg.emit(ir, CONTRACTS, {})
    evil = ("`+(()=>{globalThis.__PWN=1;return ''})()+`"
            " ${(globalThis.__PWN=1)} \\` trailing \\")
    seen = {"audit": {"keys": ["k1"],
                      "killed": [{"claim": evil, "objection": evil}]}}
    finding = {"file": "a.ts", "line": 1, "severity": "LOW",
               "claim": "a legitimate finding that triggers one refute call",
               "failure_path": "probe only"}
    nl = chr(10)
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "o.json")
        w = os.path.join(d, "w.mjs")
        with open(w, "w", encoding="utf-8") as fh:
            fh.write(nl.join([
                "import {writeFileSync} from 'node:fs';",
                "const PROMPTS=[]; let round=0;",
                "const agent=async(p,o)=>{PROMPTS.push(p);",
                "  if(String(o.label||'').includes('tree-check'))",
                "    return {head:'abc123', porcelain:''};",
                "  if(String(o.label||'').includes('refute'))",
                "    return {refuted:false, reason:'no'};",
                f"  return {{findings: round++ ? [] : [{json.dumps(finding)}]}}}};",
                "const parallel=async(t)=>Promise.all(t.map(f=>f()));",
                "const pipeline=async()=>[],log=()=>{},phase=()=>{};",
                f"const args={json.dumps({'_seen': seen})};",
                "const budget={total:null,spent:()=>0,remaining:()=>1e9};",
                "const workflow=0;",
                "(async () => {",
                js.replace("export const meta", "const meta"),
                "})().then(()=>{",
                "  const verdict = globalThis.__PWN ? 'EXECUTED'",
                "    : PROMPTS.some(p=>p.includes('trailing')) ? 'INERT'",
                "    : 'PAYLOAD LOST';",
                f"  writeFileSync({json.dumps(out)}, verdict)",
                "}).catch(()=>{",
                f"  writeFileSync({json.dumps(out)}, 'RUNTIME ERROR')",
                "})", ""]))
        if subprocess.run(["node", "--check", w], capture_output=True).returncode:
            report(name, False, "emitted JS does not parse")
            return
        subprocess.run(["node", w], capture_output=True, timeout=60)
        verdict = open(out).read() if os.path.exists(out) else "NO VERDICT"
    report(name, verdict == "INERT",
           "payload arrived verbatim as data" if verdict == "INERT"
           else verdict)


priors_payload_stays_data()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
