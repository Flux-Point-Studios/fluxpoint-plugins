#!/usr/bin/env bash
# The ratchet for guards that stop money, tested against the shape that
# defeated it: a proof that cannot run at all.
#
# --verify disables a guard and requires its proof to fail. Read alone, that
# rule cannot tell a guard that bites from a proof that never ran: a broken
# import, a collection error, a missing interpreter and a suite red for a
# fortnight all exit non-zero, and all used to report "the guard is real".
# The third case below is this tool's own motivating story passing itself,
# which is why the intact run is the property under test rather than a
# refinement of it.
#
# Executed, not inspected: every case builds a real repo, runs the real
# script, and reads its exit status.
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
GG="$PLUGIN/scripts/guard-guard.py"
pass=0; fail=0
ok()  { printf 'PASS  %-54s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-54s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

# The interpreter as a `run` line spells it: guard-guard runs the line under a
# POSIX shell, so it is single-quoted; the guards file is JSON, so that is then
# JSON-escaped. A Windows interpreter path carries backslashes both would eat.
SH_PY="'$(printf '%s' "$FPL_PY" | sed "s/'/'\\\\''/g")'"
RUN_PY="$(printf '%s' "$SH_PY" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# A repo whose guard really is held down by its proof. `run` is a bare
# interpreter call rather than pytest, so the suite does not depend on a
# test runner being installed.
mkrepo() {
  r="$WORK/$1"; rm -rf "$r"; mkdir -p "$r"
  printf 'def fee_ok(amount):\n    if amount > 100:\n        return False\n    return True\n' >"$r/server.py"
  printf 'import server\n\n\ndef test_fee():\n    assert server.fee_ok(50)\n    assert not server.fee_ok(500), "guard gone"\n\n\ntest_fee()\n' >"$r/test_fee.py"
  cat >"$r/.fluxpoint-guards.json" <<JSON
{"guards": [{
  "id": "fee-bound",
  "protects": "a fee above the cap must never be accepted",
  "guard": {"file": "server.py", "contains": "if amount > 100"},
  "proof": {"file": "test_fee.py", "test": "test_fee"},
  "mutation": {"find": "if amount > 100", "replace": "if False"},
  "expect": "guard gone",
  "run": "$RUN_PY test_fee.py"
}]}
JSON
}

# ================= 1. the healthy case still verifies =====================
mkrepo good
"$FPL_PY" "$GG" --root "$WORK/good" --verify >/dev/null 2>&1
check "a guard held down by its proof verifies" 0 $?

# `run` is a POSIX command line on every platform. On Windows shell=True would
# hand it to cmd.exe, which has no `NAME=value cmd` prefix.
mkrepo posix
"$FPL_PY" - "$WORK/posix/.fluxpoint-guards.json" "PROOF=1 $SH_PY test_fee.py" <<'PY'
import json, sys
path, run = sys.argv[1], sys.argv[2]
with open(path) as f:
    doc = json.load(f)
doc["guards"][0]["run"] = run
with open(path, "w") as f:
    json.dump(doc, f)
PY
"$FPL_PY" "$GG" --root "$WORK/posix" --verify >/dev/null 2>&1
check "a proof run written as a POSIX command line verifies" 0 $?

# ================= 2. a decorative proof is caught ========================
# The proof passes even with the guard disabled: it asserts nothing about it.
mkrepo decorative
printf 'import server\n\n\ndef test_fee():\n    assert server.fee_ok(50)\n\n\ntest_fee()\n' >"$WORK/decorative/test_fee.py"
out="$("$FPL_PY" "$GG" --root "$WORK/decorative" --verify 2>&1)"; rc=$?
check "a proof that passes without its guard is caught" 1 "$rc"
case "$out" in *"PASSED with the guard disabled"*) ok "and is named as proving nothing" "reported" ;;
  *) bad "and is named as proving nothing" "${out:0:60}" ;; esac

# ================= 3. THE REGRESSION: a proof that cannot run =============
# Every one of these exits non-zero with the guard disabled, which is exactly
# what a bitten guard looks like. Only the intact run tells them apart.
mkrepo broken
printf 'import no_such_module_at_all\n\n\ndef test_fee():\n    pass\n\n\ntest_fee()\n' >"$WORK/broken/test_fee.py"
out="$("$FPL_PY" "$GG" --root "$WORK/broken" --verify 2>&1)"; rc=$?
check "a proof that cannot even import is NOT 'the guard is real'" 1 "$rc"
case "$out" in *"does not pass with the guard INTACT"*) ok "and says the proof is the problem" "reported" ;;
  *) bad "and says the proof is the problem" "${out:0:60}" ;; esac

