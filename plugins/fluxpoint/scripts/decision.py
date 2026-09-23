#!/usr/bin/env python3
"""Record a decision so the next context cannot silently re-decide it.

The Decisions table is the one bus in this system that carries a *choice*
across context death — SessionStart injects the newest rows, and the graph
compiler can bind a frozen decision into a later campaign's prompt. Until
now only graph nodes could fill it: `record-run.py` files a `DecisionV1` a
campaign produced, and loop mode had no way in at all. So the exact failure
the schema was written against — "a decision that overturned the prior is
the one a fresh context re-decides the other way" — stayed wide open for
loop work, which is most work.

  decision.py --record [--graph WORK.md] [--id <kebab-id>] < decision.json
  decision.py --show <id> [--json]
  decision.py --none "<why nothing was decided>" --session <id>

`--record` validates against the shipped `contracts/DecisionV1.schema.json`
before writing anything. The floors in that schema are the point and are
enforced here rather than described: at least two options, each with a
`strongest_objection` of real length (including against the one that won —
an option with no objection was not examined), and a rationale long enough
that a lazy sentence cannot satisfy it.

Unlike an Evidence row, a decision has no verdict to witness: it is
inherently an assertion, and there is no gate that could certify it. What
this buys is not proof but survival — the choice, the alternatives, and the
objections outlive the context that weighed them, in a form the next one
inherits. That is why the schema floors matter more here than provenance
would.

`--none` writes a per-session marker saying no decision was made and why,
so silence is a statement rather than an absence. Nothing reads it unless
the optional distill check is armed (`FPL_DISTILL=1`), which is deliberately
off by default: a gate that starts by blocking stops gets switched off
before it has established what normal looks like.

The row is an index, not the record. Its cells are cut to fit a table —
60 characters of the choice, 160 of the rationale — and the question, every
option with its objection, and the evidence do not fit at all. So the whole
validated record is appended to `.claude/fluxpoint/decisions.jsonl` before
the row is written, the row carries the id whole, and `--show <id>` prints
the record back: the newest one for that id, from this store or from a
recorded graph run, with a count of earlier versions. The compiler's
`imports` resolve from the same store, so an operator ruling recorded here
can bind a later campaign's prompt like a decision a graph node made.

This script executes nothing and decides nothing. It validates, keeps the
record, and writes one row.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile

DEC_HDR = "| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |"
SCHEMA = "DecisionV1.schema.json"
STORE = os.path.join(".claude", "fluxpoint", "decisions.jsonl")
RUNS = os.path.join(".claude", "fluxpoint", "runs")
# An id is a lookup key, and the compiler's {{decisions.<id>}} and imports
# only accept kebab-case, so a row whose id cannot be looked up is refused
# rather than written.
DID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def cell(s, n=160):
    """One table cell: no pipes, no newlines, bounded. record-run.py's rule."""
    s = str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def load_schema(plugin_root):
    p = os.path.join(plugin_root, "contracts", SCHEMA)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"decision: cannot read {p}: {e}")


def validate(rec, schema, choice_among_options=True):
    """Findings against DecisionV1. Shallow by design, floors enforced.

    `choice_among_options` adds this script's own rule, stricter than the
    schema: the chosen option is one of the options listed. --record holds
    a ruling to it; an imported record a graph node produced was only ever
    held to the schema, and is judged by the schema alone.

    release.py validates a pasted proof the same way and for the same
    reason: this is not a general JSON Schema engine, it is the specific
    set of floors that stop a well-formed nothing from passing.
    """
    f = []
    if not isinstance(rec, dict):
        return ["the decision must be a JSON object"]
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in rec:
            f.append(f"missing required field '{key}'")
    for key, spec in props.items():
        if key not in rec:
            continue
        val = rec[key]
        want = spec.get("type")
        if want == "string" and not isinstance(val, str):
            f.append(f"{key} must be a string")
        elif want == "boolean" and not isinstance(val, bool):
            f.append(f"{key} must be true or false")
        elif want == "array" and not isinstance(val, list):
            f.append(f"{key} must be an array")
        if isinstance(val, str) and len(val.strip()) < spec.get("minLength", 0):
            f.append(f"{key} is shorter than {spec['minLength']} characters — "
                     f"{spec.get('description', 'the floor exists so a lazy output cannot satisfy it')}")
        if isinstance(val, list) and len(val) < spec.get("minItems", 0):
            f.append(f"{key} needs at least {spec['minItems']} entries — "
                     f"{spec.get('description', '')}".rstrip(" —"))

    opts = rec.get("options")
    if isinstance(opts, list):
        ospec = (props.get("options", {}).get("items", {}))
        oprops = ospec.get("properties", {})
        for i, o in enumerate(opts):
            if not isinstance(o, dict):
                f.append(f"options[{i}] must be an object")
                continue
            for key in ospec.get("required", []):
                if key not in o:
                    f.append(f"options[{i}] missing '{key}'")
            for key, spec in oprops.items():
                v = o.get(key)
                if isinstance(v, str) and len(v.strip()) < spec.get("minLength", 0):
                    f.append(
                        f"options[{i}].{key} is shorter than {spec['minLength']} "
                        f"characters — an option nobody argued against was not examined")
        chosen = rec.get("chosen")
        names = [o.get("option") for o in opts if isinstance(o, dict)]
        if choice_among_options and isinstance(chosen, str) and names and chosen not in names:
            f.append(f"chosen '{chosen}' is not one of the options considered "
                     f"({', '.join(str(n) for n in names)}) — a decision is a "
                     f"choice among the alternatives it weighed")
    return f


