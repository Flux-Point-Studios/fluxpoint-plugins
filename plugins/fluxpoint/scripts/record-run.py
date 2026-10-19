#!/usr/bin/env python3
"""Record a graph run: provenance artifact + WORK.md Evidence row.

Evidence stops being something an agent remembers to write by hand and
becomes a build artifact of the run. Reads the workflow's returned summary
JSON on stdin (or --result FILE).

Usage:
  record-run.py --run-id wf_abc --graph WORK.md [--harness 0]
                [--red-team SHIP] [--proof-audit SOUND] [--executor workflow] < result.json
"""
import argparse
import datetime
import json
import os
import re
import sys

# One Evidence discipline for both modes: loop slices write their own rows,
# graph runs get theirs appended here.
ROW_HDR = "| When (UTC) | Source | Outcome | Claim | Proof |"
LEGACY_HDR = "| When (UTC) | runId | Outcome | Nodes OK/dead | Findings | Harness | Red-team |"
# A frozen choice is a build artifact for the same reason Evidence is: the
# most expensive output of a campaign was the one thing with no slot, and a
# decision that lived only in a transcript was re-decided by the next
# context that had to ask the same question.
DEC_HDR = "| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |"
# The proof-auditor's vocabulary, worst last. A campaign with several
# proof-audit nodes files the worst verdict, the way several harness nodes
# file the worst exit: one WEAKENED is the campaign's answer.
PROOF_RANK = {"NOT-APPLICABLE": 0, "SOUND": 1, "UNPROVEN": 2, "WEAKENED": 3}
# RedTeamV1's severity ordinal. The contract binds worstSeverity to the
# findings and refuses a SHIP over HIGH or CRITICAL; this re-derives both
# from the findings themselves, so a validator that skipped the binding
# cannot file a SHIP over a CRITICAL as shippable.
SEVERITY_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def red_team_coverage_issue(coverage):
    """Validate the durable scope of a SHIP independently of schema execution."""
    if not isinstance(coverage, dict):
        return "SHIP has no coverage object"
    subject = coverage.get("subject")
    if not isinstance(subject, list) or not subject:
        return "SHIP coverage has no repository ranges"
    for item in subject:
        if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("repository", "base", "head")):
            return "SHIP coverage has an invalid repository/base/head"
        if any(not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item[key], re.IGNORECASE)
               for key in ("base", "head")):
            return "SHIP coverage must pin base/head to full Git object ids"
        if any(type(item.get(key)) is not int or item[key] < 0
               for key in ("files", "additions", "deletions")):
            return "SHIP coverage needs nonnegative integer file and line counts"
    if len({item["repository"] for item in subject}) != len(subject):
        return "SHIP coverage repeats a repository"
    if type(coverage.get("attackers")) is not int or coverage["attackers"] < 1:
        return "SHIP coverage needs a positive attacker count"
    lenses = coverage.get("lenses")
    if (not isinstance(lenses, list) or not lenses
            or any(not isinstance(lens, str) or not lens.strip() for lens in lenses)):
        return "SHIP coverage needs named lenses"
    if len(set(lenses)) != len(lenses):
        return "SHIP coverage repeats a lens"
    if type(coverage.get("sampled")) is not bool:
        return "SHIP coverage must declare whether the review sampled"
    return None


def red_team_coverage_records(summary):
    """Keep each gate's coverage beside redTeam, including foreach outputs."""
    records = []
    contracts = summary.get("contracts") or {}
    for node, value in (summary.get("results") or {}).items():
        for index, result in enumerate(value if isinstance(value, list) else [value]):
            if not isinstance(result, dict):
                continue
            if contracts.get(node) == "RedTeamV1" or (
                    contracts.get(node) is None and result.get("verdict") in ("SHIP", "BLOCK")):
                record = {"node": node, "verdict": result.get("verdict"),
                          "coverage": result.get("coverage")}
                if isinstance(value, list):
                    record["index"] = index
                records.append(record)
    return records



def red_team_unresolved(r):
    if "review" not in r:
        return False
    review = r["review"]
    return (not isinstance(review, dict) or any(k in review and
            (not isinstance(review[k], list) or bool(review[k]))
            for k in ("blockers", "unverified", "deadLenses")))


def red_team_incoherent(r):
    """Why a RedTeamV1 result cannot be taken at its verdict, or None."""
    if r.get("verdict") not in ("SHIP", "BLOCK") or not isinstance(r.get("findings"), list):
        return "invalid red-team verdict or findings array"
    if any(not isinstance(f, dict) or f.get("severity") not in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
           or any(not isinstance(f.get(k), str) for k in ("finding", "exploit_path", "minimal_fix"))
           for f in r["findings"]):
        return "malformed finding or unknown severity cannot be omitted"
    worst = max((SEVERITY_RANK.get((f or {}).get("severity"), 0)
                 for f in (r.get("findings") or []) if isinstance(f, dict)), default=0)
    name = next(k for k, v in SEVERITY_RANK.items() if v == worst)
    if r.get("verdict") == "SHIP" and worst >= SEVERITY_RANK["HIGH"]:
        return f"SHIP over a {name} finding"
    if "worstSeverityRank" in r and type(r["worstSeverityRank"]) is not int:
        return "worstSeverityRank must be an integer"
    if ("worstSeverityRank" in r or "worstSeverity" in r) and (
            r.get("worstSeverityRank") != worst
            or SEVERITY_RANK.get(r.get("worstSeverity")) != worst):
        return (f"worstSeverity {r.get('worstSeverity')}/{r.get('worstSeverityRank')} "
                f"disagrees with its findings, whose worst is {name}")
    if r.get("verdict") == "SHIP":
        issue = red_team_coverage_issue(r.get("coverage"))
        if issue:
            return issue
        if red_team_unresolved(r):
            return "SHIP has unresolved coverage or exploit blockers"
    return None



