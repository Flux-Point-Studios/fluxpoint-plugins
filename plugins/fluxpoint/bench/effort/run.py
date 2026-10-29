#!/usr/bin/env python3
"""Measure per-role effort and model choice on graded, held-out cases (#67).

Every effort setting the templates ship was asserted. This bench measures
the three roles the feature template sets, one role-shaped call at a time,
against cases with a graded score instead of the harness's green/red:

  builder   implement a spec in a fixture repo; score = fraction of the
            hidden tests that pass (the visible tests are a subset).
  red-team  review a fixture with seeded defects and plausible decoys;
            score = F1 of reported findings against the seeds, where a
            low-severity finding off the seeds is not counted against
            precision but any finding on a decoy is.
  architect plan a change in a fixture repo; score = 0.6 * F1 of the
            files named against the files the change needs, plus 0.4 *
            the fraction of required risks the plan names.

Each call is a real headless `claude -p` run with the setting under test
passed as `--model` and `--effort`; its `usage` and `total_cost_usd` are
what the meter reported, not an estimate. The split is sweep.py's: random
by seeded hash, stratified by role, never by score. Train cases are for
reading failures; the test split is the headline.

  run.py --run OUT [--models a,b] [--efforts low,medium,high]
         [--roles builder,red-team,architect] [--reps 2] [--jobs 6]
         [--max-usd 80] [--per-run-usd 3] [--cases ID,ID]
  run.py --report OUT [--json]
  run.py --selfcheck | --split

--run is resumable: a (model, effort, case, rep) already in
OUT/results.jsonl is skipped, so a run stopped by a rate limit or the
spend cap continues where it stopped. Transcript text is kept for train
cases only.
"""
import argparse
import concurrent.futures as cf
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "cases")
SPLIT_NAME = "effort-calibration"
TEST_FRACTION = 0.375
SEED = re.compile(r"\s*#\s*@(seed|decoy):([a-z0-9-]+)\s*$")
TOOLS = {
    "builder": "Read,Edit,Write,Bash,Glob,Grep",
    "red-team": "Read,Grep,Glob",
    "architect": "Read,Grep,Glob",
}
# No permission bypass: reviewers and architects only read, and a builder
# may edit inside its scratch fixture and run python3 there, nothing else.
ALLOWED = {
    "builder": "Read,Edit,Write,Glob,Grep,Bash(python3:*)",
    "red-team": "Read,Grep,Glob",
    "architect": "Read,Grep,Glob",
}


def sweep():
    spec = importlib.util.spec_from_file_location(
        "sweep", os.path.join(HERE, "..", "..", "scripts", "sweep.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_cases():
    out = []
    for cid in sorted(os.listdir(CASES)):
        path = os.path.join(CASES, cid, "case.json")
        if os.path.isfile(path):
            with open(path) as f:
                case = json.load(f)
            case["id"] = cid
            case["dir"] = os.path.join(CASES, cid)
            case["tags"] = [case["role"]]
            out.append(case)
    return out


def split(cases):
    train, test = sweep().split(cases, SPLIT_NAME, TEST_FRACTION)
    return {i: "train" for i in train} | {i: "test" for i in test}


def materialize(case, dest):
    """Copy the fixture repo into dest, stripping seed/decoy markers and
    recording the line each marker sat on."""
    marks = {}
    src = os.path.join(case["dir"], "repo")
    for root, _, files in os.walk(src):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, src)
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(full) as f:
                lines = f.read().split("\n")
            for n, line in enumerate(lines, 1):
                m = SEED.search(line)
                if m:
                    marks.setdefault(m.group(2), []).append(
                        {"kind": m.group(1), "file": rel, "line": n})
                    lines[n - 1] = line[:m.start()]
            with open(target, "w") as f:
                f.write("\n".join(lines))
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost",
                    "commit", "-qm", "fixture"], cwd=dest, check=True)
    return marks


def last_json(text):
    """The last top-level JSON object in a model's reply."""
    text = text or ""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for cand in reversed(fenced):
        try:
            return json.loads(cand)
        except ValueError:
            pass
    depth, start, found = 0, None, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    found = json.loads(text[start:i + 1])
                except ValueError:
                    pass
    return found


def f1(tp, fp, fn):
    if tp == 0:
        return 0.0
    p, r = tp / (tp + fp), tp / (tp + fn)
    return 2 * p * r / (p + r)


