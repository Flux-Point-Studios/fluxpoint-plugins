#!/usr/bin/env bash
# SessionStart hook. Stdout is injected as context the agent can read; content
# is phrased as factual statements because imperative "system command"
# phrasing can trip prompt-injection defenses and surface the text to the
# user instead. Runs on startup, resume, clear, and post-compaction.
set -u

# Interpreter name differs by platform: `python3` on Linux/macOS, `python` on a
# standard Windows install. Resolve by running each candidate, because a
# name on PATH is not evidence of an interpreter.
if [ -z "${FPL_PY:-}" ]; then
  # Probe each candidate by RUNNING it, rather than asking whether the name
  # exists. Windows ships a `python3` App Execution Alias that is on PATH by
  # default on a machine with no python3 at all: it satisfies `command -v`,
  # then prints "Python was not found" and exits 49 for every argument.
  for _fpl_cand in python3 python; do
    if command -v "$_fpl_cand" >/dev/null 2>&1 &&
       "$_fpl_cand" -c "import sys" >/dev/null 2>&1; then
      FPL_PY="$_fpl_cand"
      break
    fi
  done
  unset _fpl_cand
  if [ -z "${FPL_PY:-}" ]; then
    echo "fluxpoint: no working python interpreter on PATH" >&2
    exit 127
  fi
fi
# Force UTF-8 on every embedded interpreter's stdio. Without it Windows writes
# cp1252, so a header like "## Plan --" emitted with an em-dash comes back as
# 0x97 and every consumer that greps for the UTF-8 bytes silently misses it.
export PYTHONIOENCODING=utf-8

[ "${FPL_DISABLE:-0}" = "1" ] && exit 0
here="${0%/*}"; [ "$here" = "$0" ] && here=.
. "$here/lib.sh"

input="$(cat)"
fpl_cd_project "$input" || exit 0

sd="$(fpl_state_dir)"
sid="$(fpl_sid "$(printf '%s' "$input" | fpl_json_get session_id)")"

# Everything up to the git guard also runs in non-git roots: the compaction
# gate is scoped to durable surfaces (work file, memory store, task board)
# that exist with or without a repo, and the long non-repo session is where
# compaction losses hurt most.
mkdir -p "$sd" 2>/dev/null
# The state dir must never be committable, including in repos that skipped
# /fluxpoint:init — its markers carry session ids and disarm flags.
[ -f "$sd/.gitignore" ] || printf '*\n' >"$sd/.gitignore" 2>/dev/null
find "$sd" -maxdepth 1 \( -name '*.dirty' -o -name '*.blocks' -o -name '*.base' \
  -o -name '*.attest-warned' -o -name '*.cwin' -o -name '*.cblock' \
  -o -name '*.nodecision' \) -mtime +3 -delete 2>/dev/null
# The compaction-window snapshot, seeded only when absent — each allowed
# compaction re-seeds it, so every window is measured from its own start.
[ -f "$sd/$sid.cwin" ] \
  || printf '%s\n%s\n' "$(fpl_memory_sha)" "$(fpl_aux_sha "$sid")" \
       >"$sd/$sid.cwin" 2>/dev/null