def red_team_review_receipts(summary):
    """Recognize intermediate claims only after complete, source-bound refutation.

    The compiler writes these receipts from actual panel returns, not from the
    final gate agent. Legacy summaries without receipts retain every BLOCK.
    """
    meta = summary.get("redTeamReviews")
    if meta is None:
        return set(), []
    if not isinstance(meta, dict) or any(not isinstance(meta.get(k), dict)
                                         for k in ("terminals", "checks", "expected")):
        return set(), ["invalid red-team stage/refutation receipts"]
    results = summary.get("results") or {}
    exempt, errors = set(), []
    key = lambda f: tuple(f.get(k) for k in ("severity", "finding", "exploit_path", "minimal_fix"))
    identity = lambda c: sorted(tuple(s[k] for k in ("repository", "base", "head", "files", "additions", "deletions")) for s in c["subject"])
    for terminal, info in meta["terminals"].items():
        final = results.get(terminal)
        if not isinstance(final, dict) or final.get("verdict") != "SHIP":
            continue
        try:
            if red_team_incoherent(final):
                raise ValueError("terminal result is incoherent")
            if not isinstance(info, dict):
                raise ValueError("invalid terminal stage identity")
            sources, stages = info.get("sources"), info.get("stages")
            if (not isinstance(sources, list) or not sources or not all(isinstance(x, str) for x in sources)
                    or len(set(sources)) != len(sources) or not isinstance(stages, list)
                    or not all(isinstance(x, str) for x in stages) or not set(sources) <= set(stages)
                    or terminal in stages):
                raise ValueError("missing source/stage bindings")
            claims, attacker_count = [], 0
            for source in sources:
                values, expected = results.get(source), meta["expected"].get(source)
                if (not isinstance(values, list) or not isinstance(expected, dict)
                        or type(expected.get("attackers")) is not int or expected["attackers"] < 1
                        or len(values) != expected["attackers"]):
                    raise ValueError("missing source attacker")
                named = []
                for r in values:
                    if not isinstance(r, dict) or red_team_incoherent(r):
                        raise ValueError("malformed source review")
                    coverage = r.get("coverage")
                    if red_team_coverage_issue(coverage) or identity(coverage) != identity(final["coverage"]):
                        raise ValueError("source review lacks matching pinned coverage")
                    if coverage["sampled"] and not final["coverage"]["sampled"]:
                        raise ValueError("final review hides sampled source")
                    if not set(coverage["lenses"]) <= set(final["coverage"]["lenses"]):
                        raise ValueError("final review omits a source lens")
                    if red_team_unresolved(r):
                        raise ValueError("source has unresolved coverage")
                    if r["verdict"] == "BLOCK" and not r["findings"]:
                        raise ValueError("source blocked without a refutable claim")
                    attacker_count += coverage["attackers"]
                    named.extend(coverage["lenses"])
                    claims.extend(r["findings"])
                required = expected.get("lenses")
                if not isinstance(required, list) or not set(required) <= set(named):
                    raise ValueError("missing configured source lens")
            if final["coverage"]["attackers"] != attacker_count:
                raise ValueError("terminal attacker count differs from completed sources")
            # Include intermediate collections as well as raw attackers: a new
            # claim cannot disappear merely because the collector invented it.
            for stage in stages:
                if stage not in results:
                    raise ValueError("missing intermediate review stage")
                for row in _each(results[stage]):
                    if "verdict" in row:
                        if red_team_incoherent(row):
                            raise ValueError("malformed intermediate review")
                        if red_team_unresolved(row):
                            raise ValueError("intermediate review has unresolved blockers")
                        claims.extend(row["findings"])
                    elif "severity" in row:
                        claims.append(row)
            checked = set()
            for check in meta["checks"].values():
                if not isinstance(check, dict) or not isinstance(check.get("ids"), list):
                    raise ValueError("invalid independent-refuter binding")
                if not set(check["ids"]) & set(sources):
                    continue
                if set(check["ids"]) != set(sources):
                    raise ValueError("refuter receipt names different sources")
                if check.get("unresolved") is not False:
                    raise ValueError("intermediate review has unresolved blockers")
                requested, rows = check.get("requested"), check.get("rows")
                if type(requested) is not int or requested < 1 or not isinstance(rows, list):
                    raise ValueError("invalid requested refuter count")
                for row in rows:
                    votes = row.get("votes") if isinstance(row, dict) else None
                    if (not isinstance(votes, list) or len(votes) != requested
                            or row.get("unverified") is not False
                            or type(row.get("cast")) is not int or row["cast"] != requested
                            or type(row.get("kills")) is not int or row["kills"] != requested
                            or any(not isinstance(v, dict) or v.get("refuted") is not True
                                   or not isinstance(v.get("reason"), str) or not v["reason"].strip() for v in votes)):
                        raise ValueError("missing, divided or unresolved independent refuters")
                    if row.get("severity") in ("MEDIUM", "HIGH", "CRITICAL") and requested < 3:
                        raise ValueError("MEDIUM+ claim needs three independent refuters")
                    checked.add(key(row))
            if any(key(claim) not in checked for claim in claims):
                raise ValueError("source/intermediate claim has no unanimous refutation receipt")
            exempt.update(stages)
        except (ValueError, TypeError, KeyError) as e:
            errors.append(f"{terminal}: red-team refutation receipts refused: {e}")
    return exempt, errors


