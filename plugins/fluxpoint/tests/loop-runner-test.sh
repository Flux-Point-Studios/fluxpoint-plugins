#!/usr/bin/env bash
# The outer runner's two silent failures.
#
# Both are failures of a check that reports success rather than of a check
# that breaks, which is why neither showed up in a run log.
#
#   co-change   pair-guard diffs against HEAD by default. The runner commits
#               the iteration before grading it, so `git diff HEAD` is empty
#               and every declared pair reports "no changes to compare". A
#               pair with no `parity` command is then enforced by nothing at
#               all inside the loop — and co-change-only is the normal
#               starting state, since only the consuming repo can write a
#               parity vector.
#
#   notify      `find ... | head -1` with no `|| true` dies under
#               `set -euo pipefail` on a machine with no ~/.claude/plugins,
#               on the very line that exists to file the budget-exhausted
#               notice. It exits 1, which is what the intended path would
#               have exited too, so nothing looks wrong.
set -uo pipefail

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
GUARD="$PLUGIN/scripts/pair-guard.py"
ROOT="$(mktemp -d)"
pass=0; fail=0
trap 'cd /; rm -rf "$ROOT"' EXIT

ok()  { printf 'PASS  %-54s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-54s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

# A repo with one co-change-only pair: two files that must move together and
# no parity command to fall back on.
newrepo() {
  rm -rf "$ROOT/r"; mkdir -p "$ROOT/r"; cd "$ROOT/r" || exit 1
  git init -q -b main
  printf 'on-chain\n'  >predicate.txt
  printf 'off-chain\n' >builder.txt
  "$FPL_PY" - <<'PY' >.fluxpoint-pairs.json
import json
print(json.dumps([{
    "id": "predicate-builder",
    "source": ["predicate.txt"],
    "mirror": ["builder.txt"],
    "why": "the builder constructs what the predicate accepts",
}], indent=2))
PY
  git add -A
  git -c user.email=t@t -c user.name=t commit -qm base
}

# --- 1. the co-change tier, before and after the runner's commit ---------

newrepo
printf 'on-chain tightened\n' >predicate.txt      # one side only
"$FPL_PY" "$GUARD" --check >/dev/null 2>&1
check "a one-sided change is caught in the working tree" 1 $?

git add -A
git -c user.email=t@t -c user.name=t commit -qm "loop: iteration 1"
"$FPL_PY" "$GUARD" --check >/dev/null 2>&1
check "and is INVISIBLE once the runner commits it" 0 $?

base="$(git rev-parse HEAD~1)"
"$FPL_PY" "$GUARD" --check --against "$base" >/dev/null 2>&1
check "unless graded against where the iteration started" 1 $?

# --- 2. the runner passes that base, and the harness honors it -----------

grep -q 'iter_base="\$(git rev-parse HEAD 2>/dev/null || true)"' \
  "$PLUGIN/templates/loop.sh"
check "loop.sh captures the pre-iteration base" 0 $?

grep -q 'FPL_PAIR_AGAINST="\${FPL_PAIR_AGAINST:-\$iter_base}"' \
  "$PLUGIN/templates/loop.sh"
check "and passes it to the harness it grades with" 0 $?

grep -q 'pair_base="\${FPL_PAIR_AGAINST:-\${FPL_DIFF_BASE:-}}"' \
  "$PLUGIN/templates/harness.sh"
check "harness.sh falls back to the Stop gate's session base" 0 $?

# The Stop gate exports FPL_DIFF_BASE precisely so nothing is judged against
# HEAD; pair-guard was the one check that did not read it.
grep -q 'export FPL_DIFF_BASE' "$PLUGIN/scripts/dod-gate.sh"
check "and that base is the one the Stop gate already exports" 0 $?

# --- 3. the notification survives a machine with no plugins directory ----

tail_of_loop() {  # everything from the budget-exhausted notice down
  sed -n '/iteration budget exhausted/,$p' "$PLUGIN/templates/loop.sh"
}

cat >"$ROOT/notify.sh" <<EOF
set -euo pipefail
FPL_PY=$(command -v "$FPL_PY")
HOME="$ROOT/emptyhome"
$(tail_of_loop | sed 's/^exit 1$/echo REACHED-THE-END/')
EOF
mkdir -p "$ROOT/emptyhome"
out="$(bash "$ROOT/notify.sh" 2>&1)"
case "$out" in
  *REACHED-THE-END*) ok "the notify block survives an absent plugins dir" "reached" ;;
  *) bad "the notify block survives an absent plugins dir" "died early: ${out:0:40}" ;;
esac

