#!/usr/bin/env python3
"""Graph-shaped metrics from recorded runs — observe the graph, not the chat.

Every number here is folded deterministically out of artifacts other tools
already write: runs/<runId>.json (record-run.py), memory.jsonl (lessons with
their kill verdicts), inbox.jsonl (what waited on a person). No model reads
a transcript; a rate nobody can recompute from the artifacts is not
reported.

Per campaign it reports: run count and outcome histogram; node totals and
the death/skip rates; agent calls actually spawned vs the compile-time
plan and the runtime's own token meter (runs that predate these fields are
counted and named, never guessed); discovery behavior (rounds, dry-rule vs
ceiling endings, per-worker unique-new tallies) from the structured `data`
carried on provenance rows; reduce compression (items in -> items out);
panel kill rate from filed lessons; attestation tallies; and how many
inbox items each campaign raised.

A malformed run artifact is reported by name and skipped — what a metrics
pass says must not depend on which artifacts happened to parse, and a
summary that quietly dropped one would read as a cleaner history than the
repo has.

  metrics.py [--root .] [--runs-dir .claude/fluxpoint/runs]
             [--campaign NAME] [--json]
"""
import argparse
import json
import math
import statistics
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory as memory_mod

RUNS_DIR = os.path.join(".claude", "fluxpoint", "runs")
MEMORY = os.path.join(".claude", "fluxpoint", "memory.jsonl")
INBOX = os.path.join(".claude", "fluxpoint", "inbox.jsonl")
SLICES_DIR = os.path.join(".claude", "fluxpoint", "slices")


