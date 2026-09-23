#!/usr/bin/env python3
"""Plan and score an effort/model sweep over one campaign (issue #67).

Every effort setting in the shipped templates is asserted, never measured
(see DESIGN-NOTES, "Effort is asserted, never measured"). Measuring one
needs three things the plugin did not have: variants of a campaign that
differ only in the setting under test, a fixed set of cases split into the
cases a tuner may read and the cases it may not, and a score per case per
repetition. This script builds the first two and computes the third from
recorded runs. It spends nothing and runs no campaign; launching the runs
is `/fluxpoint:graph-run`'s job, once per variant, case and repetition.

  sweep.py --plan WORK.md --name NAME --vary builder.effort=medium,high \\
           [--vary red-team.model=default,claude-sonnet-5] \\
           [--cases cases.json] [--reps 3] [--test-fraction 0.3]
  sweep.py --score NAME [--json] [--hillclimb DIR]

--plan writes `.claude/fluxpoint/sweeps/NAME/`: one graph file per variant
(the source IR with the role settings applied, its headers kept, and the
campaign line tagged `[sweep NAME/VARIANT]` so recorded runs group by
variant), and `plan.json` with the case split and each variant's price.
It also says what the plan can resolve. A campaign's score per run is a
pass or a fail — the harness is a Definition-of-Done signal, green or red
and nothing finer — so a variant's pass rate over n runs carries a 95%
interval about 1/sqrt(n) wide either side, and a difference between two
variants smaller than that is noise. The plan prints the interval its test
split gives, and how many runs a chosen difference would need, before a
token is spent.

--score reads `.claude/fluxpoint/runs/*.json`, keeps the runs of this
sweep, and grades each: a pass is outcome COMPLETE with no red harness,
no red-team BLOCK and no WEAKENED proof audit. It reports each variant's
pass rate with its Wilson interval, output tokens spent per run and per
pass, on the train and test splits separately — the test split is the
headline. When every variant's test interval overlaps every other's, the
curve is flat at this sample size, which is itself the finding the issue
names: the work is not bound by thinking compute, and the cheapest setting
holds within noise. --hillclimb DIR writes the same runs in the layout the
claude-api hillclimb flow reads (results.jsonl per variant, _state.json
with the split), so a tuner can pick up where the sweep stops.

A run is matched to its case through `inputs.case`, which the compiled
graph carries into its summary from the launch args. Launch every sweep
run with `case` and `rep` in its args.
"""
import argparse
import hashlib
import importlib.util
import itertools
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEPS = os.path.join(".claude", "fluxpoint", "sweeps")
RUNS = os.path.join(".claude", "fluxpoint", "runs")
TAG = re.compile(r"\[sweep ([a-z0-9-]+)/([a-z0-9.-]+)\]\s*$")
Z95 = 1.959964
Z80 = 0.841621


def compiler():
    spec = importlib.util.spec_from_file_location(
        "compile_graph", os.path.join(HERE, "compile-graph.py"))
    cg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cg)
    return cg


def slug(s):
    return re.sub(r"[^a-z0-9.-]+", "-", str(s).lower()).strip("-.") or "x"


def parse_vary(specs, roles, nodes=()):
    """[(role, field, [values])] from `role.field=a,b` specs."""
    out = []
    for spec_ in specs:
        m = re.fullmatch(r"([a-z][a-z0-9-]*)\.(effort|model)=(.+)", spec_.strip())
        if not m:
            raise ValueError(f"--vary {spec_!r}: expected <role>.<effort|model>=<a>,<b>")
        role, field, values = m.group(1), m.group(2), [v.strip() for v in m.group(3).split(",")]
        if role not in roles:
            raise ValueError(f"--vary {spec_!r}: the graph declares no role '{role}' "
                             f"(roles: {', '.join(sorted(roles)) or 'none'})")
        if not all(values) or len(set(values)) != len(values):
            raise ValueError(f"--vary {spec_!r}: values must be distinct and non-empty")
        # A knob that does not reach the calls it names makes every variant
        # the same campaign under a different label, and the sweep measures
        # noise. So a role nothing uses, or a setting a node overrides
        # inline, is refused rather than swept.
        using = [n for n in nodes if n.get("role") == role and not n.get("reduce")]
        if not using:
            raise ValueError(f"--vary {spec_!r}: no node uses role '{role}', so varying "
                             f"it changes nothing")
        shadowed = [n.get("id") for n in using if field in n]
        if shadowed:
            raise ValueError(f"--vary {spec_!r}: node(s) {', '.join(shadowed)} set {field} "
                             f"inline, which overrides the role — the variant would not "
                             f"reach them; move the setting to the role first")
        out.append((role, field, values))
    if not out:
        raise ValueError("--plan needs at least one --vary")
    return out


