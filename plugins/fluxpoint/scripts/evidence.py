#!/usr/bin/env python3
"""Splice one Evidence row, for a caller that already holds the verdict.

Loop rows were written by the agent whose work they certified. The obvious
fix — a script the agent runs that executes a gate and mints a row — is
worse than it looks: a subprocess an agent launches inherits the agent's
environment, cwd, and PATH, so it cannot outrank the agent that launched
it. It would have been a fourth full harness run per iteration for
provenance no stronger than the hand-written row it replaced.

The witness already exists. `dod-gate.sh` is a Stop hook: the *runtime*
invokes it, at the moment the agent claims to be done, and it already
runs its configured harness invocation, holds the exit code, the log, and the
tree state, and already distinguishes PASS from FAIL from TIMEOUT. What was
missing was not another writer — it was a reader. So the gate writes its own
verdict, and this script is the writer it calls: it executes nothing.

Stop verdicts default to ignored local JSONL, rendered at SessionStart.
`FPL_GATE_EVIDENCE=work` opts into the shared table; both table writers
serialize their read/modify/write through the same process lock.

  evidence.py --record --source gate --outcome PASS \\
      --claim "..." --proof "..." [--graph WORK.md]

## The property this actually establishes

Not "a row cannot be forged". `WORK.md` is a markdown file that
`verify-changed.sh` and the hygiene scan both skip, so an agent can write
any text into it, including a `gate` row, and nothing here prevents that.

What it establishes is narrower and real: **there is now a row class an
honest agent never writes.** A `Source: gate` row is authored by a Stop
hook or it is authored by nobody, so a `gate` row appearing in a diff
without a corresponding gate run is a reviewable anomaly — and
`inject-state.sh` renders the two classes differently, so a fresh context
inherits agent assertions labelled as assertions rather than as evidence.
That is a review affordance, not a containment boundary, and the difference
is worth stating rather than blurring.
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile

ROW_HDR = "| When (UTC) | Source | Outcome | Claim | Proof |"
LEGACY_HDR = "| When (UTC) | runId | Outcome | Nodes OK/dead | Findings | Harness | Red-team |"
# `gate` is the class the runtime writes. `loop` and a runId are what the
# agent and the graph recorder write; they are claims, not witnessed facts.
SOURCES = {"gate", "loop"}
OUTCOMES = {"PASS", "FAIL", "TIMEOUT"}


def cell(s, n=160):
    """One table cell: no pipes, no newlines, bounded.

    The same rule record-run.py uses, because one table deserves one
    escaping rule — a claim carrying a `|` would otherwise silently forge
    extra columns.
    """
    s = str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def rows_of(text):
    """Existing Evidence rows, newest first (they are spliced at the top)."""
    m = re.search(re.escape(ROW_HDR) + r"\n\|[-:| ]+\|\n", text)
    if not m:
        return []
    out = []
    for line in text[m.end():].splitlines():
        if not line.startswith("|"):
            break
        out.append(line)
    return out


def parse_row(line):
    parts = [p.strip() for p in line.strip().strip("|").split("|")]
    return parts if len(parts) >= 5 else None


def already_said(text, source, outcome, proof):
    """True when the newest row of this source says exactly this.

    The gate fires on every stop where code changed, so a long green streak
    would otherwise write one identical row per stop and push everything
    else out of the SessionStart window. A row that establishes nothing new
    is not evidence, it is noise.
    """
    for line in rows_of(text):
        p = parse_row(line)
        if not p or p[1] != source:
            continue
        return p[2] == outcome and p[4] == cell(proof)
    return False


def splice(path, source, outcome, claim, proof, when=None):
    from workfile_lock import workfile_lock
    with workfile_lock(path):
        return _splice(path, source, outcome, claim, proof, when)


def append_local(path, source, outcome, claim, proof, when=None):
    from workfile_lock import workfile_lock
    with workfile_lock(path):
        rows = local_rows(path)
        if rows and all(rows[-1].get(k) == v for k, v in
                        (("source", source), ("outcome", outcome), ("proof", proof))):
            return "unchanged"
        row = dict(when=when or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   source=source, outcome=outcome, claim=claim, proof=proof)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return "written"


def local_rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if (isinstance(row, dict) and row.get("source") == "gate"
                            and row.get("outcome") in OUTCOMES):
                        rows.append(row)
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def render_local(path, limit=3):
    rows = local_rows(path)
    if rows:
        print(f"### Recorded by the gate (local: {path}; the runtime ran the harness itself)")
        for r in reversed(rows[-limit:]):
            print("| " + " | ".join(cell(r.get(k, "")) for k in
                  ("when", "source", "outcome", "claim", "proof")) + " |")
        if len(rows) > limit:
            print(f"[elided: {len(rows) - limit} older local gate row(s) — read {path}]")


def _splice(path, source, outcome, claim, proof, when=None):
    """Insert the row under the Evidence header. Returns a status string."""
    if not os.path.exists(path):
        return "no-file"
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if ROW_HDR not in text:
        # The legacy header has no Source column, so a classed row cannot be
        # expressed in it. Saying so beats writing a row that reads as
        # something it is not.
        return "legacy-header" if LEGACY_HDR in text else "no-header"
    if already_said(text, source, outcome, proof):
        return "unchanged"
    ts = when or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    row = (f"| {ts} | {cell(source, 12)} | {cell(outcome, 12)} | {cell(claim)} "
           f"| {cell(proof)} |")
    new, n = re.subn(re.escape(ROW_HDR) + r"\n(\|[-:| ]+\|)\n",
                     lambda m: m.group(0) + row + "\n", text, count=1)
    if not n:
        return "no-separator"
    # Atomic replace: record-run.py may be splicing the same file, and a
    # half-written work file is worse than a missing row.
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".evidence-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return "written"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", default="WORK.md",
                    help="work-state file carrying the Evidence table")
    ap.add_argument("--source", default="gate", choices=sorted(SOURCES))
    ap.add_argument("--local", help="append a local JSONL verdict instead of changing --graph")
    ap.add_argument("--render-local", metavar="JSONL", help="render local gate verdicts")
    ap.add_argument("--outcome", choices=sorted(OUTCOMES))
    ap.add_argument("--claim")
    ap.add_argument("--proof")
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    if a.render_local:
        render_local(a.render_local)
        return 0
    if not a.record or any(v is None for v in (a.outcome, a.claim, a.proof)):
        ap.error("--record, --outcome, --claim and --proof are required")

    # A newline or a pipe in a claim is either an accident or an attempt to
    # forge columns; cell() would silently flatten it, so say so instead.
    for name, val in (("claim", a.claim), ("proof", a.proof)):
        if "\n" in val or "|" in val:
            print(f"evidence: --{name} contains a newline or a pipe; a row is "
                  f"one line and five columns", file=sys.stderr)
            return 2

    status = (append_local(a.local, a.source, a.outcome, a.claim, a.proof) if a.local
              else splice(a.graph, a.source, a.outcome, a.claim, a.proof))
    if a.local:
        a.graph = a.local
    if status == "written":
        print(f"evidence: recorded a {a.source} row ({a.outcome}) in {a.graph}")
        return 0
    if status == "unchanged":
        print(f"evidence: {a.graph} already carries this {a.source} verdict; "
              f"nothing new to record")
        return 0
    msgs = {
        "no-file": f"{a.graph} does not exist",
        "no-header": f"{a.graph} has no Evidence table header",
        "legacy-header": (f"{a.graph} carries the pre-1.0 Evidence header, which "
                          f"has no Source column — run /fluxpoint:migrate"),
        "no-separator": (f"{a.graph} has an Evidence header with no separator row "
                         f"beneath it"),
    }
    print(f"evidence: {msgs.get(status, status)}; no row written", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