mkrepo redsuite
printf 'import server\n\n\ndef test_fee():\n    assert False, "red for a fortnight"\n\n\ntest_fee()\n' >"$WORK/redsuite/test_fee.py"
"$FPL_PY" "$GG" --root "$WORK/redsuite" --verify >/dev/null 2>&1
check "a permanently red proof is NOT 'the guard is real'" 1 $?

mkrepo nocmd
sed -i.bak 's#"run": .*#"run": "definitely-not-a-real-binary-xyz test_fee.py"#' "$WORK/nocmd/.fluxpoint-guards.json"
"$FPL_PY" "$GG" --root "$WORK/nocmd" --verify >/dev/null 2>&1
check "a command-not-found proof is NOT 'the guard is real'" 1 $?

# ================= 4. the tree is always restored =========================
mkrepo restore
"$FPL_PY" "$GG" --root "$WORK/restore" --verify >/dev/null 2>&1
grep -q 'if amount > 100' "$WORK/restore/server.py" \
  && ok "the guard is restored after a verify" "intact" \
  || bad "the guard is restored after a verify" "LEFT DISABLED"
[ ! -e "$WORK/restore/.fluxpoint-guards-restoring" ] \
  && ok "and the in-flight sentinel is cleared" "clean" \
  || bad "and the in-flight sentinel is cleared" "SENTINEL LEFT"

# The anchor must match EXACTLY once. `replace(find, replace, 1)` on the
# first of several hits disables a site the proof may never reach, and the
# proof's failure (or success) is then a fact about the wrong line; an
# anchor that misses is a no-op that reads the same way (issue #62).
mkrepo twice
printf '\n\ndef refund_ok(amount):\n    if amount > 100:\n        return False\n    return True\n' >>"$WORK/twice/server.py"
before="$(cat "$WORK/twice/server.py")"
out="$("$FPL_PY" "$GG" --root "$WORK/twice" --verify 2>&1)"; rc=$?
check "an anchor matching twice is refused" 1 "$rc"
case "$out" in *"matched 2 time(s)"*) ok "and the count is named" "reported" ;;
  *) bad "and the count is named" "${out:0:60}" ;; esac
[ "$(cat "$WORK/twice/server.py")" = "$before" ] \
  && ok "and the guard file is untouched" "untouched" \
  || bad "and the guard file is untouched" "CHANGED"

# A killed run is the case `finally` does not cover. SIGTERM mid-proof must
# still put the guard back, because a repo left with `if False:` in place of
# a fee bound is the silent-guard-death this tool exists to prevent.
mkrepo killed
printf 'import time\nimport server\n\n\ndef test_fee():\n    time.sleep(5)\n\n\ntest_fee()\n' >"$WORK/killed/test_fee.py"
"$FPL_PY" "$GG" --root "$WORK/killed" --verify >/dev/null 2>&1 &
gg_pid=$!
# The sentinel only appears once the INTACT run has finished and the mutated
# window is open, so the wait has to outlast a full proof run, not half of one.
waited=0
while [ "$waited" -lt 120 ]; do
  [ -e "$WORK/killed/.fluxpoint-guards-restoring" ] && break
  sleep 0.25
  waited=$((waited+1))
done
if [ -e "$WORK/killed/.fluxpoint-guards-restoring" ]; then
  kill -TERM "$gg_pid" 2>/dev/null
  wait "$gg_pid" 2>/dev/null
  # Native Windows termination can skip every interpreter cleanup callback.
  # The next invocation recovers the durable journal before checking guards.
  "$FPL_PY" "$GG" --root "$WORK/killed" --check >/dev/null 2>&1
  grep -q 'if amount > 100' "$WORK/killed/server.py" \
    && ok "SIGTERM mid-proof still restores the guard" "intact" \
    || bad "SIGTERM mid-proof still restores the guard" "LEFT DISABLED"
else
  kill -TERM "$gg_pid" 2>/dev/null; wait "$gg_pid" 2>/dev/null
  bad "SIGTERM mid-proof still restores the guard" "window never opened"
fi

# ================= 5. --check and the removal floor =======================
mkrepo floor
"$FPL_PY" "$GG" --root "$WORK/floor" --check >/dev/null 2>&1
check "a registered guard passes --check" 0 $?
sed -i.bak 's/if amount > 100/if True/' "$WORK/floor/server.py"
"$FPL_PY" "$GG" --root "$WORK/floor" --check >/dev/null 2>&1
check "a deleted guard fails --check" 1 $?