# This context may be the one that exists after a compaction. The PreCompact
# hook recorded whether anything had been written down at that moment; if
# nothing had, the reasoning behind whatever is in the tree did not survive,
# and a fresh context should know that rather than assume the diff explains
# itself.
compacted="$sd/$sid.compacted"
if [ -f "$compacted" ]; then
  cflushed="$(fpl_json_get flushed <"$compacted")"
  cworked="$(fpl_json_get codeChanged <"$compacted")"
  cblocked="$(fpl_json_get blocked <"$compacted")"
  cwhen="$(fpl_json_get when <"$compacted")"
  if [ "$cflushed" = "no" ] && [ "$cblocked" = "yes" ]; then
    echo "- CONTEXT WAS COMPACTED at ${cwhen}. The compaction gate blocked once and was overridden with still nothing flushed: no decision, memory, or task update marks what that window learned. The reasoning behind the current state was in the transcript that got summarized. Treat it as lost: re-derive from the code and the tests rather than assuming a prior decision still holds, and write down what you conclude."
  elif [ "$cflushed" = "no" ] && [ "$cworked" != "no" ]; then
    echo "- CONTEXT WAS COMPACTED at ${cwhen} with nothing written to Decisions or Notes, while code had changed. The reasoning behind the current diff — what was tried, what was ruled out, why this approach — was in the transcript that got summarized. Treat it as lost: re-derive from the code and the tests rather than assuming a prior decision still holds, and write down what you conclude."
  fi
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# The commit this session starts from, so the Stop gate can tell work done
# this session from history it inherited — including work that was committed
# before the stop, which a diff against HEAD cannot see.
#
# Written only when absent: this hook also fires on resume, /clear and
# post-compaction, and refreshing the baseline there would forgive every
# commit made before that point. The gate refreshes it itself, on green.
[ -f "$(fpl_base_file "$sid")" ] || fpl_set_base "$sid"
# The same rule for the memory snapshot: taken once, at the session's real
# start, so a later compaction cannot reset the mark it is measured against.
[ -f "$sd/$sid.snapshot" ] || fpl_memory_sha >"$sd/$sid.snapshot" 2>/dev/null

branch="$(git branch --show-current 2>/dev/null)"
dirtyn="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

echo "fluxpoint work context, generated $(date -u +%FT%TZ):"
echo "- Branch: ${branch:-detached}; uncommitted changes: ${dirtyn} path(s)."

verdict=""
for d in "$sd" "$(fpl_legacy_state_dir)"; do
  [ -f "$d/last-harness" ] && { verdict="$(cat "$d/last-harness")"; break; }
done
if [ -n "$verdict" ]; then
  echo "- Last recorded harness verdict: ${verdict}."
else
  echo "- No harness verdict has been recorded in this repo yet."
fi

if [ -f scripts/harness.sh ]; then
  echo "- Harness: scripts/harness.sh is present. A Stop-hook DoD gate runs '--full' plus a hygiene scan whenever code changed this session; red results block the stop, up to ${FPL_MAX_BLOCKS:-3} consecutive times, after which the gate yields with a checkpoint notice. Green is the only clean exit."
else
  echo "- Harness: scripts/harness.sh is absent, so the DoD gate is dormant in this repo. The /fluxpoint:init command scaffolds it."
fi

# What is waiting on a person, first. A fresh context is re-oriented with
# the goal and the Definition of Done but not with what is blocking, which
# is how a parked campaign sits unnoticed for days.
inbox_py="$(dirname "$0")/inbox.py"
if [ -f "$inbox_py" ]; then
  open_items="$("$FPL_PY" "$inbox_py" --count 2>/dev/null || echo 0)"
  if [ "${open_items:-0}" -gt 0 ] 2>/dev/null; then
    echo "- BLOCKED ON YOU: ${open_items} item(s) waiting on a person. Run /fluxpoint:status for the list, /fluxpoint:release <node> to clear one."
  fi
fi

# A gate claim the attest log contradicts makes a run's verdict worthless,
# so it outranks everything below it except what is blocked on a person.
if [ -f .fluxpoint-gates.json ] && [ -f "$sd/attest.jsonl" ]; then
  mm="$("$FPL_PY" - "$sd/runs" <<'PY' 2>/dev/null || true
import json, os, sys
d = sys.argv[1]
n = 0
for fn in os.listdir(d) if os.path.isdir(d) else []:
    if not fn.endswith(".json"):
        continue
    try:
        with open(os.path.join(d, fn), encoding="utf-8") as fh:
            a = json.load(fh)
    except Exception:
        continue
    n += ((a.get("attestation") or {}).get("tally", {}) or {}).get("mismatch", 0)