def split(cases, name, test_fraction):
    """(train_ids, test_ids): random by a seeded hash, stratified by tags[0].

    Never by score: a split chosen by how cases did is a tuner reading the
    number it will be graded on.
    """
    groups = {}
    for c in cases:
        groups.setdefault((c.get("tags") or ["untagged"])[0], []).append(c["id"])
    train, test = [], []
    for _, ids in sorted(groups.items()):
        ids = sorted(ids, key=lambda i: hashlib.sha256(f"{name}|{i}".encode()).hexdigest())
        k = int(round(len(ids) * test_fraction))
        if len(ids) >= 2:
            k = min(max(k, 1), len(ids) - 1)
        test += ids[:k]
        train += ids[k:]
    return sorted(train), sorted(test)


def half_width(n, p=0.5):
    """95% half-width of a pass rate over n runs (normal approximation)."""
    return Z95 * math.sqrt(p * (1 - p) / n) if n else float("inf")


def runs_for(diff, p=0.5):
    """Runs per variant to see a pass-rate difference `diff` at 95%/80%."""
    p1, p2 = p, min(1.0, p + diff)
    pbar = (p1 + p2) / 2
    num = (Z95 * math.sqrt(2 * pbar * (1 - pbar))
           + Z80 * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return math.ceil(num / diff ** 2)


def wilson(k, n):
    if not n:
        return (0.0, 1.0)
    p = k / n
    d = 1 + Z95 ** 2 / n
    c = (p + Z95 ** 2 / (2 * n)) / d
    h = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def plan(args):
    cg = compiler()
    with open(args.plan, encoding="utf-8") as fh:
        text = fh.read()
    try:
        ir = cg.extract_ir(text)
    except cg.GraphError as e:
        raise ValueError(f"{args.plan}: {e}")
    contracts = cg.load_contracts(os.path.join(os.path.dirname(HERE), "contracts"))
    vary = parse_vary(args.vary, ir.get("roles") or {}, ir.get("nodes") or [])
    name = slug(args.name)
    cases = [{"id": "default", "args": {}}]
    if args.cases:
        with open(args.cases, encoding="utf-8") as fh:
            cases = json.load(fh)
        if (not isinstance(cases, list) or not cases or not all(
                isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"] for c in cases)
                or len({c["id"] for c in cases}) != len(cases)):
            raise ValueError("--cases must be a JSON list of objects with distinct string ids")
    train, test = split(cases, name, args.test_fraction)
    out_dir = os.path.join(args.root, SWEEPS, name)
    os.makedirs(out_dir, exist_ok=True)
    variants = {}
    for combo in itertools.product(*[[(r, f, v) for v in vals] for r, f, vals in vary]):
        v_ir = json.loads(json.dumps(ir))
        for role, field, value in combo:
            body = v_ir["roles"].setdefault(role, {})
            if value == "default":
                body.pop(field, None)
            else:
                body[field] = value
        vid = ".".join(f"{slug(r)}-{slug(v)}" for r, _, v in combo)
        v_ir["campaign"] = f"{ir['campaign']} [sweep {name}/{vid}]"
        v_ir["name"] = f"{ir.get('name') or 'graph-campaign'}--{name}--{vid}"[:120]
        findings = cg.validate(v_ir, contracts, cg.load_gates(args.root))
        est = cg.estimate_tokens(v_ir, contracts)
        path = os.path.join(out_dir, f"{vid}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(cg.IR_FENCE.sub(lambda _: "```json graph-ir\n" + json.dumps(v_ir, indent=2)
                                     + "\n```", text, count=1))
        variants[vid] = {"settings": [{"role": r, "field": f, "value": v} for r, f, v in combo],
                         "graph": os.path.relpath(path, args.root).replace(os.sep, "/"),
                         "estimatedTokens": est["total"], "findings": findings}
    per_variant = len(cases) * args.reps
    doc = {"version": 1, "name": name, "graph": args.plan, "reps": args.reps,
           "cases": cases, "train_ids": train, "test_ids": test, "variants": variants,
           "runsPlanned": per_variant * len(variants),
           "estimatedTokens": sum(v["estimatedTokens"] for v in variants.values()) * per_variant}
    with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)

    n_test = len(test) * args.reps
    print(f"sweep {name}: {len(variants)} variant(s) x {len(cases)} case(s) x {args.reps} rep(s) "
          f"= {doc['runsPlanned']} run(s), ~{doc['estimatedTokens']:,} estimated tokens")
    for vid, v in sorted(variants.items()):
        bad = f"  IR REJECTED: {v['findings'][0]}" if v["findings"] else ""
        print(f"  {vid:<40} ~{v['estimatedTokens']:,} per run  {v['graph']}{bad}")
    print(f"  split: {len(train)} train case(s), {len(test)} test case(s) — the test "
          f"split is scored, never read")
    if n_test:
        hw = half_width(n_test)
        print(f"  resolution: a pass rate over {n_test} test run(s) per variant is known to "
              f"about ±{hw * 100:.0f} points, so two variants closer than ~{hw * 141:.0f} "
              f"points apart are indistinguishable")
    for d in (0.30, 0.20, 0.10):
        print(f"  to see a {d * 100:.0f}-point difference (95%, power 0.8): ~{runs_for(d)} "
              f"test run(s) per variant")
    print("  launch each run with /fluxpoint:graph-run on the variant's graph file, with "
          "args {...case.args, \"case\": <id>, \"rep\": <k>}, and record it as usual")
    return 1 if any(v["findings"] for v in variants.values()) else 0


def grade(art):
    """{pass, harnessGreen, shipped} for one recorded run."""
    harness = str(art.get("harnessExit"))
    green = harness in ("0", "n/a", "None")
    shipped = art.get("redTeam") != "BLOCK"
    sound = art.get("proofAudit") != "WEAKENED"
    ok = art.get("outcome") == "COMPLETE" and green and shipped and sound
    return {"pass": int(ok), "harnessGreen": int(green), "shipped": int(shipped)}


def load_runs(root, name):
    out = []
    d = os.path.join(root, RUNS)
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                art = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"sweep: {fn} is not readable JSON ({e}) — a run cannot be "
                             f"skipped, or the score depends on which files parsed")
        s = art.get("summary") or {}
        m = TAG.search(str(s.get("campaign") or ""))
        if not m or m.group(1) != name:
            continue
        inputs = s.get("inputs") or {}
        out.append({"runId": art.get("runId") or fn[:-5], "variant": m.group(2),
                    "case": str(inputs.get("case") or "default"),
                    "rep": inputs.get("rep", 0), "grade": grade(art),
                    "spent": s.get("spent") if isinstance(s.get("spent"), (int, float)) else None,
                    "estimate": (s.get("estimate") or {}).get("total"),
                    "summary": s})
    return out


def summarize(rows):
    n = len(rows)
    k = sum(r["grade"]["pass"] for r in rows)
    spent = [r["spent"] for r in rows if r["spent"] is not None]
    mean = sum(spent) / len(spent) if spent else None
    lo, hi = wilson(k, n)
    return {"runs": n, "passes": k, "passRate": (k / n) if n else None, "ci95": [lo, hi],
            "meanSpent": mean,
            "spentPerPass": (sum(spent) / k) if (spent and k) else None}


def score(args):
    name = slug(args.score)
    p = os.path.join(args.root, SWEEPS, name, "plan.json")
    if not os.path.exists(p):
        print(f"sweep: no plan at {p} — run --plan first", file=sys.stderr)
        return 2
    with open(p, encoding="utf-8") as fh:
        doc = json.load(fh)
    runs = load_runs(args.root, name)
    unknown = sorted({r["case"] for r in runs} - {c["id"] for c in doc["cases"]})
    report = {"name": name, "variants": {}, "unknownCases": unknown}
    for vid in sorted(doc["variants"]):
        mine = [r for r in runs if r["variant"] == vid]
        report["variants"][vid] = {
            "settings": doc["variants"][vid]["settings"],
            "train": summarize([r for r in mine if r["case"] in doc["train_ids"]]),
            "test": summarize([r for r in mine if r["case"] in doc["test_ids"]]),
        }
    tested = {v: s["test"] for v, s in report["variants"].items() if s["test"]["runs"]}
    flat = len(tested) >= 2 and all(
        a["ci95"][0] <= b["ci95"][1] and b["ci95"][0] <= a["ci95"][1]
        for a, b in itertools.combinations(tested.values(), 2))
    cheapest = min((v for v, t in tested.items() if t["meanSpent"] is not None),
                   key=lambda v: tested[v]["meanSpent"], default=None)
    report["flat"] = flat
    report["cheapest"] = cheapest
    if args.hillclimb:
        export(args.hillclimb, doc, runs)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"sweep {name}: {len(runs)} recorded run(s) of {doc['runsPlanned']} planned")
    for vid, v in report["variants"].items():
        t, tr = v["test"], v["train"]
        rate = lambda s: ("n/a" if s["passRate"] is None  # noqa: E731
                          else f"{s['passes']}/{s['runs']} [{s['ci95'][0]:.2f}-{s['ci95'][1]:.2f}]")
        cost = f"{t['meanSpent']:,.0f}" if t["meanSpent"] is not None else "n/a"
        print(f"  {vid:<40} test {rate(t):<22} train {rate(tr):<22} spent/run {cost}")
    if unknown:
        print(f"  {len(unknown)} run(s) name a case the plan does not have: "
              f"{', '.join(unknown[:3])} — launch with the plan's case ids")
    if len(tested) < 2:
        print("  not enough test runs to compare variants yet")
    elif flat:
        print(f"  FLAT: every variant's test interval overlaps every other's — at this "
              f"sample size no setting beats another, and the cheapest ({cheapest}) holds "
              f"quality within noise")
    else:
        print("  the test intervals separate: at least one setting differs beyond noise")
    return 0


