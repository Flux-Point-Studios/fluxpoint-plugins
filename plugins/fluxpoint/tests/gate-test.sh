#!/usr/bin/env bash
# Reproduction + regression test for the fluxpoint Stop-gate bypass.
# Builds a scratch repo whose harness is RED, then drives dod-gate.sh the way
# the Stop hook does, under each arming scenario.
set -uo pipefail
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
GATE="$PLUGIN/scripts/dod-gate.sh"
INJECT="$PLUGIN/scripts/inject-state.sh"
ROOT="$(mktemp -d)"
pass=0; fail=0

setup() { # $1 = harness exit code
  rm -rf "$ROOT/repo"; mkdir -p "$ROOT/repo/scripts" "$ROOT/repo/src"
  cd "$ROOT/repo" || exit 1
  git init -q -b main
  printf '#!/usr/bin/env bash\nexit %s\n' "$1" >scripts/harness.sh
  chmod +x scripts/harness.sh
  printf 'print("baseline")\n' >src/app.py
  git add -A
  git -c user.email=t@t -c user.name=t commit -qm baseline
  # The runtime fires SessionStart before any work, and that is where the
  # gate's baseline commit is recorded. Driving the real hook here keeps the
  # arming path end-to-end rather than assuming the file into existence.
  printf '{"session_id":"testsid","cwd":"%s"}' "$ROOT/repo" \
    | bash "$INJECT" >/dev/null 2>&1
}

run_gate() {
  printf '{"session_id":"testsid","cwd":"%s"}' "$ROOT/repo" | bash "$GATE" 2>/dev/null
}

check() { # $1=name  $2=expected(block|allow)  $3=actual output
  local got="allow"
  printf '%s' "$3" | grep -q '"decision"[[:space:]]*:[[:space:]]*"block"' && got="block"
  if [ "$got" = "$2" ]; then
    printf 'PASS  %-52s -> %s\n' "$1" "$got"; pass=$((pass+1))
  else
    printf 'FAIL  %-52s -> %s (wanted %s)\n' "$1" "$got" "$2"; fail=$((fail+1))
  fi
}

# 1. THE BYPASS: source edited via Bash tool only (no Write/Edit marker), harness RED.
setup 1
printf 'print("changed via bash tool")\n' >>src/app.py   # exactly what `cat >>` does
check "bash-tool edit, harness RED" block "$(run_gate)"

# 2. Same, but the edit was committed (git-clean tree) with the marker present:
#    the marker path must still work.
setup 1
printf 'print("changed")\n' >>src/app.py
git add -A; git -c user.email=t@t -c user.name=t commit -qm work
mkdir -p .claude/fluxpoint; : >.claude/fluxpoint/testsid.dirty
check "committed work, marker present, harness RED" block "$(run_gate)"

# 3. Untracked new source file via Bash, harness RED.
setup 1
printf 'def f(): pass\n' >src/new_module.py
check "untracked new source, harness RED" block "$(run_gate)"

# 4. No changes at all -> gate must stay dormant (no false arming).
setup 1
check "clean tree, no marker (must not arm)" allow "$(run_gate)"

# 5. Docs-only change -> skipped by _fpl_skip_path, must not arm.
setup 1
printf '# notes\n' >>README.md
check "docs-only change (must not arm)" allow "$(run_gate)"

# 6. Bash-tool edit with harness GREEN -> gate runs, passes, allows stop.
setup 0
printf 'print("ok")\n' >>src/app.py
check "bash-tool edit, harness GREEN" allow "$(run_gate)"

# 7. Tampering with the contract is surfaced on the PASS path — not just the
# harness, but every file that decides what green means.
setup 0
printf 'exit 0\n' >>scripts/harness.sh    # gutting the contract itself
out="$(run_gate)"
if printf '%s' "$out" | grep -q 'scripts/harness.sh'; then
  printf 'PASS  %-52s -> notice emitted\n' "modified harness on green"; pass=$((pass+1))
else
  printf 'FAIL  %-52s -> no notice\n' "modified harness on green"; fail=$((fail+1))
fi

# A re-recorded proof baseline turns a red ratchet green without touching a
# line of the code under test, and it used to pass in silence.
setup 0
printf 'print("ok")\n' >>src/app.py
printf '{"version":1,"counts":{"aiken.todo":9}}\n' >.fluxpoint-proof-baseline.json
out="$(run_gate)"
if printf '%s' "$out" | grep -q 'fluxpoint-proof-baseline.json'; then
  printf 'PASS  %-52s -> notice emitted\n' "re-recorded baseline on green"; pass=$((pass+1))
else
  printf 'FAIL  %-52s -> no notice\n' "re-recorded baseline on green"; fail=$((fail+1))
fi

# ---- the bypass this slice closes -------------------------------------
# Source written through the Bash tool fires no PostToolUse marker, and once
# it is committed the tree matches HEAD, so the gate used to find nothing to
# judge and the harness never ran. The work prompt tells the loop to commit
# every slice, which made this the normal path rather than a clever one.
setup 1
printf 'print("bad")\n' >>src/app.py
git add -A >/dev/null 2>&1
git -c user.email=t@t -c user.name=t commit -qm "committed mid-session"
check "committed bash-tool edit, harness RED (no marker)" block "$(run_gate)"

# The same shape on green must still allow the stop, and must not re-arm
# forever afterwards.
setup 0
printf 'print("ok")\n' >>src/app.py
git add -A >/dev/null 2>&1
git -c user.email=t@t -c user.name=t commit -qm "committed mid-session"
check "committed bash-tool edit, harness GREEN" allow "$(run_gate)"
check "and a second stop with nothing new does not re-run" allow "$(run_gate)"