def cell(s, n=160):
    """One table cell: no pipes, no newlines, bounded."""
    s = str(s).replace("|", "\\|").replace("\n", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def decision_rows(summary, ts):
    """A row per DecisionV1 the run produced, newest-run-first order.

    Imported decisions are excluded: they were decided — and filed — by the
    run named in decisionsImported, and re-filing them under every honoring
    campaign would stamp an old choice with a new date once per run.
    """
    results = summary.get("results") or {}
    contracts = summary.get("contracts") or {}
    decided = summary.get("decisions") or {}
    imported = summary.get("decisionsImported") or {}
    # Prefer the campaign's own decisions map; fall back to the contract map
    # so a graph that produced a DecisionV1 without `decides` is still filed.
    seen, rows = set(), []
    for did, rec in decided.items():
        if did in imported or not isinstance(rec, dict):
            continue
        seen.add(id(rec))
        rows.append((did, rec))
    for node, value in results.items():
        if contracts.get(node) == "DecisionV1" and isinstance(value, dict):
            if id(value) not in seen:
                rows.append((node, value))
    out = []
    for did, r in rows:
        out.append(
            f"| {ts} | {cell(did, 80)} | {cell(r.get('chosen'), 60)} "
            f"| {'YES' if r.get('overturned_prior') else 'no'} "
            f"| {cell(r.get('frozen_by') or 'none', 40)} "
            f"| {cell(r.get('rationale'))} |"
        )
    return out


def count_items(results, reducers=()):
    """Total array items across all node results.

    Deliberately generic: a review campaign's arrays are findings, a build
    campaign's are tests and evidence lines. The Evidence row therefore says
    "item(s)", not "finding(s)" — calling a slice's test list "verified
    findings" would overstate what the run actually established.

    Reduce nodes are excluded: their output is the source node's items,
    fewer, and counting both sides of a reduce would inflate "produced" by
    exactly the work the reducer exists to remove.
    """
    n = 0
    for node, v in (results or {}).items():
        if node in reducers:
            continue
        if isinstance(v, list):
            n += len(v)
        elif isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, list):
                    n += len(vv)
    return n


def _each(value):
    """Yield the result objects a node produced: one, or one per item."""
    if isinstance(value, list):
        for v in value:
            if isinstance(v, dict):
                yield v
    elif isinstance(value, dict):
        yield value


def derive(summary):
    """Read the harness exit and red-team verdict out of the run itself.

    These arrive as CLI flags defaulting to 'n/a', which makes the two
    columns that decide whether a campaign shipped depend on a live
    orchestrator remembering to pass them. The run already knows: nodes
    carry declared contracts, and the compiler emits the nodeId -> contract
    map precisely so this does not have to guess from a result's shape.

    Returns (harness_exit, red_team_verdict, blocked, proof_verdict,
    proof_surface, incoherent) with None where the campaign genuinely
    produced no such node; `incoherent` lists red-team results whose verdict
    their own findings contradict, each of which blocks like a BLOCK. The proof verdict is the ProofV1 node's, worst of several, and
    proof_surface is how many files, obligations or suites it reviewed —
    a SOUND over zero is vacuous and the row says so.
    """
    results = summary.get("results") or {}
    contracts = summary.get("contracts") or {}
    exempt, receipt_errors = red_team_review_receipts(summary)
    harness, verdict, blocked = None, ("BLOCK" if receipt_errors else None), bool(receipt_errors)
    proof, proof_surface = None, None
    incoherent = list(receipt_errors)
    for node, value in results.items():
        c = contracts.get(node)
        for r in _each(value):
            if c == "HarnessCheckV1" or (c is None and "exit" in r):
                e = r.get("exit")
                if e is not None:
                    # Worst exit across every harness node: one red run is
                    # the campaign's answer, whatever a later one says.
                    harness = e if harness in (None, 0) else harness
            if node not in exempt and (c == "RedTeamV1" or (c is None and r.get("verdict") in ("SHIP", "BLOCK"))):
                v = r.get("verdict")
                why = red_team_incoherent(r)
                if why:
                    incoherent.append(f"{node}: {why}")
                    v = "BLOCK"
                if v == "BLOCK":
                    verdict, blocked = "BLOCK", True
                elif v and verdict is None:
                    verdict = v
            pv = r.get("verdict")
            if c == "ProofV1" or (c is None and pv in PROOF_RANK and "surface" in r):
                if pv in PROOF_RANK and (proof is None or PROOF_RANK[pv] > PROOF_RANK[proof]):
                    proof = pv
                    sf = r.get("surface")
                    proof_surface = len(sf) if isinstance(sf, list) else 0
    return harness, verdict, blocked, proof, proof_surface, incoherent


