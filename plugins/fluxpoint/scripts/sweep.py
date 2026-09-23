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
sweep — the first recorded run of each (variant, case, rep), since a retry
of a recorded sample is not a second observation — and grades each: a pass is outcome COMPLETE with no red harness,
no red-team BLOCK and no WEAKENED proof audit. It reports each variant's
pass rate with its Wilson interval, output tokens spent per run and per
pass, on the train and test splits separately — the test split is the
headline. When every variant's test interval overlaps every other's, the
curve is flat at this sample size, which is itself the finding the issue
names: the work is not bound by thinking compute, and the cheapest setting
holds within noise. --hillclimb DIR writes the same runs in the layout the
claude-api hillclimb flow reads (baseline/ and v<N>/ dirs, results.jsonl per
variant, traces for the train split only, _state.json
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


def name_slug(s):
    """A sweep name as TAG reads it back: no '.', which slug() keeps for
    values. A dotted name used to plan fine and then match no run at all."""
    return re.sub(r"[^a-z0-9-]+", "-", str(s).lower()).strip("-") or "x"


CASE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")


def parse_vary(specs, roles, nodes=()):
    """[(role, field, [values])] from `role.field=a,b` specs."""
    out, seen = [], set()
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
        if (role, field) in seen:
            raise ValueError(f"--vary {spec_!r}: {role}.{field} is already varied — naming it "
                             f"twice makes variants that are one campaign under two labels")
        seen.add((role, field))
        slugs = [slug(v) for v in values]
        if len(set(slugs)) != len(slugs):
            raise ValueError(f"--vary {spec_!r}: values that differ only in case or "
                             f"punctuation name one variant — one would overwrite the other")
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

    The fraction is honored over the whole set, not rounded per group: a
    per-group round held out one case of every pair (50% asked as 10%), and
    a group of one never held out anything, so twelve cases under twelve
    tags split into no test set at all. Groups of one are pooled into one
    stratum, and every stratum keeps at least one case in train.
    """
    groups = {}
    for c in cases:
        groups.setdefault((c.get("tags") or ["untagged"])[0], []).append(c["id"])
    strata = [(tag, ids) for tag, ids in sorted(groups.items()) if len(ids) >= 2]
    pooled = [i for _, ids in sorted(groups.items()) if len(ids) == 1 for i in ids]
    if pooled:
        strata.append(("~singletons", pooled))
    n = len(cases)
    k_total = min(max(int(round(n * test_fraction)), 1), n - 1) if n >= 2 else 0
    caps = [max(0, len(ids) - 1) for _, ids in strata]
    exact = [len(ids) * k_total / n for _, ids in strata] if n else []
    quota = [min(int(math.floor(e)), c) for e, c in zip(exact, caps)]
    order = sorted(range(len(strata)), key=lambda i: (-(exact[i] - math.floor(exact[i])), strata[i][0]))
    left = k_total - sum(quota)
    while left > 0 and any(quota[i] < caps[i] for i in order):
        for i in order:
            if left and quota[i] < caps[i]:
                quota[i] += 1
                left -= 1
    train, test = [], []
    for (_, ids), k in zip(strata, quota):
        ids = sorted(ids, key=lambda i: hashlib.sha256(f"{name}|{i}".encode()).hexdigest())
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
    sys.path.insert(0, HERE)
    import specification as spec_mod
    try:
        ir = cg.extract_ir(text)
        # The variants keep the source's headers, so they compile against
        # its CONTRACTS: overlay and its SPEC: packet; the plan validates
        # and prices them against the same, or it reports defects the
        # compiled graphs would not have and prices calls they do not make.
        header = spec_mod.headers(text)
        contracts, _ = cg.resolve_contracts(os.path.join(os.path.dirname(HERE), "contracts"),
                                            header.get("CONTRACTS"), None, args.root)
    except (cg.GraphError, ValueError) as e:
        raise ValueError(f"{args.plan}: {e}")
    # The packet exactly as compile-graph.py loads it — on the same condition
    # and with its path — so a variant the compiler refuses (over the cost
    # ceiling once the packet is priced, or mutating with no packet at all)
    # is refused here too, before anyone pays for its runs.
    packet, packet_problem = None, None
    spec_path = header.get("SPEC")
    if spec_path or spec_mod.required(args.root) or any(
            n.get("mutates") or n.get("irreversible") for n in ir.get("nodes") or []):
        try:
            packet_file, _ = spec_mod.packet_paths(args.root, spec_path)
            pk, identity = spec_mod.load(args.root, spec=spec_path)
            packet = {"packet": pk, "identity": identity,
                      "path": os.path.relpath(packet_file, args.root).replace(os.sep, "/")}
        except (OSError, ValueError, TypeError) as e:
            packet_problem = f"spec required before implementation: {e}"
    agents = cg.load_agents(args.root)
    vary = parse_vary(args.vary, ir.get("roles") or {}, ir.get("nodes") or [])
    name = name_slug(args.name)
    cases = [{"id": "default", "args": {}}]
    if args.cases:
        with open(args.cases, encoding="utf-8") as fh:
            cases = json.load(fh)
        if (not isinstance(cases, list) or not cases or not all(
                isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"] for c in cases)
                or len({c["id"] for c in cases}) != len(cases)):
            raise ValueError("--cases must be a JSON list of objects with distinct string ids")
        bad = [c["id"] for c in cases if not CASE_ID.match(c["id"])]
        if bad:
            raise ValueError(f"--cases: id {bad[0]!r} must be letters, digits, '.', '_' or '-' "
                             f"(it names trace files, so no path separators)")
    train, test = split(cases, name, args.test_fraction)
    if not test:
        raise ValueError(
            f"the split holds out no test case ({len(cases)} case(s)) — with nothing held out "
            f"no variant can be compared to another, whatever the rep count. Give --cases "
            f"with at least two cases")
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
        if vid in variants:
            raise ValueError(f"two variants are both named {vid} — one would overwrite the other")
        v_ir["campaign"] = f"{ir['campaign']} [sweep {name}/{vid}]"
        v_ir["name"] = f"{ir.get('name') or 'graph-campaign'}--{name}--{vid}"[:120]
        findings = cg.validate(v_ir, contracts, cg.load_gates(args.root), agents, packet)
        if packet_problem:
            findings = [packet_problem] + findings
        est = cg.estimate_tokens(v_ir, contracts, packet)
        unmodified = all(
            ((ir.get("roles") or {}).get(r) or {}).get(f) == (None if v == "default" else v)
            for r, f, v in combo)
        path = os.path.join(out_dir, f"{vid}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(cg.IR_FENCE.sub(lambda _: "```json graph-ir\n" + json.dumps(v_ir, indent=2)
                                     + "\n```", text, count=1))
        variants[vid] = {"settings": [{"role": r, "field": f, "value": v} for r, f, v in combo],
                         "graph": os.path.relpath(path, args.root).replace(os.sep, "/"),
                         "estimatedTokens": est["total"], "findings": findings,
                         "unmodified": unmodified}
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


def order_key(when):
    """decision.py's timestamp normalization: 'YYYY-MM-DD HH:MM:SS'."""
    s = str(when or "").replace("T", " ").rstrip("Z").strip()
    return s + ":00" if len(s) == 16 else s


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
                    "order": (order_key(art.get("recordedAt") or art.get("when")),
                              art.get("runId") or fn[:-5]),
                    "summary": s})
    return out


