#!/usr/bin/env python3
"""An effort sweep can be planned, priced and scored (issue #67).

The sweep itself spends real money and runs from a session that can launch
campaigns; what a repository can hold is the machinery around it, and that
is what these cases execute: variants that differ only in the setting under
test, a split that never depends on scores, the resolution a plan can buy,
scoring from recorded runs with the test split as the headline, the flat
verdict, the hillclimb export, and the compiled graph carrying the case a
run was launched for into the summary it is scored from.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
SWEEP = os.path.join(PLUGIN, "scripts", "sweep.py")
spec = importlib.util.spec_from_file_location("sweep", SWEEP)
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)
passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<60} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


IR = {"version": 1, "name": "feat", "campaign": "ship a slice",
      "budget": {"maxNodes": 10, "cacheTtl": "1h"}, "treeGuard": False,
      "roles": {"architect": {"effort": "medium"}, "builder": {"effort": "high"}},
      "nodes": [{"id": "design", "role": "architect", "prompt": "design {{A.goal}}",
                 "contract": "DesignV1"},
                {"id": "judge", "role": "builder", "after": "design",
                 "prompt": "judge {{prev}}", "contract": "DesignV1"}]}
CASES = [{"id": f"c{i}", "args": {"goal": f"goal {i}"}, "tags": ["a" if i % 2 else "b"]}
         for i in range(10)]


def cli(root, *args):
    return subprocess.run([sys.executable, SWEEP, "--root", root, *args],
                          capture_output=True, text=True, cwd=root)


with tempfile.TemporaryDirectory() as root:
    with open(os.path.join(root, "WORK.md"), "w") as fh:
        fh.write("# w\n\nSTATUS: READY\n\n```json graph-ir\n" + json.dumps(IR) + "\n```\n")
    with open(os.path.join(root, "cases.json"), "w") as fh:
        json.dump(CASES, fh)
    r = cli(root, "--plan", "WORK.md", "--name", "effort1",
            "--vary", "builder.effort=medium,high",
            "--vary", "builder.model=default,claude-sonnet-5",
            "--cases", "cases.json", "--reps", "2")
    report("--plan builds one variant per combination", r.returncode == 0
           and "4 variant(s) x 10 case(s) x 2 rep(s) = 80 run(s)" in r.stdout,
           (r.stdout.splitlines() or [r.stderr])[0][:80])
    d = os.path.join(root, ".claude", "fluxpoint", "sweeps", "effort1")
    plan = json.load(open(os.path.join(d, "plan.json")))
    vids = sorted(plan["variants"])
    report("  named by the settings they differ in",
           "builder-high.builder-claude-sonnet-5" in vids, str(vids)[:80])
    v = open(os.path.join(d, "builder-medium.builder-default.md")).read()
    vir = json.loads(v.split("```json graph-ir\n", 1)[1].split("\n```", 1)[0])
    report("  each a runnable graph with only the role changed, header kept",
           vir["roles"]["builder"] == {"effort": "medium"} and "STATUS: READY" in v
           and vir["nodes"] == IR["nodes"], str(vir["roles"]))
    report("  and a campaign line tagged so its runs group by variant",
           vir["campaign"].endswith("[sweep effort1/builder-medium.builder-default]"),
           vir["campaign"][-50:])
    est = {k: plan["variants"][k]["estimatedTokens"] for k in vids}
    report("  priced per variant: more effort costs more, a cheaper model less",
           est["builder-high.builder-default"] > est["builder-medium.builder-default"]
           > est["builder-medium.builder-claude-sonnet-5"], str(est)[:80])
    report("the split holds out cases from every tag",
           {c["tags"][0] for c in CASES if c["id"] in plan["test_ids"]} == {"a", "b"}
           and not set(plan["test_ids"]) & set(plan["train_ids"])
           and len(plan["test_ids"]) + len(plan["train_ids"]) == 10,
           f"test {plan['test_ids']}")
    report("  and is the same split every time it is drawn",
           sw.split(CASES, "effort1", 0.3) == (plan["train_ids"], plan["test_ids"]), "stable")
    report("the plan says what it can resolve before anything is spent",
           f"resolution: a pass rate over {2 * len(plan['test_ids'])} test run(s)" in r.stdout
           and "20-point difference" in r.stdout, "stated")
    bad = cli(root, "--plan", "WORK.md", "--name", "x", "--vary", "reviewer.effort=low,high")
    report("varying a role the graph does not declare is refused",
           bad.returncode == 2 and "no role 'reviewer'" in bad.stderr, bad.stderr.strip()[-50:])
    shadow = json.loads(json.dumps(IR))
    shadow["nodes"][1]["effort"] = "xhigh"
    with open(os.path.join(root, "SHADOW.md"), "w") as fh:
        fh.write("```json graph-ir\n" + json.dumps(shadow) + "\n```\n")
    bad = cli(root, "--plan", "SHADOW.md", "--name", "x", "--vary", "builder.effort=low,high")
    report("a setting a node overrides inline is refused, not swept as noise",
           bad.returncode == 2 and "inline" in bad.stderr, bad.stderr.strip()[-60:])

    # Recorded runs, as record-run.py writes them: the high variant passes
    # every test case, the sonnet variant half of them.
    runs = os.path.join(root, ".claude", "fluxpoint", "runs")
    os.makedirs(runs)

    def rec(run_id, vid, case, rep, ok, spent, other=None):
        art = {"runId": run_id, "when": "2026-09-23 10:00", "outcome": "COMPLETE" if ok else "HALTED",
               "harnessExit": "0" if ok else "1", "redTeam": "SHIP", "proofAudit": "n/a",
               "summary": {"campaign": f"ship a slice [sweep {other or 'effort1'}/{vid}]",
                           "inputs": {"goal": "g", "case": case, "rep": rep},
                           "spent": spent, "estimate": {"total": 50000},
                           "outcome": "COMPLETE", "results": {}, "provenance": []}}
        with open(os.path.join(runs, run_id + ".json"), "w") as fh:
            json.dump(art, fh)

    n = 0
    for case in plan["test_ids"] + plan["train_ids"]:
        for rep in (0, 1):
            n += 1
            rec(f"wf_h{n}", "builder-high.builder-default", case, rep, True, 90000)
            rec(f"wf_s{n}", "builder-medium.builder-claude-sonnet-5", case, rep, rep == 0, 30000)
    rec("wf_other", "builder-high.builder-default", "c1", 0, False, 1, other="another-sweep")
    # A retry recorded for a sample that already has a run: the first
    # recorded is the observation, never the retry that replaced it.
    first = plan["test_ids"][0]
    art = json.load(open(os.path.join(runs, "wf_s1.json")))
    art.update(runId="wf_retry", recordedAt="2999-01-01 00:00:00", outcome="COMPLETE", harnessExit="0")
    art["summary"]["inputs"] = {"goal": "g", "case": first, "rep": 1}
    json.dump(art, open(os.path.join(runs, "wf_retry.json"), "w"))
    r = cli(root, "--score", "effort1", "--json")
    rep_ = json.loads(r.stdout)
    hi = rep_["variants"]["builder-high.builder-default"]["test"]
    so = rep_["variants"]["builder-medium.builder-claude-sonnet-5"]["test"]
    nt = 2 * len(plan["test_ids"])
    report("--score grades the test split per variant, from recorded runs",
           hi["runs"] == nt and hi["passes"] == nt and so["passes"] == nt // 2,
           f"{hi['passes']}/{so['passes']} of {nt}")
    report("  one run per planned sample: a retry of a recorded sample is ignored",
           rep_["duplicates"] == ["wf_retry"], str(rep_.get("duplicates")))
    report("  never counting another sweep's runs",
           rep_["variants"]["builder-high.builder-default"]["train"]["runs"]
           == 2 * len(plan["train_ids"]), "isolated")
    report("  with spent per run and per pass",
           hi["meanSpent"] == 90000 and so["spentPerPass"] == 60000, str(so["spentPerPass"]))
    report(f"at {nt} test runs each, all against half passing is still flat",
           rep_["flat"] is True and rep_["cheapest"] == "builder-medium.builder-claude-sonnet-5",
           str(rep_["flat"]))
    hc = os.path.join(root, "hc")
    cli(root, "--score", "effort1", "--hillclimb", hc)
    state = json.load(open(os.path.join(hc, "_state.json")))
    rows = open(os.path.join(hc, "builder-high.builder-default", "results.jsonl")).read().splitlines()
    report("--hillclimb writes the split and one row per (case, rep)",
           state["test_ids"] == plan["test_ids"] and len(rows) == 20
           and json.loads(rows[0])["grade"]["pass"] == 1, f"{len(rows)} rows")
    report("  with a trace per row for the tuner to read",
           len(os.listdir(os.path.join(hc, "builder-high.builder-default", "traces"))) == 20, "traces")

with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, ".local-contracts"))
    with open(os.path.join(root, ".local-contracts", "PlanV1.schema.json"), "w") as fh:
        json.dump({"$id": "PlanV1", "type": "object", "properties": {"steps": {"type": "array"}}}, fh)
    local = json.loads(json.dumps(IR))
    local["nodes"][1]["contract"] = "PlanV1"
    with open(os.path.join(root, "WORK.md"), "w") as fh:
        fh.write("CONTRACTS: .local-contracts\n\n```json graph-ir\n" + json.dumps(local) + "\n```\n")
    r = cli(root, "--plan", "WORK.md", "--name", "local", "--vary", "builder.effort=medium,high")
    report("a graph's CONTRACTS: overlay is honored while planning",
           r.returncode == 0 and "IR REJECTED" not in r.stdout, (r.stdout + r.stderr).strip()[-70:])

report("resolution math: ~1/sqrt(n) for a pass rate",
       abs(sw.half_width(100) - 0.098) < 0.001 and sw.runs_for(0.2) == 93,
       f"{sw.half_width(100):.3f}, {sw.runs_for(0.2)}")
lo, hi_ = sw.wilson(40, 40)
report("a perfect score still carries an interval", hi_ == 1.0 and 0.9 < lo < 0.95, f"{lo:.3f}")

# The compiled graph carries the case it was launched for into its summary.
spec2 = importlib.util.spec_from_file_location(
    "compile_graph", os.path.join(PLUGIN, "scripts", "compile-graph.py"))
cg = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(cg)
js = cg.emit(IR, cg.load_contracts(os.path.join(PLUGIN, "contracts")), {})
with tempfile.TemporaryDirectory() as d:
    out, w = os.path.join(d, "o.json"), os.path.join(d, "w.mjs")
    with open(w, "w") as fh:
        fh.write("import {writeFileSync} from 'node:fs';\n"
                 "const agent=async()=>({summary:'s',plan:'p',files:[],risks:[]});\n"
                 "const parallel=async(t)=>Promise.all(t.map(f=>f()));\n"
                 "const pipeline=async()=>[],log=()=>{},phase=()=>{};\n"
                 "const args={goal:'g',case:'c3',rep:1,_ledger:{secret:1}};\n"
                 "const budget={total:null,spent:()=>0,remaining:()=>1e9};const workflow=0;\n"
                 "(async () => {\n" + js.replace("export const meta", "const meta")
                 + f"\n}})().then(r=>writeFileSync({json.dumps(out)},JSON.stringify(r)))\n")
    subprocess.run(["node", w], capture_output=True, timeout=60)
    s = json.load(open(out)) if os.path.exists(out) else {}
report("a run's summary carries the case and rep it was launched with",
       (s.get("inputs") or {}).get("case") == "c3" and (s.get("inputs") or {}).get("rep") == 1,
       str(s.get("inputs")))
report("  but none of the launcher's own state",
       "_ledger" not in (s.get("inputs") or {}), "no _ keys")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
