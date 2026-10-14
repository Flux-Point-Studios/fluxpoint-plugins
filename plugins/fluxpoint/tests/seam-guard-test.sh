#!/usr/bin/env bash
# Seam ratchet: a mock of code you own is an unverified contract.
#
# The property is narrow: the count of module mocks may fall and never rise,
# first-party and third-party tracked apart because they fail differently.
# Process-edge doubles count too: their payloads need checked captures, as
# exercised by recordings-test.py.
#
# Executed end to end: every case builds a real repo, runs the real script, and
# reads the real exit code and baseline file.
set -uo pipefail

if [ -z "${FPL_PY:-}" ]; then
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
GUARD="$PLUGIN/scripts/seam-guard.py"
ROOT="$(mktemp -d)"
pass=0; fail=0

ok()  { printf 'PASS  %-56s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-56s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

newrepo() {
  rm -rf "$ROOT/r"; mkdir -p "$ROOT/r/src/__tests__"; cd "$ROOT/r" || exit 1
  git init -q -b main
}
commit() { git add -A; git -c user.email=t@t -c user.name=t commit -qm x >/dev/null 2>&1; }
guard() { "$FPL_PY" "$GUARD" --root "$ROOT/r" "$@"; }

# --- dormant ---
newrepo; commit
check "a repo with no tests is dormant" 0 "$(guard --check >/dev/null 2>&1; echo $?)"

# --- counting ---
newrepo
cat >src/__tests__/a.test.ts <<'EOF'
vi.mock('@/services/beacon/lucidTx', () => ({}));
vi.mock('../provider', () => ({}));
vi.mock('@lucid-evolution/lucid', () => ({}));
vi.stubGlobal('fetch', () => {});
EOF
commit
out="$(guard --scan)"
case "$out" in *"first_party = 2"*) ok "counts aliased and relative as first-party" "2" ;;
  *) bad "counts aliased and relative as first-party" "${out//$'\n'/ }" ;; esac
case "$out" in *"third_party = 2"*) ok "counts a bare specifier and fetch stub as third-party" "2" ;;
  *) bad "counts a bare specifier and fetch stub as third-party" "${out//$'\n'/ }" ;; esac
# A process-boundary stub must also count on its own.
newrepo
printf "vi.stubGlobal('fetch', () => {});\n" >src/__tests__/a.test.ts
commit
out="$(guard --scan)"
if case "$out" in *"first_party = 0"*) true ;; *) false ;; esac \
   && case "$out" in *"third_party = 1"*) true ;; *) false ;; esac; then
  ok "counts a process-boundary stub" "0/1"
else
  bad "counts a process-boundary stub" "${out//$'\n'/ }"
fi

# --- the ratchet ---
check "no baseline is not a failure" 0 "$(guard --check >/dev/null 2>&1; echo $?)"
guard --baseline >/dev/null 2>&1
check "arming writes a baseline" 0 "$([ -f "$ROOT/r/.fluxpoint-proof-baseline.json" ]; echo $?)"
check "at baseline is green" 0 "$(guard --check >/dev/null 2>&1; echo $?)"

# A NEW first-party mock is the thing this exists to catch.
printf "vi.mock('@/services/beacon/fallbackFill', () => ({}));\n" >>src/__tests__/a.test.ts
commit
check "a new first-party mock FAILS the ratchet" 1 "$(guard --check >/dev/null 2>&1; echo $?)"
err="$(guard --check 2>&1 >/dev/null)"
case "$err" in *fallbackFill*) ok "the failure names the offending mock" "named" ;;
  *) bad "the failure names the offending mock" "${err:0:60}" ;; esac

# Removing one is always allowed, and is the direction we want.
newrepo
printf "vi.mock('@/a', () => ({}));\nvi.mock('@/b', () => ({}));\n" >src/__tests__/a.test.ts
commit; guard --baseline >/dev/null 2>&1
printf "vi.mock('@/a', () => ({}));\n" >src/__tests__/a.test.ts
commit
check "removing a mock is green" 0 "$(guard --check >/dev/null 2>&1; echo $?)"