print(n)
PY
)"
  if [ "${mm:-0}" -gt 0 ] 2>/dev/null; then
    echo "- ATTESTATION MISMATCH: ${mm} recorded gate claim(s) contradict .claude/fluxpoint/attest.jsonl, which is the hook-minted record of what those commands actually exited. Those runs' verdicts are not trustworthy; /fluxpoint:status lists them."
  fi
fi

# What the repo keeps re-learning, above what it has learned. A lesson filed
# a second time used to REPLACE the first, so the store looked identical the
# third time round and only a human noticing the pattern could act on it.
# This is the one memory line phrased as a demand rather than a recollection,
# because the answer to it is a gate and never another lesson. Silent in a
# repo that has filed nothing, and it exits 0 no matter what.
rec_py="$(dirname "$0")/recurrence-guard.py"
if [ -f "$rec_py" ] && [ -f "$sd/memory.jsonl" ]; then
  "$FPL_PY" "$rec_py" --for-session 2>/dev/null || true
fi

# Latest graph run, if this repo runs campaigns.
runs="$sd/runs"
if [ -d "$runs" ]; then
  last="$(ls -1t "$runs"/*.json 2>/dev/null | head -1)"
  if [ -n "$last" ]; then
    echo "- Last graph run: $(fpl_run_summary "$last")."
  fi
fi

# Context recalled from the derived index, ranked against the work file and
# the session's touched paths. Offline and never rebuilding by contract —
# a SessionStart that spends its window rebuilding injects nothing at all —
# so a stale or absent index degrades to a named fallback inside recall.py,
# and the section is silent when the recall corpus is empty. The wall
# clock guard exists because a hook that can wedge a session is a worse
# failure than the memory it was protecting.
recall_py="$(dirname "$0")/recall.py"
if [ "${FPL_RECALL_INJECT:-1}" = "1" ] && [ -f "$recall_py" ]; then
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 "$FPL_PY" "$recall_py" --for-session 2>/dev/null \
      || echo "- Memory recall skipped (overran its 5s slot); run /fluxpoint:recall for the ranked view."
  else
    "$FPL_PY" "$recall_py" --for-session 2>/dev/null || true
  fi
fi

state="$(fpl_state_file || true)"
if [ -n "$state" ]; then
  # A fixed line window is the wrong shape for this file. In the shipped
  # 90-line template the Evidence header sits at 85 and Notes at 88, and a
  # real campaign is worse — the graph-ir block alone can run 30+ lines. So
  # record-run.py did the hard part correctly and the bootstrap never showed
  # it: a fresh context was re-oriented with the goal and the Definition of
  # Done but not with what was proven or what is blocking.
  echo "- ${state} (sections that matter, newest evidence first):"
  "$FPL_PY" - "$state" "$here" <<'PY'
import re, sys

text = open(sys.argv[1], errors="replace").read()
lines = text.splitlines()
out, elided = [], []
BUDGET = 90  # lines emitted, not lines scanned


def section(name):
    """Body lines of '## name', up to the next header."""
    body, inside = [], False
    for ln in lines:
        if re.match(r"^##+\s", ln):
            if inside:
                break
            inside = ln.strip().lower().startswith(f"## {name.lower()}")
            continue
        if inside:
            body.append(ln)
    return [b for b in body if b.strip()]


for ln in lines[:12]:
    if re.match(r"^(STATUS|MODE):", ln.strip()):
        out.append(ln.strip())

# Open work only, and blocked items carry who they are blocked on — the
# loop skips `[~]`, so hiding them here would hide why nothing is moving.
plan = [b for b in section("Plan") if re.match(r"^\s*-\s*\[( |~)\]", b)]
if plan:
    out.append("")
    out.append(f"## Plan — {len(plan)} open item(s)")
    out += [p.rstrip() for p in plan[:12]]
    if len(plan) > 12:
        elided.append(f"{len(plan) - 12} more Plan item(s)")