def export(out, doc, runs):
    """The sweep's runs in the claude-api hillclimb layout, one dir per variant."""
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "_state.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"reps": doc["reps"], "train_ids": doc["train_ids"], "test_ids": doc["test_ids"],
                   "goal": {"target": "pass", "direction": "higher", "hold": ["spent_tokens"]},
                   "metrics": [{"id": "pass", "kind": "binary", "label": "Campaign passed"},
                               {"id": "harnessGreen", "kind": "binary"},
                               {"id": "shipped", "kind": "binary"}],
                   "perf_fields": [{"id": "spent_tokens", "label": "Output tokens spent",
                                    "unit": "tok"}]}, fh, indent=2)
    cases = {c["id"]: c for c in doc["cases"]}
    for vid in doc["variants"]:
        vdir = os.path.join(out, vid)
        os.makedirs(os.path.join(vdir, "traces"), exist_ok=True)
        with open(os.path.join(vdir, "summary.json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"description": ", ".join(
                f"{s['role']}.{s['field']}={s['value']}" for s in doc["variants"][vid]["settings"]),
                "target": "code"}, fh)
        with open(os.path.join(vdir, "results.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
            for r in (r for r in runs if r["variant"] == vid):
                case = cases.get(r["case"], {})
                fh.write(json.dumps({
                    "prompt_id": r["case"], "rep": r["rep"],
                    "prompt": json.dumps(case.get("args") or {}),
                    "tags": case.get("tags") or [], "grade": r["grade"], "model": "mixed",
                    "spent_tokens": r["spent"], "meta": {"runId": r["runId"]},
                    "usage": {"output_tokens": r["spent"] or 0}}) + "\n")
                trace = [{"role": "user", "content": json.dumps(case.get("args") or {})},
                         {"role": "assistant", "content": json.dumps(
                             {k: r["summary"].get(k) for k in
                              ("outcome", "results", "provenance")}, indent=1)}]
                with open(os.path.join(vdir, "traces", f"{r['case']}_rep{r['rep']}.json"),
                          "w", encoding="utf-8", newline="\n") as tf:
                    json.dump(trace, tf)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--name")
    ap.add_argument("--vary", action="append", default=[])
    ap.add_argument("--cases")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--test-fraction", type=float, default=0.3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--hillclimb", metavar="DIR")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", metavar="GRAPH")
    g.add_argument("--score", metavar="NAME")
    a = ap.parse_args()
    try:
        if a.plan:
            if not a.name:
                ap.error("--plan needs --name")
            if a.reps < 1 or not 0 <= a.test_fraction < 1:
                ap.error("--reps must be >= 1 and --test-fraction in [0, 1)")
            return plan(a)
        return score(a)
    except (OSError, ValueError) as e:
        print(f"sweep: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
