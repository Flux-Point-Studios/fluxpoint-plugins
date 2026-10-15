#!/usr/bin/env bash
# Tests for the unified state model and the pre-1.0 compatibility path that
# /fluxpoint:migrate relies on.
set -uo pipefail

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

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(mktemp -d)"
pass=0; fail=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then printf 'PASS  %-50s -> %s\n' "$1" "$3"; pass=$((pass+1))
  else printf 'FAIL  %-50s -> %s (wanted %s)\n' "$1" "$3" "$2"; fail=$((fail+1)); fi
}
contains() { # name needle file
  if grep -qF "$2" "$3"; then printf 'PASS  %-50s -> found\n' "$1"; pass=$((pass+1))
  else printf 'FAIL  %-50s -> MISSING\n' "$1"; fail=$((fail+1)); fi
}

newrepo() {
  rm -rf "$ROOT/r"; mkdir -p "$ROOT/r/scripts"; cd "$ROOT/r" || exit 1
  git init -q -b main
  printf '#!/usr/bin/env bash\nexit 0\n' >scripts/harness.sh; chmod +x scripts/harness.sh
  printf 'x\n' >f.txt; git add -A
  git -c user.email=t@t -c user.name=t commit -qm base
}
RESULT='{"campaign":"c","outcome":"COMPLETE","results":{"find":[{"a":1},{"a":2}]},"provenance":[{"node":"find","status":"OK"},{"node":"g","status":"DEAD"}]}'

# --- 1. state-file resolution: WORK.md wins, LOOP.md still honored ---
newrepo
. "$PLUGIN/scripts/lib.sh"
printf '# WORK\n' >WORK.md; printf '# LOOP\n' >LOOP.md
check "state file prefers WORK.md" "WORK.md" "$(fpl_state_file)"
rm WORK.md
check "state file falls back to LOOP.md" "LOOP.md" "$(fpl_state_file)"
rm LOOP.md
fpl_state_file >/dev/null 2>&1
check "no state file -> non-zero" "1" "$?"

# --- 2. unified 5-column Evidence table gets the graph row ---
newrepo
legacy_work() { sed '/^SPEC: /d' "$PLUGIN/templates/WORK.md" > WORK.md; }
legacy_work
printf '%s' "$RESULT" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_u1 --harness 0 --red-team SHIP >/dev/null
contains "unified table: row appended" "| wf_u1 | BLOCKED-REDTEAM |" WORK.md
contains "unified table: claim column" "1 node(s) OK, 1 dead, 2 produced item(s)" WORK.md
contains "unified table: proof column" "harness exit 0; red-team BLOCK" WORK.md
check "provenance artifact written" "0" "$([ -f .claude/fluxpoint/runs/wf_u1.json ] && echo 0 || echo 1)"

# --- 2b. budget-skipped nodes are reported, never filed as success ---
newrepo
legacy_work
SKIPPED='{"campaign":"c","outcome":"COMPLETE","results":{},"provenance":[{"node":"a","status":"OK"},{"node":"b","status":"SKIPPED","detail":"budget floor"}]}'
printf '%s' "$SKIPPED" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_sk1 >/dev/null
contains "skipped node: flagged in Evidence claim" "1 SKIPPED on budget — coverage incomplete" WORK.md
check "skipped node: not counted as OK" "1" \
  "$("$FPL_PY" -c "import json;print(json.load(open('.claude/fluxpoint/runs/wf_sk1.json'))['nodesOk'])")"
check "skipped node: counted in artifact" "1" \
  "$("$FPL_PY" -c "import json;print(json.load(open('.claude/fluxpoint/runs/wf_sk1.json'))['nodesSkipped'])")"

# --- 2c. the two columns that decide shipping are derived, not declared ---
# They arrived as CLI flags defaulting to 'n/a', so whether a campaign
# reported its own blocking verdict depended on an orchestrator remembering
# to pass it. The run already knows; the recorder now reads it.
newrepo
legacy_work
BLOCKED='{"campaign":"c","outcome":"COMPLETE","contracts":{"gate":"HarnessCheckV1","rt":"RedTeamV1"},
 "results":{"gate":{"exit":0},"rt":{"verdict":"BLOCK","findings":[{"severity":"HIGH","finding":"boundary bypass","exploit_path":"control and attack reached the boundary","minimal_fix":"enforce the boundary check"}]}},
 "provenance":[{"node":"gate","status":"OK"},{"node":"rt","status":"OK"}]}'
printf '%s' "$BLOCKED" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_b1 >/dev/null
contains "BLOCK verdict degrades the outcome" "| wf_b1 | BLOCKED-REDTEAM |" WORK.md
contains "BLOCK verdict is named in the claim" "red-team returned BLOCK — not shippable" WORK.md
contains "verdict derived into the proof column" "red-team BLOCK" WORK.md
check "artifact records the derived verdict" "BLOCK" \
  "$("$FPL_PY" -c "import json;print(json.load(open('.claude/fluxpoint/runs/wf_b1.json'))['redTeam'])")"

# A flag that disagrees with the run does not get to win.
newrepo
legacy_work
printf '%s' "$BLOCKED" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_b2 \
  --harness 0 --red-team SHIP >/dev/null
