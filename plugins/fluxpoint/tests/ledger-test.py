#!/usr/bin/env python3
"""Once-only ledger: the guard is executed, not inspected.

The failure this exists to prevent is specific and expensive. The recovery
path the graph-engineering skill actively prescribes — repair one node,
resume, replay the unchanged prefix — re-runs every node after the repair.
If one of those performs an unrepeatable effect, a targeted repair mints
twice. Grepping the emitted source for a guard would not prove the guard
runs, so every case here compiles the graph and executes it under stubs,
counting the agent calls that actually happened.
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

LEDGER_PY = os.path.join(PLUGIN, "scripts", "ledger.py")
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


IR = {
    "version": 1,
    "name": "ceremony",
    "campaign": "one irreversible mainnet genesis that must be the last migration",
    "budget": {"maxNodes": 8},
    "requiredArgs": ["confirm"],
    "nodes": [
        {"id": "dry-run", "phase": "Rehearse",
         "prompt": "build the genesis tx without submitting it",
         "contract": "HarnessCheckV1"},
        {"id": "check", "phase": "Rehearse", "after": "dry-run",
         "prompt": "independently re-derive the tx from {{prev}} and report the exit code",
         "contract": "HarnessCheckV1", "independent": True, "verifies": "dry-run",
         "haltWhen": "exit != 0"},
        {"id": "genesis", "phase": "Ceremony",
         "prompt": "IRREVERSIBLE: submit the one-shot NFT mint to mainnet",
         "contract": "HarnessCheckV1", "irreversible": True},
    ],
}


def run(ir, args):
    """Execute the compiled graph under stubs.

    Returns (summary_dict_or_None, [prompts actually spawned], stderr).
    """
    args = dict({'_launch': {'nonce':'ledger-fixture','root':'/fixture'}, '_replayLaunchNonce':'ledger-launch'}, **args)
    js = cg.emit(ir, CONTRACTS)
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.json")
        spawned = os.path.join(d, "spawned.json")
        wrapped = os.path.join(d, "w.mjs")
        with open(wrapped, "w") as fh:
            fh.write(
                "import {writeFileSync} from 'node:fs';\n"
                "const SPAWNED=[];\n"
                "let agent=async(p)=>{SPAWNED.push(p);"
                "return {exit:0,command:'aiken build',output:'ok',txHash:'tx_deadbeef'}};\n"
                "const parallel=async(t)=>Promise.all(t.map(f=>f()));\n"
                "const pipeline=async()=>[],log=()=>{},phase=()=>{};\n"
                f"const args={json.dumps(args)};\n"
                "const budget={total:null,spent:()=>0,remaining:()=>1e9};\n"
                "const workflow=0;\n"
                "(async () => {\n"
                + js.replace("export const meta", "const meta")
                + "\n})().then(r => {\n"
                f"  writeFileSync({json.dumps(out)}, JSON.stringify(r || null));\n"
                f"  writeFileSync({json.dumps(spawned)}, JSON.stringify(SPAWNED));\n"
                "}).catch(e => {\n"
                f"  writeFileSync({json.dumps(spawned)}, JSON.stringify(SPAWNED));\n"
                "  console.error(String(e && e.message || e));\n"
                "})\n"
            )
        p = subprocess.run([sys.executable and "node", wrapped],
                           capture_output=True, timeout=60, text=True)
        summary = None
        if os.path.exists(out):
            with open(out) as fh:
                summary = json.load(fh)
        calls = []
        if os.path.exists(spawned):
            with open(spawned) as fh:
                calls = json.load(fh)
        return summary, calls, p.stderr


def is_genesis(prompt):
    return "one-shot NFT mint" in prompt


# ============ 1. first run: the effect fires once and is recorded ==========
summary, calls, err = run(IR, {"confirm": "genesis", "_ledger": {}})
fired = [c for c in calls if is_genesis(c)]
report("first run performs the effect", len(fired) == 1, f"{len(fired)} spawn(s)")
report("first run completes", (summary or {}).get("outcome") == "COMPLETE",
       (summary or {}).get("outcome", f"no summary; stderr={err[:60]}"))
rows = (summary or {}).get("ledger") or []
report("first run returns a ledger record", len(rows) == 1, f"{len(rows)} record(s)")
KEY = rows[0]["key"] if rows else None
report("the record is keyed by campaign and node",
       bool(KEY) and KEY.startswith(IR["campaign"] + "|genesis|"), str(KEY)[:60])

# ============ 2. the resume case: this is the whole point =================
# Same graph, same args, ledger now carrying the effect. The upstream nodes
# re-run exactly as a targeted repair would; the ceremony must not.
replay_ledger = {KEY: {"key": KEY, "node": "genesis", "result": {"exit": 0, "txHash": "tx_deadbeef"}}}
summary2, calls2, err2 = run(IR, {"confirm": "genesis", "_ledger": replay_ledger})
fired2 = [c for c in calls2 if is_genesis(c)]
report("a resume does NOT perform the effect again", len(fired2) == 0,
       f"{len(fired2)} spawn(s)")
report("upstream nodes still ran (this was a real resume)",
       len(calls2) >= 2, f"{len(calls2)} upstream spawn(s)")
prov = {p["node"]: p["status"] for p in (summary2 or {}).get("provenance", [])}
report("the replay is recorded as REPLAYED", prov.get("genesis") == "REPLAYED",
       prov.get("genesis", "absent"))
report("the recorded result is restored, not lost",
       ((summary2 or {}).get("results") or {}).get("genesis", {}).get("txHash") == "tx_deadbeef",
       str(((summary2 or {}).get("results") or {}).get("genesis"))[:50])
report("a replay writes no second ledger row",
       len((summary2 or {}).get("ledger") or []) == 0,
       f"{len((summary2 or {}).get('ledger') or [])} record(s)")

# ============ 3. an unconfirmed effect refuses to fire ====================
summary3, calls3, _ = run(IR, {"_ledger": {}, "confirm": "some-other-node"})
fired3 = [c for c in calls3 if is_genesis(c)]
report("an unnamed node refuses to fire", len(fired3) == 0, f"{len(fired3)} spawn(s)")
report("refusal is its own outcome",
       (summary3 or {}).get("outcome") == "CONFIRM-REQUIRED",
       (summary3 or {}).get("outcome", "no summary"))

# Confirming the campaign is not confirming the effect.
summary3b, calls3b, _ = run(IR, {"_ledger": {}, "confirm": "yes"})
report("a blanket 'yes' does not authorize the ceremony",
       not [c for c in calls3b if is_genesis(c)], "refused")

# ============ 4. a missing ledger disarms nothing =========================
# The guard reads args. If a launcher forgot to pass the ledger, the run must
# die rather than proceed with replay protection silently switched off.
summary4, calls4, err4 = run(IR, {"confirm": "genesis"})
report("no ledger passed -> the run throws", summary4 is None and "irreversible" in err4,
       (err4.strip()[:56] or "no error"))
report("no ledger passed -> nothing was spawned", len(calls4) == 0, f"{len(calls4)} spawn(s)")

# ============ 5. the failed gate still stops the ceremony =================
# The ordering rule exists so the adversarial check runs BEFORE the effect;
# prove the halt actually beats the mint rather than merely preceding it.
def run_red_gate():
    js = cg.emit(copy.deepcopy(IR), CONTRACTS)
    with tempfile.TemporaryDirectory() as d:
        out, spawned = os.path.join(d, "o.json"), os.path.join(d, "s.json")
        wrapped = os.path.join(d, "w.mjs")
        with open(wrapped, "w") as fh:
            fh.write(
                "import {writeFileSync} from 'node:fs';\n"
                "const SPAWNED=[];\n"
                # The independent re-derivation disagrees: exit 1.
                "let agent=async(p)=>{SPAWNED.push(p);"
                "return {exit: p.includes('re-derive') ? 1 : 0,"
                "command:'x',output:'',txHash:'tx_x'}};\n"
                "const parallel=async(t)=>Promise.all(t.map(f=>f()));\n"
                "const pipeline=async()=>[],log=()=>{},phase=()=>{};\n"
                'const args={"confirm":"genesis","_ledger":{},"_launch":{"nonce":"ledger-fixture","root":"/fixture"},"_replayLaunchNonce":"ledger-launch"};\n'
                "const budget={total:null,spent:()=>0,remaining:()=>1e9};\n"
                "const workflow=0;\n"
                "(async () => {\n" + js.replace("export const meta", "const meta")
                + "\n})().then(r => {"
                f"writeFileSync({json.dumps(out)}, JSON.stringify(r||null));"
                f"writeFileSync({json.dumps(spawned)}, JSON.stringify(SPAWNED))}})"
                ".catch(e => {"
                f"writeFileSync({json.dumps(spawned)}, JSON.stringify(SPAWNED));"
                "console.error(String(e))})\n")
        subprocess.run(["node", wrapped], capture_output=True, timeout=60)
        s = json.load(open(out)) if os.path.exists(out) else None
        c = json.load(open(spawned)) if os.path.exists(spawned) else []
        return s, c


s5, c5 = run_red_gate()
report("a red gate halts before the ceremony", (s5 or {}).get("outcome") == "HALTED",
       (s5 or {}).get("outcome", "no summary"))
report("a red gate leaves the effect unfired", not [c for c in c5 if is_genesis(c)],
       "unfired")

# ============ 6. the ledger file itself ==================================
with tempfile.TemporaryDirectory() as root:
    def led(*a):
        return subprocess.run([sys.executable, LEDGER_PY, "--root", root, *a],
                              capture_output=True, text=True)

    r = led("--load", "--campaign", IR["campaign"])
    report("empty ledger loads as {} , not as missing", r.stdout.strip() == "{}",
           r.stdout.strip()[:30])

    run_summary = {"campaign": IR["campaign"], "outcome": "COMPLETE",
                   "ledger": [{"key": "k1", "campaign": IR["campaign"], "node": "genesis",
                               "result": {"txHash": "tx_abc", "exit": 0}}]}
    p = subprocess.run([sys.executable, LEDGER_PY, "--root", root, "--append",
                        "--run-id", "wf_1"], input=json.dumps(run_summary),
                       capture_output=True, text=True)
    report("append writes the effect", "genesis" in p.stdout, p.stdout.strip()[:50])
    report("the tx hash is pulled out as evidence", "tx_abc" in p.stdout,
           p.stdout.strip()[:60])

    r = led("--load", "--campaign", IR["campaign"])
    loaded = json.loads(r.stdout)
    report("a recorded effect loads back by key", "k1" in loaded, str(list(loaded))[:40])

    # Recording the same run twice must not double the file, or a retried
    # record-run would make the ledger disagree with itself.
    subprocess.run([sys.executable, LEDGER_PY, "--root", root, "--append",
                    "--run-id", "wf_1"], input=json.dumps(run_summary),
                   capture_output=True, text=True)
    with open(os.path.join(root, ".claude", "fluxpoint", "irreversible.jsonl")) as fh:
        lines = [l for l in fh if l.strip()]
    report("appending the same key twice keeps one row", len(lines) == 1,
           f"{len(lines)} row(s)")

    r = led("--load", "--campaign", "a different campaign entirely")
    report("another campaign's ledger is not consulted", r.stdout.strip() == "{}",
           r.stdout.strip()[:30])

    r = led("--list")
    report("--list names what already fired", "genesis" in r.stdout and "tx_abc" in r.stdout,
           r.stdout.strip().splitlines()[0][:50] if r.stdout.strip() else "empty")

    # A corrupt line must not silently shrink the set of effects known to
    # have happened — that is the one direction this file may not fail in.
    with open(os.path.join(root, ".claude", "fluxpoint", "irreversible.jsonl"), "a") as fh:
        fh.write("{not json\n")
    r = led("--load", "--campaign", IR["campaign"])
    report("a corrupt row is a hard error, never a skipped effect",
           r.returncode != 0 and "not valid JSON" in (r.stderr + r.stdout),
           (r.stderr or r.stdout).strip()[:50])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