def repair_receipts(summary, graph):
    """Freeze the actual graph's independent checker descriptors at recording."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repair_checks.py")
    spec = importlib.util.spec_from_file_location("repair_checks_recorder", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    frozen = helper.from_graph(graph)
    commits, problems = helper.checked(summary, frozen)
    return frozen, commits, problems


def checked_commits(summary, graph):
    return repair_receipts(summary, graph)[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--graph", default="WORK.md",
                    help="the graph file the run was compiled from: its SPEC: header "
                         "names the packet the run is held to")
    ap.add_argument("--evidence", default=None,
                    help="the file carrying the Evidence and Decisions tables the rows "
                         "go into (default: --graph). A campaign in GRAPH.<name>.md "
                         "with a packet of its own files into WORK.md this way")
    ap.add_argument("--result", help="file holding the workflow's return value (default stdin)")
    ap.add_argument("--harness", default="n/a", help="independent harness exit code")
    ap.add_argument("--red-team", default="n/a", help="SHIP | BLOCK | n/a")
    ap.add_argument("--proof-audit", default="n/a",
                    help="SOUND | WEAKENED | UNPROVEN | NOT-APPLICABLE | n/a")
    ap.add_argument("--executor", default="workflow", choices=("workflow", "degraded-subagents"))
    ap.add_argument("--state-dir", default=None,
                    help="must resolve to <root>/.claude/fluxpoint/runs")
    ap.add_argument("--root", default=".", help="repo root holding .claude/fluxpoint")
    args = ap.parse_args()
    args.root = os.path.realpath(args.root)
    canonical_runs = os.path.realpath(os.path.join(args.root, ".claude", "fluxpoint", "runs"))
    if args.state_dir is not None and os.path.realpath(args.state_dir) != canonical_runs:
        ap.error("--state-dir must resolve to " + canonical_runs)
    args.state_dir = canonical_runs
    if (not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", args.run_id or "")
            or args.run_id.endswith('.') or args.run_id in (".", "..")
            or args.run_id.split('.')[0].upper() in ({"CON", "PRN", "AUX", "NUL"}
                | {"COM" + str(n) for n in range(1, 10)} | {"LPT" + str(n) for n in range(1, 10)})):
        ap.error("--run-id must be a safe filename component without drive prefixes or streams")
    evidence_file = args.evidence or args.graph

    raw = open(args.result, encoding="utf-8").read() if args.result else sys.stdin.read()
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"record-run: result is not JSON: {e}", file=sys.stderr)
        return 1

    # 0. Irreversible effects go into the once-only ledger before anything
    # can refuse this record. The ledger does not vouch for the run — it
    # records that an effect nobody can undo already happened, so the next
    # resume replays it instead of performing it twice. Refusing the record
    # first (a stale packet, say) used to leave the effect on chain with no
    # row, and the next resume asked to confirm it again.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ledger_written = []
    try:
        import ledger as _ledger
        ledger_written = _ledger.append_from_summary(args.root, summary, args.run_id)
        for r in ledger_written:
            print(f"record-run: ledger recorded {r['node']} — {r['evidence']}")
    except Exception as e:  # noqa: BLE001 - never lose the Evidence row over this
        print(f"record-run: ledger append failed: {e}", file=sys.stderr)

    # Pin bytes are harness inputs, not a claim an agent can vouch for. Do
    # this after the once-only ledger append, but before any run/Evidence row.
    try:
        import evidence_pins as _pins
        _pins.validate_pinned_inputs(args.root, summary.get("inputs") or {},
                                     summary.get("evidenceUnpinned") or [])
    except (OSError, ValueError, TypeError) as e:
        print(f"record-run: evidence pin verification failed: {e}", file=sys.stderr)
        return 1

    import specification as _spec
    harness_identity = None
    try:
        # The graph file's SPEC: header names the packet this campaign is
        # held to; a repository can carry one per campaign.
        spec_path = _spec.canonical(_spec.graph_headers(args.graph).get("SPEC"))
    except (OSError, ValueError) as e:
        print(f"record-run: {args.graph}: {e}", file=sys.stderr)
        return 1
    if "specification" in summary or spec_path or _spec.required(args.root):
        try:
            ran = _spec.canonical(summary.get("specificationPath"))
            if ran and spec_path and ran != spec_path:
                raise ValueError(f"the run was compiled against {ran}, but {args.graph} "
                                 f"declares SPEC: {spec_path}")
            _, identity = _spec.load(args.root, spec=spec_path or ran)
            if summary.get("specification") != identity:
                raise ValueError("run specification differs from the locked packet")
            harness_identity = _spec.harness_record(args.root, spec=spec_path or ran)
        except (OSError, ValueError, TypeError) as e:
            print(f"record-run: spec verification failed: {e}", file=sys.stderr)
            if ledger_written:
                print(f"record-run: the once-only ledger still recorded "
                      f"{len(ledger_written)} irreversible effect(s) from this run — "
                      f"they happened whatever the packet says, and a resume "
                      f"must replay them rather than fire them again",
                      file=sys.stderr)
            return 1

    prov = summary.get("provenance") or []
    ok = sum(1 for p in prov if p.get("status") == "OK")
    dead = sum(1 for p in prov if p.get("status") == "DEAD")
    # Work declined for budget is neither success nor failure, and must never
    # be filed as either — a skipped node means the campaign covered less
    # ground than it set out to.
    skipped = sum(1 for p in prov if p.get("status") == "SKIPPED")
    partial = [p for p in prov if p.get("status") == "INCOMPLETE"]
    outcome = summary.get("outcome", "UNKNOWN")
    findings = count_items(summary.get("results"),
                           set(summary.get("reducers") or []))
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

    # Derived beats declared: the flags are a fallback for a run whose
    # summary carries no such node, never an override of one that does.
    d_harness, d_verdict, blocked, d_proof, proof_surface, incoherent = derive(summary)
    red_team_coverage = red_team_coverage_records(summary)
    for why in incoherent:
        print(f"record-run: red-team result refused at its verdict — {why}",
              file=sys.stderr)
    harness = str(d_harness) if d_harness is not None else args.harness
    red_team = d_verdict if d_verdict is not None else args.red_team
    if d_verdict is None and red_team == "SHIP":
        incoherent.append("SHIP flag has no recorded red-team verdict and coverage")
        blocked, red_team = True, "BLOCK"
        print("record-run: a SHIP flag without a coverage-bearing result cannot "
              "establish a shippable review", file=sys.stderr)
    proof_audit = d_proof if d_proof is not None else args.proof_audit
    if blocked and outcome in ("COMPLETE", "INCOMPLETE", "UNKNOWN"):
        # A campaign does not get to report COMPLETE over a blocking
        # verdict it collected. Halting is the graph's job; refusing to
        # file the run as clean is this script's.
        outcome = "BLOCKED-REDTEAM"
    weakened = d_proof == "WEAKENED"
    if weakened and outcome in ("COMPLETE", "INCOMPLETE", "UNKNOWN"):
        # "Treat WEAKENED as harness-red" was policy; this is the mechanism.
        outcome = "BLOCKED-PROOF"
    elif d_proof == "UNPROVEN" and outcome in ("COMPLETE", "UNKNOWN"):
        # No checker ran. That is the same sentence as a prove: node that
        # cited no attestation: the declared verification did not happen.
        outcome = "INCOMPLETE"

    # 1a. Irreversible effects were filed into the once-only ledger at step
    # 0, before anything in this script could refuse the record.

    # 1b. Park the waits and raise anything that needs a person. A campaign
    # that parks instead of halting is only an improvement if somebody finds
    # out; otherwise it is a quieter failure than the halt it replaced.
    campaign = summary.get("campaign", "")
    blocked_nodes = [p.get("node") for p in prov if p.get("status") == "BLOCKED"]
    try:
        import inbox as _inbox
        recs = summary.get("recommendations") or {}
        for p in prov:
            if p.get("status") == "BLOCKED":
                # The recommendation is the point of the row. An inbox item
                # that only says "blocked on you" moves the work to a person
                # without moving it forward.
                r = recs.get(p.get("node")) or {}
                detail = p.get("detail") or "waiting on a person"
                if r.get("chosen"):
                    detail = (f"RECOMMENDED: {r['chosen']}\n      WHY: "
                              f"{r.get('rationale', '')}\n      ASK: {detail}")
                    rejected = [o.get("option") for o in (r.get("options") or [])
                                if o.get("option") and o.get("option") != r["chosen"]]
                    if rejected:
                        detail += f"\n      ALSO CONSIDERED: {', '.join(rejected[:3])}"
                _inbox.add(args.root, "blocked", p.get("node"), campaign, detail)
        if outcome == "CONFIRM-REQUIRED":
            refused = [p.get("node") for p in prov if p.get("status") == "REFUSED"]
            _inbox.add(args.root, "confirm-required", refused[0] if refused else "",
                       campaign,
                       "an irreversible node refused to fire without being named "
                       "in confirm")
        waits = summary.get("waits") or []
        if waits:
            wdir = os.path.join(args.root, ".claude", "fluxpoint", "waits")
            os.makedirs(wdir, exist_ok=True)
            for w in waits:
                rec = dict(w)
                rec["campaign"] = campaign
                # The resume point rides with the wait: a poller that knows
                # the condition cleared but not which run to continue has
                # moved the problem rather than solved it.
                rec["runId"] = args.run_id
                rec.setdefault("lastChecked", 0)
                safe = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                               for ch in f"{campaign}.{w.get('node','')}")[:120]
                with open(os.path.join(wdir, f"{safe}.json"), "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(rec, fh, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"record-run: inbox/waits update failed: {e}", file=sys.stderr)

    # 1c. Cross-check every claimed gate exit against the hook-minted log.
    # Warn mode: a mismatch is said loudly and filed, but does not rewrite the
    # outcome yet. The corpus this produces is what earns the enforcing
    # version — a check that starts by failing runs is a check people turn
    # off before it has established what normal looks like.
    attestation = None
    try:
        import attest as _attest
        checks, afindings = _attest.verify_claims(args.root, summary)
        for f in afindings:
            print(f"record-run: {f}", file=sys.stderr)
        # A manifest the witness refuses mints no rows, so every citation in
        # this run is unverifiable. For a run that declared prove: nodes that
        # is the declared verification not happening, not a warning to print
        # beside a COMPLETE: a fabricated attestId would otherwise be filed
        # clean.
        if afindings and outcome in ("COMPLETE", "UNKNOWN") and (summary.get("prove") or summary.get("cites") or {}):
            outcome = "INCOMPLETE"
            print("record-run: the gate manifest is refused, so no prove: citation in "
                  "this run can be checked — filing it INCOMPLETE", file=sys.stderr)
        if checks:
            tally = {"checked": len(checks)}
            for st in ("ATTESTED", "UNATTESTED", "MISMATCH", "STALE", "SUPERSEDED-RED"):
                n = sum(1 for c in checks if c["status"] == st)
                if n:
                    tally[st.lower()] = n
            fault_ids = {ident for check in checks for ident in check.get('faultEventIds', [])}
            if fault_ids:
                tally['fault_events'] = len(fault_ids)
            # Recorded separately because the two absences mean different
            # things: a claimed red with no row is expected (failures are not
            # attested), a claimed green with no row is not.
            sg = sum(1 for c in checks
                     if c["status"] == "UNATTESTED" and c.get("claimedExit") == 0)
            if sg:
                tally["unattested_claiming_pass"] = sg
            attestation = {"tally": tally, "checks": checks}
            for c in checks:
                stream = sys.stderr if c["status"] in ("MISMATCH", "SUPERSEDED-RED") else sys.stdout
                print(f"record-run: attest [{c['status']}] {c['node']} — {c['detail']}",
                      file=stream)
                if c["status"] in ("MISMATCH", "SUPERSEDED-RED"):
                    import inbox as _ibx
                    _ibx.add(args.root, "attest-mismatch", c["node"], campaign,
                             c["detail"])
            # A node that declared `verify: prove:<gate>` asked to be held to
            # the hook's record. A contradiction there is not a warning: the
            # run's own verification says its exit codes are not what
            # happened, and a campaign does not get to file that as clean.
            # Nodes that merely happen to match a declared gate stay observed
            # rather than enforced — a check that starts by failing runs gets
            # switched off, and that lesson still holds for everyone who did
            # not opt in.
            if any(c.get('declared') and type(c.get('claimedExit')) is int and
                   c['claimedExit'] != 0 for c in checks) and outcome in ('COMPLETE', 'UNKNOWN'):
                outcome = 'INCOMPLETE'
                print('record-run: a declared gate step is red — filing INCOMPLETE', file=sys.stderr)
            tampered = [c for c in checks
                        if c["status"] in ("MISMATCH", "SUPERSEDED-RED") and c.get("declared")]
            unproven = [c for c in checks
                        if c["status"] == "UNATTESTED" and c.get("declared")]
            # A citation that matches its row but not this run — minted
            # before the launch, on another commit, or already backing a
            # node — means the declared verification did not happen here.
            # Filed INCOMPLETE rather than TAMPERED: a lazy citation and a
            # resume launched with a fresh stamp look the same from here,
            # and neither is a clean run.
            stale = [c for c in checks
                     if c["status"] == "STALE" and c.get("declared")]
            if stale and not tampered and outcome in ("COMPLETE", "UNKNOWN"):
                outcome = "INCOMPLETE"
                print(f"record-run: {len(stale)} declared prove: node(s) cite an "
                      f"execution this run did not produce — "
                      + "; ".join(f"{c['node']}: {c['detail']}" for c in stale),
                      file=sys.stderr)
            if tampered:
                outcome = "TAMPERED-EXECUTION"
                print(f"record-run: {len(tampered)} declared prove: node(s) "
                      f"contradict the attest log — filing this run as "
                      f"TAMPERED-EXECUTION", file=sys.stderr)
            # Said whatever else this run is filed as: a STALE citation beside
            # it used to set INCOMPLETE first and silence this — the one
            # absence worth suspecting, hidden behind a lesser one.
            if unproven and not tampered:
                # The verification the graph declared did not happen. That is
                # not tampering — an executor that never routes through the
                # Bash tool leaves no rows — but it is not a clean run either.
                #
                # Absence is not equally suspicious in both directions, and
                # saying so is the only thing the cross-check can do about the
                # measured asymmetry: PostToolUse does not fire when a Bash
                # call fails, so a red gate is never attested. A node claiming
                # a NON-ZERO exit with no row is therefore consistent with a
                # gate that really failed — the log could not have recorded it.
                # A node claiming exit 0 with no row is the shape MISMATCH
                # exists to catch and structurally cannot: the runtime does
                # mint rows for passes, so a claimed pass should have left one.
                silent_green = [c for c in unproven if c.get("claimedExit") == 0]
                if outcome in ("COMPLETE", "UNKNOWN"):
                    outcome = "INCOMPLETE"
                print(f"record-run: {len(unproven)} declared prove: node(s) cited "
                      f"no attestation — the declared verification did not run",
                      file=sys.stderr)
                if silent_green:
                    print(f"record-run: {len(silent_green)} of them claim exit 0 "
                          f"with no row. A pass leaves a row; a failure may not. "
                          f"Treat these as the unverified ones: "
                          + ", ".join(sorted(c["node"] for c in silent_green)),
                          file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - a witness must never eat the record
        print(f"record-run: attestation cross-check failed: {e}", file=sys.stderr)
        if summary.get('prove') or summary.get('cites'):
            outcome = 'INCOMPLETE'

    # 1. Durable provenance artifact.
    import replay_memo as _replay
    replay_checked, rejudges, replay_problems = [], [], []
    try:
        replay_checked = _replay.verify_replays(args.root, summary, graph=args.graph)
        rejudges = _replay.verify_rejudges(args.root, summary, graph=args.graph)
    except (ValueError, OSError, TypeError) as exc:
        replay_problems.append(str(exc))
        outcome = "REFUSED-REPLAY"
        print("record-run: REPLAY-PROVENANCE — " + str(exc), file=sys.stderr)
    repair_checkers, checked, repair_problems = repair_receipts(summary, args.graph)
    for issue in repair_problems:
        print("record-run: checked commit refused — " + issue, file=sys.stderr)
    os.makedirs(args.state_dir, exist_ok=True)
    art = os.path.join(args.state_dir, f"{args.run_id}.json")
    with open(art, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "runId": args.run_id,
                "when": ts,
                # To the second: `when` keeps the Evidence row's minute for
                # every reader that parses it, and this is what orders a run
                # against a decision recorded in the same minute.
                "recordedAt": datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"),
                "executor": args.executor,
                "graph": os.path.relpath(os.path.realpath(args.graph), args.root).replace(os.sep, "/"),
                "checked": checked,
                "repairCheckers": repair_checkers,
                "repairCheckProblems": repair_problems,
                "replayChecked": replay_checked,
                "rejudges": rejudges,
                "replayProblems": replay_problems,
                "outcome": outcome,
                "nodesOk": ok,
                "nodesDead": dead,
                "nodesSkipped": skipped,
                "findings": findings,
                "harnessExit": harness,
                "harness": harness_identity,
                "redTeam": red_team,
                "redTeamCoverage": red_team_coverage,
                "proofAudit": proof_audit,
                "attestation": attestation,
                "summary": summary,
            },
            fh,
            indent=2,
        )

    if replay_problems:
        print(f"record-run: {art}; artifact retained, Evidence row refused", file=sys.stderr)
        return 2

    # 1d. Lessons, filed from the run's own summary. Written after the run
    # artifact exists on purpose: memory.py refuses provenance that points at
    # no recorded run, which is the check that stops a fabricated summary
    # from planting durable knowledge.
    lessons = None
    try:
        import memory as _memory
        written, mfindings = _memory.append_from_summary(
            args.root, summary, args.run_id, args.state_dir)
        for f in mfindings:
            print(f"record-run: memory {f}", file=sys.stderr)
        if written:
            killed = sum(1 for r in written if r["status"] == "killed")
            lessons = {"filed": len(written), "killed": killed}
            print(f"record-run: filed {len(written)} lesson(s), {killed} killed")
    except Exception as e:  # noqa: BLE001 - never lose the Evidence row over this
        print(f"record-run: lesson filing failed: {e}", file=sys.stderr)

    # 1e. The recall index, refreshed after the lessons landed so the next
    # session's SessionStart section and the next campaign's seedmap rank
    # over what this run just filed. The index is a rebuildable projection
    # — a failure here costs recall freshness, never the Evidence row, and
    # the store stays the only truth. Dense backfill runs only when a key
    # is present; FPL_MEMORY_INDEX=0 / FPL_MEMORY_EMBED=0 opt out.
    recall_stats = None
    if os.environ.get("FPL_MEMORY_INDEX", "1") != "0":
        try:
            import recall as _recall
            recall_stats = _recall.refresh(
                args.root,
                embed=os.environ.get("FPL_MEMORY_EMBED", "1") != "0")
            print(f"record-run: recall index refreshed — "
                  f"{recall_stats['docs']} doc(s), dense "
                  f"{recall_stats['provider']}"
                  + (f", {recall_stats['pending']} pending"
                     if recall_stats.get("pending") else ""))
        except Exception as e:  # noqa: BLE001 - never lose the Evidence row over this
            print(f"record-run: recall index refresh failed: {e}",
                  file=sys.stderr)

    # 2. Evidence row, appended under whichever table header the file carries.
    claim = f"graph run: {ok} node(s) OK, {dead} dead, {findings} produced item(s)"
    if incoherent:
        claim += ("; red-team findings or coverage contradict its verdict (" + "; ".join(incoherent[:2])
                  + ") — filed as BLOCK")
    elif blocked:
        claim += "; red-team returned BLOCK — not shippable"
    if weakened:
        claim += ("; proof-audit returned WEAKENED — verification got weaker while "
                  "the checker stayed green")
    elif d_proof == "UNPROVEN":
        claim += "; proof-audit UNPROVEN — no checker ran, static review only"
    elif d_proof == "SOUND" and not proof_surface:
        claim += ("; proof-audit SOUND over an EMPTY surface — vacuous, treat the "
                  "proofs as unreviewed")
    if blocked_nodes:
        claim += (f"; {len(blocked_nodes)} node(s) BLOCKED on a person "
                  f"({', '.join(blocked_nodes[:3])})")
    if skipped:
        claim += f"; {skipped} SKIPPED on budget — coverage incomplete"
    for rec in red_team_coverage:
        cov = rec.get("coverage")
        if not isinstance(cov, dict):
            claim += f"; red-team {cell(rec['node'], 60)} coverage missing"
            continue
        ranges = cov.get("subject") or []
        if isinstance(ranges, list) and all(isinstance(item, dict) for item in ranges):
            files = sum(item.get("files", 0) for item in ranges if type(item.get("files")) is int)
            claim += (f"; red-team {cell(rec['node'], 60)}: {len(ranges)} repo range(s), "
                      f"{files} files, {cell(cov.get('attackers'), 20)} attacker(s), "
                      f"{len(cov['lenses']) if isinstance(cov.get('lenses'), list) else 0} lens(es), "
                      + ("SAMPLED" if cov.get("sampled") is True else
                         "sampling unknown" if type(cov.get("sampled")) is not bool else "full declared scope"))
    seeded = summary.get("memorySeeded") or {}
    if seeded or lessons:
        total_seeded = sum(v for v in seeded.values() if isinstance(v, int))
        # A sweep standing on prior ground and one starting cold produce the
        # same finding count, so the row has to say which this was.
        parts = [f"seeded {total_seeded} prior key(s)"] if seeded else []
        if lessons:
            parts.append(f"filed {lessons['filed']} lesson(s), "
                         f"{lessons['killed']} killed")
        # The ranking universe is named so keyed and keyless teammates
        # producing different seed orders stay attributable from the row.
        if recall_stats:
            parts.append(f"recall: {recall_stats['provider']}")
        claim += "; memory: " + ", ".join(parts)
    imported = summary.get("decisionsImported") or {}
    if replay_checked:
        claim += "; REPLAYED-MEMO " + ", ".join(r["node"] for r in replay_checked)
    for rejudge in rejudges:
        detail = json.dumps({k: rejudge.get(k, "") for k in ("gate", "nonce", "reason", "note")}, ensure_ascii=False)
        claim += "; REJUDGED " + detail.replace("|", "&#124;")
    if imported:
        claim += ("; honors " + ", ".join(
            f"{d}@{imported[d]}" for d in sorted(imported)[:2]))
        if len(imported) > 2:
            claim += f" +{len(imported) - 2} more imported decision(s)"
    for p in partial:
        claim += f"; {p.get('node')} INCOMPLETE — {p.get('detail') or 'did not run to exhaustion'}"
    if attestation:
        t = attestation["tally"]
        # Each category said on its own: one does not excuse another.
        if t.get("mismatch"):
            claim += (f"; {t['mismatch']} gate claim(s) CONTRADICT the attested "
                      f"execution log — the exit codes are not trustworthy")
        if t.get('superseded-red'):
            claim += f"; {t['superseded-red']} green gate claim(s) leave earlier red attempts unsettled"
        if t.get('fault_events'):
            claim += f"; {t['fault_events']} verified host crash event(s) retained"
        if t.get("stale"):
            claim += (f"; {t['stale']} gate claim(s) STALE — they cite executions "
                      f"from before this run, another commit, or another node")
        if t.get("unattested"):
            claim += (f"; {t['unattested']} gate claim(s) UNATTESTED — self-reported "
                      f"exit code(s), no hook-minted record"
                      + (f", {t['unattested_claiming_pass']} of them claiming a pass"
                         if t.get("unattested_claiming_pass") else ""))
    if d_proof == "NOT-APPLICABLE":
        proof_cell = "n/a (no proof surface)"
    elif d_proof in ("SOUND", "WEAKENED"):
        proof_cell = f"{d_proof} ({proof_surface} surface item(s))"
    else:
        proof_cell = proof_audit
    proof = (f"harness exit {harness}; red-team {red_team}; proof-audit {proof_cell}; "
             f"executor {args.executor}")
    if harness_identity:
        proof += f"; harness sha256 {harness_identity['sha256']}"
        if harness_identity['changedPaths']:
            proof += "; harness differs from lock: " + ", ".join(cell(p, 500) for p in harness_identity['changedPaths'])
    if attestation:
        att = attestation["tally"]
        proof += ("; attestation " + ", ".join(
            f"{k} {v}" for k, v in sorted(att.items()) if k != "checked"))
    row = f"| {ts} | {args.run_id} | {outcome} | {claim} | {proof} |"
    legacy_row = (
        f"| {ts} | {args.run_id} | {outcome} | {ok}/{dead} | {findings} "
        f"| {harness}"
        + (f"; sha256 {harness_identity['sha256']}; changed: "
           + (", ".join(cell(p, 500) for p in harness_identity['changedPaths']) or "none")
           if harness_identity else "")
        + f" | {red_team} |"
    )

    from workfile_lock import workfile_lock
    row_appended = False
    with workfile_lock(evidence_file):
        # 2a. Decisions, spliced under their own header when the file has one.
        drows = decision_rows(summary, ts)
        if drows and os.path.exists(evidence_file):
            text = open(evidence_file, encoding="utf-8").read()
            if DEC_HDR in text:
                text, n = re.subn(
                    re.escape(DEC_HDR) + r"\n\|[-| ]+\|\n",
                    lambda m: m.group(0) + "\n".join(drows) + "\n", text, count=1)
                if n:
                    open(evidence_file, "w", encoding="utf-8", newline="\n").write(text)
                    for d in drows:
                        print(d)
            else:
                print(f"record-run: {len(drows)} decision(s) recorded in the run "
                      f"artifact, but {evidence_file} has no Decisions table to append "
                      f"to — add one so a frozen choice outlives this run",
                      file=sys.stderr)

        if os.path.exists(evidence_file):
            from evidence_table import append as append_evidence
            text = open(evidence_file, encoding="utf-8").read()
            text, status, used_header = append_evidence(text, {ROW_HDR: row, LEGACY_HDR: legacy_row})
            if status == "ok":
                open(evidence_file, "w", encoding="utf-8", newline="\n").write(text)
                row = row if used_header == ROW_HDR else legacy_row
                row_appended = True
            else:
                why = "no recognized Evidence header" if status == "no-header" else "no separator beneath its Evidence header"
                print(f"record-run: {evidence_file} has {why}; artifact written but row NOT appended. "
                      f"Add {ROW_HDR} and a separator outside code fences, or pass --evidence WORK.md", file=sys.stderr)
        else:
            print(f"record-run: Evidence file {evidence_file} does not exist; artifact "
                  f"written but row NOT appended. Add {ROW_HDR} and its separator, "
                  f"or pass --evidence WORK.md", file=sys.stderr)
    print(f"record-run: {art}")
    if row_appended:
        print(row)
    return 0 if row_appended else 3


if __name__ == "__main__":
    sys.exit(main())