def one_per_sample(runs):
    """(kept, duplicates): exactly one run per (variant, case, rep).

    Two artifacts for one planned sample are one observation recorded twice,
    and counting both narrows every interval the sweep reports. The FIRST
    one recorded is the sample: keeping the newest would let a retry that
    replaced a failure count as the observation, which is retry-until-pass.
    """
    kept, dupes = {}, []
    for r in sorted(runs, key=lambda r: r["order"]):
        k = (r["variant"], r["case"], str(r["rep"]))
        if k in kept:
            dupes.append(r)
        else:
            kept[k] = r
    return list(kept.values()), dupes


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
    name = name_slug(args.score)
    p = os.path.join(args.root, SWEEPS, name, "plan.json")
    if not os.path.exists(p):
        print(f"sweep: no plan at {p} — run --plan first", file=sys.stderr)
        return 2
    with open(p, encoding="utf-8") as fh:
        doc = json.load(fh)
    runs, dupes = one_per_sample(load_runs(args.root, name))
    unknown = sorted({r["case"] for r in runs} - {c["id"] for c in doc["cases"]})
    report = {"name": name, "variants": {}, "unknownCases": unknown,
              "duplicates": [d["runId"] for d in dupes]}
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
    # The report first: an export that fails must not take the score with it.
    if args.json:
        print(json.dumps(report, indent=2), flush=True)
        if args.hillclimb:
            export(args.hillclimb, doc, runs)
        return 0
    print(f"sweep {name}: {len(runs)} recorded run(s) of {doc['runsPlanned']} planned")
    for vid, v in report["variants"].items():
        t, tr = v["test"], v["train"]
        rate = lambda s: ("n/a" if s["passRate"] is None  # noqa: E731
                          else f"{s['passes']}/{s['runs']} [{s['ci95'][0]:.2f}-{s['ci95'][1]:.2f}]")
        cost = f"{t['meanSpent']:,.0f}" if t["meanSpent"] is not None else "n/a"
        print(f"  {vid:<40} test {rate(t):<22} train {rate(tr):<22} spent/run {cost}")
    if dupes:
        print(f"  {len(dupes)} duplicate run(s) of an already-recorded sample ignored — the "
              f"first recorded run of each (variant, case, rep) is the sample: "
              f"{', '.join(d['runId'] for d in dupes[:3])}")
    if unknown:
        print(f"  {len(unknown)} run(s) name a case the plan does not have: "
              f"{', '.join(unknown[:3])} — launch with the plan's case ids")
    if len(tested) < 2:
        print("  not enough test runs to compare variants yet")
    elif flat:
        print(f"  FLAT: every variant's test interval overlaps every other's — at this "
              f"sample size no setting beats another, "
              + (f"and the cheapest ({cheapest}) holds quality within noise" if cheapest
                 else "and no run recorded its spend, so no cheapest setting can be named"))
    else:
        print("  the test intervals separate: at least one setting differs beyond noise")
    if args.hillclimb:
        sys.stdout.flush()
        export(args.hillclimb, doc, runs)
    return 0


