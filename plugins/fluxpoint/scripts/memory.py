#!/usr/bin/env python3
"""Lessons: what campaigns established, kept where the next campaign reads.

A sweep's most expensive output is not its surviving findings — it is the
argument that produced them. The panel's objections, and above all the
findings it *killed* and why, are recorded in `runs/<runId>.json` and then
never looked at again. So the next sweep over the same ground re-finds the
same items, spends the same refuter fan-out re-arguing them, and arrives at
the same verdicts, having learned nothing from the run before it. The
measured lesson from the first campaign was that verification dominates
cost; re-buying round one every time is where that cost goes.

This file is where a finding stops being per-run. Rows are appended by
`record-run.py` from the compiled graph's own summary — never written by an
agent — keyed by the node's declared dedupe fields, so two runs finding the
same thing produce the same key.

  memory.py --append --run-id ID    file lessons from a run summary (stdin)
  memory.py --load --tag T          print the seed map for args._seen
  memory.py --list [--tag T]        what is known, for humans
  memory.py --adopt WORKTREE        import a pre-upgrade worktree store

Two disciplines carried over from the ledger, both load-bearing:

  * A malformed line is a hard error, never a skipped row. A memory that
    silently shrinks is worse than no memory, because a sweep seeded from a
    truncated file believes it is standing on more ground than it is.
  * Identity is the thing, not the event: `tag|dedupeKey`, latest state
    wins. Re-finding a lesson appends a new row rather than editing the old
    one, so the history of what was believed when stays readable.
  * Latest-state-wins applies to the CLAIM and never to the count. Every row
    carries `arrivals` — the distinct runs that have filed this identity —
    and, where the emitting node declared one, `classArrivals` over a coarser
    class key. Without those, a lesson found a third time left a store that
    looked exactly as it had the first time, which is how a defect recurs
    while every individual fix for it was correct. recurrence-guard.py reads
    them; this file only counts.

What this deliberately does NOT do: suppress. Seeds are advisory — they
tell a finder what earlier rounds surfaced so it can spend itself on new
ground, and nothing here drops an item a finder reports anyway. A killed
lesson is a prior, not a verdict; the code it was about can change, and a
sweep that silently dropped a re-found item would hide exactly the
regression it was run to catch.
"""
import argparse
import datetime
import json
import os
import re
import sys
import hashlib
from pathlib import Path
import subprocess
import tempfile

from workfile_lock import workfile_lock

MEMORY = os.path.join(".claude", "fluxpoint", "memory.jsonl")
RUNS = os.path.join(".claude", "fluxpoint", "runs")
STATUSES = {"surviving", "killed", "superseded"}
REQUIRED = ("tag", "dedupeKey", "claim", "status", "node", "provenance")


