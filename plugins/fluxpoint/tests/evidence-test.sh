#!/usr/bin/env bash
# Gate-authored Evidence: one row class the agent never writes.
#
# The honest claim under test is narrow, so the tests are too. It is NOT
# "a row cannot be forged" — WORK.md is a markdown file both the PostToolUse
# hook and the hygiene scan skip, so an agent can type anything into it.
# It is: the Stop hook records its own verdict, that class is visibly
# distinct, and a fresh context is handed agent assertions labelled as
# assertions rather than as evidence.
#
# Also pinned here: `plugin_script` used to kill `harness.sh --full` outright
# in any repo where ~/.claude/plugins does not exist — find exits 1, pipefail
# propagates it, set -e ends the run — which is precisely the case the
# function exists to support.
set -uo pipefail
# This suite exercises the explicit work-file compatibility contract.
export FPL_GATE_EVIDENCE=work

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
export PYTHONIOENCODING=utf-8

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
export CLAUDE_PLUGIN_ROOT="$PLUGIN"
EV="$PLUGIN/scripts/evidence.py"
GATE="$PLUGIN/scripts/dod-gate.sh"
INJECT="$PLUGIN/scripts/inject-state.sh"
ROOT="$(mktemp -d)"
R="$ROOT/r"
pass=0; fail=0

ok()  { printf 'PASS  %-56s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-56s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

work_md() {
  cat >"$R/WORK.md" <<'EOF'
# work

STATUS: WIP
MODE: loop

## Evidence

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|
EOF
}

mkrepo() { # $1 = harness exit code
  rm -rf "$R"; mkdir -p "$R/scripts" "$R/src"; cd "$R" || exit 1
  git init -q -b main
  printf '#!/usr/bin/env bash\nexit %s\n' "$1" >scripts/harness.sh
  chmod +x scripts/harness.sh
  printf 'x = 1\n' >src/app.py
  work_md
  git add -A; git -c user.email=t@t -c user.name=t commit -qm base
}

stop_input() { printf '{"session_id":"s","cwd":"%s"}' "$R"; }
run_gate() { stop_input | bash "$GATE" >/dev/null 2>&1; }
# grep -c prints 0 AND exits 1 on no match, so a naive `|| echo 0` emits two
# lines and every comparison against it fails for the wrong reason.
gate_rows() {
  [ -f "$R/WORK.md" ] || { echo 0; return; }
  grep -c '^| .* | gate |' "$R/WORK.md" 2>/dev/null | head -1
}

# ================= 1. the gate records its own verdict ===================
mkrepo 0
printf 'y = 2\n' >>src/app.py          # arm the gate: code changed
run_gate
check "a green stop records a gate row" 1 "$(gate_rows)"
grep -q '| gate | PASS |' "$R/WORK.md" \
  && ok "the row carries the gate's own verdict" "PASS" \
  || bad "the row carries the gate's own verdict" "$(grep '^| 20' "$R/WORK.md" | head -1)"
grep -qE 'harness --full exit 0 @[0-9a-f]+ tree:[0-9a-f]+ dirty:[0-9]+ log:[0-9a-z]+' \
  "$R/WORK.md" \
  && ok "the proof names exit, commit, tree, dirt and log" "complete" \
  || bad "the proof names exit, commit, tree, dirt and log" "$(grep '^| 20' "$R/WORK.md" | head -1)"

# A long green streak must not fill the table with identical rows.
run_gate
check "an unchanged green verdict is not repeated" 1 "$(gate_rows)"

# ...but a changed tree is a different verdict, even when the number of
# dirty paths is identical — which is why the proof hashes the tree rather
# than counting it. Without that, a verdict taken before the work and one
# taken after are the same row.
printf 'z = 3\n' >>src/app.py
run_gate
check "editing an already-dirty file still records a new row" 2 "$(gate_rows)"

# ================= 2. a blocked stop is not an outcome ===================
mkrepo 1
printf 'y = 2\n' >>src/app.py
run_gate
check "an ordinary blocked stop records nothing" 0 "$(gate_rows)"
check "and the session is blocked, not stopped" 1 \
  "$(cat "$R/.claude/fluxpoint/s.blocks" 2>/dev/null || echo 0)"

# The checkpoint yield IS a stop, with the work unfinished. FPL_MAX_BLOCKS
# is 3, and the counter is read before it is bumped, so the fourth armed
# stop is the one that yields.
run_gate; run_gate; run_gate
check "the checkpoint yield records a FAIL row" 1 "$(gate_rows)"
grep -q '| gate | FAIL |' "$R/WORK.md" \
  && ok "and it says the task is not done" "FAIL" \
  || bad "and it says the task is not done" "missing"

# ================= 3. the writer executes nothing ========================
mkrepo 0
# The whole reason this is a writer and not a minting executor: a process
# the agent launches cannot outrank the agent. Imports, not prose — the
# docstring discusses subprocesses at length precisely to explain their
# absence.
grep -qE '^\s*(import|from)\s+(subprocess|os\.system|shlex)' "$EV" \
  && bad "evidence.py runs no commands" "it spawns processes" \
  || ok "evidence.py runs no commands" "writer only"

# A pipe in a claim would forge columns; a newline would forge a row.
"$FPL_PY" "$EV" --record --graph "$R/WORK.md" --outcome PASS \
  --claim 'a | b' --proof 'x' >/dev/null 2>&1
check "a pipe in a claim is refused, not silently escaped" 2 "$?"
"$FPL_PY" "$EV" --record --graph "$R/WORK.md" --outcome PASS \
  --claim "$(printf 'a\nb')" --proof 'x' >/dev/null 2>&1
check "a newline in a claim is refused" 2 "$?"
check "an unknown outcome is refused" 2 \
  "$("$FPL_PY" "$EV" --record --graph "$R/WORK.md" --outcome MAYBE \
       --claim 'aaaaaaaaaaaaaaaaaaaaaa' --proof 'x' >/dev/null 2>&1; echo $?)"

# The row must land in a real table, and say so when it cannot.
printf '# no table here\n' >"$R/NOTABLE.md"
check "a file with no Evidence header is reported, not written" 3 \
  "$("$FPL_PY" "$EV" --record --graph "$R/NOTABLE.md" --outcome PASS \
       --claim 'aaaaaaaaaaaaaaaaaaaaaa' --proof 'x' >/dev/null 2>&1; echo $?)"
cat >"$R/LEGACY.md" <<'EOF'
## Evidence

| When (UTC) | runId | Outcome | Nodes OK/dead | Findings | Harness | Red-team |
|---|---|---|---|---|---|---|
EOF
out="$("$FPL_PY" "$EV" --record --graph "$R/LEGACY.md" --outcome PASS \
  --claim 'aaaaaaaaaaaaaaaaaaaaaa' --proof 'x' 2>&1)"
case "$out" in *migrate*) ok "a pre-1.0 table is named, not silently skipped" "named" ;;
  *) bad "a pre-1.0 table is named, not silently skipped" "${out:0:44}" ;; esac