dod = [b for b in section("Definition of Done") if re.match(r"^\s*-\s*\[ \]", b)]
if dod:
    out.append("")
    out.append(f"## Definition of Done — {len(dod)} unmet")
    out += [d.rstrip() for d in dod[:8]]
    if len(dod) > 8:
        elided.append(f"{len(dod) - 8} more unmet DoD item(s)")

rows = [b for b in section("Decisions")
        if b.strip().startswith("|") and not re.match(r"^\|[-:| ]+\|$", b.strip())]
rows = rows[1:]  # drop the header row itself
if rows:
    out.append("")
    out.append(f"## Decisions — newest {min(3, len(rows))} of {len(rows)}")
    out += [r.rstrip() for r in rows[:3]]
    # A row is an index cut to fit a table; the condition that mattered is
    # often the part past the cut. Point a cut row at its whole record.
    cut = []
    for r in rows[:3]:
        cells = [c.strip() for c in r.strip().strip("|").split("|")]
        if "\u2026" in r and len(cells) > 1 and re.match(r"^[a-z][a-z0-9-]*$", cells[1]):
            cut.append(cells[1])
    for did in cut:
        out.append(f"  (cut: the whole record, options, objections and evidence: "
                   f"bash \"{sys.argv[2]}/py.sh\" decision.py --show {did})")
    if len(rows) > 3:
        elided.append(f"{len(rows) - 3} older Decisions row(s)")

# Evidence is the one section a fresh context inherits as fact, and until now
# every row rode in identically whether a Stop hook recorded it or the agent
# typed it. They are not the same kind of statement, and the budget was
# first-come: a run of agent-written rows evicted every witnessed one.
# So the classes are separated, labelled, and given their own budgets.
ev = [b for b in section("Evidence")
      if b.strip().startswith("|") and not re.match(r"^\|[-:| ]+\|$", b.strip())]
ev = ev[1:]


def source_of(row):
    parts = [p.strip() for p in row.strip().strip("|").split("|")]
    return parts[1] if len(parts) >= 2 else ""


gate_rows = [r for r in ev if source_of(r) == "gate"]
claim_rows = [r for r in ev if source_of(r) != "gate"]
if ev:
    out.append("")
    out.append(f"## Evidence — {len(ev)} row(s): "
               f"{len(gate_rows)} recorded by the Stop gate, "
               f"{len(claim_rows)} asserted by whoever wrote them")
    if gate_rows:
        out.append("### Recorded by the gate (the runtime ran the harness itself)")
        out += [r.rstrip() for r in gate_rows[:3]]
        if len(gate_rows) > 3:
            elided.append(f"{len(gate_rows) - 3} older gate row(s)")
    if claim_rows:
        out.append("### Asserted, not witnessed — a claim, weighed accordingly")
        out += [r.rstrip() for r in claim_rows[:3]]
        if len(claim_rows) > 3:
            elided.append(f"{len(claim_rows) - 3} older asserted row(s)")

notes = section("Notes for the next iteration") or section("Notes for the next run")
if notes:
    out.append("")
    out.append("## Notes")
    out += [n.rstrip() for n in notes[:10]]

if len(out) > BUDGET:
    elided.append(f"{len(out) - BUDGET} further line(s)")
    out = out[:BUDGET]
print("\n".join(out) if out else "(no recognizable sections; showing nothing "
                                 "rather than a truncated head)")
if elided:
    # Named, because silent truncation is how a bootstrap reads as complete.
    print(f"[elided: {'; '.join(elided)} — read {sys.argv[1]} directly]")
PY
  if [ "$state" = "LOOP.md" ]; then
    echo "- Note: LOOP.md is the pre-1.0 state file and is still honored. /fluxpoint:migrate folds it into WORK.md, which carries one Definition of Done and one Evidence table for both loop slices and graph campaigns."
  fi
fi
exit 0