# --- importActual: a partial mock keeps the real module in play ---
newrepo
cat >src/__tests__/a.test.ts <<'EOF'
vi.mock('@/services/x', async () => ({ ...(await vi.importActual('@/services/x')), y: 1 }));
EOF
commit
out="$(guard --scan)"
case "$out" in *"first_party = 0"*) ok "a partial mock via importActual is not a wall" "0" ;;
  *) bad "a partial mock via importActual is not a wall" "${out//$'\n'/ }" ;; esac

# --- it must not eat an unrelated section of a shared baseline ---
newrepo
printf "vi.mock('@/a', () => ({}));\n" >src/__tests__/a.test.ts
printf '{"counts":{"todo":3}}\n' >.fluxpoint-proof-baseline.json
commit
guard --baseline >/dev/null 2>&1
if grep -q '"todo": 3' .fluxpoint-proof-baseline.json && grep -q '"seams"' .fluxpoint-proof-baseline.json; then
  ok "arming preserves another ratchet's section" "both present"
else
  bad "arming preserves another ratchet's section" "$(tr -d '\n ' <.fluxpoint-proof-baseline.json)"
fi

# --- jest is spelled differently and counts the same ---
newrepo
printf "jest.mock('../thing');\n" >src/__tests__/a.test.ts
commit
out="$(guard --scan)"
case "$out" in *"first_party = 1"*) ok "jest.mock counts too" "1" ;;
  *) bad "jest.mock counts too" "${out//$'\n'/ }" ;; esac

# --- doMock is in the regex; without a fixture that branch can die silently ---
newrepo
printf "vi.doMock('../thing');\n" >src/__tests__/a.test.ts
commit
out="$(guard --scan)"
case "$out" in *"first_party = 1"*) ok "vi.doMock counts too" "1" ;;
  *) bad "vi.doMock counts too" "${out//$'\n'/ }" ;; esac

# --- one partial mock must not exempt the wall-offs that share its file ---
# The whole-file skip was a bypass: a single importActual anywhere — even in
# a comment — exempted every other module's wall-off mock, forever.
newrepo
cat >src/__tests__/a.test.ts <<'EOF'
vi.mock('@/services/x', async () => ({ ...(await vi.importActual('@/services/x')), y: 1 }));
vi.mock('@/services/wallet', () => ({}));
vi.mock('cbor-x', () => ({}));
EOF
commit
out="$(guard --scan)"
if case "$out" in *"first_party = 1"*) true ;; *) false ;; esac \
   && case "$out" in *"third_party = 1"*) true ;; *) false ;; esac; then
  ok "a partial mock exempts itself, not its neighbours" "walls still counted"
else
  bad "a partial mock exempts itself, not its neighbours" "${out//$'\n'/ }"
fi

# --- vitest's importOriginal helper is the same partial mock, newer spelling ---
newrepo
cat >src/__tests__/a.test.ts <<'EOF'
vi.mock('@/services/x', async (importOriginal) => ({ ...(await importOriginal()), y: vi.fn() }));
EOF
commit
out="$(guard --scan)"
case "$out" in *"first_party = 0"*) ok "importOriginal is a partial mock, not a wall" "0" ;;
  *) bad "importOriginal is a partial mock, not a wall" "${out//$'\n'/ }" ;; esac

# --- prose about a mock is not a mock ---
newrepo
printf "// we deliberately do not vi.mock('@/wallet') here\nit('works', () => {});\n" \
  >src/__tests__/a.test.ts
commit
out="$(guard --scan)"
case "$out" in *"first_party = 0"*) ok "a comment mentioning vi.mock is not counted" "0" ;;
  *) bad "a comment mentioning vi.mock is not counted" "${out//$'\n'/ }" ;; esac