def grade_builder(case, work, reply, marks):
    hidden = os.path.join(case["dir"], "hidden")
    dest = os.path.join(work, "_hidden_tests")
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(hidden, dest)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "_hidden_tests", "-t", "."],
            cwd=work, capture_output=True, text=True, timeout=120)
        out = proc.stderr + proc.stdout
    except subprocess.TimeoutExpired:
        return 0.0, {"error": "hidden tests timed out"}
    ran = re.search(r"Ran (\d+) tests?", out)
    total = sum(len(re.findall(r"^\s+def test_", open(os.path.join(hidden, n)).read(), re.M))
                for n in os.listdir(hidden) if n.endswith(".py"))
    if not ran:
        return 0.0, {"error": "no test summary"}
    bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", out))
    n = int(ran.group(1))
    passed = max(0, n - bad) if n == total else 0
    return passed / total, {"passed": passed, "total": total, "ran": n}


def grade_redteam(case, work, reply, marks):
    doc = last_json(reply) or {}
    findings = [f for f in doc.get("findings", []) if isinstance(f, dict)]
    # A seed may be marked on several lines (the check and the use); a
    # finding on any of them finds it. Only high and medium findings count
    # against precision: a true low-severity note is not a false alarm.
    seeds = {k: v for k, v in marks.items() if v[0]["kind"] == "seed"}
    decoys = [m for v in marks.values() for m in v if m["kind"] == "decoy"]
    tol = case.get("line_tolerance", 3)
    hit, fp, decoy_hits = set(), 0, 0

    def near(fname, line, m, t):
        return (not fname or fname.endswith(m["file"])) and abs(line - m["line"]) <= t

    for f in findings:
        counted = str(f.get("severity", "")).lower() not in ("low", "info", "informational")
        try:
            line = int(f.get("line"))
        except (TypeError, ValueError):
            fp += counted
            continue
        fname = os.path.normpath(str(f.get("file", ""))).lstrip("./")
        close = sorted((min(abs(line - m["line"]) for m in ms), sid) for sid, ms in seeds.items()
                       if sid not in hit and any(near(fname, line, m, tol) for m in ms))
        match = close[0][1] if close else None
        if match:
            hit.add(match)
            continue
        if any(near(fname, line, d, 1) for d in decoys):
            decoy_hits += 1
            fp += 1
            continue
        fp += counted
    score = f1(len(hit), fp, len(seeds) - len(hit))
    return score, {"found": sorted(hit), "seeds": len(seeds), "false_positives": fp,
                   "decoy_hits": decoy_hits, "parsed": bool(doc)}


def grade_architect(case, work, reply, marks):
    doc = last_json(reply) or {}
    # Only existing non-test files are graded: a new module's name and
    # whether tests go in a new or an existing file are choices, not errors.
    src = os.path.join(case["dir"], "repo")
    existing = {os.path.relpath(os.path.join(r, n), src)
                for r, _, ns in os.walk(src) for n in ns}
    named = {os.path.normpath(str(p)).lstrip("./") for p in doc.get("files_to_change", [])}
    named = {p for p in named if p in existing and not p.startswith("tests/")}
    need = set(case["truth"]["files"])
    tp = len(named & need)
    file_f1 = f1(tp, len(named - need), len(need - named))
    text = json.dumps(doc.get("risks", [])).lower() + " " + str(doc.get("first_failing_test", "")).lower()
    groups = case["truth"]["risks"]
    risk_hits = [g["id"] for g in groups if any(re.search(k, text) for k in g["any"])]
    score = 0.6 * file_f1 + 0.4 * (len(risk_hits) / len(groups))
    return score, {"file_f1": round(file_f1, 3), "named": sorted(named), "risks_hit": risk_hits,
                   "parsed": bool(doc)}


GRADERS = {"builder": grade_builder, "red-team": grade_redteam, "architect": grade_architect}

SUFFIX = {
    "builder": "\n\nWork in the current directory. Do not edit or delete existing tests. "
               "Run the visible tests with `python3 -m unittest discover -s tests -t .`. "
               "The visible tests are a subset of the acceptance tests. "
               "When you are done, reply with one line saying so.",
    "red-team": "\n\nThe code is in the current directory; read what you need. Report only "
                "defects that an attacker or a bad input can actually trigger, with the line "
                "where the defect is. Do not report style, naming or hardening suggestions. "
                "End your reply with one JSON object and nothing after it:\n"
                '{"findings": [{"file": "path", "line": N, "severity": "high|medium|low", '
                '"title": "...", "attack": "..."}]}',
    "architect": "\n\nThe repository is in the current directory; read it before planning. "
                 "Do not write code. End your reply with one JSON object and nothing after it:\n"
                 '{"files_to_change": ["path", ...], "first_failing_test": "...", '
                 '"steps": ["..."], "risks": ["..."]}\n'
                 "List every existing or new file the change must touch and no others.",
}