# A hygiene marker committed mid-session used to be invisible: the scan
# diffed against HEAD, so committing laundered it past the gate.
setup 0
printf 'x = 1  # TODO: finish this\n' >>src/app.py
git add -A >/dev/null 2>&1
git -c user.email=t@t -c user.name=t commit -qm "committed a marker"
out="$(run_gate)"
if printf '%s' "$out" | grep -q 'hygiene scan RED'; then
  printf 'PASS  %-52s -> caught\n' "committed hygiene marker"; pass=$((pass+1))
else
  printf 'FAIL  %-52s -> laundered by committing\n' "committed hygiene marker"; fail=$((fail+1))
fi

# False positives are the failure that gets a gate disabled. A baseline that
# cannot describe the current branch must degrade to HEAD rather than diff
# the tree against unrelated history.
setup 0
git checkout -q -b other 2>/dev/null
printf 'unrelated\n' >other.py
git add -A >/dev/null 2>&1
git -c user.email=t@t -c user.name=t commit -qm "on another branch"
git checkout -q - 2>/dev/null
check "a baseline that is not an ancestor falls back to HEAD" allow "$(run_gate)"

# ---- a gate that cannot finish must stop RUNNING, not just stop blocking ----
# Observed 2026-08-11 in a repo whose harness --full takes ~35 min against a
# 540s ceiling: the gate times out, yields after $max blocks -- and then re-runs
# the entire suite on EVERY subsequent stop, paying full wall-clock to reach a
# conclusion it has already abandoned. The operator's only escape was Esc.
# Yielding is a decision about a tree; it must hold until that tree changes.
#
# The run witness lives OUTSIDE the repo on purpose. A counter file inside it
# is part of `git status`, so writing to it moves the very tree hash the guard
# compares -- the first draft of this test failed for exactly that reason and
# the guard was innocent.
setup 1
runs="$ROOT/harness-runs"; rm -f "$runs"
cat >scripts/harness.sh <<SH
#!/usr/bin/env bash
echo run >>"$runs"
exit 1
SH
chmod +x scripts/harness.sh
printf 'print("changed")
' >>src/app.py
export FPL_MAX_BLOCKS=1
run_gate >/dev/null   # attempt 1 of 1 -> block
run_gate >/dev/null   # count >= max   -> yield, records the tree
before="$(grep -c . "$runs" 2>/dev/null || echo 0)"
run_gate >/dev/null   # nothing changed since the yield -> must not run at all
after="$(grep -c . "$runs" 2>/dev/null || echo 0)"
if [ "$before" = "$after" ]; then
  printf 'PASS  %-52s -> %s
' "yielded gate does not re-run an unchanged tree" "still $after"; pass=$((pass+1))
else
  printf 'FAIL  %-52s -> %s
' "yielded gate does not re-run an unchanged tree" "$before then $after"; fail=$((fail+1))
fi
# ...but a real edit must re-arm it, or yielding would disable the gate for good.
printf 'print("more work")
' >>src/app.py
run_gate >/dev/null
again="$(grep -c . "$runs" 2>/dev/null || echo 0)"
if [ "$again" -gt "$after" ]; then
  printf 'PASS  %-52s -> %s
' "a changed tree re-arms the yielded gate" "$after then $again"; pass=$((pass+1))
else
  printf 'FAIL  %-52s -> %s
' "a changed tree re-arms the yielded gate" "stuck at $again"; fail=$((fail+1))
fi
unset FPL_MAX_BLOCKS

# ---- the gate enforces the contract the REPO names, not a hardcoded --full ----
# A repo whose --full outgrows the 600s hook ceiling could previously only be
# rescued by FPL_DISABLE=1, which trades a slow gate for no gate. FPL_HARNESS_ARGS
# lets it enforce a cheaper contract every stop instead.
setup 0
argfile="$ROOT/harness-args"; rm -f "$argfile"
cat >scripts/harness.sh <<SH
#!/usr/bin/env bash
echo "\$@" >>"$argfile"
exit 0
SH
chmod +x scripts/harness.sh
printf 'print("changed")\n' >>src/app.py
FPL_HARNESS_ARGS="--changed src/app.py" run_gate >/dev/null
got="$(cat "$argfile" 2>/dev/null)"
if [ "$got" = "--changed src/app.py" ]; then
  printf 'PASS  %-52s -> %s\n' "FPL_HARNESS_ARGS reaches the harness" "$got"; pass=$((pass+1))
else
  printf 'FAIL  %-52s -> %s\n' "FPL_HARNESS_ARGS reaches the harness" "${got:-<nothing>}"; fail=$((fail+1))
fi
# Unset must still mean --full: this is a widening, not a default change.
setup 0
rm -f "$argfile"
cat >scripts/harness.sh <<SH
#!/usr/bin/env bash
echo "\$@" >>"$argfile"
exit 0
SH
chmod +x scripts/harness.sh
printf 'print("changed again")\n' >>src/app.py
run_gate >/dev/null
got="$(cat "$argfile" 2>/dev/null)"
if [ "$got" = "--full" ]; then
  printf 'PASS  %-52s -> %s\n' "unset still means --full" "$got"; pass=$((pass+1))
else
  printf 'FAIL  %-52s -> %s\n' "unset still means --full" "${got:-<nothing>}"; fail=$((fail+1))
fi

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
