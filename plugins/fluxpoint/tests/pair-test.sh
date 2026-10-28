#!/usr/bin/env bash
# Relation gate: a pair can be broken while both halves are green.
#
# The case this exists for, from a real delivery: an on-chain predicate is
# tightened, its off-chain builder is not, 1830 tests keep passing, and the
# change rejects every honest transaction. Each artifact is individually
# correct. Nothing in the suite evaluates them together.
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
PG=""$FPL_PY" $PLUGIN/scripts/pair-guard.py --root ."
ROOT="$(mktemp -d)"
pass=0; fail=0

ok()  { printf 'PASS  %-56s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-56s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

mkrepo() {
  rm -rf "$ROOT/r"; mkdir -p "$ROOT/r/contracts/policy" "$ROOT/r/api" "$ROOT/r/offchain"
  cd "$ROOT/r" || exit 1
  git init -q -b main
  printf 'fn window() { 300 }\n'   >contracts/policy/policy_window.ak
  printf 'WINDOW = 300\n'          >api/policies.py
  printf 'window = 300\n'          >offchain/tx_build.py
  printf 'x\n'                     >unrelated.txt
  git add -A; git -c user.email=t@t -c user.name=t commit -qm base
}
manifest() { cat >.fluxpoint-pairs.json; }

# ================= 1. dormant with no manifest ============================
mkrepo
out="$($PG --check 2>&1)"; rc=$?
check "no manifest: exits 0" 0 "$rc"
case "$out" in *"no declared relations"*) ok "no manifest: says why" "dormant" ;;
  *) bad "no manifest: says why" "$out" ;; esac

# ================= 2. the live case: one side moves alone =================
mkrepo
manifest <<'EOF'
[{"id":"policy-window",
  "source":"contracts/**/policy*.ak",
  "mirror":["api/policies.py","offchain/tx_build.py"],
  "why":"the on-chain window and the builder's window must agree"}]
EOF
git add -A; git -c user.email=t@t -c user.name=t commit -qm manifest
printf 'fn window() { 120 }\n' >contracts/policy/policy_window.ak
err="$($PG --check 2>&1 >/dev/null)"; rc=$?
check "on-chain tightened alone: fails" 1 "$rc"
case "$err" in *policy-window*) ok "names the pair" "reported" ;;
  *) bad "names the pair" "${err:0:60}" ;; esac
case "$err" in *"policy_window.ak"*) ok "names the file that moved" "reported" ;;
  *) bad "names the file that moved" "${err:0:60}" ;; esac
case "$err" in *"must agree"*) ok "carries the declared reason" "reported" ;;
  *) bad "carries the declared reason" "${err:0:60}" ;; esac

# Moving both sides together is the whole point of declaring the pair.
printf 'WINDOW = 120\n' >api/policies.py
$PG --check >/dev/null 2>&1
check "both sides moved together: passes" 0 "$?"

# ================= 3. an unrelated change is not the gate's business =====
mkrepo
manifest <<'EOF'
[{"id":"policy-window","source":"contracts/**/policy*.ak","mirror":["api/policies.py"]}]
EOF
git add -A; git -c user.email=t@t -c user.name=t commit -qm manifest
printf 'y\n' >>unrelated.txt
$PG --check >/dev/null 2>&1
check "an unrelated file does not trip the gate" 0 "$?"

# The default direction is the stated defect class, not both ways: an
# off-chain refactor that touches no predicate is not a divergence, and a
# gate that cries about it gets deleted rather than obeyed.
printf 'WINDOW = 999\n' >api/policies.py
$PG --check >/dev/null 2>&1
check "mirror alone does not trip an asymmetric pair" 0 "$?"

mkrepo
manifest <<'EOF'
[{"id":"two-way","source":"contracts/**/policy*.ak",
  "mirror":["api/policies.py"],"symmetric":true}]
EOF
git add -A; git -c user.email=t@t -c user.name=t commit -qm manifest
printf 'WINDOW = 999\n' >api/policies.py
$PG --check >/dev/null 2>&1
check "mirror alone trips a symmetric pair" 1 "$?"

# ================= 4. globs do not over-reach ============================
# `*` must not span directories, or a pair silently covers more than the
# manifest claims and the co-change rule fires on unrelated files.
mkrepo
mkdir -p contracts/policy/nested
printf 'x\n' >contracts/policy/nested/policy_deep.ak
git add -A; git -c user.email=t@t -c user.name=t commit -qm nested
manifest <<'EOF'
[{"id":"shallow","source":"contracts/policy/*.ak","mirror":["api/policies.py"]}]
EOF
git add -A; git -c user.email=t@t -c user.name=t commit -qm manifest
printf 'y\n' >contracts/policy/nested/policy_deep.ak
$PG --check >/dev/null 2>&1
check "a single * does not span directories" 0 "$?"
printf 'fn w() { 1 }\n' >contracts/policy/policy_window.ak
$PG --check >/dev/null 2>&1
check "the same pattern still matches at its own level" 1 "$?"

# ================= 5. parity: the relation as an exit code ===============
# Co-change proves somebody touched both files. Only parity proves they
# agree, which is why a pair without one is reported as half-checked.
mkrepo
printf '#!/bin/sh\ngrep -q "$WANT" api/policies.py && grep -q "$WANT" contracts/policy/policy_window.ak\n' \
  >check_parity.sh; chmod +x check_parity.sh
# The parity is a POSIX command line on every platform. On Windows shell=True
# would hand it to cmd.exe, which has no `NAME=value cmd` prefix.
manifest <<'EOF'
[{"id":"policy-window","source":"contracts/**/policy*.ak",
  "mirror":["api/policies.py"],"parity":"WANT=300 sh check_parity.sh"}]