class Budget:
    def __init__(self, cap):
        self.cap, self.spent, self.lock = cap, 0.0, threading.Lock()

    def add(self, usd):
        with self.lock:
            self.spent += usd

    def left(self):
        with self.lock:
            return self.cap - self.spent


def one(case, model, effort, rep, split_of, per_run, budget):
    if budget.left() <= 0:
        return None
    work = tempfile.mkdtemp(prefix=f"fluxbench-{case['id']}-")
    try:
        marks = materialize(case, work)
        prompt = case["prompt"] + SUFFIX[case["role"]]
        cmd = ["claude", "-p", "--output-format", "json", "--no-session-persistence",
               "--model", model, "--effort", effort, "--strict-mcp-config",
               "--setting-sources", "project", "--tools", TOOLS[case["role"]],
               "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED[case["role"]],
               "--max-budget-usd", str(per_run)]
        t0 = time.time()
        stderr = ""
        try:
            proc = subprocess.run(cmd, input=prompt, cwd=work, capture_output=True,
                                  text=True, timeout=1800)
            stderr = proc.stderr
            meta = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
        except subprocess.TimeoutExpired:
            meta = {"is_error": True, "result": "timeout"}
        wall = time.time() - t0
        usage = meta.get("usage") or {}
        cost = float(meta.get("total_cost_usd") or 0.0)
        budget.add(cost)
        error = None
        if meta.get("is_error") or not meta:
            error = (meta.get("result") if meta else None) or stderr[-400:] or "no json"
        reply = meta.get("result") or ""
        score, detail = GRADERS[case["role"]](case, work, reply, marks)
        served = list((meta.get("modelUsage") or {}).keys())
        row = {
            "case": case["id"], "role": case["role"], "split": split_of[case["id"]],
            "model": model, "effort": effort, "rep": rep, "score": round(score, 4),
            "detail": detail, "cost_usd": round(cost, 6),
            "output_tokens": usage.get("output_tokens", 0),
            "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
            "input_tokens": usage.get("input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0),
            "turns": meta.get("num_turns"), "wall_s": round(wall, 1),
            "subtype": meta.get("subtype"), "served_models": served, "error": error,
        }
        if split_of[case["id"]] == "train":
            row["reply"] = reply[-4000:]
        return row
    finally:
        shutil.rmtree(work, ignore_errors=True)


def selfcheck():
    """Every grader must give a reference answer 1.0 and a stub less."""
    bad = 0
    for case in load_cases():
        work = tempfile.mkdtemp(prefix="fluxbench-check-")
        try:
            marks = materialize(case, work)
            grade = GRADERS[case["role"]]
            low, _ = grade(case, work, "", marks)
            if case["role"] == "builder":
                shutil.copytree(os.path.join(case["dir"], "reference"), work, dirs_exist_ok=True)
                high, detail = grade(case, work, "", marks)
            elif case["role"] == "red-team":
                found = [{"file": v[0]["file"], "line": v[0]["line"], "severity": "high"}
                         for v in marks.values() if v[0]["kind"] == "seed"]
                high, detail = grade(case, work, json.dumps({"findings": found}), marks)
                if not any(v[0]["kind"] == "decoy" for v in marks.values()):
                    high, detail = 0.0, {"error": "no decoy"}
            else:
                t = case["truth"]
                risks = [g["any"][0].replace("\\", "").replace(".{0,60}", " ")
                         .replace(".{0,40}", " ").replace(".{0,30}", " ").replace(".{0,20}", " ")
                         for g in t["risks"]]
                high, detail = grade(case, work, json.dumps(
                    {"files_to_change": t["files"], "risks": risks}), marks)
            ok = high == 1.0 and low < 1.0
            bad += not ok
            print(f"{'ok ' if ok else 'BAD'} {case['id']:14} stub={low:.2f} reference={high:.2f}"
                  f"{'' if ok else ' ' + json.dumps(detail)}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if bad else 0


def run(args):
    os.makedirs(args.run, exist_ok=True)
    path = os.path.join(args.run, "results.jsonl")
    done = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                if not r.get("error"):
                    done.add((r["model"], r["effort"], r["case"], r["rep"]))
    cases = load_cases()
    split_of = split(cases)
    roles = set(args.roles.split(","))
    only = set(args.cases.split(",")) if args.cases else None
    todo = [(c, m, e, r) for r in range(args.reps) for c in cases
            for m in args.models.split(",") for e in args.efforts.split(",")
            if c["role"] in roles and (not only or c["id"] in only)
            and (m, e, c["id"], r) not in done]
    with open(os.path.join(args.run, "split.json"), "w") as f:
        json.dump(split_of, f, indent=1, sort_keys=True)
    budget = Budget(args.max_usd)
    lock = threading.Lock()
    print(f"{len(todo)} runs to do, {len(done)} already recorded", flush=True)
    with cf.ThreadPoolExecutor(args.jobs) as pool, open(path, "a") as out:
        futs = {pool.submit(one, c, m, e, r, split_of, args.per_run_usd, budget): (c["id"], m, e, r)
                for c, m, e, r in todo}
        for n, fut in enumerate(cf.as_completed(futs), 1):
            key = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # a grader or fixture bug must not kill the sweep
                print(f"[{n}/{len(todo)}] {key} crashed: {exc}", flush=True)
                continue
            if row is None:
                continue
            with lock:
                out.write(json.dumps(row) + "\n")
                out.flush()
            print(f"[{n}/{len(todo)}] {key} score={row['score']} ${row['cost_usd']:.3f} "
                  f"spent=${budget.spent:.2f}{' ERR ' + str(row['error'])[:80] if row['error'] else ''}",
                  flush=True)
    print(f"spent ${budget.spent:.2f}")


def mean_ci(xs):
    """Mean with a 95% t-ish interval (normal for n >= 30)."""
    n = len(xs)
    if not n:
        return (float("nan"),) * 3
    m = sum(xs) / n
    if n < 2:
        return m, 0.0, 1.0
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    t = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31,
         10: 2.26}.get(n, 2.0 if n < 30 else 1.96)
    h = t * sd / math.sqrt(n)
    return m, max(0.0, m - h), min(1.0, m + h)


# The settings WORK.feature.md ships: role effort on the session's model.
BASELINE = {"architect": "medium", "builder": "high", "red-team": "high"}
MARGIN = 0.05
BOOT = 4000


def paired(rows, cfg, base, sp):
    """Per-case mean of (cfg - base) over the (case, rep) samples both have."""
    by = {}
    for r in rows:
        if r["split"] == sp:
            by[(r["model"], r["effort"], r["case"], r["rep"])] = r["score"]
    diffs = {}
    for (m, e, c, rep), s in by.items():
        if (m, e) == cfg and (base[0], base[1], c, rep) in by:
            diffs.setdefault(c, []).append(s - by[(base[0], base[1], c, rep)])
    return {c: sum(v) / len(v) for c, v in diffs.items()}


def boot_ci(per_case, seed):
    """Case-clustered bootstrap 95% interval of the mean paired difference."""
    import random
    vals = list(per_case.values())
    if not vals:
        return None, None, None
    mean = sum(vals) / len(vals)
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(vals) for _ in vals) / len(vals) for _ in range(BOOT))
    return mean, means[int(0.025 * BOOT)], means[int(0.975 * BOOT) - 1]