mkrepo removed
"$FPL_PY" "$GG" --root "$WORK/removed" --baseline >/dev/null 2>&1
printf '{"guards": []}\n' >"$WORK/removed/.fluxpoint-guards.json"
"$FPL_PY" "$GG" --root "$WORK/removed" --check >/dev/null 2>&1
check "dropping a guard from the manifest fails the floor" 1 $?

# ========== 6. the DISABLED run must refuse non-evidence too ==============
# The intact run closes "the proof cannot run". These two close its twin: a
# proof that reddens for a reason with nothing to do with the guard. Both
# reported "the guard is real" before the manifest carried `expect`.

# 6a. DECORATION. The proof only walks the happy path, so it never exercises the
# pin at all; the mutation deletes the constant, so the module raises NameError
# and the proof reddens on a broken reference. Non-zero, and evidence of
# nothing. This is the shape the tool is named for, wearing a passing costume.
r="$WORK/decoration"; rm -rf "$r"; mkdir -p "$r"
printf 'PINNED_VKH = "abc123"\n\n\ndef serve(vkh):\n    if vkh != PINNED_VKH:\n        raise ValueError("unpinned key accepted")\n    return vkh\n' >"$r/server.py"
printf 'import server\n\n\ndef test_pin():\n    assert server.serve("abc123") == "abc123"\n\n\ntest_pin()\n' >"$r/test_pin.py"
cat >"$r/.fluxpoint-guards.json" <<JSON
{"guards": [{
  "id": "key-pin",
  "protects": "serving an unpinned key produces addresses no client can rebuild",
  "guard": {"file": "server.py", "contains": "if vkh != PINNED_VKH:"},
  "proof": {"file": "test_pin.py", "test": "test_pin"},
  "mutation": {"find": "PINNED_VKH = \"abc123\"", "replace": "pass"},
  "expect": "unpinned key accepted",
  "run": "$RUN_PY test_pin.py"
}]}
JSON
out="$("$FPL_PY" "$GG" --root "$r" --verify 2>&1)"; rc=$?
check "a DECORATIVE proof reddened by a broken module is refused" 1 "$rc"
case "$out" in
  *"not for the declared reason"*) ok "and the refusal names the declared reason as absent" "named" ;;
  *) bad "and the refusal names the declared reason as absent" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-60)" ;;
esac
grep -q 'PINNED_VKH = "abc123"' "$r/server.py" \
  && ok "and the guard is restored after that refusal" "intact" \
  || bad "and the guard is restored after that refusal" "LEFT DISABLED"

# 6b. KILLED. A proof that dies from a signal evaluated nothing. 143 is what a
# shell reports for a SIGTERM-killed child, which is the shape a CI cancellation
# or a job timeout actually delivers -- spelled as an exit so the case is
# deterministic on every platform instead of racing a real kill. The proof
# passes intact and dies only inside the mutated window.
r="$WORK/killedproof"; rm -rf "$r"; mkdir -p "$r"
printf 'def fee_ok(amount):\n    if amount > 100:\n        return False\n    return True\n' >"$r/server.py"
printf 'import sys\n\n\ndef test_fee():\n    if "if False" in open("server.py").read():\n        sys.exit(143)\n\n\ntest_fee()\n' >"$r/test_fee.py"
cat >"$r/.fluxpoint-guards.json" <<JSON
{"guards": [{
  "id": "fee-bound",
  "protects": "a fee above the cap must never be accepted",
  "guard": {"file": "server.py", "contains": "if amount > 100"},
  "proof": {"file": "test_fee.py", "test": "test_fee"},
  "mutation": {"find": "if amount > 100", "replace": "if False"},
  "expect": "guard gone",
  "run": "$RUN_PY test_fee.py"
}]}
JSON
out="$("$FPL_PY" "$GG" --root "$r" --verify 2>&1)"; rc=$?
check "a KILLED proof is refused, not read as 'the guard is real'" 1 "$rc"
case "$out" in
  *"the guard is real"*) bad "and the verdict does not claim the guard is real" "CLAIMED REAL" ;;
  *KILLED*)              ok  "and the verdict does not claim the guard is real" "named as killed" ;;
  *)                     bad "and the verdict does not claim the guard is real" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-60)" ;;
esac