def read_store(path):
    """Every stored record, oldest first. A malformed line is a hard error.

    The ledger's discipline, for the ledger's reason: skipping a line that
    does not parse would silently lose a decision, and this file exists so
    that none is lost.
    """
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"decision: {path}:{i} is not valid JSON: {e}")
    return out


def keep(root, did, rec, graph):
    """Append the whole record to the store; returns the stored line."""
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    body = json.dumps(rec, sort_keys=True)
    line = {"recordId": "dec_" + hashlib.sha256(f"{did}|{when}|{body}".encode()).hexdigest()[:12],
            "id": did, "when": when, "source": "decision.py", "graph": graph, "record": rec}
    p = os.path.join(root, STORE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(line) + "\n")
    return line


def derive_id(root, question):
    """An id for a record recorded without --id.

    Kebab-case from the question's first 40 characters, starting with a
    letter ('d-' in front of a leading digit), or a hash of the question
    when it has no ASCII letters at all. Once ids became the key --show and
    imports look a record up by, two different questions sharing a 40-char
    prefix read as one decision's two versions — so a derived id that the
    store already holds for another question gets the question's hash.
    """
    base = re.sub(r"[^a-z0-9]+", "-", question[:40].lower()).strip("-")
    tag = hashlib.sha256(question.encode("utf-8")).hexdigest()[:8]
    if not base:
        return "d-" + tag
    if not base[0].isalpha():
        base = "d-" + base
    base = base[:55].rstrip("-")
    # Both places --show and imports read: the store and recorded runs.
    for _, _, rec in find(root, base):
        if rec.get("question") != question:
            return f"{base}-{tag}"
    return base


def order_key(when):
    """A timestamp from either source as 'YYYY-MM-DD HH:MM:SS'.

    Stored records carry seconds; run artifacts carry `recordedAt` with
    seconds and, before 1.43, only a minute-precision `when`. Compared as
    raw text, 'HH:MM:SS' sorts after 'HH:MM' whatever the order the two
    happened in, so both are brought to one shape first. A minute-only
    artifact is read as the start of its minute: which one came first in
    that minute was never recorded.
    """
    s = str(when or "").replace("T", " ").rstrip("Z").strip()
    return s + ":00" if len(s) == 16 else s


def run_decision(artifact, did):
    """The DecisionV1 a recorded graph run filed under `did`, or None.

    record-run.py's filing order: the campaign's own decisions map, then a
    node contracted to DecisionV1 whose id is the decision id.
    """
    summary = artifact.get("summary") or {}
    # A decision this run IMPORTED rides in its decisions map as provenance
    # (record-run.py's decision_rows skips it for the same reason). Read as
    # a fresh ruling it was dated by this run and could hide a newer one
    # recorded while the run was parked.
    if did in (summary.get("decisionsImported") or {}):
        return None
    rec = (summary.get("decisions") or {}).get(did)
    if isinstance(rec, dict):
        return rec
    rec = (summary.get("results") or {}).get(did)
    if (summary.get("contracts") or {}).get(did) == "DecisionV1" and isinstance(rec, dict):
        return rec
    return None


def find(root, did):
    """[(when, source, record)] for one decision id, oldest first.

    Both places a decision is kept: this store (operator rulings, loop
    work) and recorded graph runs (a node that decided).
    """
    # Ordered by time, then by position: two rulings in one second are
    # still told apart by which was appended last.
    hits = [(order_key(r.get("when")), (1, i), str(r.get("recordId") or "store"), r.get("record"))
            for i, r in enumerate(read_store(os.path.join(root, STORE)))
            if r.get("id") == did and isinstance(r.get("record"), dict)]
    runs = os.path.join(root, RUNS)
    for fn in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(runs, fn), encoding="utf-8") as fh:
                art = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        rec = run_decision(art, did) if isinstance(art, dict) else None
        if rec is not None:
            hits.append((order_key(art.get("recordedAt") or art.get("when")), (0, 0),
                         fn[:-len(".json")], rec))
    return [(w, src, rec) for w, _, src, rec in sorted(hits, key=lambda h: (h[0], h[1], h[2]))]