# --- 4. one iteration contract, two agent CLIs ----------------------------
# Fakes stand in for both CLIs and record what they were handed; the repo is
# already DONE and green, so a run that reaches the harness exits 0.
fakebin="$ROOT/bin"; mkdir -p "$fakebin"
cat >"$fakebin/claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$FAKE_LOG.args"
exit 0
EOF
cat >"$fakebin/codex" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$FAKE_LOG.args"
cat >"$FAKE_LOG.stdin"
exit 0
EOF
chmod +x "$fakebin/claude" "$fakebin/codex"
looprepo() {
  rm -rf "$ROOT/l"; mkdir -p "$ROOT/l/scripts"; cd "$ROOT/l" || exit 1
  git init -q -b main; git config user.email t@t; git config user.name t
  printf '#!/usr/bin/env bash\nexit 0\n' >scripts/harness.sh; chmod +x scripts/harness.sh
  printf 'STATUS: DONE\n' >WORK.md
  printf 'work the next slice\n' >WORK_PROMPT.md
  git add -A; git commit -qm base
}
looprepo
FAKE_LOG="$ROOT/claude" PATH="$fakebin:$PATH" MAX_ITER=1 bash "$PLUGIN/templates/loop.sh" >/dev/null 2>&1
check "loop.sh: the default driver is claude; green and DONE exits 0" 0 $?
grep -qx -- '-p' "$ROOT/claude.args" && grep -q 'work the next slice' "$ROOT/claude.args"
check "loop.sh: claude receives the work prompt with -p" 0 $?
looprepo
FAKE_LOG="$ROOT/codex" PATH="$fakebin:$PATH" MAX_ITER=1 AGENT_CLI=codex bash "$PLUGIN/templates/loop.sh" >/dev/null 2>&1
check "loop.sh: AGENT_CLI=codex drives codex exec" 0 $?
[ "$(head -1 "$ROOT/codex.args")" = "exec" ] && grep -q 'work the next slice' "$ROOT/codex.stdin"
check "loop.sh: codex receives the work prompt on stdin" 0 $?
looprepo
FAKE_LOG="$ROOT/x" PATH="$fakebin:$PATH" MAX_ITER=1 AGENT_CLI=other bash "$PLUGIN/templates/loop.sh" >/dev/null 2>&1
check "loop.sh: an unknown AGENT_CLI is refused before anything runs" 2 $?
[ -f "$ROOT/x.args" ] && bad "loop.sh: nothing ran for the unknown driver" "a CLI ran" \
  || ok "loop.sh: nothing ran for the unknown driver" "nothing ran"

# --- 5. declared metrics stop on plateau and report why -----------------
cat >"$fakebin/claude" <<'EOF'
#!/usr/bin/env bash
"$FPL_PY" - <<'PYAGENT'
import os
from pathlib import Path
p = Path('iteration')
i = int(p.read_text()) if p.exists() else 0
values = os.environ['FAKE_SERIES'].split(',')
Path('value').write_text(values[min(i, len(values)-1)])
p.write_text(str(i + 1))
PYAGENT
EOF
chmod +x "$fakebin/claude"
metricrepo() {
  looprepo
  printf 'STATUS: ACTIVE\n' >WORK.md
  printf '10' >value
  printf '.claude/\n' >.gitignore
  "$FPL_PY" - "$FPL_PY" <<'PYMETRIC' >.fluxpoint-metrics.json
import json, sys
print(json.dumps({'metrics': [{'name': 'cost', 'direction': 'down',
    'command': [sys.argv[1], '-c', 'print(open("value").read())']}]}))
PYMETRIC
  git add -A; git commit -qm metrics
}
metricrepo
FPL_PY="$FPL_PY" FPL_PLUGIN_ROOT="$PLUGIN" FAKE_SERIES=10,10,10,10 \
  PATH="$fakebin:$PATH" MAX_ITER=8 bash "$PLUGIN/templates/loop.sh" >"$ROOT/flat.log" 2>&1
check "flat metric stops after plateau window" 3 "$(cat iteration)"
grep -q '"kind": "plateau"' .claude/fluxpoint/inbox.jsonl
check "plateau reason is recorded in inbox" 0 $?
metricrepo
FPL_PY="$FPL_PY" FPL_PLUGIN_ROOT="$PLUGIN" FAKE_SERIES=9,8,7,6,5 \
  PATH="$fakebin:$PATH" MAX_ITER=5 bash "$PLUGIN/templates/loop.sh" >"$ROOT/improving.log" 2>&1
check "improving metric continues to iteration budget" 5 "$(cat iteration)"
grep -q '"kind": "budget-exhausted"' .claude/fluxpoint/inbox.jsonl
check "improving metric reports budget, not plateau" 0 $?
metricrepo
FPL_PY="$FPL_PY" FPL_PLUGIN_ROOT="$PLUGIN" FAKE_SERIES=9,99,8,7,6 \
  PATH="$fakebin:$PATH" MAX_ITER=5 bash "$PLUGIN/templates/loop.sh" >"$ROOT/noisy.log" 2>&1
check "one worse iteration does not trigger a plateau" 5 "$(cat iteration)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