def compare(rows, base_model):
    """Each role's grid against its shipped setting, and a cheaper pick.

    The pick is made on the train split only: the cheapest setting whose
    paired train difference is no worse than -MARGIN. Its held-out
    difference is then read once, on the test split.
    """
    out = {}
    for role in sorted({r["role"] for r in rows}):
        rr = [r for r in rows if r["role"] == role]
        base = (base_model, BASELINE[role])
        cost = {}
        for r in rr:
            cost.setdefault((r["model"], r["effort"]), []).append(r["cost_usd"])
        if base not in cost:
            continue
        bcost = sum(cost[base]) / len(cost[base])
        cfgs = []
        for cfg in sorted(cost):
            seed = int(hashlib.sha256(f"{role}|{cfg}".encode()).hexdigest()[:8], 16)
            tr = boot_ci(paired(rr, cfg, base, "train"), seed)
            te = boot_ci(paired(rr, cfg, base, "test"), seed + 1)
            c = sum(cost[cfg]) / len(cost[cfg])
            cfgs.append({"model": cfg[0], "effort": cfg[1], "cost_per_run": c,
                         "cost_vs_base": c / bcost if bcost else None,
                         "train_diff": tr, "test_diff": te})
        ok = [x for x in cfgs if x["train_diff"][0] is not None and x["train_diff"][0] >= -MARGIN]
        pick = min(ok, key=lambda x: x["cost_per_run"]) if ok else None
        flat = all(x["test_diff"][1] is not None and x["test_diff"][1] <= 0 <= x["test_diff"][2]
                   for x in cfgs if (x["model"], x["effort"]) != base)
        out[role] = {"baseline": {"model": base[0], "effort": base[1], "cost_per_run": bcost},
                     "configs": cfgs, "pick": pick, "flat_on_test": flat}
    return out