def read_runs(runs_dir):
    """[(runId, artifact)] sorted oldest first, plus unreadable findings."""
    runs, bad = [], []
    if not os.path.isdir(runs_dir):
        return runs, bad
    for fn in sorted(os.listdir(runs_dir)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(runs_dir, fn)
        try:
            with open(p, encoding="utf-8") as fh:
                art = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            bad.append(f"UNREADABLE {p}: {e}")
            continue
        if not isinstance(art, dict):
            bad.append(f"UNREADABLE {p}: not a run artifact (not an object)")
            continue
        runs.append((fn[: -len(".json")], art))
    runs.sort(key=lambda r: (str(r[1].get("when") or ""), r[0]))
    return runs, bad


def read_jsonl(path):
    """Rows plus named findings for lines that do not parse."""
    rows, bad = [], []
    if not os.path.exists(path):
        return rows, bad
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                bad.append(f"UNREADABLE {path}:{i}: {e}")
    return rows, bad


def _bump(d, k, by=1):
    d[k] = d.get(k, 0) + by


def _int(x):
    """Strict integer read: a mistyped field is the artifact's problem and
    must fail that artifact's fold, never coerce or crash the report."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError(f"expected a number, got {type(x).__name__} {x!r}")
    return int(x)


def _empty():
    return {
        "runs": 0, "outcomes": {},
        "nodesOk": 0, "nodesDead": 0, "nodesSkipped": 0, "nodesBlocked": 0,
        "spawned": 0, "planned": 0, "spent": 0, "runsWithoutSpendData": 0,
        # What the compiler priced against what the runtime metered, and the
        # effort each call ran at: the raw material for an effort sweep, which
        # is asserted today and measured only when these two numbers meet.
        "estimated": 0, "runsWithEstimate": 0, "effortMix": {},
        "discovery": {"rounds": 0, "dryEnded": 0, "ceilingEnded": 0,
                      "truncated": 0, "halted": 0,
                      "found": 0, "fresh": 0, "kept": 0},
        "reduce": {"in": 0, "out": 0},
        "lessons": {"filed": 0, "killed": 0},
        "attestation": {},
        "inboxRaised": 0,
    }


def _merge(into, delta):
    for k, v in delta.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                _bump(into[k], k2, v2)
        else:
            into[k] += v


def _fold_one(art):
    """(campaign name, delta dict) for one artifact; raises on a bad one."""
    summary = art.get("summary") or {}
    name = str(summary.get("campaign") or "<unnamed>")
    c = _empty()
    c["runs"] = 1
    _bump(c["outcomes"], str(art.get("outcome") or "UNKNOWN"))
    c["nodesOk"] = _int(art.get("nodesOk") or 0)
    c["nodesDead"] = _int(art.get("nodesDead") or 0)
    c["nodesSkipped"] = _int(art.get("nodesSkipped") or 0)
    if isinstance(summary.get("spawned"), int):
        c["spawned"] = summary["spawned"]
        c["planned"] = _int(summary.get("planned") or 0)
        c["spent"] = _int(summary.get("spent") or 0)
    else:
        # A run compiled before these fields existed is a fact about the
        # history, not a zero to average in.
        c["runsWithoutSpendData"] = 1
    att = (art.get("attestation") or {}).get("tally") or {}
    for k, v in att.items():
        if isinstance(v, int):
            _bump(c["attestation"], k, v)
    est = summary.get("estimate")
    if isinstance(est, dict) and isinstance(est.get("total"), int):
        c["estimated"] = est["total"]
        c["runsWithEstimate"] = 1
    prof = summary.get("profile")
    if isinstance(prof, dict):
        for rec in prof.values():
            if isinstance(rec, dict) and rec.get("effort"):
                _bump(c["effortMix"], str(rec["effort"]), _int(rec.get("calls") or 0))
    # Discovery endings are classified per NODE, not per run — a run with
    # two sweeps has two endings, and blurring them into one would erase a
    # convergence or a truncation from the split status.md tells the
    # operator to quote.
    round_nodes, ceiling_nodes, skipped_nodes, dead_nodes = set(), set(), set(), set()
    prov = summary.get("provenance") or []
    if not isinstance(prov, list):
        raise ValueError(f"provenance is {type(prov).__name__}, not a list")
    for p in prov:
        if not isinstance(p, dict):
            raise ValueError("provenance row is not an object")
        node = str(p.get("node") or "")
        st = p.get("status")
        if st == "BLOCKED":
            c["nodesBlocked"] += 1
        if st == "INCOMPLETE" and "ceiling" in str(p.get("detail") or ""):
            ceiling_nodes.add(node)
        if st == "SKIPPED":
            skipped_nodes.add(node)
        if st == "DEAD":
            dead_nodes.add(node)
        d = p.get("data")
        if isinstance(d, dict):
            # Only the per-round tally notes count as rounds: a dead
            # round's note carries {round, returned, of} and no 'found',
            # and a partial-death round would otherwise be counted twice.
            if "round" in d and "found" in d:
                round_nodes.add(node)
                c["discovery"]["rounds"] += 1
                c["discovery"]["found"] += _int(d.get("found") or 0)
                c["discovery"]["fresh"] += _int(d.get("fresh") or 0)
                c["discovery"]["kept"] += _int(d.get("kept") or 0)
            if "before" in d and "after" in d:
                c["reduce"]["in"] += _int(d.get("before") or 0)
                c["reduce"]["out"] += _int(d.get("after") or 0)
    outcome = str(art.get("outcome") or "")
    for node in round_nodes:
        # A sweep ends exactly one way: on its ceiling; truncated by the
        # budget (floor or ceiling-declined spawns file SKIPPED); halted by
        # onRed with a dead round; or — only when nothing cut it short —
        # on the dry rule. Filing a truncated sweep as converged reports
        # convergence where there was truncation.
        if node in ceiling_nodes:
            c["discovery"]["ceilingEnded"] += 1
        elif node in skipped_nodes:
            c["discovery"]["truncated"] += 1
        elif outcome == "NODE-DEAD" and node in dead_nodes:
            c["discovery"]["halted"] += 1
        else:
            c["discovery"]["dryEnded"] += 1
    return name, c


def fold(runs, lessons, inbox_rows):
    """Per-campaign aggregates plus named findings for artifacts that could
    not be folded. One bad artifact fails alone: it is reported by name and
    skipped, and never takes the report down with it or half-counts."""
    out = {}
    findings = []
    run_campaign = {}
    for run_id, art in runs:
        try:
            name, delta = _fold_one(art)
        except Exception as e:  # noqa: BLE001 - the finding IS the handling
            findings.append(
                f"MALFORMED run artifact {run_id}: {type(e).__name__}: {e} "
                f"— skipped, not silently averaged")
            continue
        run_campaign[run_id] = name
        c = out.setdefault(name, _empty())
        _merge(c, delta)
    for i, row in enumerate(lessons, 1):
        try:
            prov = row.get("provenance") or {}
            name = run_campaign.get(str(prov.get("runId") or ""))
            if name is None:
                continue
            out[name]["lessons"]["filed"] += 1
            if row.get("status") == "killed":
                out[name]["lessons"]["killed"] += 1
        except Exception as e:  # noqa: BLE001
            findings.append(f"MALFORMED lesson row {i}: {type(e).__name__}: {e}")
    for i, row in enumerate(inbox_rows, 1):
        try:
            name = str(row.get("campaign") or "")
            if name in out:
                out[name]["inboxRaised"] += 1
        except Exception as e:  # noqa: BLE001
            findings.append(f"MALFORMED inbox row {i}: {type(e).__name__}: {e}")
    return out, findings


def pct(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def render(folded):
    lines = []
    for name in sorted(folded):
        c = folded[name]
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(c["outcomes"].items()))
        lines.append(f"campaign: {name}")
        lines.append(f"  runs: {c['runs']} ({outcomes})")
        judged = c["nodesOk"] + c["nodesDead"]
        lines.append(
            f"  nodes: {c['nodesOk']} ok, {c['nodesDead']} dead "
            f"(death rate {pct(c['nodesDead'], judged)}), "
            f"{c['nodesSkipped']} skipped on budget, "
            f"{c['nodesBlocked']} blocked on a person")
        if c["runs"] > c["runsWithoutSpendData"]:
            lines.append(
                f"  agent calls: {c['spawned']} spawned of {c['planned']} "
                f"planned worst-case ({pct(c['spawned'], c['planned'])}); "
                f"{c['spent']} tokens by the runtime's meter")
        if c["runsWithoutSpendData"]:
            lines.append(
                f"  ({c['runsWithoutSpendData']} run(s) predate spawn/spend "
                f"recording and are not in those numbers)")
        if c["runsWithEstimate"]:
            # Spent over estimated, on the runs that carry both. Far from 1
            # means the compiler's stated assumptions are wrong for this
            # campaign, which is the finding: correct the constants, not the
            # ceiling.
            spent_est = [1 for _ in range(c["runsWithEstimate"])]
            ratio = (f"{c['spent'] / c['estimated']:.2f}x" if c["estimated"]
                     and c["spent"] else "n/a")
            lines.append(
                f"  cost model: {c['estimated']} estimated vs {c['spent']} spent "
                f"across {len(spent_est)} estimated run(s) (spent/estimated {ratio}); "
                f"effort mix: "
                + (", ".join(f"{k} {v} call(s)" for k, v in sorted(c["effortMix"].items()))
                   or "unrecorded"))
        d = c["discovery"]
        if d["rounds"]:
            lines.append(
                f"  discovery: {d['rounds']} round(s), {d['found']} found -> "
                f"{d['fresh']} new after dedup -> {d['kept']} kept; "
                f"endings: {d['dryEnded']} dry rule, "
                f"{d['ceilingEnded']} ceiling, {d['truncated']} budget-"
                f"truncated, {d['halted']} halted — only the dry rule is "
                f"convergence")
        r = c["reduce"]
        if r["in"]:
            lines.append(
                f"  reduce: {r['in']} item(s) in -> {r['out']} out "
                f"(compression {pct(r['in'] - r['out'], r['in'])})")
        les = c["lessons"]
        if les["filed"]:
            lines.append(
                f"  verification: {les['filed']} lesson(s) filed, "
                f"{les['killed']} killed by panels "
                f"(kill rate {pct(les['killed'], les['filed'])})")
        if c["attestation"]:
            att = ", ".join(f"{k} {v}" for k, v in sorted(c["attestation"].items())
                            if k != "checked")
            lines.append(f"  attestation: {att or 'none recorded'}")
        if c["inboxRaised"]:
            lines.append(f"  inbox: raised {c['inboxRaised']} item(s) for a person")
        lines.append("")
    return "\n".join(lines).rstrip()


def read_slices(slices_dir):
    records, findings = [], []
    if not os.path.isdir(slices_dir):
        return records, findings
    for filename in sorted(os.listdir(slices_dir)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(slices_dir, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                records.append((filename[:-5], json.load(fh)))
        except (OSError, ValueError) as e:
            findings.append(f"UNREADABLE slice artifact {path}: {e}")
    return records, findings


def fold_calibration(records, campaign=None):
    """Mean of each slice's unitless measured/predicted ratio, per role.

    Averaging ratios lets a role's bytes and milliseconds coexist without
    adding unlike units or letting a large numeric metric dominate. Pending
    predictions are counted, never averaged as zero measured gains.
    """
    ratios, per_metric = {}, {}
    pending = completed = 0
    findings = []
    for sid, row in records:
        try:
            if not isinstance(row, dict) or row.get("version") != 1 or row.get("sliceId") != sid:
                raise ValueError("expected a version 1 slice artifact with matching sliceId")
            name = row.get("campaign")
            if not isinstance(name, str) or not name:
                raise ValueError("campaign is missing")
            if campaign and campaign != name:
                continue
            role, metric = row.get("role"), row.get("metric")
            if not isinstance(role, str) or not role.strip() or not isinstance(metric, str) or not metric:
                raise ValueError("role and metric must be nonempty strings")
            predicted = row.get("predictedDelta")
            if type(predicted) not in (int, float) or not math.isfinite(predicted) or predicted <= 0:
                raise ValueError("predictedDelta must be finite and positive")
            before = row.get("before")
            direction = row.get("direction")
            if type(before) is not int or direction not in ("down", "up"):
                raise ValueError("invalid starting measurement or direction")
            if row.get("status") == "STARTED":
                pending += 1
                continue
            after = row.get("after")
            if row.get("status") != "COMPLETE" or type(after) is not int:
                raise ValueError("expected COMPLETE with an integer ending measurement")
            measured = before - after if direction == "down" else after - before
            if type(row.get("measuredDelta")) is not int or row["measuredDelta"] != measured:
                raise ValueError("measuredDelta disagrees with recorded endpoints")
            ratio = measured / predicted
            if not math.isfinite(ratio):
                raise ValueError("measured/predicted is not finite")
            ratios.setdefault(role, []).append(ratio)
            per_metric.setdefault(role, {}).setdefault(metric, []).append(ratio)
            completed += 1
        except (TypeError, ValueError, OverflowError) as e:
            findings.append(f"MALFORMED slice artifact {sid}: {e} — excluded from calibration")
    roles = {}
    for role, values in sorted(ratios.items()):
        roles[role] = dict(slices=len(values), meanRatio=statistics.mean(values), metrics={
            metric: dict(slices=len(samples), meanRatio=statistics.mean(samples))
            for metric, samples in sorted(per_metric[role].items())})
    return dict(roles=roles, completedSlices=completed, pendingSlices=pending), findings


def render_calibration(calibration):
    lines = []
    for role, data in sorted(calibration["roles"].items()):
        lines.append(f"gain calibration: {role}: measured/predicted {data['meanRatio']:.2f}x "
                     f"(mean across {data['slices']} completed slice(s))")
        for metric, sample in sorted(data["metrics"].items()):
            lines.append(f"  {metric}: {sample['meanRatio']:.2f}x across {sample['slices']} slice(s)")
    if calibration["pendingSlices"]:
        lines.append(f"gain calibration: {calibration['pendingSlices']} started slice(s) await "
                     "ending measurements; excluded from ratios")
    return "\n".join(lines)



FIX_ORIGIN_NAMES = ("pre-existing", "incomplete-fix", "fix-introduced", "cross-slice", "spec-text")


def fold_fix_origins(runs, campaign=None):
    """Count observed findings per check, not unique bugs inferred from prose."""
    reports, findings = [], []
    for run_id, artifact in runs:
        summary = artifact.get("summary") or {}
        name = str(summary.get("campaign") or "<unnamed>")
        if campaign and campaign != name or "fixOrigins" not in summary:
            continue
        rows = summary["fixOrigins"]
        if not isinstance(rows, list):
            findings.append(f"MALFORMED fixOrigins for run {run_id}: expected per-check array")
            continue
        total = {key: 0 for key in FIX_ORIGIN_NAMES}
        rounds, invalid, seen = [], 0, set()
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k]
                                                   for k in ("node", "item")):
                    raise ValueError("missing checker/item identity")
                if type(row.get("round")) is not int or row["round"] < 1:
                    raise ValueError("round must be a positive integer")
                identity = (row["node"], row["item"], row["round"])
                if identity in seen:
                    raise ValueError("duplicate checker/item/round tally")
                origins = row.get("origins")
                if not isinstance(origins, dict) or set(origins) != set(FIX_ORIGIN_NAMES):
                    raise ValueError("each origin category must be recorded explicitly")
                if any(type(count) is not int or count < 0 for count in origins.values()):
                    raise ValueError("origin counts must be nonnegative integers")
                observed = {key: 0 for key in FIX_ORIGIN_NAMES}
                if not isinstance(row.get("findings"), list):
                    raise ValueError("findings must preserve the checked observations")
                for finding in row["findings"]:
                    if not isinstance(finding, dict) or finding.get("origin") not in observed:
                        raise ValueError("finding has no known replay origin")
                    observed[finding["origin"]] += 1
                if observed != origins:
                    raise ValueError("origin tally disagrees with preserved findings")
                seen.add(identity)
                count = sum(origins.values())
                rounds.append(dict(node=row["node"], item=row["item"], round=row["round"],
                                   origins=origins, findings=count,
                                   fixIntroducedShare=origins["fix-introduced"] / count if count else None))
                for key, value in origins.items():
                    total[key] += value
            except (ValueError, TypeError) as exc:
                invalid += 1
                findings.append(f"MALFORMED fixOrigins {run_id}[{index}]: {exc} — excluded")
        count = sum(total.values())
        reports.append(dict(runId=run_id, campaign=name, origins=total, rounds=rounds,
                            findings=count, invalidRounds=invalid,
                            fixIntroducedShare=total["fix-introduced"] / count if count else None))
    return reports, findings


def render_fix_origins(reports):
    lines = []
    for report in reports:
        origins = report["origins"]
        lines.append(f"repair origins: run {report['runId']}: fix-introduced "
                     f"{origins['fix-introduced']}/{report['findings']} "
                     f"({pct(origins['fix-introduced'], report['findings'])}) finding observations")
        for row in report["rounds"]:
            lines.append(f"  {row['node']} / {row['item']} round {row['round']}: fix-introduced "
                         f"{row['origins']['fix-introduced']}/{row['findings']} "
                         f"({pct(row['origins']['fix-introduced'], row['findings'])}); "
                         + ", ".join(f"{key} {row['origins'][key]}" for key in FIX_ORIGIN_NAMES))
        if report["invalidRounds"]:
            lines.append(f"  {report['invalidRounds']} invalid round tally/tallies excluded; overall coverage is incomplete")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--runs-dir", default=None,
                    help=f"default: <root>/{RUNS_DIR}")
    ap.add_argument("--campaign", help="only this campaign")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    directories = ([args.runs_dir] if args.runs_dir else list(dict.fromkeys([
        os.path.realpath(os.path.join(args.root, RUNS_DIR)),
        os.path.realpath(memory_mod.runs_path(args.root))])))
    run_map, findings = {}, []
    for directory in directories:
        part, bad = read_runs(directory)
        findings += bad
        for run_id, record in part:
            prior = run_map.get(run_id)
            order = lambda row: (str(row.get('recordedAt') or row.get('when') or ''), json.dumps(row, sort_keys=True))
            if prior is None or order(record) > order(prior):
                run_map[run_id] = record
    runs = sorted(run_map.items(), key=lambda item: (str(item[1].get('when') or ''), item[0]))
    lessons, f2, seen = [], [], set()
    with memory_mod.workfile_lock(memory_mod.path_for(args.root)):
        for path in memory_mod.source_paths(args.root):
            part, bad = read_jsonl(path)
            f2 += bad
            for row in part:
                key = json.dumps(row, sort_keys=True)
                if key not in seen:
                    lessons.append(row)
                    seen.add(key)
    inbox_rows, f3 = read_jsonl(os.path.join(args.root, INBOX))
    findings += f2 + f3

    folded, ffold = fold(runs, lessons, inbox_rows)
    findings += ffold
    slices, slice_findings = read_slices(os.path.join(args.root, SLICES_DIR))
    calibration, calibration_findings = fold_calibration(slices, args.campaign)
    findings += slice_findings + calibration_findings
    origin_reports, origin_findings = fold_fix_origins(runs, args.campaign)
    findings += origin_findings
    for f in findings:
        print(f"metrics: {f}", file=sys.stderr)
    if args.campaign:
        folded = {k: v for k, v in folded.items() if k == args.campaign}

    if args.as_json:
        json.dump({"campaigns": folded, "gainCalibration": calibration, "fixOrigins": origin_reports, "findings": findings}, sys.stdout,
                  indent=2, sort_keys=True)
        print()
    elif not folded:
        print("metrics: no recorded runs" + (f" for campaign '{args.campaign}'"
                                             if args.campaign else ""))
    else:
        print(render(folded))
    if not args.as_json and (calibration["roles"] or calibration["pendingSlices"]):
        print(render_calibration(calibration))
    if not args.as_json and origin_reports:
        print(render_fix_origins(origin_reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
