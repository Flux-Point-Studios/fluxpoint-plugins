#!/usr/bin/env python3
"""Regression suite for the role effort bench (bench/effort/run.py, #67).

Spends nothing and starts no model: it checks that every case's grader
gives its reference answer 1.0 and a stub less, and pins the grading rules
a sweep's numbers rest on (closest-seed matching, decoys, severity, the
architect's file scope, the split and the report).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "..", "bench", "effort", "run.py")
spec = importlib.util.spec_from_file_location("effort_bench", RUN)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f": {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# 1. Every case: reference scores 1.0, stub scores less.
proc = subprocess.run([sys.executable, RUN, "--selfcheck"], capture_output=True, text=True, timeout=600)
check("selfcheck: every grader separates reference from stub", proc.returncode == 0,
      proc.stdout[-2000:] + proc.stderr[-2000:])

cases = {c["id"]: c for c in bench.load_cases()}
roles = {c["role"] for c in cases.values()}
check("cases cover all three roles", roles == {"builder", "red-team", "architect"}, roles)

# 2. The split is stratified by role and holds out every role.
split = bench.split(list(cases.values()))
for role in roles:
    ids = [i for i, c in cases.items() if c["role"] == role]
    test = [i for i in ids if split[i] == "test"]
    check(f"split holds out some {role} cases and keeps some for train",
          0 < len(test) < len(ids), (role, test))
check("split is deterministic", split == bench.split(list(cases.values())))

# 3. Reply parsing takes the last JSON object, fenced or bare.
check("last_json: trailing object after prose",
      bench.last_json('notes {"a": 1} more {"findings": []}') == {"findings": []})
check("last_json: fenced block wins",
      bench.last_json('```json\n{"x": 2}\n```\nthen {"broken": ') == {"x": 2})
check("last_json: nothing parses", bench.last_json("no json here") is None)


def red(case_id, findings):
    case = cases[case_id]
    work = tempfile.mkdtemp(prefix="fluxbench-test-")
    marks = bench.materialize(case, work)
    return bench.grade_redteam(case, work, json.dumps({"findings": findings}), marks), marks


# 4. Adjacent seeds: a finding goes to the closest unmatched seed, so a
#    prefix finding on its own line does not steal the symlink seed above.
(_, _), marks = red("r-files", [])
prefix = marks["prefix"][0]["line"]
symlink_lines = [m["line"] for m in marks["symlink"]]
(score, detail), _ = red("r-files", [
    {"file": "serve.py", "line": prefix, "severity": "medium"},
    {"file": "serve.py", "line": symlink_lines[-1], "severity": "high"}])
check("red-team: closest seed wins on adjacent seeds", score == 1.0, detail)

# 5. A true low-severity note off the seeds costs nothing; a medium one does;
#    a finding on a decoy always counts, whatever its severity.
seeds = [{"file": m[0]["file"], "line": m[0]["line"], "severity": "high"}
         for k, m in red("r-quota", [])[1].items() if m[0]["kind"] == "seed"]
(score, _), _ = red("r-quota", seeds + [{"file": "quota.py", "line": 1, "severity": "low"}])
check("red-team: off-seed low finding not counted", score == 1.0, score)
(score, _), _ = red("r-quota", seeds + [{"file": "quota.py", "line": 1, "severity": "medium"}])
check("red-team: off-seed medium finding counted", 0 < score < 1.0, score)
decoy = next(m[0] for m in red("r-quota", [])[1].values() if m[0]["kind"] == "decoy")
(score, detail), _ = red("r-quota", seeds + [{"file": decoy["file"], "line": decoy["line"], "severity": "low"}])
check("red-team: decoy finding counted even at low", score < 1.0 and detail["decoy_hits"] == 1, detail)
(score, detail), _ = red("r-quota", [])
check("red-team: no findings scores 0", score == 0.0, detail)

# 6. Markers never reach the fixture the model reads.
work = tempfile.mkdtemp(prefix="fluxbench-test-")
bench.materialize(cases["r-session"], work)
text = open(os.path.join(work, "session.py")).read()
check("materialize strips seed and decoy markers", "@seed" not in text and "@decoy" not in text)

# 7. Architect: new files and test files are not graded; a missing source
#    file and a wrong existing one are.
case = cases["a-audit"]
truth = case["truth"]["files"]
risks = " ".join(g["any"][0] for g in case["truth"]["risks"])


def arch(files, risk_text=risks):
    return bench.grade_architect(case, None, json.dumps(
        {"files_to_change": files, "risks": [risk_text]}), {})[0]


check("architect: new and test files ignored",
      arch(truth + ["users/audit.py", "tests/test_audit.py"]) == arch(truth))
check("architect: missing source file costs", arch(truth[1:]) < arch(truth))
check("architect: wrong existing file costs", arch(truth + ["users/notify.py"]) < arch(truth))
check("architect: risks weigh 0.4", abs(arch(truth, "") - 0.6) < 1e-9, arch(truth, ""))

# 8. The report reads a results file without a model and keeps errors out.
out = tempfile.mkdtemp(prefix="fluxbench-test-")
rows = [
    {"case": "b-csv", "role": "builder", "split": "test", "model": "m", "effort": "low", "rep": r,
     "score": s, "detail": {}, "cost_usd": 0.01, "output_tokens": 100, "turns": 3, "wall_s": 1.0,
     "error": None} for r, s in enumerate([1.0, 0.5])]
rows.append(dict(rows[0], rep=9, score=0.0, error="rate limited"))
with open(os.path.join(out, "results.jsonl"), "w") as f:
    f.writelines(json.dumps(r) + "\n" for r in rows)
proc = subprocess.run([sys.executable, RUN, "--report", out, "--json"], capture_output=True, text=True)
rep = json.loads(proc.stdout)["configs"]
check("report: errored runs excluded, mean over graded runs",
      len(rep) == 1 and rep[0]["test"]["n"] == 2 and abs(rep[0]["test"]["score"] - 0.75) < 1e-9, rep)

# 9. The recorded sweep: every run metered, served by the model it names,
#    and graded on the split the current cases still produce.
rec = os.path.join(HERE, "..", "bench", "effort", "results", "2026-10-29")
rows = [json.loads(l) for l in open(os.path.join(rec, "results.jsonl"))]
check("recorded sweep: full grid present", len(rows) == 3 * 3 * len(cases) * 3, len(rows))
check("recorded sweep: no errored run", not any(r["error"] for r in rows))
check("recorded sweep: each run served by the model it names",
      all(r["served_models"] == [r["model"]] for r in rows))
check("recorded sweep: each run metered", all(r["cost_usd"] > 0 for r in rows))
check("recorded sweep: split matches the cases", all(r["split"] == split[r["case"]] for r in rows))
check("recorded sweep: transcripts kept for train only",
      not any("reply" in r for r in rows if r["split"] == "test"))
cmp = bench.compare([r for r in rows if not r["error"]], "claude-opus-5-5")
check("recorded sweep: builder curve flat on held-out cases", cmp["builder"]["flat_on_test"])

if failures:
    print(f"{len(failures)} failed")
    sys.exit(1)
print("all passed")