# --- the categories ratchet independently: a fall cannot pay for a rise ---
newrepo
printf "vi.mock('@/a', () => ({}));\nvi.mock('lib-x', () => ({}));\n" >src/__tests__/a.test.ts
commit; guard --baseline >/dev/null 2>&1
printf "vi.mock('lib-x', () => ({}));\nvi.mock('lib-y', () => ({}));\n" >src/__tests__/a.test.ts
commit
check "a third-party rise hidden by a first-party fall still FAILS" 1 \
  "$(guard --check >/dev/null 2>&1; echo $?)"

# --- an unreadable shared baseline must be loud, never a silent disarm ---
newrepo
printf "vi.mock('@/a', () => ({}));\n" >src/__tests__/a.test.ts
printf 'not json' >.fluxpoint-proof-baseline.json
commit
check "corrupt baseline FAILS --check instead of disarming" 1 \
  "$(guard --check >/dev/null 2>&1; echo $?)"
check "corrupt baseline FAILS --baseline instead of eating it" 1 \
  "$(guard --baseline >/dev/null 2>&1; echo $?)"
grep -q 'not json' .fluxpoint-proof-baseline.json \
  && ok "and the unreadable file is left untouched" "preserved" \
  || bad "and the unreadable file is left untouched" "overwritten"

# --- an uncommitted test file counts, or green flips red at the commit ---
newrepo
printf 'x = 1\n' >src/app.py; commit
printf "vi.mock('@/a', () => ({}));\n" >src/__tests__/new.test.ts
out="$(guard --scan)"
case "$out" in *"first_party = 1"*) ok "an untracked test file is scanned" "counted" ;;
  *) bad "an untracked test file is scanned" "${out//$'\n'/ }" ;; esac

# --- Python test repos arm the same ratchet, even before adding doubles ---
newrepo
printf 'def test_x(): pass\n' >src/test_app.py; commit
out="$(guard --baseline 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && case "$out" in *"baseline armed"*) true ;; *) false ;; esac \
   && [ -f .fluxpoint-proof-baseline.json ]; then
  ok "--baseline in a Python repo arms and says so" "armed"
else
  bad "--baseline in a Python repo arms and says so" "rc=$rc ${out//$'\n'/ }"
fi

# --- the SCAFFOLDED harness enforces it, not just this repo's test suite ---
# init.md tells every repo with tests to arm this ratchet; the DoD gate runs
# the repo's scripts/harness.sh, which /fluxpoint:init copies from
# templates/harness.sh. If that template never invokes seam-guard --check,
# the ratchet is armed everywhere and enforced nowhere — a witness nothing
# consults, which is the failure class this plugin exists to refuse.
newrepo
mkdir -p scripts && cp "$PLUGIN/templates/harness.sh" scripts/harness.sh
printf "vi.mock('@/a', () => ({}));\n" >src/__tests__/a.test.ts
commit; guard --baseline >/dev/null 2>&1
printf "vi.mock('@/b', () => ({}));\n" >>src/__tests__/a.test.ts
commit
out="$(cd "$ROOT/r" && env -u CLAUDE_PLUGIN_ROOT FPL_PLUGIN_ROOT="$PLUGIN" \
  bash scripts/harness.sh --full 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && case "$out" in *"first_party mocks rose"*) true ;; *) false ;; esac; then
  ok "the scaffolded harness enforces the ratchet" "rise is RED end to end"
else
  bad "the scaffolded harness enforces the ratchet" "rc=$rc ${out//$'\n'/ }"
fi
# ...and at baseline the same scaffolded run is green, so the gate added
# above cannot be the thing that reddens an innocent repo.
guard --baseline >/dev/null 2>&1
commit
out="$(cd "$ROOT/r" && env -u CLAUDE_PLUGIN_ROOT FPL_PLUGIN_ROOT="$PLUGIN" \
  bash scripts/harness.sh --full 2>&1)"; rc=$?
check "the scaffolded harness at baseline stays green" 0 "$rc"

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