def variant_dirs(doc):
    """{vid: dir} in the hillclimb layout's names: `baseline` and `v<N>`.

    Its report builder reads only directories named exactly that and
    ignores the rest, so a directory per variant id was an export nothing
    could read. The baseline is the variant that leaves the source graph's
    settings as they are, else the first planned.
    """
    vids = list(doc["variants"])
    base = next((v for v in vids if doc["variants"][v].get("unmodified")), vids[0] if vids else None)
    order = [base] + [v for v in vids if v != base] if base else []
    return {v: ("baseline" if i == 0 else f"v{i}") for i, v in enumerate(order)}


def export(out, doc, runs):
    """The sweep's runs in the claude-api hillclimb layout, one dir per variant."""
    os.makedirs(out, exist_ok=True)
    dirs = variant_dirs(doc)
    with open(os.path.join(out, "_state.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"reps": doc["reps"], "train_ids": doc["train_ids"], "test_ids": doc["test_ids"],
                   "variants": {d: vid for vid, d in dirs.items()},
                   "goal": {"target": "pass", "direction": "higher", "hold": ["spent_tokens"]},
                   "metrics": [{"id": "pass", "kind": "binary", "label": "Campaign passed"},
                               {"id": "harnessGreen", "kind": "binary"},
                               {"id": "shipped", "kind": "binary"}],
                   "perf_fields": [{"id": "spent_tokens", "label": "Output tokens spent",
                                    "unit": "tok"}]}, fh, indent=2)
    cases = {c["id"]: c for c in doc["cases"]}
    train = set(doc["train_ids"])
    for vid, dname in dirs.items():
        vdir = os.path.join(out, dname)
        os.makedirs(os.path.join(vdir, "traces"), exist_ok=True)
        settings = ", ".join(f"{s['role']}.{s['field']}={s['value']}"
                             for s in doc["variants"][vid]["settings"])
        with open(os.path.join(vdir, "summary.json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"description": f"{vid}: {settings}", "target": "code"}, fh)
        with open(os.path.join(vdir, "change.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{vid}: {settings}\n")
        with open(os.path.join(vdir, "results.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
            for r in (r for r in runs if r["variant"] == vid):
                case = cases.get(r["case"], {})
                fh.write(json.dumps({
                    "prompt_id": r["case"], "rep": r["rep"],
                    "prompt": json.dumps(case.get("args") or {}),
                    "tags": case.get("tags") or [], "grade": r["grade"], "model": "mixed",
                    "spent_tokens": r["spent"], "meta": {"runId": r["runId"]},
                    "usage": {"output_tokens": r["spent"] or 0}}) + "\n")
                # The test split is scored, never read: a tuner handed its
                # transcripts would be grading itself on what it studied.
                if r["case"] not in train:
                    continue
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
            if a.reps < 1 or not 0 < a.test_fraction < 1:
                ap.error("--reps must be >= 1 and --test-fraction in (0, 1): a sweep "
                         "with no held-out cases cannot compare its variants")
            return plan(a)
        return score(a)
    except (OSError, ValueError) as e:
        print(f"sweep: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
