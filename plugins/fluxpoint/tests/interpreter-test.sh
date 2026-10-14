#!/usr/bin/env bash
# Interpreter resolution, against the shape that actually breaks it.
#
# The property under test: a name on PATH is not evidence of an interpreter.
# Windows ships a `python3` App Execution Alias, enabled by default, on
# machines with no python3 installed at all. It satisfies `command -v`, and
# then prints "Python was not found; run without arguments to install from
# the Microsoft Store" and exits 49 for EVERY argument shape. A resolver that
# picks by name therefore selects a program that cannot run anything, and
# under `set -euo pipefail` the caller dies at its first use with only the
# Store's own string as the diagnostic.
#
# Two halves, because the defect had two lives. The behavioural half runs the
# real scripts/py.sh with a mimic alias first on PATH. The structural half
# asserts no copy of the resolver has regressed to the name test — there are
# twenty of them, and one reverted copy is a Windows repo that cannot run its
# own harness.
set -uo pipefail

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$PLUGIN/../.." && pwd)"
pass=0; fail=0

ok()  { printf 'PASS  %-54s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-54s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

BIN="$(mktemp -d)"
trap 'rm -rf "$BIN"' EXIT

# The alias, reproduced: succeeds at `command -v`, fails at everything else.
cat >"$BIN/python3" <<'ALIAS'
#!/usr/bin/env bash
echo "Python was not found; run without arguments to install from the Microsoft Store, or disable this shortcut from Settings > Apps > Advanced app settings > App execution aliases."
exit 49
ALIAS
chmod +x "$BIN/python3"

# A real interpreter under the other name. Resolved before PATH is shadowed so
# the stub cannot recurse into itself, and resolved by probing the plain names
# only: an inherited FPL_PY may be a native Windows path, and a `exec C:\...`
# stub loses its backslashes to the shell. Building the fixture out of the bug
# is how this test spent its first run.
REAL=""
for _c in python3 python; do
  if command -v "$_c" >/dev/null 2>&1 && "$_c" -c "import sys" >/dev/null 2>&1; then
    REAL="$(command -v "$_c")"
    break
  fi
done
if [ -z "$REAL" ]; then
  echo "interpreter-test: no working interpreter to build the fixture with" >&2
  exit 127
fi
printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$REAL" >"$BIN/python"
chmod +x "$BIN/python"

# --- behavioural: the shipped resolver, with the alias winning the name test ---
#
# lib.sh is the sourced copy every hook picks up, so this exercises the real
# code path rather than a transcription of it.

got="$(PATH="$BIN:$PATH" bash -c '
  unset FPL_PY
  . "'"$PLUGIN"'/scripts/lib.sh" >/dev/null 2>&1 || exit $?
  "$FPL_PY" -c "print(\"INTERPRETER-OK\")" 2>&1' 2>&1)"
case "$got" in
  *INTERPRETER-OK*) ok "the alias is skipped for a working interpreter" "ran" ;;
  *"Python was not found"*)
    bad "the alias is skipped for a working interpreter" "resolved the Store alias" ;;
  *) bad "the alias is skipped for a working interpreter" "${got:0:60}" ;;
esac

# The fixture has to be able to fail, or it proves nothing: confirm the alias
# really is what a name-only resolver would have picked.
PATH="$BIN:$PATH" command -v python3 >/dev/null 2>&1
check "the alias satisfies command -v" 0 $?
PATH="$BIN:$PATH" python3 -c "import sys" >/dev/null 2>&1
check "and cannot execute anything (the Store alias exit)" 49 $?

# --- and refuses honestly when nothing works ---

rm -f "$BIN/python"
PATH="$BIN" "$BASH" -c 'unset FPL_PY; . "'"$PLUGIN"'/scripts/lib.sh"' >/dev/null 2>&1
check "no working interpreter exits 127, not 49" 127 $?

# --- structural: no copy of the resolver has gone back to the name test ---

# Both patterns carry a one-character regex class so this file does not match
# its own greps. A structural check that scans a tree containing itself counts
# itself: the first form of this test reported one stale copy on a fully-fixed
# tree and could never go green, because the string it hunts for lived in the
# line doing the hunting. `pytho[n]3` matches `python3` in every other file
# and never the pattern here, wherever this file is moved or copied.
stale=$(cd "$REPO" && git grep -l 'then FPL_PY=pytho[n]3' -- '*.sh' | wc -l | tr -d ' ')
check "no copy resolves the interpreter by name alone" 0 "$stale"

# Runtime launchers now share a cached resolver. Counting textual copies
# penalizes consolidation; gate-regression-test.py checks cold, warm, alias
# fallback, WindowsApps rejection and PATH invalidation with real launches.

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