contains "a SHIP flag cannot override a BLOCK in the run" "| wf_b2 | BLOCKED-REDTEAM |" WORK.md

# Worst exit wins: one red harness node is the campaign's answer.
newrepo
legacy_work
TWOGATES='{"campaign":"c","outcome":"COMPLETE","contracts":{"g1":"HarnessCheckV1","g2":"HarnessCheckV1"},
 "results":{"g1":{"exit":0},"g2":{"exit":2}},"provenance":[{"node":"g1","status":"OK"}]}'
printf '%s' "$TWOGATES" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_b3 --harness 0 >/dev/null
contains "a red harness node beats a green one" "harness exit 2" WORK.md

# A run from before contracts were emitted still gets read.
newrepo
legacy_work
OLD='{"campaign":"c","outcome":"COMPLETE","results":{"rt":{"verdict":"BLOCK","findings":[]}},
 "provenance":[{"node":"rt","status":"OK"}]}'
printf '%s' "$OLD" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_b4 >/dev/null
contains "no contracts map: falls back to result shape" "| wf_b4 | BLOCKED-REDTEAM |" WORK.md

# A SHIP flag without a coverage-bearing review cannot certify a campaign.
newrepo
legacy_work
printf '%s' "$RESULT" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_b5 \
  --harness 0 --red-team SHIP >/dev/null
contains "no gate node: SHIP flag is refused" "harness exit 0; red-team BLOCK" WORK.md
contains "no gate node: incomplete review blocks" "| wf_b5 | BLOCKED-REDTEAM |" WORK.md

# --- 3. legacy 7-column GRAPH.md table still works (mid-migration repo) ---
newrepo
{ printf '## Evidence\n\n'
  printf '| When (UTC) | runId | Outcome | Nodes OK/dead | Findings | Harness | Red-team |\n'
  printf '|---|---|---|---|---|---|---|\n'; } >GRAPH.md
printf '%s' "$RESULT" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_l1 --graph GRAPH.md --harness 0 >/dev/null
contains "legacy table: 7-column row appended" "| wf_l1 | COMPLETE | 1/1 | 2 | 0 |" GRAPH.md

# --- 4. no Evidence table: artifact still written, warning emitted, exit 3 ---
newrepo
printf '# WORK\nno table here\n' >WORK.md
err="$(printf '%s' "$RESULT" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_n1 2>&1 >/dev/null)"
check "missing table -> exit 3" "3" "$?"
check "missing table -> artifact exists" "0" "$([ -f .claude/fluxpoint/runs/wf_n1.json ] && echo 0 || echo 1)"
case "$err" in *"no Evidence table header"*) check "missing table -> warns" "warn" "warn" ;;
  *) check "missing table -> warns" "warn" "silent" ;; esac

# --- 5. SessionStart injection reflects the unified world ---
newrepo
legacy_work
printf '%s' "$RESULT" | "$FPL_PY" "$PLUGIN/scripts/record-run.py" --run-id wf_s1 --harness 0 --red-team SHIP >/dev/null
mkdir -p .claude/fluxpoint; printf 'PASS 2026-01-01T00:00:00Z\n' >.claude/fluxpoint/last-harness
out="$(printf '{"session_id":"s","cwd":"%s"}' "$ROOT/r" | bash "$PLUGIN/scripts/inject-state.sh")"
printf '%s' "$out" >"$ROOT/inject.txt"
contains "inject: harness verdict" "Last recorded harness verdict: PASS" "$ROOT/inject.txt"
contains "inject: last graph run" "Last graph run: wf_s1 BLOCKED-REDTEAM" "$ROOT/inject.txt"
contains "inject: names the state file" "WORK.md (sections that matter" "$ROOT/inject.txt"
# The point of section-aware extraction: in the shipped 90-line template the
# Evidence header sits at 85 and Notes at 88, so a fixed 80-line window hid
# the row record-run.py had just written.
contains "inject: shows the Evidence row it just wrote" "wf_s1" "$ROOT/inject.txt"
contains "inject: shows the Notes section" "## Notes" "$ROOT/inject.txt"
contains "inject: shows open Plan items" "## Plan —" "$ROOT/inject.txt"
contains "inject: shows unmet DoD" "## Definition of Done —" "$ROOT/inject.txt"

# --- 6. legacy repo: LOOP.md injected AND migrate hint shown ---
newrepo
printf '# LOOP: legacy goal\nSTATUS: ACTIVE\n' >LOOP.md
mkdir -p .claude/fluxpoint-loop; printf 'FAIL 2026-01-01T00:00:00Z\n' >.claude/fluxpoint-loop/last-harness
out="$(printf '{"session_id":"s","cwd":"%s"}' "$ROOT/r" | bash "$PLUGIN/scripts/inject-state.sh")"
printf '%s' "$out" >"$ROOT/inject2.txt"
contains "legacy: reads pre-1.0 state dir" "Last recorded harness verdict: FAIL" "$ROOT/inject2.txt"
contains "legacy: injects LOOP.md" "LOOP.md (sections that matter" "$ROOT/inject2.txt"
contains "legacy: suggests migrate" "/fluxpoint:migrate" "$ROOT/inject2.txt"

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