# Column-aligned separators are what most markdown formatters emit.
mkrepo 0
"$FPL_PY" - "$R/WORK.md" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read().replace("|---|---|---|---|---|", "|:---|:---|:---:|---|---:|")
open(p, "w").write(s)
PY
printf 'y = 2\n' >>src/app.py
run_gate
check "an aligned separator row still takes the splice" 1 "$(gate_rows)"

# ================= 4. the read side separates the classes ================
mkrepo 0
printf 'y = 2\n' >>src/app.py
run_gate
for i in 1 2 3 4 5 6; do
  "$FPL_PY" "$EV" --record --graph "$R/WORK.md" --source loop --outcome PASS \
    --claim "slice $i did the thing it said it did" --proof "asserted by the agent $i" \
    >/dev/null 2>&1
done
out="$(stop_input | bash "$INJECT" 2>&1)"
case "$out" in *"Recorded by the gate"*) ok "the bootstrap names the witnessed class" "labelled" ;;
  *) bad "the bootstrap names the witnessed class" "missing" ;; esac
case "$out" in *"Asserted, not witnessed"*) ok "and labels the rest as assertions" "labelled" ;;
  *) bad "and labels the rest as assertions" "missing" ;; esac
# Six agent rows would previously have evicted the gate row from a flat
# newest-5 window; the reserved budget is what stops that.
case "$out" in *"| gate | PASS |"*)
  ok "agent rows cannot evict the witnessed row" "gate row survived" ;;
  *) bad "agent rows cannot evict the witnessed row" "EVICTED" ;; esac

# ================= 5. dormancy and the plugin_script regression ==========
mkrepo 0
rm "$R/WORK.md"
printf 'y = 2\n' >>src/app.py
run_gate
check "no work file: the gate still exits clean" 0 "$?"

# find exits 1 on a missing plugin dir; pipefail + set -e used to end the run.
cat >"$ROOT/ps.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
EOF
sed -n '/^plugin_script()/,/^}/p' "$PLUGIN/templates/harness.sh" >>"$ROOT/ps.sh"
# Isolate only the discovery filesystem. The resolver intentionally also
# searches the real Codex cache, which may already contain this script, and
# an explicit PLUGIN_ROOT bypasses discovery altogether. Remap both find
# roots to missing scratch directories while keeping real find's exit 1;
# HOME and CODEX_HOME retain the caller's values throughout the test.
cat >>"$ROOT/ps.sh" <<'EOF'
unset FPL_PLUGIN_ROOT CLAUDE_PLUGIN_ROOT PLUGIN_ROOT
find() {
  printf '%s\n' "$1" "$2" >"$FPL_EVIDENCE_FIND_FIXTURE/find-roots"
  shift 2
  command find "$FPL_EVIDENCE_FIND_FIXTURE/missing-claude" \
    "$FPL_EVIDENCE_FIND_FIXTURE/missing-codex" "$@"
}
p="$(plugin_script proof-guard.py)"
echo "survived:[$p]"
EOF
mkdir -p "$ROOT/cache-fixture"
out="$(FPL_EVIDENCE_FIND_FIXTURE="$ROOT/cache-fixture" bash "$ROOT/ps.sh" 2>&1)"; rc=$?
check "plugin_script survives a missing plugin directory" 0 "$rc"
case "$out" in *"survived:[]"*) ok "and returns empty rather than exploding" "empty" ;;
  *) bad "and returns empty rather than exploding" "${out:0:40}" ;; esac
expected_roots="$(printf '%s\n' "$HOME/.claude/plugins" "${CODEX_HOME:-$HOME/.codex}/plugins/cache")"
if [ "$(cat "$ROOT/cache-fixture/find-roots" 2>/dev/null)" = "$expected_roots" ]; then
  ok "missing-plugin fixture covers both runtime discovery roots" "Claude + Codex"
else
  bad "missing-plugin fixture covers both runtime discovery roots" "unexpected discovery roots"
fi

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
