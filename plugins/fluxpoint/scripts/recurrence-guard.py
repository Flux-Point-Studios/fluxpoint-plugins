#!/usr/bin/env python3
"""A lesson learned twice is not a lesson. It is a missing gate.

WHY. The lesson store had both halves of this and never joined them.
Identity is `tag|dedupeKey` with latest-state-wins, so the second time a run
found the same thing the new row REPLACED the old one; the consolidate pass
then merged near-duplicates into a single canonical claim. The third time the
system learned a thing, the store looked exactly as it had the first time.
Recurrence was not merely uncounted — the only place that counted it spent
the number on a retrieval ranking boost and told nobody.

The measured case: a deploy recipe carried a hand-typed list of the modules
to package. Three separate rounds each found a different module missing from
it, each fix was correct, and the whole service went down every time, because
knowledge was never the problem. Knowledge was written down three times.
Nobody joined the three instances into one class, because that join needed a
human to notice two unrelated repos rhyming in the same week.

WHAT THIS DOES. Past the threshold an item stops being addressable by
restating it. It demands a HarnessCheckV1 — a command whose exit code is that
lesson's live verdict — registered in .fluxpoint-recurrence.json and EXECUTED
here, not reported. Until that command runs and exits as declared, the item
stays loud at every session start and the harness stays red.

  recurrence-guard.py --check        gate: every recurrence has a passing check
  recurrence-guard.py --scan         humans: what has recurred, and how often
  recurrence-guard.py --for-session  the SessionStart line; always exits 0

THE THRESHOLD IS TWO, and it is not configurable. Once is learning. Twice is
a pattern, and on the measured history a gate at two would have demanded the
check on the second occurrence — ahead of the third and fourth, which are the
ones that took the service down. At three it would have fired after the
outage it exists to prevent. One is not a threshold at all: it would demand a
harness check for every lesson ever filed, and a gate that fires on
everything is one people learn to skim. A knob here would only ever be turned
one way.

MANIFEST (.fluxpoint-recurrence.json), committed beside the code:

  {"checks": [
    {"identity": "defect-sweep|hand-maintained-enumeration",
     "command": "python tools/package.py --verify",
     "expectExit": 0,
     "why": "derives the package from the import graph and imports the built
             artifact in isolation, so a missing module cannot ship"}
  ]}

`identity` is an exact `tag|classKey` or `tag|dedupeKey`. No globs: one
pattern covering everything is how a gate becomes decoration.

WHAT THIS CANNOT DO, stated because a check that overclaims is worse than
none: it proves the registered command RAN and exited as declared. It cannot
prove the command would have caught the defect. Prove that separately —
guard-guard.py --verify disables a guard and requires its proof to redden,
which is the mutation this gate has no way to perform on an arbitrary repo.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memory as memory_mod  # noqa: E402 - sibling module, house idiom

MANIFEST = ".fluxpoint-recurrence.json"
THRESHOLD = 2
TIMEOUT = 900
TAIL_LINES = 6
TAIL_CHARS = 800

# The store is a local jsonl anyone can write and --for-session speaks into
# an agent's context, so store text gets the same hostile-input discipline
# recall applies: control and separator characters flattened, lengths capped.
CTRL_RE = re.compile("[\\u0000-\\u001f\\u007f-\\u009f\\u2028\\u2029]")

# Shell words the PATH probe cannot resolve but the shell can. A closed,
# stable set — not an enumeration that grows.
SHELL_BUILTINS = frozenset(
    {"cd", "echo", "true", "false", "test", "[", "exit", "set", "!", "(",
     "{", "if", "for", "while", "until", "case"})


def _clean(v, cap=500):
    return CTRL_RE.sub(" ", str(v))[:cap]


def read_tolerant(path):
    """Rows plus the count of lines that were not JSON.

    memory.py fails hard on a torn line, and rightly: a seed map that
    silently shrank would tell a sweep it stands on more ground than it
    does. A gate has the opposite failure mode — a JSONL write interrupted
    mid-append must not take out the harness of a repo that has nothing to
    do with recurrence. So the count is reported every time, and it is only
    fatal where the repo armed this gate on purpose (see check()): a torn
    store is the one way to disarm a gate the repo asked for.
    """
    rows, bad = [], 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return rows, bad


def load_manifest(root):
    """{identity: check}. A malformed manifest is fatal — it is hand-written."""
    path = os.path.join(root, MANIFEST)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"recurrence: {MANIFEST} is not valid JSON: {exc}")
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise SystemExit(f"recurrence: {MANIFEST} must carry a 'checks' list")
    out = {}
    for c in checks:
        if not isinstance(c, dict) or not isinstance(c.get("identity"), str) \
                or not isinstance(c.get("command"), str) or not c["command"].strip():
            raise SystemExit(
                f"recurrence: every check needs a string 'identity' and a "
                f"non-empty string 'command'; got {json.dumps(c)[:80]}")
        # Refused rather than coerced: `"expectExit": "0"` compared against an
        # int exit code is a gate that can never be satisfied, and one that
        # can never go green is one somebody deletes.
        if not isinstance(c.get("expectExit", 0), int) \
                or isinstance(c.get("expectExit", 0), bool):
            raise SystemExit(
                f"recurrence: {c['identity']!r} declares expectExit "
                f"{c['expectExit']!r}, which is not an integer exit code")
        out[c["identity"]] = c
    return out


def promoted(rows):
    """Identities past the threshold, coarsest first, newest evidence in each.

    A lesson the panel keeps KILLING is excluded. That is a finder re-finding
    a non-defect, not a defect recurring, and demanding a harness check for it
    would fill the gate with items nobody can act on — which is how a register
    teaches its readers to skim.
    """
    status = {}
    for r in rows:
        status[f"{r.get('tag')}|{r.get('dedupeKey')}"] = r.get("status")
    live = [r for r in rows
            if status.get(f"{r.get('tag')}|{r.get('dedupeKey')}") != "killed"]
    inst, cls = memory_mod.arrival_index(live)

    items, covered = [], set()
    for ident, e in sorted(cls.items()):
        if len(e["runs"]) < THRESHOLD:
            continue
        items.append({"identity": ident, "grain": "class",
                      "runs": e["runs"], "members": e["identities"],
                      "claim": e["claim"]})
        covered.update(e["identities"])
    for ident, e in sorted(inst.items()):
        if len(e["runs"]) < THRESHOLD or ident in covered:
            continue
        items.append({"identity": ident, "grain": "instance",
                      "runs": e["runs"], "members": [ident],
                      "claim": e["claim"]})
    return items


def run_check(root, check):
    """Execute the registered command. Returns a HarnessCheckV1 triple.

    Executed here rather than reported, because HarnessCheckV1 is three
    fields with no proof the command ever ran — the schema is satisfied
    perfectly by a fabricated triple.

    The executable is resolved BEFORE the shell gets the command. Under
    shell=True a nonexistent program is absorbed into an ordinary exit code
    (1 on cmd.exe, 127 on sh), so a command that never existed would read
    exactly like a command that ran and failed — and would satisfy any
    matching expectExit forever. Not-found is its own verdict: exit null,
    never comparable to a declared code. The command runs through the
    platform shell (cmd.exe on Windows), so cross-platform manifests should
    lead with a real executable, not shell syntax.
    """
    cmd = check["command"]
    try:
        first = (shlex.split(cmd, posix=(os.name != "nt")) or [""])[0]
    except ValueError:
        first = cmd.split()[0] if cmd.split() else ""
    first = first.strip('"')
    if first not in SHELL_BUILTINS and not shutil.which(first) \
            and not os.path.exists(os.path.join(root, first)):
        return {"exit": None, "command": cmd,
                "tail": f"could not be spawned: {first!r} is not a known "
                        f"executable — a check that cannot start has no "
                        f"verdict to report"}
    try:
        p = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                           text=True, timeout=TIMEOUT)
        out = f"{p.stdout or ''}\n{p.stderr or ''}".strip()
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, code = f"did not finish in {TIMEOUT}s", 124
    except OSError as exc:
        out, code = f"could not be started: {exc}", None
    tail = "\n".join(out.splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]
    return {"exit": code, "command": cmd, "tail": tail}


def describe(item, check, indent="  "):
    """The demand, in the words that make restating it obviously insufficient.

    Every store-sourced string is flattened and capped before it can start a
    line: this output lands in agent context at session start, and a crafted
    row must not get to speak in the session's voice.
    """
    lines = [
        f"{indent}[{_clean(item['identity'], 120)}] arrived "
        f"{len(item['runs'])} time(s) across runs "
        f"{_clean(', '.join(item['runs']), 200)}"
    ]
    if item["grain"] == "class":
        shown = [_clean(m, 120) for m in item["members"][:4]]
        more = f" (+{len(item['members']) - len(shown)} more)" \
            if len(item["members"]) > len(shown) else ""
        lines.append(f"{indent}    as {len(item['members'])} separate "
                     f"finding(s): {', '.join(shown)}{more}")
    if item.get("claim"):
        lines.append(f"{indent}    latest claim: {_clean(item['claim'], 110)}")
    if check is None:
        stub = json.dumps({"identity": item["identity"],
                           "command": "<the command that would have caught this>",
                           "expectExit": 0,
                           "why": "<what it derives instead of enumerating>"})
        lines.append(f"{indent}    NO CHECK REGISTERED. Restating this lesson "
                     f"is not an answer to it.")
        lines.append(f"{indent}    Add to {MANIFEST}: {stub}")
    return lines


def evaluate(root, rows, manifest):
    """(problems, receipts) for the promoted set. Runs every registered check."""
    problems, receipts = [], []
    for item in promoted(rows):
        check = (manifest or {}).get(item["identity"])
        if check is None:
            problems.append("\n".join(describe(item, None)))
            continue
        result = run_check(root, check)
        want = check.get("expectExit", 0)
        receipts.append((item, result))
        if result["exit"] != want:
            problems.append("\n".join(
                describe(item, check)
                + [f"      its check exited {result['exit']}, not {want}: "
                   f"{result['command']}"]
                + [f"      {ln}" for ln in result["tail"].splitlines()]))
    return problems, receipts


def load(root):
    """(rows, bad, manifest) or None when this repo has no store at all."""
    paths = memory_mod.source_paths(root)
    if not any(os.path.exists(path) for path in paths):
        return None
    rows, bad, seen = [], 0, set()
    with memory_mod.workfile_lock(memory_mod.path_for(root)):
        for path in paths:
            part, unreadable = read_tolerant(path)
            bad += unreadable
            for row in part:
                key = json.dumps(row, sort_keys=True)
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
    rows.sort(key=memory_mod.history_order)
    return rows, bad, load_manifest(root)


def check(root):
    loaded = load(root)
    if loaded is None:
        return 0
    rows, bad, manifest = loaded
    problems, receipts = evaluate(root, rows, manifest)

    if bad:
        note = (f"recurrence: {bad} unreadable line(s) in "
                f"{', '.join(memory_mod.source_paths(root))} — arrivals are undercounted by at least "
                f"that much")
        if manifest is not None:
            problems.append(
                note + f"\n      {MANIFEST} arms this gate, and a torn store is "
                f"the one way to disarm it silently. Repair the store.")
        else:
            print(note, file=sys.stderr)

    for item, result in receipts:
        print(f"recurrence: {item['identity']} x{len(item['runs'])} — "
              f"HarnessCheckV1 {json.dumps(result)}")
    if problems:
        print("\nrecurrence: FAILED — a lesson learned twice is not a lesson, "
              "it is a missing gate.\n"
              "Recording it again is the failure, not the remedy.\n",
              file=sys.stderr)
        for p in problems:
            print(p, file=sys.stderr)
        print("", file=sys.stderr)
        return 1
    if receipts:
        print(f"recurrence: {len(receipts)} recurrence(s), each held down by a "
              f"check that ran")
    return 0


def scan(root):
    loaded = load(root)
    if loaded is None:
        print("recurrence: no lesson store in this repo")
        return 0
    rows, bad, manifest = loaded
    items = promoted(rows)
    print(f"recurrence: {len(rows)} row(s), {len(items)} past the threshold "
          f"of {THRESHOLD}" + (f", {bad} unreadable" if bad else ""))
    for item in items:
        registered = (manifest or {}).get(item["identity"])
        for ln in describe(item, registered):
            print(ln)
        if registered:
            print(f"      check: {registered['command']} "
                  f"(expect {registered.get('expectExit', 0)})")
    # The blind spot itself, counted: instance keys name a defect's location
    # or wording and almost never collide (the measured history produced
    # three keys for one defect), so a classless lesson sits outside this
    # gate entirely. Latest state per identity — history rows don't inflate
    # the census.
    live = {}
    for r in rows:
        live[f"{r.get('tag')}|{r.get('dedupeKey')}"] = r
    by_tag = {}
    for r in live.values():
        # promoted() excludes killed identities from the gate entirely, so
        # a killed classless row is not part of the blind spot — counting
        # it would overstate what the gate cannot see.
        if r.get("status") == "killed":
            continue
        t = by_tag.setdefault(r.get("tag"), [0, 0])
        t[1] += 1
        if not r.get("classKey"):
            t[0] += 1
    for tag, (classless, total) in sorted(by_tag.items()):
        if classless:
            print(f"  [{_clean(tag, 80)}] {classless} of {total} lesson(s) "
                  f"carry no class — recurrence across them is invisible at "
                  f"class grain; only their exact instance keys can recur")
    return 0


def for_session(root):
    """The SessionStart line. Exits 0 no matter what — a hook that can wedge
    a session is a worse failure than the alarm it was carrying."""
    try:
        loaded = load(root)
        if loaded is None:
            return 0
        rows, _, manifest = loaded
        # Unchecked only: an item already held down by a registered check is
        # the harness's business, not the reader's.
        items = [i for i in promoted(rows)
                 if (manifest or {}).get(i["identity"]) is None]
        if not items:
            return 0
        print(f"- RECURRENCE: {len(items)} item(s) have now been learned "
              f"{THRESHOLD}+ times. A lesson learned twice is not a lesson, it "
              f"is a missing gate — recording it again is the failure, not the "
              f"remedy. Each needs a command whose exit code is its verdict, "
              f"registered in {MANIFEST} and run by the harness.")
        for item in items[:3]:
            for ln in describe(item, None, indent="    "):
                print(ln)
        if len(items) > 3:
            print(f"    [{len(items) - 3} more — run "
                  f"recurrence-guard.py --scan]")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - a hook may never raise
        # SystemExit is named because it is the likely one: the manifest is
        # hand-written, load_manifest refuses a malformed one, and refusing
        # is correct for the gate and wrong for a session-start line. The
        # message can carry manifest bytes, so it gets the same flattening
        # as store text before it reaches session context.
        print(f"- Recurrence check unavailable: {_clean(exc, 200)}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--scan", action="store_true")
    mode.add_argument("--for-session", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.for_session:
        return for_session(root)
    if a.scan:
        return scan(root)
    return check(root)


if __name__ == "__main__":
    sys.exit(main())