def report(args):
    rows = []
    with open(os.path.join(args.report, "results.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            if not r.get("error"):
                rows.append(r)
    table = {}
    for r in rows:
        table.setdefault((r["role"], r["model"], r["effort"]), []).append(r)
    out = []
    for (role, model, effort), rs in sorted(table.items()):
        entry = {"role": role, "model": model, "effort": effort}
        for sp in ("test", "train"):
            sub = [r for r in rs if r["split"] == sp]
            m, lo, hi = mean_ci([r["score"] for r in sub])
            entry[sp] = {"n": len(sub), "score": m, "lo": lo, "hi": hi}
        entry["cost_per_run"] = sum(r["cost_usd"] for r in rs) / len(rs)
        entry["output_tokens_per_run"] = sum(r["output_tokens"] for r in rs) / len(rs)
        entry["turns_per_run"] = sum(r["turns"] or 0 for r in rs) / len(rs)
        entry["wall_s_per_run"] = sum(r["wall_s"] for r in rs) / len(rs)
        if role == "red-team":
            entry["decoy_hits_per_run"] = sum(r["detail"].get("decoy_hits", 0) for r in rs) / len(rs)
        out.append(entry)
    cmp = compare(rows, args.baseline_model)
    if args.json:
        print(json.dumps({"configs": out, "compare": cmp}, indent=1))
        return
    print(f"{'role':10} {'model':18} {'effort':7} {'test n':>6} {'test':>6} {'95% CI':>13} "
          f"{'train':>6} {'$/run':>7} {'out tok':>8} {'turns':>5}")
    for e in out:
        t = e["test"]
        print(f"{e['role']:10} {e['model']:18} {e['effort']:7} {t['n']:6d} {t['score']:6.3f} "
              f"[{t['lo']:.2f},{t['hi']:.2f}]{'':>1} {e['train']['score']:6.3f} "
              f"{e['cost_per_run']:7.3f} {e['output_tokens_per_run']:8.0f} {e['turns_per_run']:5.1f}")
    print(f"\n{len(rows)} graded runs, ${sum(r['cost_usd'] for r in rows):.2f} metered")

    def fmt(d):
        return "     n/a" if d[0] is None else f"{d[0]:+.3f} [{d[1]:+.2f},{d[2]:+.2f}]"

    for role, c in cmp.items():
        b = c["baseline"]
        print(f"\n{role}: paired difference against the shipped {b['model']} / {b['effort']}"
              f" (${b['cost_per_run']:.3f}/run); case-clustered bootstrap 95% intervals")
        print(f"  {'model':18} {'effort':7} {'train diff':>22} {'test diff':>22} {'cost x':>7}")
        for x in c["configs"]:
            print(f"  {x['model']:18} {x['effort']:7} {fmt(x['train_diff']):>22} "
                  f"{fmt(x['test_diff']):>22} {x['cost_vs_base']:7.2f}")
        p = c["pick"]
        if p:
            print(f"  pick (train only, cheapest within {MARGIN} of baseline): {p['model']} / "
                  f"{p['effort']}, held-out diff {fmt(p['test_diff'])}, cost x{p['cost_vs_base']:.2f}")
        print(f"  every test interval spans 0 (flat at this sample size): {c['flat_on_test']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", metavar="OUT")
    g.add_argument("--report", metavar="OUT")
    g.add_argument("--split", action="store_true", help="print the train/test split")
    g.add_argument("--selfcheck", action="store_true",
                   help="check every grader against its reference answer and a stub")
    ap.add_argument("--models", default="claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5")
    ap.add_argument("--efforts", default="low,medium,high")
    ap.add_argument("--roles", default="builder,red-team,architect")
    ap.add_argument("--cases")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--max-usd", type=float, default=80.0)
    ap.add_argument("--per-run-usd", type=float, default=3.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline-model", default="claude-opus-5-5",
                    help="the session model the shipped role efforts run on")
    args = ap.parse_args()
    if args.selfcheck:
        sys.exit(selfcheck())
    if args.split:
        print(json.dumps(split(load_cases()), indent=1, sort_keys=True))
    elif args.run:
        run(args)
    else:
        report(args)


if __name__ == "__main__":
    main()