def storage_home(root):
    """Durable clone-wide store; explicit homes may be synchronized externally."""
    override = os.environ.get("FPL_MEMORY_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    try:
        common = subprocess.run(
            ["git", "-C", os.path.abspath(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True, capture_output=True, timeout=5)
        if common.returncode == 0 and common.stdout.strip():
            return os.path.join(common.stdout.strip(), "fluxpoint")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.join(os.path.abspath(root), ".claude", "fluxpoint")


def path_for(root):
    return os.path.join(storage_home(root), "memory.jsonl")


def runs_path(root):
    return os.path.join(storage_home(root), "runs")


def source_paths(root):
    """Shared lessons plus the checkout's pre-upgrade store, without duplication."""
    paths = [path_for(root), os.path.join(os.path.abspath(root), MEMORY)]
    return list(dict.fromkeys(os.path.realpath(p) for p in paths))


def _read_paths(paths):
    out, seen = [], set()
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as e:
                    raise SystemExit(f"memory: {p}:{i} is not valid JSON: {e}")
                if not isinstance(row, dict):
                    raise SystemExit(f"memory: {p}:{i} is not a lesson object")
                key = json.dumps(row, sort_keys=True)
                if key not in seen:
                    out.append(row)
                    seen.add(key)
    return out


def read(root):
    """Every filed lesson. Readers share the append transaction's lock."""
    paths = source_paths(root)
    if not any(os.path.exists(p) for p in paths):
        return []
    with workfile_lock(path_for(root)):
        return _read_paths(paths)


def history_order(row):
    # ISO timestamps from older releases are UTC. Normalize aware offsets as
    # well so a peer's offset spelling cannot reverse chronological order.
    value = row.get("establishedWhen") or ""
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        stamp = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        stamp = float("-inf")
    return stamp, str((row.get("provenance") or {}).get("runId") or ""), json.dumps(row, sort_keys=True)


def current(root, tag=None):
    """Newest timestamp then run id wins, independently of merged file order."""
    state = {}
    for r in sorted(read(root), key=history_order):
        if tag and r.get("tag") != tag:
            continue
        state[f"{r.get('tag')}|{r.get('dedupeKey')}"] = r
    return list(state.values())


def retain_run(root, run_id, source):
    """Keep a source run after its worktree disappears, with atomic publication."""
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", run_id or ""):
        raise ValueError("memory: invalid run id")
    source = Path(source)
    target = Path(runs_path(root)) / (run_id + ".json")
    if source.resolve() == target.resolve():
        return str(target)
    data = source.read_bytes()
    # Reject invalid copies before publishing them as durable provenance.
    record = json.loads(data.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("memory: run artifact must be an object")
    target.parent.mkdir(parents=True, exist_ok=True)
    with workfile_lock(target):
        if target.exists() and target.read_bytes() == data:
            return str(target)
        # Retain older attempts when a run id is reused by a resumed attempt.
        if target.exists():
            prior = target.read_bytes()
            history = target.parent / "history" / run_id
            history.mkdir(parents=True, exist_ok=True)
            (history / (hashlib.sha256(prior).hexdigest() + ".json")).write_bytes(prior)
        fd, temp = tempfile.mkstemp(prefix=".memory-run-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return str(target)


def adopt(root, worktree):
    """Import a legacy worktree store once; leave its original bytes untouched."""
    legacy = Path(worktree).resolve() / ".claude" / "fluxpoint"
    source = legacy / "memory.jsonl"
    destination = path_for(root)
    if source.resolve() == Path(destination).resolve():
        return 0
    with workfile_lock(destination):
        rows = _read_paths([str(source)])
        existing = {json.dumps(row, sort_keys=True) for row in _read_paths([destination])}
        added = [row for row in rows if json.dumps(row, sort_keys=True) not in existing]
        if added:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            with open(destination, "a", encoding="utf-8", newline="\n") as out:
                out.write("".join(json.dumps(row) + "\n" for row in added))
                out.flush()
                os.fsync(out.fileno())
    for run in (legacy / "runs").glob("*.json"):
        retain_run(root, run.stem, run)
    return len(added)


def class_components(v):
    """Normalized components of a class key, empties PRESERVED.

    The write path needs the empties: a half-filled multi-key class is an
    unnamed shape, and eating the empty component would truncate 'x|' into
    'x' — a collision with a genuine class, manufacturing recurrence."""
    parts = re.split(r"[|.]", str(v or "").lower())
    return [re.sub(r"[^a-z0-9]+", "-", p).strip("-") for p in parts]


def norm_class(v):
    """One spelling per class. Case and punctuation only — never meaning.

    Multi-key classes arrive with their components '|'-joined by the sink,
    and the component boundary IS meaning: ('x','y-z') and ('x-y','z') are
    different classes. Each component normalizes alone and the boundary
    survives as '.', which component normalization can never produce.
    '.' in raw input is treated as the same boundary, keeping the function
    idempotent — its own output round-trips unchanged. This READ-side form
    tolerates stray boundaries; the write path refuses them instead."""
    return ".".join(class_components(v)).strip(".")


def absorb(inst, cls, row):
    """Fold one row into the arrival index. Returns its (instance, class) entry.

    An arrival is a RUN, not a row: a run that files the same item twice
    learned it once. Counted across the whole append-only file, so
    supersession cannot lower it — the newest row wins on CONTENT and
    inherits the count, which is the join the store was missing. Content
    latest-wins was never the defect; forgetting how many times the content
    had to be written was.
    """
    rid = (row.get("provenance") or {}).get("runId")
    tag = row.get("tag")
    ident = f"{tag}|{row.get('dedupeKey')}"
    ie = inst.setdefault(ident, {"runs": [], "first": None, "claim": ""})
    if rid and rid not in ie["runs"]:
        ie["runs"].append(rid)
    if not ie["first"]:
        ie["first"] = row.get("establishedWhen")
    ie["claim"] = row.get("claim") or ie["claim"]
    ck = norm_class(row.get("classKey"))
    if not ck:
        return ie, None
    ce = cls.setdefault(f"{tag}|{ck}", {"runs": [], "identities": [],
                                        "claim": ""})
    if rid and rid not in ce["runs"]:
        ce["runs"].append(rid)
    if ident not in ce["identities"]:
        ce["identities"].append(ident)
    ce["claim"] = row.get("claim") or ce["claim"]
    return ie, ce


def arrival_index(rows):
    """({tag|dedupeKey: entry}, {tag|classKey: entry}) over rows, oldest first."""
    inst, cls = {}, {}
    for r in rows:
        absorb(inst, cls, r)
    return inst, cls


def validate(row):
    """Shallow shape check against LessonV1. Returns a list of findings.

    These rows are machine-produced, so this is a self-check rather than a
    gate on someone's typing — but a producer bug that files half a lesson
    would poison every later sweep, and this is the cheapest place to catch
    one.
    """
    f = []
    for k in REQUIRED:
        if k not in row:
            f.append(f"missing required field '{k}'")
    if row.get("status") not in STATUSES and "status" in row:
        f.append(f"status must be one of {sorted(STATUSES)}, got {row.get('status')!r}")
    claim = row.get("claim")
    if isinstance(claim, str) and len(claim.strip()) < 20:
        f.append("claim is under 20 characters — a lesson nobody can act on")
    prov = row.get("provenance")
    if "provenance" in row and (not isinstance(prov, dict) or not prov.get("runId")):
        f.append("provenance.runId required — a lesson with no run behind it is an assertion")
    return f


def _append_from_summary(root, summary, run_id, state_dir=None):
    """File a row per lesson the run emitted. Returns (written, findings).

    The runId has to name a recorded run. Provenance that points at nothing
    is the opening a fabricated summary would use to plant durable lessons,
    and a row here outlives the run that made it.
    """
    rows = (summary or {}).get("memory") or []
    if not rows:
        return [], []
    runs_dir = state_dir or runs_path(root)
    if state_dir is None and not os.path.exists(os.path.join(runs_dir, f"{run_id}.json")):
        runs_dir = os.path.join(root, RUNS)
    if not os.path.exists(os.path.join(runs_dir, f"{run_id}.json")):
        return [], [
            f"refusing to file {len(rows)} lesson(s): no run artifact for '{run_id}' "
            f"in {runs_dir} — provenance that points at nothing is how a "
            f"fabricated summary would plant durable memory"]
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    retain_run(root, run_id, os.path.join(runs_dir, f"{run_id}.json"))
    history = sorted(_read_paths(source_paths(root)), key=history_order)
    known = {f"{r.get('tag')}|{r.get('dedupeKey')}": r for r in history}
    inst, cls = arrival_index(history)
    written, findings = [], []
    p = path_for(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    for r in rows:
        if not isinstance(r, dict):
            findings.append(f"skipped a lesson that is not an object: {r!r:.60}")
            continue
        row = {
            "tag": r.get("tag"),
            "dedupeKey": r.get("dedupeKey"),
            "claim": r.get("claim"),
            "status": r.get("status"),
            "objection": r.get("objection") or "",
            "kills": r.get("kills", 0),
            "node": r.get("node"),
            "provenance": {"runId": run_id},
            "establishedWhen": when,
        }
        if "classKey" in r:
            # The sink only emits the field when the node DECLARED a
            # class. ANY empty component means the finder never named
            # the full shape: filing the truncated remainder would
            # collide with a genuine class ('x|' is not 'x'), and
            # dropping it silently would re-open the recurrence blind
            # spot one layer down. The row still files — a lesson is
            # not hostage to its class — but classless, and loudly.
            segs = class_components(r.get("classKey"))
            if segs and all(segs):
                row["classKey"] = ".".join(segs)
            else:
                findings.append(
                    f"lesson from node '{r.get('node')}' carries a "
                    f"declared class left empty or half-empty — the "
                    f"finder never named the full shape, so recurrence "
                    f"of this defect kind cannot be counted")
        bad = validate(row)
        if bad:
            findings.append(
                f"skipped a malformed lesson from node "
                f"'{row.get('node')}': {'; '.join(bad)}")
            continue
        prior = known.get(f"{row['tag']}|{row['dedupeKey']}")
        if prior and prior.get("provenance", {}).get("runId") != run_id:
            # Nothing is rewritten: the older row keeps its place in the
            # file and the new one names what it replaced.
            row["supersededBy"] = run_id
        # The count the store used to throw away. Latest-state-wins is
        # right for the CLAIM and wrong for the fact that it had to be
        # made again, so the new row carries the arrival history forward
        # instead of resetting it. A recurrence gate reads this.
        ie, ce = absorb(inst, cls, row)
        row["arrivals"] = len(ie["runs"])
        row["firstSeen"] = ie["first"]
        if ce is not None:
            row["classArrivals"] = len(ce["runs"])
        written.append(row)
        known[f"{row['tag']}|{row['dedupeKey']}"] = row
    if written:
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(json.dumps(row) + "\n" for row in written))
            fh.flush()
            os.fsync(fh.fileno())
    return written, findings


def append_from_summary(root, summary, run_id, state_dir=None):
    if not (summary or {}).get("memory"):
        return [], []
    with workfile_lock(path_for(root)):
        return _append_from_summary(root, summary, run_id, state_dir)


def seed_map(root, tags):
    """{tag: {keys, killed}} for args._seen, one entry per requested tag.

    `keys` is what earlier runs surfaced, in the same shape the compiled
    graph's own seen-list uses. `killed` carries the claim and the objection
    that killed it, which is the part a fresh context cannot reconstruct.
    """
    out = {}
    for tag in tags:
        rows = current(root, tag)
        out[tag] = {
            "keys": [r["dedupeKey"] for r in rows if r.get("dedupeKey")],
            "killed": [
                {"claim": r.get("claim"), "objection": r.get("objection") or ""}
                for r in rows if r.get("status") == "killed"
            ],
        }
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--tag", action="append", default=[],
                    help="tag to load or list; repeatable")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--result", help="run summary JSON file (default stdin)")
    ap.add_argument("--state-dir", help="recorded-runs directory (provenance check)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--append", action="store_true")
    g.add_argument("--load", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--adopt", metavar="WORKTREE")
    a = ap.parse_args()

    if a.adopt:
        print(f"memory: adopted {adopt(a.root, a.adopt)} lesson(s)")
        return 0

    if a.load:
        if not a.tag:
            ap.error("--load requires at least one --tag")
        # Always valid JSON, even when empty: a first sweep has no lessons,
        # which is different from a loader that failed to run.
        print(json.dumps(seed_map(a.root, a.tag)))
        return 0

    if a.list:
        rows = current(a.root, a.tag[0] if a.tag else None)
        if not rows:
            print("memory: no lessons filed yet")
            return 0
        by_tag = {}
        for r in rows:
            by_tag.setdefault(r.get("tag"), []).append(r)
        print(f"memory: {len(rows)} lesson(s) across {len(by_tag)} tag(s)")
        for tag, rs in sorted(by_tag.items()):
            killed = sum(1 for r in rs if r.get("status") == "killed")
            print(f"  [{tag}] {len(rs)} lesson(s), {killed} killed")
            for r in rs[:10]:
                n = max(r.get("arrivals") or 1, r.get("classArrivals") or 1)
                seen = f" x{n}" if n > 1 else ""
                print(f"      ({r.get('status')}{seen}) {str(r.get('claim'))[:90]}")
                if r.get("objection"):
                    print(f"          objection: {str(r['objection'])[:80]}")
            if len(rs) > 10:
                print(f"      [{len(rs) - 10} more — read {path_for(a.root)}]")
        return 0

    if not a.run_id:
        ap.error("--append requires --run-id")
    raw = open(a.result, encoding="utf-8").read() if a.result else sys.stdin.read()
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"memory: result is not JSON: {e}", file=sys.stderr)
        return 1
    written, findings = append_from_summary(a.root, summary, a.run_id, a.state_dir)
    for f in findings:
        print(f"memory: {f}", file=sys.stderr)
    if written:
        killed = sum(1 for r in written if r["status"] == "killed")
        print(f"memory: filed {len(written)} lesson(s), {killed} killed")
    else:
        print("memory: no lessons in this run")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