EOF
git add -A; git -c user.email=t@t -c user.name=t commit -qm manifest
out="$($PG --check 2>&1)"; rc=$?
check "parity passes when the two sides agree" 0 "$rc"
case "$out" in *"parity ok"*) ok "a passing parity check is reported" "reported" ;;
  *) bad "a passing parity check is reported" "${out:0:60}" ;; esac

# Now break the relation while BOTH files are individually valid, and
# without touching either side in this diff — the case co-change cannot see.
printf 'WINDOW = 120\n' >api/policies.py
printf 'window = 120\n' >offchain/tx_build.py
printf 'fn window() { 120 }\n' >contracts/policy/policy_window.ak
git add -A; git -c user.email=t@t -c user.name=t commit -qm "moved together, still wrong"
err="$($PG --check 2>&1 >/dev/null)"; rc=$?
check "parity catches disagreement with a clean diff" 1 "$rc"
case "$err" in *"parity command failed"*) ok "parity failure names the command" "reported" ;;
  *) bad "parity failure names the command" "${err:0:60}" ;; esac

$PG --check --no-parity >/dev/null 2>&1
check "--no-parity skips the expensive tier" 0 "$?"

# ================= 6. a half-declared pair is a hard error ===============
# A manifest entry that claims coverage it cannot deliver is worse than no
# entry, because the report says the relation is checked.
mkrepo
manifest <<'EOF'
[{"id":"lonely","source":"contracts/**/*.ak"}]
EOF
err="$($PG --check 2>&1 >/dev/null)"; rc=$?
check "a pair with no mirror is rejected" 1 "$rc"
case "$err" in *"not a relation"*) ok "says why one side is not a pair" "reported" ;;
  *) bad "says why one side is not a pair" "${err:0:60}" ;; esac

manifest <<'EOF'
[{"id":"typo","source":"a","mirror":["b"],"parrity":"x"}]
EOF
err="$($PG --check 2>&1 >/dev/null)"; rc=$?
check "a misspelled field is rejected, not ignored" 1 "$rc"
case "$err" in *"unknown field"*) ok "the typo is named" "reported" ;;
  *) bad "the typo is named" "${err:0:60}" ;; esac

manifest <<'EOF'
{not json
EOF
$PG --check >/dev/null 2>&1
check "a corrupt manifest fails loudly" 1 "$?"

# ================= 7. --list surfaces what is NOT checked ================
mkrepo
manifest <<'EOF'
[{"id":"has-parity","source":"a/*.ak","mirror":["b.py"],"parity":"true"},
 {"id":"no-parity","source":"c/*.ak","mirror":["d.py"]}]
EOF
out="$($PG --list 2>&1)"
case "$out" in *"nothing evaluates the two together"*)
    ok "a co-change-only pair is called out as half-checked" "reported" ;;
  *) bad "a co-change-only pair is called out as half-checked" "${out:0:60}" ;; esac

# ================= 8. CI diffs a branch, not the working tree ============
mkrepo
manifest <<'EOF'
[{"id":"policy-window","source":"contracts/**/policy*.ak","mirror":["api/policies.py"]}]
EOF
git add -A; git -c user.email=t@t -c user.name=t commit -qm manifest
git checkout -q -b feature
printf 'fn window() { 42 }\n' >contracts/policy/policy_window.ak
git add -A; git -c user.email=t@t -c user.name=t commit -qm "committed, so the working tree is clean"
$PG --check >/dev/null 2>&1
check "a committed change is invisible to a working-tree diff" 0 "$?"
$PG --check --against main >/dev/null 2>&1
check "--against finds it across the branch" 1 "$?"

# ================= 9. untracked files exist under every base =============
# The Stop gate routes through --against, and the untracked listing lived
# only in the HEAD branch — so at Stop time a mirror sitting as a new
# untracked file false-REDded the co-change rule over work that was present
# and correct, and a source change existing only untracked was invisible.
# Both directions of the docstring's own contract, on exactly the path the
# Stop gate takes.
mkrepo
manifest <<'EOF'
[{"id":"policy-window","source":"contracts/**/policy*.ak","mirror":["api/policies.py"]}]
EOF
mkdir -p api; printf 'W = 1\n' >api/policies.py
git add -A; git -c user.email=t@t -c user.name=t commit -qm base
base="$(git rev-parse HEAD)"
printf 'fn window() { 42 }\n' >contracts/policy/policy_window.ak
git add contracts; git -c user.email=t@t -c user.name=t commit -qm "source committed"
# The mirror satisfied as a NEW UNTRACKED file: must be green, not a false red.
git rm -q api/policies.py && git -c user.email=t@t -c user.name=t commit -qm "mirror moves"
mkdir -p api                          # git rm pruned the emptied directory
printf 'W = 2\n' >api/policies.py     # untracked now
$PG --check --against "$base" >/dev/null 2>&1
check "--against: an untracked mirror satisfies the co-change" 0 "$?"
# A source change that exists ONLY untracked: must be seen, not fail open.
mkrepo
manifest <<'EOF'
[{"id":"policy-window","source":"contracts/**/policy*.ak","mirror":["api/policies.py"]}]
EOF
git add -A; git -c user.email=t@t -c user.name=t commit -qm base
base="$(git rev-parse HEAD)"
# A NEW file matching the source glob — mkrepo's policy_window.ak is tracked,
# and modifying a tracked file is visible to `git diff HEAD` on any code;
# only a genuinely untracked file exercises the listing this pins.
printf 'fn bounds() { 7 }\n' >contracts/policy/policy_bounds.ak    # untracked
$PG --check --against "$base" >/dev/null 2>&1
check "--against: an untracked source change is not invisible" 1 "$?"

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