# ========== 7. the same-file refusal reads the RESOLVED path ==============
# A proof living in the guard's own file is refused: one deletion takes both.
# Spelling the same file two ways walked straight past a string comparison.
mkrepo alias
cat >"$WORK/alias/.fluxpoint-guards.json" <<JSON
{"guards": [{
  "id": "fee-bound",
  "protects": "a fee above the cap must never be accepted",
  "guard": {"file": "server.py", "contains": "if amount > 100"},
  "proof": {"file": "./server.py", "test": "fee_ok"},
  "mutation": {"find": "if amount > 100", "replace": "if False"},
  "expect": "guard gone",
  "run": "$RUN_PY test_fee.py"
}]}
JSON
out="$("$FPL_PY" "$GG" --root "$WORK/alias" --check 2>&1)"; rc=$?
check "a proof aliased into the guard file is still refused" 1 "$rc"
case "$out" in
  *"same file as the guard"*) ok "and that refusal says why" "named" ;;
  *) bad "and that refusal says why" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-60)" ;;
esac

# ============ containment: the manifest cannot reach outside the repo =====
# verify_one WRITES the mutation to the guard path, and os.path.join hands an
# absolute or ../-escaping entry the whole filesystem. A manifest arrives by
# PR and is reviewed as data — a "file" field is not read as "this path gets
# overwritten" — and a kill mid-verify leaves the mutated file wherever it
# landed, with the restore sentinel written inside the repo where nobody is
# looking for a casualty outside it.
mkrepo escape
"$FPL_PY" - "$WORK/escape/.fluxpoint-guards.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["guards"][0]["guard"]["file"] = "../outside.py"
json.dump(d, open(sys.argv[1], "w"))
PY
printf 'x = 1\n' >"$WORK/outside.py"
out="$("$FPL_PY" "$GG" --root "$WORK/escape" --check 2>&1)"; rc=$?
check "a ../-escaping guard path is refused" 2 "$rc"
case "$out" in *"outside the repo"*) ok "and the refusal says why" "named" ;;
  *) bad "and the refusal says why" "${out:0:60}" ;; esac

mkrepo escape2
"$FPL_PY" - "$WORK/escape2/.fluxpoint-guards.json" "$WORK/outside.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["guards"][0]["proof"]["file"] = sys.argv[2]
json.dump(d, open(sys.argv[1], "w"))
PY
"$FPL_PY" "$GG" --root "$WORK/escape2" --check >/dev/null 2>&1
check "an absolute proof path is refused too" 2 "$?"

# ============ the SCAFFOLDED harness runs --check once armed ==============
# guard-guard shipped in v1.27.0 wired to nothing: the README described it,
# its suite was green, and no onboarded repo would ever invoke it — a gate
# nothing runs, reading as coverage. The template block is gated on the
# manifest existing, because guard-guard is deliberately loud without one.
mkrepo wired
( cd "$WORK/wired" && git init -q -b main && mkdir -p scripts \
  && cp "$PLUGIN/templates/harness.sh" scripts/harness.sh \
  && git add -A && git -c user.email=t@t -c user.name=t commit -qm base )
out="$(cd "$WORK/wired" && env -u CLAUDE_PLUGIN_ROOT FPL_PLUGIN_ROOT="$PLUGIN" \
  bash scripts/harness.sh --full 2>&1)"; rc=$?
check "scaffolded harness: an intact guard is green" 0 "$rc"
"$FPL_PY" - "$WORK/wired/server.py" <<'PY'
import sys
open(sys.argv[1], "w").write("def fee_ok(amount):\n    return True\n")
PY
out="$(cd "$WORK/wired" && env -u CLAUDE_PLUGIN_ROOT FPL_PLUGIN_ROOT="$PLUGIN" \
  bash scripts/harness.sh --full 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && case "$out" in *"no longer contains the guard"*) true ;; *) false ;; esac; then
  ok "scaffolded harness: a deleted guard is RED end to end" "check bites"
else
  bad "scaffolded harness: a deleted guard is RED end to end" "rc=$rc ${out//$'\n'/ }"
fi
rm -rf "$WORK/wired/.fluxpoint-guards.json"
( cd "$WORK/wired" && env -u CLAUDE_PLUGIN_ROOT FPL_PLUGIN_ROOT="$PLUGIN" \
  bash scripts/harness.sh --full >/dev/null 2>&1 )
check "scaffolded harness: no manifest stays dormant, not loud" 0 "$?"

"$FPL_PY" "$PLUGIN/tests/guard-recovery-test.py"
check "restart recovers abrupt native termination and preserves unsafe state" 0 "$?"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