def render(did, when, source, rec, earlier):
    """A stored DecisionV1, whole, as text a reader can act on."""
    yes = lambda b: "yes" if b else "no"  # noqa: E731
    lines = [f"decision {did} — {when or 'undated'}, from {source}"
             + (f" [{earlier} earlier version(s): --json shows only this one]" if earlier else ""),
             f"question: {rec.get('question')}", "options:"]
    for o in rec.get("options") or []:
        if not isinstance(o, dict):
            continue
        mark = "  [CHOSEN]" if o.get("option") == rec.get("chosen") else ""
        lines.append(f"  - {o.get('option')} (argued by {o.get('argued_by')}){mark}")
        lines.append(f"      strongest objection: {o.get('strongest_objection')}")
    lines += [f"chosen: {rec.get('chosen')}", f"rationale: {rec.get('rationale')}",
              f"overturned prior: {yes(rec.get('overturned_prior'))} · frozen by: "
              f"{rec.get('frozen_by') or 'none'} · reversible: {yes(rec.get('reversible'))}",
              "evidence:"]
    lines += [f"  - {e}" for e in rec.get("evidence") or []] or ["  (none)"]
    return "\n".join(lines)


def splice(path, row):
    if not os.path.exists(path):
        return "no-file"
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if DEC_HDR not in text:
        return "no-header"
    new, n = re.subn(re.escape(DEC_HDR) + r"\n(\|[-:| ]+\|)\n",
                     lambda m: m.group(0) + row + "\n", text, count=1)
    if not n:
        return "no-separator"
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".decision-", suffix=".tmp")
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
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--graph", default="WORK.md")
    ap.add_argument("--plugin-root", default=os.path.dirname(here))
    ap.add_argument("--id", default="", help="short kebab-case id for the row")
    ap.add_argument("--result", help="decision JSON file (default stdin)")
    ap.add_argument("--session", default="nosession")
    ap.add_argument("--json", action="store_true", help="with --show: the record as JSON")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", action="store_true")
    g.add_argument("--show", metavar="ID")
    g.add_argument("--none", metavar="REASON")
    a = ap.parse_args()

    if a.show:
        hits = find(a.root, a.show)
        if not hits:
            print(f"decision: no record of '{a.show}' in {STORE} or any recorded run",
                  file=sys.stderr)
            return 1
        when, source, rec = hits[-1]
        print(json.dumps(rec, indent=2) if a.json
              else render(a.show, when, source, rec, len(hits) - 1))
        return 0

    if a.none:
        if len(a.none.strip()) < 20:
            print("decision: --none needs a real reason (20+ chars). Saying why "
                  "nothing was decided is the point; 'n/a' is not a statement.",
                  file=sys.stderr)
            return 2
        sd = os.path.join(a.root, ".claude", "fluxpoint")
        os.makedirs(sd, exist_ok=True)
        p = os.path.join(sd, f"{a.session}.nodecision")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(a.none.strip() + "\n")
        print(f"decision: recorded that this session decided nothing — {p}")
        return 0

    raw = (open(a.result, encoding="utf-8").read() if a.result else sys.stdin.read())
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"decision: input is not JSON: {e}", file=sys.stderr)
        return 1

    findings = validate(rec, load_schema(a.plugin_root))
    if findings:
        print("decision: REFUSED — this is not a DecisionV1\n", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        print("\n  The floors are the point: a decision worth surviving context "
              "death\n  names the alternatives it beat and the best case against "
              "each,\n  including against the one that won.", file=sys.stderr)
        return 1

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    did = a.id or derive_id(a.root, str(rec.get("question", "")))
    if not DID.match(did):
        why = ("it is empty" if not did else "it must start with a letter"
               if not did[0].isalpha() else "it must be lowercase letters, digits and "
               "hyphens, at most 64 characters")
        print(f"decision: --id '{did}' is not usable: {why} — the id is how --show "
              f"and a later campaign's imports find this record", file=sys.stderr)
        return 1
    # The record is kept before the row is written, so a missing table, a
    # full disk at the splice, or a crash in between still leaves the whole
    # decision on disk; the row only indexes it.
    kept = keep(a.root, did, rec, a.graph)
    row = (f"| {ts} | {did} | {cell(rec.get('chosen'), 60)} "
           f"| {'YES' if rec.get('overturned_prior') else 'no'} "
           f"| {cell(rec.get('frozen_by') or 'none', 40)} "
           f"| {cell(rec.get('rationale'))} |")

    graph = a.graph if os.path.isabs(a.graph) else os.path.join(a.root, a.graph)
    status = splice(graph, row)
    if status == "written":
        print(f"decision: recorded '{did}' in {a.graph}")
        print(row)
        print(f"decision: the whole record is {kept['recordId']} in {STORE} — "
              f"decision.py --show {did}")
        if rec.get("overturned_prior"):
            print("decision: this one overturned the prior — it is exactly the "
                  "kind a fresh context re-decides the other way, which is why "
                  "it is now on disk.")
        return 0
    msgs = {
        "no-file": f"{graph} does not exist",
        "no-header": (f"{graph} has no Decisions table — add one so a frozen "
                      f"choice outlives this run"),
        "no-separator": f"{graph} has a Decisions header with no separator row",
    }
    print(f"decision: {msgs.get(status, status)}; no row written — the whole "
          f"record is kept as {kept['recordId']} (decision.py --show {did})",
          file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
