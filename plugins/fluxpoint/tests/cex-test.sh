#!/usr/bin/env bash
# Counterexample ledger: a prover's failing input outlives the run.
#
# Fixtures are shaped exactly like real `aiken check` JSON (v1.1.23): no
# `kind` discriminator — the published --show-json-schema advertises one and
# the binary never writes it — and the per-module array is `tests`, not the
# schema's `test`. A parser written from that schema breaks on day one, so
# these fixtures pin the real shape.
#
# The suite is weighted toward the two ways this feature fails badly: a pin
# that is accepted while proving nothing (gaming), and a red that is not a
# weakening (a guard people learn to ignore is worse than no guard).
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
CEX="$PLUGIN/scripts/cex.py"
INBOX="$PLUGIN/scripts/inbox.py"
ROOT="$(mktemp -d)"
R="$ROOT/r"
pass=0; fail=0

ok()  { printf 'PASS  %-58s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-58s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

cex() { "$FPL_PY" "$CEX" --root "$R" "$@"; }
rc_of() { cex "$@" >/dev/null 2>&1; echo $?; }
rows() { [ -f "$R/.fluxpoint-cex.jsonl" ] && grep -c . "$R/.fluxpoint-cex.jsonl" || echo 0; }
cexid() { # first cexId in the store
  "$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY'
import json, sys
print(json.loads(open(sys.argv[1]).readline())["cexId"])
PY
}

mkrepo() {
  rm -rf "$R"; mkdir -p "$R/lib" "$R/validators"; cd "$R" || exit 1
  git init -q -b main
  if [ "${1:-aiken}" = aiken ]; then
    printf 'name = "x/y"\n' >aiken.toml
  fi
  git add -A; git -c user.email=t@t -c user.name=t commit -qm base
}
commit() { git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm "${1:-x}"; }

# Real v1.1.23 output shape. `prop_shape` fails with a record counterexample,
# `add_one_3` is a failing unit test, `expected_to_fail` is a PASS that
# carries a counterexample because it is `fail`-annotated.
fixture() {
  cat <<'EOF'
{
  "seed": 42,
  "summary": { "total": 4, "passed": 2, "failed": 2, "kind": { "unit": 2, "property": 2 } },
  "modules": [
    { "name": "x/y/vault",
      "summary": { "total": 4, "passed": 2, "failed": 2, "kind": { "unit": 2, "property": 2 } },
      "tests": [
        { "title": "prop_shape", "status": "fail", "on_failure": "fail_immediately",
          "iterations": 3, "counterexample": "Circle { radius: 200 }" },
        { "title": "add_one_3", "status": "fail", "on_failure": "fail_immediately",
          "execution_units": { "mem": 200, "cpu": 16100 },
          "assertion": "× expected\n│ 2\n× to equal\n│ 1" },
        { "title": "prop_ok", "status": "pass", "on_failure": "fail_immediately",
          "iterations": 100, "counterexample": null },
        { "title": "expected_to_fail", "status": "pass", "on_failure": "succeed_eventually",
          "iterations": 1, "counterexample": "Square { side: 9 }" }
      ] }
  ]
}
EOF
}

# ================= 1. dormant, and the store is a TRACKED artifact ========
mkrepo
check "nothing recorded: --check is silent and green" 0 "$(rc_of --check)"

fixture | cex --ingest --exit 1 >/dev/null 2>&1
check "a failing run records its counterexamples" 2 "$(rows)"
[ -f "$R/.fluxpoint-cex.jsonl" ] && ok "the store is at the repo root, not under .claude" "root" \
  || bad "the store is at the repo root, not under .claude" "missing"
# The whole ratchet is worthless if the plugin's own installer gitignores it.
printf '.claude/fluxpoint/\n' >"$R/.gitignore"; commit ignore
git -C "$R" check-ignore -q .fluxpoint-cex.jsonl \
  && bad "the store is committable, not gitignored" "IGNORED — check would be per-machine" \
  || ok "the store is committable, not gitignored" "tracked"
[ -f "$R/.fluxpoint-cex/$(cexid).json" ] && ok "frozen evidence rides alongside it" "written" \
  || bad "frozen evidence rides alongside it" "missing"

# ================= 2. what must NOT be recorded ===========================
out="$(cex --list)"
case "$out" in *expected_to_fail*)
  bad "a fail-annotated PASS is not a counterexample" "recorded a green test" ;;
  *) ok "a fail-annotated PASS is not a counterexample" "skipped" ;; esac
case "$out" in *prop_ok*) bad "a passing property is not recorded" "recorded" ;;
  *) ok "a passing property is not recorded" "skipped" ;; esac

# Re-ingesting the same run is a hit, not a second row.
fixture | cex --ingest --exit 1 >/dev/null 2>&1
check "re-finding the same counterexample appends nothing" 2 "$(rows)"

# ================= 3. the loud path, which is never red ===================
mkrepo
printf '    | FAIL [after 1 test] prop_shape\n' | cex --ingest --exit 1 >/dev/null 2>&1
check "terminal-box output (pre-1.1.6) is not silently dropped" 0 "$?"
check "and it files an inbox item" 1 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
check "but it never turns the build red" 0 \
  "$(printf 'boxes\n' | cex --ingest --exit 1 >/dev/null 2>&1; echo $?)"

mkrepo
check "an empty stdout with exit 0 is dormant" 0 \
  "$(printf '' | cex --ingest --exit 0 >/dev/null 2>&1; echo $?)"
check "and files nothing" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
# The ill-formed-fuzzer panic writes nothing to stdout and must not read as
# "no failures".
printf '' | cex --ingest --exit 101 >/dev/null 2>&1
check "an exit-101 panic with no stdout is reported" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"

# Format drift that keeps modules[].tests[] but changes what is inside it
# would otherwise stop ingestion forever while reporting a clean run.
mkrepo
"$FPL_PY" - <<'PY' | cex --ingest --exit 1 >/dev/null 2>&1
import json
print(json.dumps({"seed": 1, "summary": {"failed": 3},
                  "modules": [{"name": "m", "tests": [
                      {"title": "t", "status": "failed"}]}]}))
PY
check "summary.failed > 0 with nothing extracted is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"

# ================= 4. pinning: the anti-prose check =======================
setup_pinnable() {
  mkrepo
  fixture | cex --ingest --exit 1 >/dev/null 2>&1
  ID="$(cexid)"
}
write_test() { # $1 = path, $2 = body text
  printf 'test %s() {\n  %s\n}\n' "$ID" "$2" >"$R/$1"; commit t >/dev/null
}

setup_pinnable
cex --pin "$ID" >/dev/null 2>&1
check "step 1 leaves the row OPEN" 0 "$(rc_of --check)"
[ -f "$R/.claude/fluxpoint/cex/$ID.ak.draft" ] \
  && ok "step 1 writes a draft into scratch, not into source" "scratch" \
  || bad "step 1 writes a draft into scratch, not into source" "missing"

# The tool must not be able to satisfy its own containment check.
cp "$R/.claude/fluxpoint/cex/$ID.ak.draft" "$R/.claude/fluxpoint/cex/$ID.ak"
check "the tool's own draft is not a legal pin target" 1 \
  "$(rc_of --pin "$ID" --file ".claude/fluxpoint/cex/$ID.ak" --test-name "$ID")"

write_test lib/reg.ak 'pred(Circle { radius: 200 })'
check "a real test containing the value is accepted" 0 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"
check "and --check is green" 0 "$(rc_of --check)"

# Every way of appearing to pin without pinning.
setup_pinnable
write_test lib/reg.ak '// Circle { radius: 200 }
  pred(other)'
check "the value in a COMMENT is refused" 1 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"

setup_pinnable
write_test lib/reg.ak 'let _n = @"Circle { radius: 200 }"
  helper() == helper()'
check "the value in a STRING literal is refused" 1 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"

setup_pinnable
write_test lib/reg.ak 'let v = Circle { radius: 200 }
  True'
check "a body that binds the value then ignores it is refused" 1 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"

setup_pinnable
printf 'test %s() fail {\n  pred(Circle { radius: 200 })\n}\n' "$ID" >"$R/lib/reg.ak"
commit f >/dev/null
check "a fail-annotated regression is refused (it inverts the oracle)" 1 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"

setup_pinnable
write_test lib/reg.ak 'pred(Circle { radius: 199 })'
check "a value that was retyped rather than pasted is refused" 1 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"

# Containment is scoped to the named test's body: a scalar sitting anywhere
# else in the file must not satisfy it.
setup_pinnable
printf 'const limit = 200\n\ntest %s() {\n  True == True\n}\n' "$ID" >"$R/lib/reg.ak"
commit s >/dev/null
check "the value elsewhere in the FILE does not count" 1 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"

setup_pinnable
write_test validators/reg.ak 'pred(Circle { radius: 200 })'
git -C "$R" rm -q --cached validators/reg.ak
check "an untracked pin target is refused" 1 \
  "$(rc_of --pin "$ID" --file validators/reg.ak --test-name "$ID")"

setup_pinnable
mkdir -p "$R/scratchpad"
printf 'test %s() {\n  pred(Circle { radius: 200 })\n}\n' "$ID" >"$R/scratchpad/reg.ak"
commit o >/dev/null
check "a pin outside a compile root is refused" 1 \
  "$(rc_of --pin "$ID" --file scratchpad/reg.ak --test-name "$ID")"

setup_pinnable
write_test lib/reg.ak 'pred(Circle { radius: 200 })'
check "a test name not naming the cexId is refused" 1 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "some_other_test")"

# An opaque type renders as something that is not writable source; requiring
# the constructor name would make a legitimate pin impossible.
mkrepo
"$FPL_PY" - <<'PY' | cex --ingest --exit 1 >/dev/null 2>&1
import json
print(json.dumps({"seed": 1, "summary": {"failed": 1}, "modules": [
    {"name": "x/y/v", "tests": [
        {"title": "prop_value", "status": "fail", "iterations": 2,
         "counterexample": 'Dict([(#"2cd15ed0", True)])'}]}]}))
PY
ID="$(cexid)"
printf 'test %s() {\n  pred(dict.from_list([Pair(#"2cd15ed0", True)]))\n}\n' "$ID" \
  >"$R/lib/reg.ak"; commit d >/dev/null
check "an opaque value pinned via its real constructor is accepted" 0 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"

# A unit test has no generated input, so its assertion is the payload — and
# pinning it to the very test that is failing must not be free.
mkrepo
fixture | cex --ingest --exit 1 >/dev/null 2>&1
UID_="$("$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY'
import json, sys
for l in open(sys.argv[1]):
    r = json.loads(l)
    if r["kind"] == "unit": print(r["cexId"]); break
PY
)"
printf 'test %s() {\n  True == True\n}\n' "$UID_" >"$R/lib/u.ak"; commit u >/dev/null
check "a unit pin with none of the assertion values is refused" 1 \
  "$(rc_of --pin "$UID_" --file lib/u.ak --test-name "$UID_")"
printf 'test %s() {\n  add_one(2) == 1\n}\n' "$UID_" >"$R/lib/u.ak"; commit u2 >/dev/null
check "and accepted when it carries them" 0 \
  "$(rc_of --pin "$UID_" --file lib/u.ak --test-name "$UID_")"

# ================= 5. --check: red only on real weakening =================
setup_pinnable
write_test lib/reg.ak 'pred(Circle { radius: 200 })'
cex --pin "$ID" --file lib/reg.ak --test-name "$ID" >/dev/null 2>&1
rm "$R/lib/reg.ak"; commit rm >/dev/null
check "a deleted regression file is red" 1 "$(rc_of --check)"

setup_pinnable
write_test lib/reg.ak 'pred(Circle { radius: 200 })'
cex --pin "$ID" --file lib/reg.ak --test-name "$ID" >/dev/null 2>&1
printf 'test %s() {\n  True\n}\n' "$ID" >"$R/lib/reg.ak"; commit h >/dev/null
check "a regression hollowed out in place is red" 1 "$(rc_of --check)"

# Reformatting is not weakening: aiken fmt wraps and adds a trailing comma.
mkrepo
"$FPL_PY" - <<'PY' | cex --ingest --exit 1 >/dev/null 2>&1
import json
print(json.dumps({"seed": 1, "summary": {"failed": 1}, "modules": [
    {"name": "x/y/v", "tests": [
        {"title": "prop_cfg", "status": "fail", "iterations": 1,
         "counterexample": "Config { alpha: 12, beta: True }"}]}]}))
PY
ID="$(cexid)"
printf 'test %s() {\n  pred(\n    Config {\n      alpha: 12,\n      beta: True,\n    },\n  )\n}\n' "$ID" \
  >"$R/lib/reg.ak"; commit w >/dev/null
check "a reformatted multi-line value still pins" 0 \
  "$(rc_of --pin "$ID" --file lib/reg.ak --test-name "$ID")"
check "and stays green" 0 "$(rc_of --check)"

# A file move is not a weakening; re-pinning is the way out and needs no
# retirement.
git -C "$R" mv lib/reg.ak validators/reg.ak; commit mv >/dev/null
check "a moved regression is red until re-pinned" 1 "$(rc_of --check)"
check "re-pinning an already-pinned row is allowed" 0 \
  "$(rc_of --pin "$ID" --file validators/reg.ak --test-name "$ID")"
check "and it is green again" 0 "$(rc_of --check)"

# An open row never fails the build: finding a bug must not punish the finder.
mkrepo
fixture | cex --ingest --exit 1 >/dev/null 2>&1
check "unpinned counterexamples never fail --check" 0 "$(rc_of --check)"

# ================= 6. the release valve costs a reviewed diff =============
setup_pinnable
write_test lib/reg.ak 'pred(Circle { radius: 200 })'
cex --pin "$ID" --file lib/reg.ak --test-name "$ID" >/dev/null 2>&1
rm "$R/lib/reg.ak"; commit rm2 >/dev/null
check "retiring with a short reason is refused" 1 \
  "$(rc_of --retire "$ID" --reason "fixed")"
check "retiring with no Decisions row is refused" 1 \
  "$(rc_of --retire "$ID" --reason "the radius field is now a bounded newtype")"
cat >"$R/WORK.md" <<EOF
# w

## Decisions

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|
| 2026-08-08 01:00 | retire-cex | drop the pin | no | none | $ID no longer constructible: radius is a bounded newtype |
EOF
commit dec >/dev/null
check "retiring named in Decisions is allowed" 0 \
  "$(rc_of --retire "$ID" --reason "the radius field is now a bounded newtype")"
check "and --check goes green" 0 "$(rc_of --check)"

# ================= 7. a re-found pinned cex is an inert pin ===============
setup_pinnable
write_test lib/reg.ak 'pred(Circle { radius: 200 })'
cex --pin "$ID" --file lib/reg.ak --test-name "$ID" >/dev/null 2>&1
before="$("$FPL_PY" "$INBOX" --root "$R" --count)"
fixture | cex --ingest --exit 1 >/dev/null 2>&1
after="$("$FPL_PY" "$INBOX" --root "$R" --count)"
[ "$after" -gt "$before" ] \
  && ok "re-finding a PINNED counterexample raises an inert-pin item" "raised" \
  || bad "re-finding a PINNED counterexample raises an inert-pin item" "swallowed"

# ================= 8. the store fails hard, never quietly =================
printf 'not json\n' >>"$R/.fluxpoint-cex.jsonl"
err="$(cex --check 2>&1 >/dev/null)"
case "$err" in *"is not valid JSON"*) ok "a corrupted store fails hard on read" "hard error" ;;
  *) bad "a corrupted store fails hard on read" "${err:0:44}" ;; esac

# ================= 9. a second prover: dafny =============================
# The ledger, the containment rule, the refusal to mint from a green run and
# the INGEST-FAILED row are shared; only the parser is per prover. Fixtures
# are the literal shape `dafny verify --extract-counterexample` prints
# (Dafny 3: `name : type = value` state blocks; Dafny 4: the same model as
# `assume` statements; Dafny 4.9.1: ` Related counterexample:` with the
# literal on the left), each captured from a real run, never from
# documentation.
mkdafny() {
  mkrepo dafny
  mkdir -p "$R/src" "$R/test"
  cat >"$R/src/example.dfy" <<'EOF'
module M {
  method Fill(a: array?<int>, n: int, j: int, k: int)
    requires a != null && a.Length == n
    modifies a
  {
    var i := 0;
    while i < n
      invariant 0 <= i <= n
    {
      a[i] := i;
      i := i + 1;
    }
  }
}
EOF
  git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm dafny
}
dafny3() {
  cat <<'EOF'
src/example.dfy(8,16): Error: This loop invariant might not be maintained by the loop.
src/example.dfy(8,16): Related message: loop invariant violation

Dafny program verifier finished with 1 verified, 1 error
Counterexample for first failing assertion: 
src/example.dfy(2,2): initial state:
        a : _System.array?<int> = ()
        n : int = 1237
        j : int = 196
        k : int = 1236
src/example.dfy(6,14):
        a : _System.array?<int> = ()
        n : int = 1237
        i : int = 0
        j : int = 196
        k : int = 1236
EOF
}
dafny4() {
  cat <<'EOF'
src/example.dfy(8,16): Error: this loop invariant could not be proved to be maintained by the loop

Dafny program verifier finished with 1 verified, 1 error
Counterexample for first failing assertion:
src/example.dfy(2,2): initial state:
    assume n == 1237 && j == 196 && k == 1236;
EOF
}
dcex() { cex --ingest --tool dafny "$@"; }
dcexid() { "$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2'
import json, sys
for l in open(sys.argv[1]):
    r = json.loads(l)
    if r["tool"] == "dafny": print(r["cexId"]); break
PY2
}

mkdafny
dafny3 | dcex --exit 4 >/dev/null 2>&1
check "a dafny 3 model records one counterexample" 1 "$(rows)"
out="$(cex --list)"
case "$out" in *"[dafny] src/example.dfy:Fill"*) ok "the row names the tool and the enclosing method" "selector" ;;
  *) bad "the row names the tool and the enclosing method" "${out:0:80}" ;; esac
"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "the initial state is the recorded input, in order" "n, j, k" \
  || bad "the initial state is the recorded input, in order" "wrong payload"
import json, sys
r = json.loads(open(sys.argv[1]).readline())
# Every assignment of the initial state, in the order the prover printed it.
# The opaque array renders as `()` and yields no literal, so it never becomes
# something a pin must contain; it stays in the record because it is evidence.
assert r["input"] == "a == (), n == 1237, j == 196, k == 1236", r["input"]
assert r["signature"] == "counterexample" and r["inputForm"] == "dafny-model"
assert r["containment"] == r["input"]
PY2
dafny3 | dcex --exit 4 >/dev/null 2>&1
check "re-ingesting the same model appends nothing" 1 "$(rows)"

mkdafny
dafny4 | dcex --exit 4 >/dev/null 2>&1
check "the dafny 4 assume spelling records the same literals" 1 "$(rows)"
"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "and reads the conjuncts as assignments" "assume" \
  || bad "and reads the conjuncts as assignments" "wrong payload"
import json, sys
r = json.loads(open(sys.argv[1]).readline())
assert r["input"] == "n == 1237, j == 196, k == 1236", r["input"]
PY2

# Dafny 4.9.1, captured 2026-09-11 (`dafny verify --extract-counterexample`
# on the module below, exit 4): the heading is ` Related counterexample:`
# under the Error line, a WARNING about model consistency follows it, the
# state lines are indented, and the literal sits LEFT of the `==`. A parser
# written to the two earlier shapes read this run as a bare assertion and
# lost the model, which is why the real output is the fixture.
mkdafny49() {
  mkrepo dafny
  mkdir -p "$R/src" "$R/test"
  cat >"$R/src/vault.dfy" <<'EOF'
module Vault {
  method Withdraw(bal: int, amt: int) returns (out: int)
    requires amt > 0
    ensures out == bal - amt
    ensures out >= 0
  {
    out := bal - amt;
  }

  lemma {:axiom} Helper(x: int)
    ensures x * x >= 0

  method {:verify false} Skipped(n: int) returns (r: int)
    ensures r > n
  {
    r := n;
  }

  method UsesAssume(n: int) returns (r: int)
    ensures r > 0
  {
    assume n > 0;
    r := n;
  }
}
EOF
  git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm dafny49
}
dafny49() {
  cat <<'EOF'
src/vault.dfy(13,25): Warning: The {:verify false} attribute should only be used during development. Consider using a bodyless method together with the {:axiom} attribute instead
   |
13 |   method {:verify false} Skipped(n: int) returns (r: int)
   |                          ^^^^^^^

src/vault.dfy(22,4): Warning: assume statement has no {:axiom} annotation
   |
22 |     assume n > 0;
   |     ^^^^^^^^^^^^^

src/vault.dfy(6,2): Error: a postcondition could not be proved on this return path
 Related counterexample:
 WARNING: the following counterexample may be inconsistent or invalid. See dafny.org/dafny/DafnyRef/DafnyRef#sec-counterexamples
 src/vault.dfy(6,2): initial state:
 assume 0 == bal && 1 == amt;
 src/vault.dfy(7,20):
 assume 0 == bal && 1 == amt && -1 == out;
 
  |
6 |   {
  |   ^

src/vault.dfy(5,12): Related location: this is the postcondition that could not be proved
  |
5 |     ensures out >= 0
  |             ^^^^^^^^


Dafny program verifier finished with 1 verified, 1 error
EOF
}
mkdafny49
dafny49 | dcex --exit 4 >/dev/null 2>&1
check "a dafny 4.9 'Related counterexample' records the model" 1 "$(rows)"
"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "with the literal-left conjuncts read as name == value" "bal == 0, amt == 1" \
  || bad "with the literal-left conjuncts read as name == value" "wrong payload"
import json, sys
r = json.loads(open(sys.argv[1]).readline())
assert r["input"] == "bal == 0, amt == 1", r["input"]
assert r["signature"] == "counterexample" and r["modelState"] == "initial state", r
assert r["title"] == "Withdraw" and r["selector"] == "src/vault.dfy:Withdraw", r["selector"]
PY2
DID="$(dcexid)"
printf 'include "../src/vault.dfy"\n\nmethod {:test} %s() {\n  var r := Vault.Withdraw(0, 1); expect r == -1;\n}\n' "$DID" >"$R/test/reg.dfy"
commit p >/dev/null
check "and a test calling the method with those values pins" 0 \
  "$(rc_of --pin "$DID" --file test/reg.dfy --test-name "$DID")"


# ----- what a dafny pin has to survive
dpin() { # $1 = body
  printf 'include "../src/example.dfy"\n\nmethod {:test} %s() {\n  %s\n}\n' "$DID" "$1" >"$R/test/reg.dfy"
  commit p >/dev/null
}
mkdafny; dafny3 | dcex --exit 4 >/dev/null 2>&1; DID="$(dcexid)"
cex --pin "$DID" >/dev/null 2>&1
[ -f "$R/.claude/fluxpoint/cex/$DID.dfy.draft" ] \
  && ok "the draft carries the prover's own extension" "dfy" \
  || bad "the draft carries the prover's own extension" "missing"
dpin 'var a := new int[1237]; M.Fill(a, 1237, 196, 1236); expect a[0] == 0;'
check "a tracked .dfy test carrying the literals in order pins" 0 \
  "$(rc_of --pin "$DID" --file test/reg.dfy --test-name "$DID")"
check "and --check is green" 0 "$(rc_of --check)"

mkdafny; dafny3 | dcex --exit 4 >/dev/null 2>&1; DID="$(dcexid)"
dpin '// Fill(a, 1237, 196, 1236)
  expect 1 == 1;'
check "the values in a COMMENT are refused" 1 \
  "$(rc_of --pin "$DID" --file test/reg.dfy --test-name "$DID")"
dpin 'var a := new int[1237]; M.Fill(a, 1237, 196, 1236); assume false; expect a[0] == 9;'
check "an assume in the body is refused (anything verifies under it)" 1 \
  "$(rc_of --pin "$DID" --file test/reg.dfy --test-name "$DID")"
printf 'include "../src/example.dfy"\n\nmethod {:test} {:verify false} %s() {\n  var a := new int[1237]; M.Fill(a, 1237, 196, 1236); expect a[0] == 0;\n}\n' "$DID" >"$R/test/reg.dfy"
commit v >/dev/null
check "{:verify false} on the test is refused" 1 \
  "$(rc_of --pin "$DID" --file test/reg.dfy --test-name "$DID")"
dpin 'assert true;'
check "an assert-true body is refused" 1 \
  "$(rc_of --pin "$DID" --file test/reg.dfy --test-name "$DID")"
dpin 'var a := new int[1237]; M.Fill(a, 1237, 196, 1238); expect a[0] == 0;'
check "a retyped value is refused" 1 \
  "$(rc_of --pin "$DID" --file test/reg.dfy --test-name "$DID")"
printf 'test %s() {\n  pred(1237, 196, 1236)\n}\n' "$DID" >"$R/lib/reg.ak"; commit ak >/dev/null
check "a pin in another prover's language is refused" 1 \
  "$(rc_of --pin "$DID" --file lib/reg.ak --test-name "$DID")"

# ----- failures without a model, and runs that record nothing
mkdafny
printf 'src/example.dfy(3,12): Error: a postcondition could not be proved on this return path\nsrc/example.dfy(3,12): Related location: this is the postcondition that could not be proved\n\nDafny program verifier finished with 0 verified, 1 error\n' \
  | dcex --exit 4 >/dev/null 2>&1
check "an error with no model is recorded as an assertion" 1 "$(rows)"
DID="$(dcexid)"
check "and stays open rather than failing --check" 0 "$(rc_of --check)"
dpin 'expect 1 == 1;'
err="$(cex --pin "$DID" --file test/reg.dfy --test-name "$DID" 2>&1 >/dev/null)"
case "$err" in *"no literal"*) ok "it cannot be pinned mechanically, and says so" "refused" ;;
  *) bad "it cannot be pinned mechanically, and says so" "${err:0:60}" ;; esac

mkdafny
printf 'src/example.dfy(4,9): Error: unresolved identifier: Lenght\n1 resolution/type errors detected in example.dfy\n' \
  | dcex --exit 2 >/dev/null 2>&1
check "a resolution error is not a counterexample" 0 "$(rows)"
check "and files nothing" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
dafny3 | dcex --exit 0 >/dev/null 2>&1
check "exit 0 records nothing whatever was printed" 0 "$(rows)"

# ----- drift is loud and never red
mkdafny
printf 'src/example.dfy(8,16): Error: This loop invariant might not be maintained by the loop.\n\nDafny program verifier finished with 1 verified, 1 error\nCounterexample for first failing assertion:\nsrc/example.dfy(2,2): initial state:\n    <opaque model dump>\n' \
  | dcex --exit 4 >/dev/null 2>&1
check "a model heading with nothing readable under it is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
check "and it is not red" 0 "$(printf 'x\n' | dcex --exit 4 >/dev/null 2>&1; echo $?)"
mkdafny
printf 'Unhandled exception: System.Something\n' | dcex --exit 3 >/dev/null 2>&1
check "output with no Error line and no summary is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
out="$("$FPL_PY" "$INBOX" --root "$R" --list 2>/dev/null || cat "$R/.claude/fluxpoint/inbox.jsonl")"
case "$out" in *"dafny"*) ok "the inbox row names the prover that drifted" "named" ;;
  *) bad "the inbox row names the prover that drifted" "${out:0:60}" ;; esac
check "an unknown tool is refused" 1 "$(printf 'x\n' | cex --ingest --tool nosuch --exit 1 >/dev/null 2>&1; echo $?)"

# ----- the scaffolded harness captures dafny the way it captures aiken
mkdafny
mkdir -p "$R/scripts" "$R/bin"
cp "$PLUGIN/templates/harness.sh" "$R/scripts/harness.sh"; chmod +x "$R/scripts/harness.sh"
dafny3 >"$R/bin/dafny.out"
# The shim holds the harness to what Dafny 4.9.1 accepts: a project file or
# the .dfy files themselves, never a bare `.` (which the real CLI refused
# with exactly this line, and which a `**/*.dfy` arm glob had hidden).
cat >"$R/bin/dafny" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "4.9.1"; exit 0; fi
for a; do
  [ "$a" = "." ] && { echo "CLI: Error: command-line argument '.' is neither a recognized option nor a Dafny input file (.dfy, .doo, or .toml)."; exit 1; }
done
case " $*" in *.dfy*|*dfyconfig.toml*) ;; *) echo "shim: no Dafny input file among: $*" >&2; exit 2 ;; esac
cat "$(dirname "$0")/dafny.out"; exit 4
EOF
chmod +x "$R/bin/dafny"
git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm shim
( cd "$R" && env -u CLAUDE_PLUGIN_ROOT PATH="$R/bin:$PATH" FPL_PLUGIN_ROOT="$PLUGIN" \
    bash scripts/harness.sh --full >"$ROOT/harness.log" 2>&1 ); hrc=$?
[ "$hrc" -ne 0 ] && ok "the harness still fails on the prover's exit" "rc=$hrc" \
  || bad "the harness still fails on the prover's exit" "rc=0"
check "and the counterexample was recorded on the way" 1 "$(rows)"
[ "$(rows)" = 1 ] || { echo "      harness output:"; tail -15 "$ROOT/harness.log" | sed 's/^/      /'; }

# ================= 10. a third prover: kani ==============================
# Captured from a real run, 2026-09-11: Kani 0.67.0 (cargo plugin, CBMC
# 6.8.0) on the crate below, `cargo kani -Z concrete-playback
# --concrete-playback=print`, exit 1. Three harnesses: `fine` verifies,
# `div_is_total` fails on a division by zero with a two-value playback,
# `bump_never_overflows` fails on an overflow with a one-value playback.
# The only edit is the crate path. The green fixture is the same run's
# `fine` section with the closing summary in the form Kani prints it.
mkkani() {
  mkrepo kani
  mkdir -p "$R/src" "$R/tests"
  printf '[package]\nname = "vault"\nversion = "0.1.0"\nedition = "2021"\n' >"$R/Cargo.toml"
  cat >"$R/src/lib.rs" <<'EOF'
pub fn bump(x: u8) -> u8 { x + 1 }
pub fn safe_div(a: u32, b: u32) -> u32 { a / b }

#[cfg(kani)]
mod verification {
    use super::*;
    #[kani::proof]
    fn bump_never_overflows() {
        let x: u8 = kani::any();
        let y = bump(x);
        assert!(y > x);
    }
    #[kani::proof]
    #[kani::unwind(2)]
    fn div_is_total() {
        let a: u32 = kani::any();
        let b: u32 = kani::any();
        kani::assume(a < 10);
        let _ = safe_div(a, b);
    }
    #[kani::proof]
    fn fine() {
        let x: u8 = kani::any();
        kani::assume(x < 200);
        assert!(bump(x) > x);
    }
}
EOF
  git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm kani
}
kani_fail() {
  cat <<'EOF'
Kani Rust Verifier 0.67.0 (cargo plugin)
   Compiling vault v0.1.0 (/work/vault)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.12s
Checking harness verification::fine...
CBMC 6.8.0 (cbmc-6.8.0)
CBMC version 6.8.0 (cbmc-6.8.0) 64-bit x86_64 linux
Reading GOTO program from file /work/vault/target/kani/x86_64-unknown-linux-gnu/debug/deps/vault-fbaffb161a6495d2__RNvNtCs3Wmn5sT4Q3D_5vault12verification4fine.out
Generating GOTO Program
Adding CPROVER library (x86_64)
Removal of function pointers and virtual functions
Generic Property Instrumentation
Running with 16 object bits, 48 offset bits (user-specified)
Starting Bounded Model Checking
Runtime Symex: 0.00185952s
size of program expression: 132 steps
simple slicing removed 4 assignments
Generated 4 VCC(s), 4 remaining after simplification
Runtime Postprocess Equation: 1.7325e-05s
Passing problem to propositional reduction
converting SSA
Runtime Convert SSA: 0.000401488s
Running propositional reduction
Post-processing
Runtime Post-process: 8.278e-06s
Solving with CaDiCaL 2.0.0
221 variables, 134 clauses
SAT checker: instance is SATISFIABLE
Runtime Solver: 0.000125766s
Runtime decision procedure: 0.000591431s
Running propositional reduction
Solving with CaDiCaL 2.0.0
222 variables, 135 clauses
SAT checker: instance is UNSATISFIABLE
Runtime Solver: 8.0674e-05s
Runtime decision procedure: 0.000120791s

RESULTS:
Check 1: bump.assertion.1
	 - Status: SUCCESS
	 - Description: "attempt to add with overflow"
	 - Location: src/lib.rs:1:28 in function bump

Check 2: verification::fine.assertion.1
	 - Status: SUCCESS
	 - Description: "assertion failed: bump(x) > x"
	 - Location: src/lib.rs:25:9 in function verification::fine


SUMMARY:
 ** 0 of 2 failed

VERIFICATION:- SUCCESSFUL
Verification Time: 0.04398764s

WARNING: Kani could not produce a concrete playback for `verification::fine` because there were no failing panic checks or satisfiable cover statements.
Checking harness verification::div_is_total...
CBMC 6.8.0 (cbmc-6.8.0)
CBMC version 6.8.0 (cbmc-6.8.0) 64-bit x86_64 linux
Reading GOTO program from file /work/vault/target/kani/x86_64-unknown-linux-gnu/debug/deps/vault-fbaffb161a6495d2__RNvNtCs3Wmn5sT4Q3D_5vault12verification12div_is_total.out
Generating GOTO Program
Adding CPROVER library (x86_64)
Removal of function pointers and virtual functions
Generic Property Instrumentation
Running with 16 object bits, 48 offset bits (user-specified)
Starting Bounded Model Checking
Runtime Symex: 0.00119103s
size of program expression: 81 steps
simple slicing removed 8 assignments
Generated 4 VCC(s), 4 remaining after simplification
Runtime Postprocess Equation: 1.423e-05s
Passing problem to propositional reduction
converting SSA
Runtime Convert SSA: 0.000407966s
Running propositional reduction
Post-processing
Runtime Post-process: 6.863e-06s
Solving with CaDiCaL 2.0.0
540 variables, 247 clauses
SAT checker: instance is SATISFIABLE
Runtime Solver: 7.7401e-05s
Runtime decision procedure: 0.000546957s
Running propositional reduction
Solving with CaDiCaL 2.0.0
541 variables, 248 clauses
SAT checker: instance is SATISFIABLE
Runtime Solver: 6.7224e-05s
Runtime decision procedure: 0.000112819s
Running propositional reduction
Solving with CaDiCaL 2.0.0
542 variables, 249 clauses
SAT checker: instance is UNSATISFIABLE
Runtime Solver: 1.8032e-05s
Runtime decision procedure: 4.239e-05s

RESULTS:
Check 1: safe_div.assertion.1
	 - Status: FAILURE
	 - Description: "attempt to divide by zero"
	 - Location: src/lib.rs:2:42 in function safe_div

Check 2: safe_div.arithmetic_overflow.1
	 - Status: SUCCESS
	 - Description: "attempt to divide by zero"
	 - Location: src/lib.rs:2:42 in function safe_div

Check 3: safe_div.division-by-zero.1
	 - Status: SUCCESS
	 - Description: "division by zero"
	 - Location: src/lib.rs:2:42 in function safe_div


SUMMARY:
 ** 1 of 3 failed
Failed Checks: attempt to divide by zero
 File: "src/lib.rs", line 2, in safe_div

VERIFICATION:- FAILED
Verification Time: 0.0251693s

Concrete playback unit test for `verification::div_is_total`:
```
/// Test generated for harness `verification::div_is_total` 
///
/// Check for `assertion`: "attempt to divide by zero"

#[test]
fn kani_concrete_playback_div_is_total_7808176011615954438() {
    let concrete_vals: Vec<Vec<u8>> = vec![
        // 8
        vec![8, 0, 0, 0],
        // 0
        vec![0, 0, 0, 0],
    ];
    kani::concrete_playback_run(concrete_vals, div_is_total);
}
```
INFO: To automatically add the concrete playback unit test(s) to the src code, run Kani with `--concrete-playback=inplace`.
Checking harness verification::bump_never_overflows...
CBMC 6.8.0 (cbmc-6.8.0)
CBMC version 6.8.0 (cbmc-6.8.0) 64-bit x86_64 linux
Reading GOTO program from file /work/vault/target/kani/x86_64-unknown-linux-gnu/debug/deps/vault-fbaffb161a6495d2__RNvNtCs3Wmn5sT4Q3D_5vault12verification20bump_never_overflows.out
Generating GOTO Program
Adding CPROVER library (x86_64)
Removal of function pointers and virtual functions
Generic Property Instrumentation
Running with 16 object bits, 48 offset bits (user-specified)
Starting Bounded Model Checking
Runtime Symex: 0.00154564s
size of program expression: 116 steps
simple slicing removed 4 assignments
Generated 4 VCC(s), 4 remaining after simplification
Runtime Postprocess Equation: 3.0981e-05s
Passing problem to propositional reduction
converting SSA
Runtime Convert SSA: 0.000309627s
Running propositional reduction
Post-processing
Runtime Post-process: 6.178e-06s
Solving with CaDiCaL 2.0.0
201 variables, 108 clauses
SAT checker: instance is SATISFIABLE
Runtime Solver: 5.4463e-05s
Runtime decision procedure: 0.000415933s
Running propositional reduction
Solving with CaDiCaL 2.0.0
202 variables, 109 clauses
SAT checker: instance is SATISFIABLE
Runtime Solver: 4.5938e-05s
Runtime decision procedure: 7.6684e-05s
Running propositional reduction
Solving with CaDiCaL 2.0.0
203 variables, 110 clauses
SAT checker: instance is UNSATISFIABLE
Runtime Solver: 5.2796e-05s
Runtime decision procedure: 8.0448e-05s

RESULTS:
Check 1: verification::bump_never_overflows.assertion.1
	 - Status: SUCCESS
	 - Description: "assertion failed: y > x"
	 - Location: src/lib.rs:11:9 in function verification::bump_never_overflows

Check 2: bump.assertion.1
	 - Status: FAILURE
	 - Description: "attempt to add with overflow"
	 - Location: src/lib.rs:1:28 in function bump


SUMMARY:
 ** 1 of 2 failed
Failed Checks: attempt to add with overflow
 File: "src/lib.rs", line 1, in bump

VERIFICATION:- FAILED
Verification Time: 0.02676706s

Concrete playback unit test for `verification::bump_never_overflows`:
```
/// Test generated for harness `verification::bump_never_overflows` 
///
/// Check for `assertion`: "attempt to add with overflow"

#[test]
fn kani_concrete_playback_bump_never_overflows_1923147272287585563() {
    let concrete_vals: Vec<Vec<u8>> = vec![
        // 255
        vec![255],
    ];
    kani::concrete_playback_run(concrete_vals, bump_never_overflows);
}
```
INFO: To automatically add the concrete playback unit test(s) to the src code, run Kani with `--concrete-playback=inplace`.
Manual Harness Summary:
Verification failed for - verification::div_is_total
Verification failed for - verification::bump_never_overflows
Complete - 1 successfully verified harnesses, 2 failures, 3 total.
EOF
}
kani_green() {
  cat <<'EOF'
Kani Rust Verifier 0.67.0 (cargo plugin)
   Compiling vault v0.1.0 (/work/vault)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.12s
Checking harness verification::fine...
CBMC 6.8.0 (cbmc-6.8.0)
CBMC version 6.8.0 (cbmc-6.8.0) 64-bit x86_64 linux
Reading GOTO program from file /work/vault/target/kani/x86_64-unknown-linux-gnu/debug/deps/vault-fbaffb161a6495d2__RNvNtCs3Wmn5sT4Q3D_5vault12verification4fine.out
Generating GOTO Program
Adding CPROVER library (x86_64)
Removal of function pointers and virtual functions
Generic Property Instrumentation
Running with 16 object bits, 48 offset bits (user-specified)
Starting Bounded Model Checking
Runtime Symex: 0.00185952s
size of program expression: 132 steps
simple slicing removed 4 assignments
Generated 4 VCC(s), 4 remaining after simplification
Runtime Postprocess Equation: 1.7325e-05s
Passing problem to propositional reduction
converting SSA
Runtime Convert SSA: 0.000401488s
Running propositional reduction
Post-processing
Runtime Post-process: 8.278e-06s
Solving with CaDiCaL 2.0.0
221 variables, 134 clauses
SAT checker: instance is SATISFIABLE
Runtime Solver: 0.000125766s
Runtime decision procedure: 0.000591431s
Running propositional reduction
Solving with CaDiCaL 2.0.0
222 variables, 135 clauses
SAT checker: instance is UNSATISFIABLE
Runtime Solver: 8.0674e-05s
Runtime decision procedure: 0.000120791s

RESULTS:
Check 1: bump.assertion.1
	 - Status: SUCCESS
	 - Description: "attempt to add with overflow"
	 - Location: src/lib.rs:1:28 in function bump

Check 2: verification::fine.assertion.1
	 - Status: SUCCESS
	 - Description: "assertion failed: bump(x) > x"
	 - Location: src/lib.rs:25:9 in function verification::fine


SUMMARY:
 ** 0 of 2 failed

VERIFICATION:- SUCCESSFUL
Verification Time: 0.04398764s

WARNING: Kani could not produce a concrete playback for `verification::fine` because there were no failing panic checks or satisfiable cover statements.
Manual Harness Summary:
Complete - 1 successfully verified harnesses, 0 failures, 1 total.
EOF
}
# The same run without `-Z concrete-playback`: checks and locations, no values.
kani_noplay() { kani_fail | sed '/^Concrete playback unit test/,/^INFO:/d'; }
kcex() { cex --ingest --tool kani "$@"; }
kcexid() { # $1 = a substring of the harness name
  "$FPL_PY" - "$R/.fluxpoint-cex.jsonl" "$1" <<'PY2'
import json, sys
for l in open(sys.argv[1]):
    r = json.loads(l)
    if r["tool"] == "kani" and sys.argv[2] in r["title"]: print(r["cexId"]); break
PY2
}

mkkani
kani_green | kcex --exit 0 >/dev/null 2>&1
check "a green kani run records nothing" 0 "$(rows)"
kani_fail | kcex --exit 0 >/dev/null 2>&1
check "exit 0 records nothing whatever was printed" 0 "$(rows)"
kani_fail | kcex --exit 1 >/dev/null 2>&1
check "two failing harnesses record two counterexamples, the green one none" 2 "$(rows)"
out="$(cex --list)"
case "$out" in *"[kani] src/lib.rs:verification::div_is_total"*) ok "a row names the tool, the file and the harness" "selector" ;;
  *) bad "a row names the tool, the file and the harness" "${out:0:80}" ;; esac
"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "the playback comment values are the recorded input, in harness order" "8, 0 / 255" \
  || bad "the playback comment values are the recorded input, in harness order" "wrong payload"
import json, sys
rows = {json.loads(l)["title"]: json.loads(l) for l in open(sys.argv[1])}
d, b = rows["verification::div_is_total"], rows["verification::bump_never_overflows"]
# Kani does not name its inputs: the `// value` lines above each byte
# vector, in harness order, are the record. The raw bytes ride along as the
# prover's own second spelling of the same values.
assert d["input"] == "8, 0" and d["containment"] == "8, 0", d["input"]
assert d["containmentAlt"] == "vec![8, 0, 0, 0], vec![0, 0, 0, 0]", d["containmentAlt"]
assert b["input"] == "255" and b["containmentAlt"] == "vec![255]", b
for r in (d, b):
    assert r["signature"] == "counterexample" and r["inputForm"] == "kani-concrete-playback"
    assert r["kind"] == "harness" and r["module"] == "src/lib.rs"
assert "attempt to divide by zero" in d["assertion"] and "src/lib.rs:2:42" in d["assertion"], d["assertion"]
assert "attempt to add with overflow" in b["assertion"] and "src/lib.rs:1:28" in b["assertion"], b["assertion"]
PY2
kani_fail | kcex --exit 1 >/dev/null 2>&1
check "re-ingesting the same run appends nothing" 2 "$(rows)"

# ----- what a kani pin has to survive
kpin() { # $1 = attribute lines above #[test] (may be empty), $2 = body
  printf 'use vault::*;\n\n%s#[test]\nfn %s() {\n  %s\n}\n' "$1" "$KID" "$2" >"$R/tests/reg.rs"
  commit p >/dev/null
}
mkkani; kani_fail | kcex --exit 1 >/dev/null 2>&1; KID="$(kcexid bump)"
cex --pin "$KID" >/dev/null 2>&1
[ -f "$R/.claude/fluxpoint/cex/$KID.rs.draft" ] \
  && ok "the draft carries the prover's own extension" "rs" \
  || bad "the draft carries the prover's own extension" "missing"
kpin '' 'assert!(std::panic::catch_unwind(|| bump(255)).is_err());'
check "a tracked #[test] carrying the value pins" 0 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
check "and --check is green" 0 "$(rc_of --check)"

mkkani; kani_fail | kcex --exit 1 >/dev/null 2>&1; KID="$(kcexid bump)"
kpin $'#[should_panic]\n' 'assert!(bump(255) > 0);'
check "#[should_panic] is refused (it inverts the oracle)" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin $'#[ignore]\n' 'assert!(std::panic::catch_unwind(|| bump(255)).is_err());'
check "#[ignore] is refused (it never runs)" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
printf 'use vault::*;\n\nfn %s() {\n  assert!(std::panic::catch_unwind(|| bump(255)).is_err());\n}\n' "$KID" >"$R/tests/reg.rs"; commit plain >/dev/null
check "a plain fn with neither #[test] nor #[kani::proof] is refused" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' 'assert!(true);'
check "an assert!(true) body is refused" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' 'let x: u8 = 255;'
check "a body that binds the value and calls nothing is refused" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' '// bump(255)
  assert!(bump(2) > 2);'
check "the value in a COMMENT is refused" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' 'let s = "255"; assert!(!s.is_empty());'
check "the value in a STRING literal is refused" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' 'assert!(std::panic::catch_unwind(|| bump(254)).is_err());'
check "a retyped value is refused" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
printf 'method {:test} %s() {\n  expect bump(255) == 0;\n}\n' "$KID" >"$R/tests/reg.dfy"; commit dfy >/dev/null
check "a pin in another prover's language is refused" 1 \
  "$(rc_of --pin "$KID" --file tests/reg.dfy --test-name "$KID")"

# A proof harness is as legal a pin as a unit test: cargo kani runs it.
printf '\n#[cfg(kani)]\n#[kani::proof]\n#[kani::unwind(3)]\nfn %s() {\n    assert!(bump(255) > 0);\n}\n' "$KID" >>"$R/src/lib.rs"
commit proof >/dev/null
check "a #[kani::proof] harness carrying the value pins" 0 \
  "$(rc_of --pin "$KID" --file src/lib.rs --test-name "$KID")"

# The test Kani itself writes under --concrete-playback=inplace, renamed to
# carry the cexId: the value is in a `// value` comment (stripped) AND in
# the byte vector, which is the prover's own second spelling.
mkkani; kani_fail | kcex --exit 1 >/dev/null 2>&1; KID="$(kcexid bump)"
kani_fail | sed -n '/^\/\/\/ Test generated for harness `verification::bump_never_overflows`/,/^}$/p' \
  | sed "s/kani_concrete_playback_bump_never_overflows_[0-9]*/$KID/" >"$R/tests/reg.rs"; commit inplace >/dev/null
check "Kani's own inplace playback test pins" 0 \
  "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
# The two-value harness: u32 inputs, so each `// value` sits above a
# four-byte vector. Either spelling pins; the bytes only as the whole
# vectors Kani wrote, never as bare leaves.
KID="$(kcexid div)"
kpin '' 'assert!(std::panic::catch_unwind(|| safe_div(8, 0)).is_err());'
check "the interpreted values pin" 0 "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' 'kani::concrete_playback_run(vec![vec![8, 0, 0, 0], vec![0, 0, 0, 0]], div_is_total);'
check "so do the byte vectors Kani wrote" 0 "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' 'assert!(std::panic::catch_unwind(|| safe_div(8, 1)).is_err());'
check "a body missing one value is refused" 1 "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"
kpin '' 'assert!(std::panic::catch_unwind(|| safe_div(0, 8)).is_err());'
check "the values out of harness order are refused" 1 "$(rc_of --pin "$KID" --file tests/reg.rs --test-name "$KID")"

# ----- failures without values, and runs that record nothing
mkkani
kani_noplay | kcex --exit 1 >/dev/null 2>&1
check "failures without playback values are recorded as assertions" 2 "$(rows)"
KID="$(kcexid bump)"
check "and stay open rather than failing --check" 0 "$(rc_of --check)"
kpin '' 'assert!(std::panic::catch_unwind(|| bump(255)).is_err());'
err="$(cex --pin "$KID" --file tests/reg.rs --test-name "$KID" 2>&1 >/dev/null)"
case "$err" in *"no literal"*) ok "one cannot be pinned mechanically, and says so" "refused" ;;
  *) bad "one cannot be pinned mechanically, and says so" "${err:0:60}" ;; esac
mkkani
printf 'error[E0425]: cannot find value `z` in this scope\n --> src/lib.rs:9:5\n\nerror: could not compile `vault` (lib) due to 1 previous error\n' \
  | kcex --exit 1 >/dev/null 2>&1
check "a build error is not a counterexample" 0 "$(rows)"
check "and files nothing" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"

# ----- drift is loud and never red
mkkani
check "a summary counting a failure the parser could not extract exits 0" 0 \
  "$(kani_fail | sed 's/2 failures, 3 total/3 failures, 4 total/' | kcex --exit 1 >/dev/null 2>&1; echo $?)"
check "and files INGEST-FAILED" 1 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkkani
printf 'RESULTS:\nVERIFICATION:- FAILED\n' | kcex --exit 1 >/dev/null 2>&1
check "a FAILED verdict with no harness name is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkkani
printf 'thread main panicked at kani-driver\n' | kcex --exit 101 >/dev/null 2>&1
check "output with no verdict and no rustc error is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
out="$("$FPL_PY" "$INBOX" --root "$R" --list 2>/dev/null || cat "$R/.claude/fluxpoint/inbox.jsonl")"
case "$out" in *"kani"*) ok "the inbox row names the prover that drifted" "named" ;;
  *) bad "the inbox row names the prover that drifted" "${out:0:60}" ;; esac

# ----- --check: red on real weakening
mkkani; kani_fail | kcex --exit 1 >/dev/null 2>&1; KID="$(kcexid bump)"
kpin '' 'assert!(std::panic::catch_unwind(|| bump(255)).is_err());'
cex --pin "$KID" --file tests/reg.rs --test-name "$KID" >/dev/null 2>&1
git -C "$R" rm -q tests/reg.rs; commit rm >/dev/null
check "a deleted kani regression is red" 1 "$(rc_of --check)"

# ----- the scaffolded harness captures kani the way it captures dafny
mkkani
mkdir -p "$R/scripts" "$R/bin"
cp "$PLUGIN/templates/harness.sh" "$R/scripts/harness.sh"; chmod +x "$R/scripts/harness.sh"
kani_fail >"$R/bin/kani.out"
# `cargo` stands in for the whole toolchain: fmt passes, clippy is absent
# (its --version fails, so the harness skips it), test passes, and `kani`
# prints the captured run and exits 1 the way the real plugin did.
cat >"$R/bin/cargo" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  fmt) exit 0 ;;
  clippy) exit 1 ;;
  test) exit 0 ;;
  kani) cat "$(dirname "$0")/kani.out"; exit 1 ;;
  *) echo "cargo shim: unexpected $*" >&2; exit 2 ;;
esac
EOF
printf '#!/usr/bin/env bash\necho "cargo-kani 0.67.0"\nexit 0\n' >"$R/bin/cargo-kani"
chmod +x "$R/bin/cargo" "$R/bin/cargo-kani"
git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm shim
( cd "$R" && env -u CLAUDE_PLUGIN_ROOT PATH="$R/bin:$PATH" FPL_PLUGIN_ROOT="$PLUGIN" \
    bash scripts/harness.sh --full >"$ROOT/harness.log" 2>&1 ); hrc=$?
check "the harness fails with the prover's exit" 1 "$hrc"
check "and both counterexamples were recorded on the way" 2 "$(rows)"
[ "$(rows)" = 2 ] || { echo "      harness output:"; tail -15 "$ROOT/harness.log" | sed 's/^/      /'; }

# ================= 11. a fourth prover: apalache =========================
# Captured from a real run, 2026-09-11: Apalache 0.47.2 on the module
# below, `apalache-mc check --inv=Inv --length=5 Counter.tla`, exit 12 —
# the stdout (paths edited to /work/spec), the ITF trace it wrote, and the
# type error the same run printed before the variables carried @type
# annotations. Two things the documentation had wrong: the files are
# violation1.* (not counterexample1.*), and the ITF's #meta carries
# format, varTypes and description but no source, so the spec name is read
# from the _apalache-out/<Spec.tla>/ directory. Only itf_rich is assembled,
# from the ITF format description (ADR-015), to exercise the encodings this
# spec does not produce.
mkapalache() {
  mkrepo apalache
  cat >"$R/Counter.tla" <<'EOF'
---- MODULE Counter ----
EXTENDS Integers
VARIABLES
  \* @type: Int;
  x,
  \* @type: Set(Str);
  names
Init == x = 0 /\ names = {"a"}
Next == x' = x + 1 /\ names' = names \union {"b"}
Inv == x < 2
====
EOF
  git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm tla
}
itf_fixture() {
  cat <<'EOF'
{
  "#meta": {
    "format": "ITF",
    "varTypes": {
      "names": "Set(Str)",
      "x": "Int"
    },
    "format-description": "https://apalache-mc.org/docs/adr/015adr-trace.html",
    "description": "Created by Apalache on Fri Sep 11 00:20:54 UTC 2026"
  },
  "vars": [
    "names",
    "x"
  ],
  "states": [
    {
      "#meta": {
        "index": 0
      },
      "names": {
        "#set": [
          "a"
        ]
      },
      "x": {
        "#bigint": "0"
      }
    },
    {
      "#meta": {
        "index": 1
      },
      "names": {
        "#set": [
          "a",
          "b"
        ]
      },
      "x": {
        "#bigint": "1"
      }
    },
    {
      "#meta": {
        "index": 2
      },
      "names": {
        "#set": [
          "a",
          "b"
        ]
      },
      "x": {
        "#bigint": "2"
      }
    }
  ]
}
EOF
}
# Every documented value encoding in one state, for the renderer.
itf_rich() {
  cat <<'EOF'
{
  "#meta": { "format": "ITF", "source": "Rich.tla" },
  "vars": [ "n", "s", "b", "r", "t", "m", "u", "q" ],
  "states": [
    { "#meta": { "index": 0 }, "n": { "#bigint": "-5" }, "s": "abc", "b": true,
      "r": { "a": 1, "b": "x" }, "t": { "#tup": [ 1, 2 ] },
      "m": { "#map": [ [ 1, "a" ], [ 2, "b" ] ] }, "u": { "#unserializable": "Nat" },
      "q": [ 7, 8 ] }
  ]
}
EOF
}
AP_DIR="_apalache-out/Counter.tla/2026-09-11T00-20-52_330647260225453583"
apalache_out() { # $1 = the run directory as the checker prints it
  local d="$1"
  cat <<EOF
PASS #13: BoundedChecker                                          I@00:20:53.785
State 0: Checking 1 state invariants                              I@00:20:54.142
State 0: state invariant 0 holds.                                 I@00:20:54.144
Step 0: picking a transition out of 1 transition(s)               I@00:20:54.147
State 1: Checking 1 state invariants                              I@00:20:54.162
State 1: state invariant 0 holds.                                 I@00:20:54.163
Step 1: picking a transition out of 1 transition(s)               I@00:20:54.163
State 2: Checking 1 state invariants                              I@00:20:54.169
Check the trace in: $d/violation1.tla, $d/MCviolation1.out, $d/violation1.json, $d/violation1.itf.json I@00:20:54.308
State 2: state invariant 0 violated.                              I@00:20:54.308
Found 1 error(s)                                                  I@00:20:54.309
The outcome is: Error                                             I@00:20:54.313
Checker has found an error                                        I@00:20:54.318
It took me 0 days  0 hours  0 min  1 sec                          I@00:20:54.318
Total time: 1.352 sec                                             I@00:20:54.318
EXITCODE: ERROR (12)
EOF
}
write_itf() { mkdir -p "$R/$AP_DIR"; itf_fixture >"$R/$AP_DIR/violation1.itf.json"; }
acex() { cex --ingest --tool apalache "$@"; }
acexid() { "$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2'
import json, sys
for l in open(sys.argv[1]):
    r = json.loads(l)
    if r["tool"] == "apalache": print(r["cexId"]); break
PY2
}

mkapalache
printf 'Checker reports no error up to computation length 5        I@00:21:00.100\nThe outcome is: NoError                                           I@00:21:00.101\nIt took me 0 days  0 hours  0 min  1 sec                          I@00:21:00.102\nEXITCODE: OK\n' | acex --exit 0 >/dev/null 2>&1
check "a green apalache run records nothing" 0 "$(rows)"
write_itf
apalache_out "$R/$AP_DIR" | acex --exit 0 >/dev/null 2>&1
check "exit 0 records nothing whatever was printed" 0 "$(rows)"
apalache_out "$R/$AP_DIR" | acex --exit 12 >/dev/null 2>&1
check "a violated invariant records one trace" 1 "$(rows)"
out="$(cex --list)"
case "$out" in *"[apalache] Counter.tla:3-state-trace"*) ok "the row names the tool, the spec and the trace length" "selector" ;;
  *) bad "the row names the tool, the spec and the trace length" "${out:0:80}" ;; esac
"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "every state is rendered in order; the first is what a pin must carry" 'names = {"a"}, x = 0' \
  || bad "every state is rendered in order; the first is what a pin must carry" "wrong payload"
import json, sys
r = json.loads(open(sys.argv[1]).readline())
assert r["input"] == 'names = {"a"}, x = 0 ; names = {"a", "b"}, x = 1 ; names = {"a", "b"}, x = 2', r["input"]
assert r["containment"] == 'names = {"a"}, x = 0', r["containment"]
assert r["signature"] == "counterexample" and r["inputForm"] == "itf"
# The spec name comes from the run directory: the ITF names no source, and
# a key carrying the timestamped directory would file the same trace as new
# on every run.
assert r["kind"] == "trace" and r["module"] == "Counter.tla" and r["title"] == "Counter (3 states)", r
assert r["assertion"] == "invariant violated after 2 transition(s)", r["assertion"]
assert r["trace"].endswith("violation1.itf.json"), r["trace"]
PY2
apalache_out "$AP_DIR" | acex --exit 12 >/dev/null 2>&1
check "a relative trace path resolves against the root, and dedupes" 1 "$(rows)"
mkdir -p "$R/_apalache-out/Counter.tla/2026-09-11T09-00-00_1"; itf_fixture >"$R/_apalache-out/Counter.tla/2026-09-11T09-00-00_1/violation1.itf.json"
apalache_out "$R/_apalache-out/Counter.tla/2026-09-11T09-00-00_1" | acex --exit 12 >/dev/null 2>&1
check "the same trace from a later run directory dedupes too" 1 "$(rows)"

mkapalache
itf_rich >"$R/rich.itf.json"
acex --from "$R/rich.itf.json" --exit 12 >/dev/null 2>&1
check "--from pointed at the ITF file itself is read as the trace" 1 "$(rows)"
"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "bigint, set, map, tuple, record, sequence and unserializable all render" "TLA-ish" \
  || bad "bigint, set, map, tuple, record, sequence and unserializable all render" "wrong rendering"
import json, sys
r = json.loads(open(sys.argv[1]).readline())
assert r["input"] == ('n = -5, s = "abc", b = TRUE, r = [a |-> 1, b |-> "x"], t = <<1, 2>>, '
                      'm = SetAsFun({<<1, "a">>, <<2, "b">>}), u = Nat, q = <<7, 8>>'), r["input"]
assert r["module"] == "Rich.tla" and r["selector"] == "Rich.tla:1-state-trace", r["selector"]
assert r["assertion"] == "invariant violated after 0 transition(s)"
PY2

# ----- what an apalache pin has to survive
apin() { # $1 = the definition text (may span lines)
  printf -- '---- MODULE Reg ----\nEXTENDS Integers\nVARIABLES x, names\n\n%s\n====\n' "$1" >"$R/Reg.tla"
  commit p >/dev/null
}
mkapalache; write_itf; apalache_out "$AP_DIR" | acex --exit 12 >/dev/null 2>&1; AID="$(acexid)"
cex --pin "$AID" >/dev/null 2>&1
[ -f "$R/.claude/fluxpoint/cex/$AID.tla.draft" ] \
  && ok "the draft carries the prover's own extension" "tla" \
  || bad "the draft carries the prover's own extension" "missing"
case "$(head -1 "$R/.claude/fluxpoint/cex/$AID.tla.draft")" in '\*'*) ok "and speaks TLA+ down to its comment marker" '\*' ;;
  *) bad "and speaks TLA+ down to its comment marker" "$(head -c 20 "$R/.claude/fluxpoint/cex/$AID.tla.draft")" ;; esac
apin "$AID == names = {\"a\"} /\\ x = 0"
check "a tracked .tla operator carrying the first state pins" 0 \
  "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
check "and --check is green" 0 "$(rc_of --check)"
apin "$AID ==
  /\\ names = {\"a\"}
  /\\ x = 0

Other == x = 0"
check "a bulleted multi-line body pins, and stops at the next definition" 0 \
  "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"

mkapalache; write_itf; apalache_out "$AP_DIR" | acex --exit 12 >/dev/null 2>&1; AID="$(acexid)"
apin "ASSUME $AID == names = {\"a\"} /\\ x = 0"
check "an ASSUME is refused (the checker takes it as given)" 1 \
  "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
apin "$AID == TRUE"
check "a TRUE body is refused" 1 "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
apin "$AID == x = 0

Other == names = {\"a\"}"
check "a body missing a literal is refused, even with it in the NEXT definition" 1 \
  "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
apin "$AID == names = {\"b\"} /\\ x = 0"
check "a retyped value is refused" 1 "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
apin "$AID == \\* names = {\"a\"} /\\ x = 0
  x = 1"
check "the values in a line COMMENT are refused" 1 "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
apin "$AID == (* names = {\"a\"} /\\ x = 0 *) x = 1"
check "the values in a block COMMENT are refused" 1 "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
apin "$AID == s = \"names = {a} /\\ x = 0\""
check "the values in a STRING are refused" 1 "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
printf '#[test]\nfn %s() { assert!(f("a", 0)); }\n' "$AID" >"$R/reg.rs"; commit rs >/dev/null
check "a pin in another prover's language is refused" 1 \
  "$(rc_of --pin "$AID" --file reg.rs --test-name "$AID")"

# A string-valued state is pinnable: string literals are digested rather
# than blanked, so `"abc"` matches `"abc"` and nothing else can hide in one.
mkapalache; itf_rich >"$R/rich.itf.json"; acex --from "$R/rich.itf.json" --exit 12 >/dev/null 2>&1
AID="$(acexid)"; cex --pin "$AID" >/dev/null 2>&1
apin "$AID ==
  $(tail -1 "$R/.claude/fluxpoint/cex/$AID.tla.draft")"
check "the draft's first-state conjunction, pasted into a tracked module, pins" 0 \
  "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"
apin "$AID == n = -5 /\\ s = \"abd\" /\\ b = TRUE /\\ r = [a |-> 1, b |-> \"x\"] /\\ t = <<1, 2>> /\\ m = SetAsFun({<<1, \"a\">>, <<2, \"b\">>}) /\\ u = Nat /\\ q = <<7, 8>>"
check "with one string retyped it is refused" 1 "$(rc_of --pin "$AID" --file Reg.tla --test-name "$AID")"

# ----- runs that record nothing, and drift that is loud but never red
mkapalache
printf 'PASS #1: TypeCheckerSnowcat                                       I@00:20:29.600\n > Running Snowcat .::.                                           I@00:20:29.600\nCounter.tla:3:11-3:11: type input error: Expected a type annotation for VARIABLE x E@00:20:29.631\nIt took me 0 days  0 hours  0 min  0 sec                          I@00:20:29.632\nTotal time: 0.601 sec                                             I@00:20:29.632\nEXITCODE: ERROR (255)\n' \
  | acex --exit 255 >/dev/null 2>&1
check "a type error is not a counterexample" 0 "$(rows)"
check "and files nothing" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkapalache
check "a named trace file that does not exist exits 0" 0 \
  "$(apalache_out "$AP_DIR" | acex --exit 12 >/dev/null 2>&1; echo $?)"
check "and files INGEST-FAILED" 1 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkapalache
printf 'State 2: state invariant 0 violated.                              I@00:20:54.308\nEXITCODE: ERROR (12)\n' | acex --exit 12 >/dev/null 2>&1
check "a violation naming no .itf.json trace is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkapalache; mkdir -p "$R/$AP_DIR"; printf '{"vars": ["x"]}\n' >"$R/$AP_DIR/violation1.itf.json"
apalache_out "$AP_DIR" | acex --exit 12 >/dev/null 2>&1
check "a trace file with no states is a parser break" 1 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkapalache
printf 'java.lang.OutOfMemoryError: Java heap space\n' | acex --exit 1 >/dev/null 2>&1
check "output with no EXITCODE and no trace is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
out="$("$FPL_PY" "$INBOX" --root "$R" --list 2>/dev/null || cat "$R/.claude/fluxpoint/inbox.jsonl")"
case "$out" in *"apalache"*) ok "the inbox row names the prover that drifted" "named" ;;
  *) bad "the inbox row names the prover that drifted" "${out:0:60}" ;; esac

# ----- --check: red on real weakening
mkapalache; write_itf; apalache_out "$AP_DIR" | acex --exit 12 >/dev/null 2>&1; AID="$(acexid)"
apin "$AID == names = {\"a\"} /\\ x = 0"
cex --pin "$AID" --file Reg.tla --test-name "$AID" >/dev/null 2>&1
git -C "$R" rm -q Reg.tla; commit rm >/dev/null
check "a deleted apalache regression is red" 1 "$(rc_of --check)"

# ----- the scaffolded harness captures apalache the way it captures dafny
mkapalache
mkdir -p "$R/scripts" "$R/bin"
cp "$PLUGIN/templates/harness.sh" "$R/scripts/harness.sh"; chmod +x "$R/scripts/harness.sh"
itf_fixture >"$R/bin/cex.itf.json"
# The shim writes the trace where the checker would and prints the lines
# that name it, then exits 12 the way `apalache-mc check` did.
cat >"$R/bin/apalache-mc" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "version" ]; then echo "0.47.2"; exit 0; fi
d="\$PWD/$AP_DIR"
mkdir -p "\$d"
cp "\$(dirname "\$0")/cex.itf.json" "\$d/violation1.itf.json"
printf 'PASS #13: BoundedChecker                                          I@00:20:53.785\nState 2: Checking 1 state invariants                              I@00:20:54.169\nCheck the trace in: %s/violation1.tla, %s/MCviolation1.out, %s/violation1.json, %s/violation1.itf.json I@00:20:54.308\nState 2: state invariant 0 violated.                              I@00:20:54.308\nEXITCODE: ERROR (12)\n' "\$d" "\$d" "\$d" "\$d"
exit 12
EOF
chmod +x "$R/bin/apalache-mc"
git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm shim
( cd "$R" && env -u CLAUDE_PLUGIN_ROOT PATH="$R/bin:$PATH" FPL_PLUGIN_ROOT="$PLUGIN" \
    FPL_APALACHE_ARGS="--inv=Inv Counter.tla" \
    bash scripts/harness.sh --full >"$ROOT/harness.log" 2>&1 ); hrc=$?
check "the harness fails with the checker's exit" 12 "$hrc"
check "and the trace was recorded on the way" 1 "$(rows)"
[ "$(rows)" = 1 ] || { echo "      harness output:"; tail -15 "$ROOT/harness.log" | sed 's/^/      /'; }
mkapalache
( cd "$R" && env -u CLAUDE_PLUGIN_ROOT PATH="$R/bin:$PATH" FPL_PLUGIN_ROOT="$PLUGIN" \
    bash scripts/harness.sh --full >"$ROOT/harness.log" 2>&1 ); hrc=$?
check "without FPL_APALACHE_ARGS the checker is not invoked at all" 0 "$(rows)"

# ================= 12. a fifth prover: fast-check, off-chain =============
# Captured from a real run, 2026-09-11: fast-check 4.x under vitest 5.0.0 on
# the module below, `npx vitest run`, exit 1. Two property failures and one
# passing property; the stack frames are trimmed and the crate path edited,
# nothing else. The plain-assertion and load-error fixtures come from runs of
# the same project.
#
# This is the first tool in the ledger whose failures are NOT all
# counterexamples: a vitest run mixes property failures with ordinary
# assertion failures, and only the first kind carries a shrunk input.
mkfc() {
  mkrepo fastcheck
  mkdir -p "$R/test"
  printf '{ "name": "app", "type": "module", "scripts": { "test": "vitest run" } }\n' \
    >"$R/package.json"
  cat >"$R/test/datum.test.ts" <<'EOF'
import { describe, it, expect } from "vitest";
import fc from "fast-check";

export function encode(n: number): string { return n < 1000 ? `n${n}` : "overflow"; }
export function decode(s: string): number { return Number(s.slice(1)); }
export function render(n: number, s: string): string { return `${n}${s}`; }

describe("datum", () => {
  it("attack_datum_round_trip survives encode then decode", () => {
    fc.assert(fc.property(fc.integer({ min: 0, max: 5000 }), (n) => {
      expect(decode(encode(n))).toBe(n);
    }));
  });
});
EOF
  git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm fc
}
fc_fail() {
  cat <<'EOF'
 RUN  v5.0.0 /work/app

 ❯ test/datum.test.ts (3 tests | 2 failed) 28ms

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  test/datum.test.ts > datum > attack_datum_round_trip survives encode then decode
Error: Property failed after 1 tests
{ seed: -1590747016, path: "0:1:0:2:0:2:0:1", endOnFailure: true }
Counterexample: [1000]
Shrunk 7 time(s)

Hint: Enable verbose mode in order to have the list of all failing values encountered during the run
 ❯ Module.assert node_modules/fast-check/lib/fast-check.js:2542:7
 ❯ test/datum.test.ts:9:8

Caused by: AssertionError: expected NaN to be 1000 // Object.is equality
 ❯ test/datum.test.ts:10:33

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/2]⎯

 FAIL  test/datum.test.ts > datum > two inputs
Error: Property failed after 1 tests
{ seed: -1736740566, path: "0:3:0:5:8:11:14:17:20:22:24:25", endOnFailure: true }
Counterexample: [-10,"         "]
Shrunk 11 time(s)

Hint: Enable verbose mode in order to have the list of all failing values encountered during the run
 ❯ Module.assert node_modules/fast-check/lib/fast-check.js:2542:7

Caused by: AssertionError: expected 12 to be less than 12
 ❯ test/datum.test.ts:16:33

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[2/2]⎯

 Test Files  1 failed (1)
      Tests  2 failed | 1 passed (3)
EOF
}
fc_plain() {
  cat <<'EOF'
 RUN  v5.0.0 /work/app

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  t2/plain.test.ts > plain > a normal assertion failure, no property
AssertionError: expected 4 to be 5 // Object.is equality

- Expected
+ Received

- 5
+ 4

 ❯ t2/plain.test.ts:3:71

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯

 Test Files  1 failed (1)
      Tests  1 failed (1)
EOF
}
fc_green() {
  cat <<'EOF'
 RUN  v5.0.0 /work/app

 ✓ test/datum.test.ts (1 test) 5ms

 Test Files  1 passed (1)
      Tests  1 passed (1)
EOF
}
fcex() { cex --ingest --tool fastcheck "$@"; }
fcid() { # $1 = substring of the title
  "$FPL_PY" - "$R/.fluxpoint-cex.jsonl" "$1" <<'PY2'
import json, sys
for l in open(sys.argv[1]):
    r = json.loads(l)
    if r["tool"] == "fastcheck" and sys.argv[2] in r["title"]: print(r["cexId"]); break
PY2
}

mkfc
fc_green | fcex --exit 0 >/dev/null 2>&1
check "a green run records nothing" 0 "$(rows)"
fc_fail | fcex --exit 0 >/dev/null 2>&1
check "exit 0 records nothing whatever was printed" 0 "$(rows)"
fc_fail | fcex --exit 1 >/dev/null 2>&1
check "two property failures record two counterexamples" 2 "$(rows)"
out="$(cex --list)"
case "$out" in *"[fastcheck] test/datum.test.ts > datum > attack_datum_round_trip"*)
  ok "the row names the tool, the file and the full test path" "selector" ;;
  *) bad "the row names the tool, the file and the full test path" "${out:0:80}" ;; esac

# A plain assertion failure carries no generated input. Recording one would
# put a value in the ledger that no generator ever produced.
mkfc
fc_plain | fcex --exit 1 >/dev/null 2>&1
check "a plain assertion failure is not a counterexample" 0 "$(rows)"
check "and files nothing" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"

mkfc
fc_fail | fcex --exit 1 >/dev/null 2>&1
"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "the shrunk input, seed and replay path are all recorded" "verbatim" \
  || bad "the shrunk input, seed and replay path are all recorded" "wrong payload"
import json, sys
rows = {json.loads(l)["title"]: json.loads(l) for l in open(sys.argv[1])}
a = rows["attack_datum_round_trip survives encode then decode"]
b = rows["two inputs"]
assert a["input"] == "[1000]", a["input"]
assert a["seed"] == -1590747016 and a["replayPath"] == "0:1:0:2:0:2:0:1", a
assert a["iterations"] == 1 and a["shrinks"] == 7, a
assert a["kind"] == "property" and a["inputForm"] == "js-literal"
assert "expected NaN to be 1000" in a["assertion"], a["assertion"]
# NOT whitespace-collapsed. fast-check generates arbitrary strings, and this
# counterexample is nine spaces: folding them would record a value the
# generator never produced and pin a test against the wrong one.
assert b["input"] == '[-10,"         "]', repr(b["input"])
assert b["input"].count(" ") == 9, b["input"].count(" ")
PY2
fc_fail | fcex --exit 1 >/dev/null 2>&1
check "re-ingesting the same run appends nothing" 2 "$(rows)"

# ----- what a fast-check pin has to survive
tspin() { # $1 = the it() modifier (may be empty), $2 = body
  cat >"$R/test/reg.test.ts" <<EOF
import { it, expect } from "vitest";
import { render } from "./datum.test";
it$1("$FID regression", () => {
  $2
});
EOF
  commit p >/dev/null
}
mkfc; fc_fail | fcex --exit 1 >/dev/null 2>&1; FID="$(fcid 'two inputs')"
cex --pin "$FID" >/dev/null 2>&1
[ -f "$R/.claude/fluxpoint/cex/$FID.ts.draft" ] \
  && ok "the draft carries the prover's own extension" "ts" \
  || bad "the draft carries the prover's own extension" "missing"
tspin '' 'expect(render(-10, "         ").length).toBeGreaterThanOrEqual(12);'
check "a test carrying both values in order pins" 0 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
check "and --check is green" 0 "$(rc_of --check)"

# The nine-space string is the whole point: one space is a different value.
tspin '' 'expect(render(-10, " ").length).toBeGreaterThanOrEqual(12);'
check "the same string retyped shorter is refused" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
tspin '' 'expect(render(10, "         ").length).toBeGreaterThanOrEqual(12);'
check "a retyped number is refused" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
tspin '' 'expect(render("         ", -10).length).toBeGreaterThanOrEqual(12);'
check "the values out of order are refused" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
tspin '.skip' 'expect(render(-10, "         ").length).toBeGreaterThanOrEqual(12);'
check ".skip is refused (the runner never executes it)" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
tspin '.todo' 'expect(render(-10, "         ").length).toBeGreaterThanOrEqual(12);'
check ".todo is refused" 1 "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
tspin '.fails' 'expect(render(-10, "         ").length).toBeGreaterThanOrEqual(12);'
check ".fails is refused (it inverts the oracle)" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
err="$(cex --pin "$FID" --file test/reg.test.ts --test-name "$FID" 2>&1 >/dev/null)"
case "$err" in *"inverts the oracle"*) ok "and says why" "said" ;;
  *) bad "and says why" "${err:0:60}" ;; esac
tspin '' 'expect(true).toBe(true);'
check "an expect(true) body is refused" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
tspin '' '// render(-10, "         ")
  expect(render(1, "x").length).toBe(2);'
check "the values in a COMMENT are refused" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
tspin '' 'const s = "render(-10,          )"; expect(s.length).toBe(21);'
check "the values inside a STRING are refused" 1 \
  "$(rc_of --pin "$FID" --file test/reg.test.ts --test-name "$FID")"
printf 'test %s() {\n  pred(-10)\n}\n' "$FID" >"$R/lib/reg.ak"; commit ak >/dev/null
check "a pin in another prover's language is refused" 1 \
  "$(rc_of --pin "$FID" --file lib/reg.ak --test-name "$FID")"

# ----- --check: red on real weakening
mkfc; fc_fail | fcex --exit 1 >/dev/null 2>&1; FID="$(fcid 'two inputs')"
tspin '' 'expect(render(-10, "         ").length).toBeGreaterThanOrEqual(12);'
cex --pin "$FID" --file test/reg.test.ts --test-name "$FID" >/dev/null 2>&1
git -C "$R" rm -q test/reg.test.ts; commit rm >/dev/null
check "a deleted regression is red" 1 "$(rc_of --check)"

# ----- drift is loud and never red
mkfc
check "a summary counting more blocks than it printed exits 0" 0 \
  "$(fc_fail | sed 's/Failed Tests 2/Failed Tests 3/' | fcex --exit 1 >/dev/null 2>&1; echo $?)"
check "and files INGEST-FAILED" 1 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkfc
printf 'Error: Cannot find module "./missing.js"\n' | fcex --exit 1 >/dev/null 2>&1
check "a run that never reached a test records nothing" 0 "$(rows)"
check "and files nothing" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkfc
printf 'some other runner said something else entirely\n' | fcex --exit 1 >/dev/null 2>&1
check "output this parser does not know is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
out="$("$FPL_PY" "$INBOX" --root "$R" --list 2>/dev/null || cat "$R/.claude/fluxpoint/inbox.jsonl")"
case "$out" in *"fastcheck"*) ok "the inbox row names the prover that drifted" "named" ;;
  *) bad "the inbox row names the prover that drifted" "${out:0:60}" ;; esac

# ----- the scaffolded harness captures the test run
mkfc
mkdir -p "$R/scripts" "$R/bin"
cp "$PLUGIN/templates/harness.sh" "$R/scripts/harness.sh"; chmod +x "$R/scripts/harness.sh"
fc_fail >"$R/bin/vitest.out"
cat >"$R/bin/npm" <<'EOF'
#!/usr/bin/env bash
[ "$1" = run ] || { echo "npm shim: unexpected $*" >&2; exit 2; }
case "$2" in
  test) cat "$(dirname "$0")/vitest.out"; exit 1 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$R/bin/npm"
git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm shim
( cd "$R" && env -u CLAUDE_PLUGIN_ROOT PATH="$R/bin:$PATH" FPL_PLUGIN_ROOT="$PLUGIN" \
    bash scripts/harness.sh --full >"$ROOT/harness.log" 2>&1 ); hrc=$?
check "the harness fails with the runner's exit" 1 "$hrc"
check "and both counterexamples were recorded on the way" 2 "$(rows)"
[ "$(rows)" = 2 ] || { echo "      harness output:"; tail -15 "$ROOT/harness.log" | sed 's/^/      /'; }
# A repo with no `test` script must not be aborted by the absence: the
# helper returns 0 rather than letting `set -e` take the run down.
mkfc
mkdir -p "$R/scripts" "$R/bin"
cp "$PLUGIN/templates/harness.sh" "$R/scripts/harness.sh"; chmod +x "$R/scripts/harness.sh"
printf '{ "name": "app", "type": "module" }\n' >"$R/package.json"
printf '#!/usr/bin/env bash\necho "npm shim: should not be called with $*" >&2\nexit 9\n' >"$R/bin/npm"
chmod +x "$R/bin/npm"
git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm noscript
( cd "$R" && env -u CLAUDE_PLUGIN_ROOT PATH="$R/bin:$PATH" FPL_PLUGIN_ROOT="$PLUGIN" \
    bash scripts/harness.sh --full >"$ROOT/harness.log" 2>&1 ); hrc=$?
check "a package.json with no test script is green, not aborted" 0 "$hrc"

# ================= 13. the wide sweep: the gate's seed is pinned ==========
# `--full` runs `aiken check --seed 1` so a shrink is reproducible and this
# ledger can dedupe. The cost was never stated: every run then explores the
# SAME cases. The fix is the split mutation-guard already makes — the cheap
# half stays in the gate, the expensive half runs off-session and carries
# the exploration.
#
# `--seed <UINT>` and `--max-success <UINT>` are read off `aiken check
# --help` from a real v1.1.9 binary, not assumed.
mkfuzz() {  # $1 = seeds that fail, space separated
  mkrepo
  mkdir -p "$R/bin"
  printf '%s\n' "$1" >"$R/bin/failing-seeds"
  # The shim is `aiken check --seed S --max-success M`. It fails for the
  # seeds named above, with a counterexample whose value is the seed, so a
  # sweep that really does rotate finds something new each time.
  cat >"$R/bin/aiken" <<'EOF'
#!/usr/bin/env bash
seed=""; maxs=""
while [ $# -gt 0 ]; do
  case "$1" in
    --seed) seed="$2"; shift 2 ;;
    --max-success) maxs="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$maxs" ] || { echo "shim: no --max-success" >&2; exit 2; }
echo "$seed $maxs" >>"$(dirname "$0")/calls"
if grep -qw "$seed" "$(dirname "$0")/failing-seeds" 2>/dev/null; then
  cat <<JSON
{ "seed": $seed,
  "summary": { "total": 1, "passed": 0, "failed": 1, "kind": { "unit": 0, "property": 1 } },
  "modules": [ { "name": "x/vault",
    "summary": { "total": 1, "passed": 0, "failed": 1, "kind": { "unit": 0, "property": 1 } },
    "tests": [ { "title": "prop_holds", "status": "fail",
                 "on_failure": "fail_immediately", "iterations": 7,
                 "counterexample": "Deep { depth: $seed }" } ] } ] }
JSON
  exit 1
fi
printf '{ "seed": %s, "summary": { "total": 1, "passed": 1, "failed": 0, "kind": { "unit": 0, "property": 1 } }, "modules": [] }\n' "$seed"
EOF
  chmod +x "$R/bin/aiken"
  printf '{ "version": 1, "tool": "aiken" }\n' >"$R/.fluxpoint-fuzz.json"
  commit fuzz >/dev/null
}
sw() { ( cd "$R" && PATH="$R/bin:$PATH" "$FPL_PY" "$CEX" --root "$R" --sweep "$@" ); }
rc_sw() { sw "$@" >/dev/null 2>&1; echo $?; }
stamp() { "$FPL_PY" -c "
import json,sys
print(json.load(open('$R/.fluxpoint-fuzz.json')).get('last',{}).get(sys.argv[1],''))" "$1"; }

mkrepo
check "no fuzz config: --check says nothing about a sweep" 0 "$(rc_of --check)"
case "$(cex --check 2>&1)" in *sweep*) bad "and stays silent" "mentioned a sweep" ;;
  *) ok "and stays silent" "silent" ;; esac
check "--sweep without a config is refused" 1 "$(rc_sw)"

mkfuzz "3 7"
out="$(cex --check 2>&1)"
case "$out" in *"never run"*"seed is pinned"*)
  ok "a declared sweep that never ran is named every run" "named" ;;
  *) bad "a declared sweep that never ran is named every run" "${out:0:70}" ;; esac
check "and does not fail the gate on its own" 0 "$(rc_of --check)"

check "a sweep runs and exits 0" 0 "$(rc_sw --seeds 5 --max-success 250)"
check "  it ran five seeds" 5 "$(wc -l <"$R/bin/calls" | tr -d ' ')"
check "  starting at 2, since the gate already owns seed 1" "2 250" "$(head -1 "$R/bin/calls")"
check "  and it raised max-success past the default" "6 250" "$(tail -1 "$R/bin/calls")"
check "  the stamp records where it got to" 6 "$(stamp to)"
check "  and how many new counterexamples it found" 1 "$(stamp found)"
check "  which are in the ledger" 1 "$(rows)"
out="$(cex --list)"
case "$out" in *"Deep { depth: 3 }"*) ok "  recorded by the parser the gate already uses" "seed 3" ;;
  *) bad "  recorded by the parser the gate already uses" "${out:0:70}" ;; esac

# The whole point: a second sweep explores new ground rather than repeating.
: >"$R/bin/calls"
sw --seeds 5 --max-success 250 >/dev/null 2>&1
check "a second sweep starts where the first stopped" "7 250" "$(head -1 "$R/bin/calls")"
check "  and finds what the first could not reach" 2 "$(rows)"
case "$(cex --list)" in *"Deep { depth: 7 }"*) ok "  the seed-7 case is now in the ledger" "found" ;;
  *) bad "  the seed-7 case is now in the ledger" "missing" ;; esac
check "  the stamp moved with it" 11 "$(stamp to)"

# ----- staleness is named, and fatal only if the repo asks
mkfuzz "3"
sw --seeds 2 >/dev/null 2>&1
check "a sweep on this exact tree is green and says so" 0 "$(rc_of --check)"
case "$(cex --check)" in *"on this exact tree"*) ok "  naming the tree it covered" "said" ;;
  *) bad "  naming the tree it covered" "silent" ;; esac
printf 'x\n' >"$R/moved.txt"; commit moved >/dev/null
printf 'y\n' >"$R/moved2.txt"; commit moved2 >/dev/null
case "$(cex --check)" in *"2 commit(s) ago"*) ok "a sweep two commits back is named as stale" "2" ;;
  *) bad "a sweep two commits back is named as stale" "$(cex --check | head -1)" ;; esac
check "  and staleness alone does not fail the gate" 0 "$(rc_of --check)"
"$FPL_PY" - "$R/.fluxpoint-fuzz.json" <<'PY2'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["failWhenStale"] = True; d["maxStaleCommits"] = 1
json.dump(d, open(p, "w"), indent=2)
PY2
commit strict >/dev/null
check "failWhenStale past the limit is RED" 1 "$(rc_of --check)"
case "$(cex --check 2>&1 >/dev/null)" in *"past the 1 this repo allows"*)
  ok "  and names the limit" "said" ;;
  *) bad "  and names the limit" "silent" ;; esac

# ----- a config nobody can read is not a pass
mkrepo; printf '{not json\n' >"$R/.fluxpoint-fuzz.json"; commit bad >/dev/null
check "a malformed fuzz config is RED, never silent" 1 "$(rc_of --check)"
mkrepo; printf '{ "version": 1, "tool": "nosuch" }\n' >"$R/.fluxpoint-fuzz.json"; commit t >/dev/null
check "a tool with no sweep command is RED" 1 "$(rc_of --check)"
case "$(cex --check 2>&1 >/dev/null)" in *"explicit 'command'"*) ok "  and says what to give it" "said" ;;
  *) bad "  and says what to give it" "silent" ;; esac

# ================= 14. a sixth prover: proptest, Rust property tests ======
# Captured from a real run, 2026-11-11: proptest 1.12.0 under cargo 1.97.0
# on the crate below, `PROPTEST_RNG_SEED=2 cargo test --workspace --quiet`,
# exit 101, with RUST_BACKTRACE=0. Three property failures, one plain
# assertion failure and one passing property; thread ids and the crate path
# (edited to /work/ledger) aside, nothing is changed. The replay, plain,
# green, --nocapture and build-error fixtures are runs of the same crate.
#
# Like vitest, a cargo test run mixes property failures with ordinary ones,
# and only the first kind carries a shrunk input. proptest prints that input
# with pretty Debug, so a vector spans lines; and it is the second tool
# whose strings are data, so two spaces must stay two.
mkpt() {
  mkrepo proptest
  mkdir -p "$R/src" "$R/tests"
  printf '[package]\nname = "ledger"\nversion = "0.1.0"\nedition = "2021"\n\n[dev-dependencies]\nproptest = "1"\n' >"$R/Cargo.toml"
  cat >"$R/src/lib.rs" <<'EOF'
pub fn fee(amount: u64, bps: u64) -> u64 {
    // Bug: the divisor should be 10_000.
    amount * bps / 9_000
}

pub fn encode(name: &str, tags: &[u8]) -> Vec<u8> {
    let mut out = vec![name.len() as u8];
    out.extend_from_slice(name.as_bytes());
    out.extend_from_slice(tags);
    out
}

pub fn decode(bytes: &[u8]) -> (String, Vec<u8>) {
    let n = bytes[0] as usize;
    let name = String::from_utf8_lossy(&bytes[1..1 + n]).into_owned();
    // Bug: a tag byte of 0 ends the list.
    let tags = bytes[1 + n..].iter().copied().take_while(|b| *b != 0).collect();
    (name, tags)
}

pub fn normalise(name: &str) -> String {
    // Bug: runs of spaces are kept.
    name.trim_start().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        #[test]
        fn fee_never_exceeds_amount(amount in 0u64..1_000_000, bps in 0u64..=10_000) {
            prop_assert!(fee(amount, bps) <= amount);
        }

        #[test]
        fn name_roundtrips(name in "[a-z]{0,8}", tags in proptest::collection::vec(any::<u8>(), 0..4)) {
            prop_assert_eq!(decode(&encode(&name, &tags)), (name.clone(), tags.clone()));
        }

        #[test]
        fn no_run_of_spaces_survives(name in "[a-z ]{0,8}") {
            prop_assert!(!normalise(&name).contains("  "), "a run of spaces survived");
        }

        #[test]
        fn widening_is_lossless(x in any::<u8>()) {
            prop_assert!(u16::from(x) < 256);
        }
    }

    #[test]
    fn full_rate_is_the_whole_amount() {
        assert_eq!(fee(9_000, 10_000), 9_000);
    }
}
EOF
  git -C "$R" add -A; git -C "$R" -c user.email=t@t -c user.name=t commit -qm proptest
}
pt_fail() {
  cat <<'EOF'

running 5 tests
tests::full_rate_is_the_whole_amount --- FAILED
tests::fee_never_exceeds_amount --- FAILED
tests::no_run_of_spaces_survives --- FAILED
tests::name_roundtrips --- FAILED
.
failures:

---- tests::full_rate_is_the_whole_amount stdout ----

thread 'tests::full_rate_is_the_whole_amount' (19633) panicked at src/lib.rs:55:9:
assertion `left == right` failed
  left: 10000
 right: 9000
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

---- tests::fee_never_exceeds_amount stdout ----
proptest: Saving this and future failures in /work/ledger/proptest-regressions/lib.txt
proptest: If this test was run on a CI system, you may wish to add the following line to your copy of the file. (You may need to create it.)
cc 53a42655cb4af2951240af1a0272c9b27ec4835ae438aaa2248c6885102423b6

thread 'tests::fee_never_exceeds_amount' (19632) panicked at src/lib.rs:31:5:
Test failed: assertion failed: fee(amount, bps) <= amount at src/lib.rs:34.
minimal failing input: amount = 180, bps = 9050
	successes: 13
	local rejects: 0
	global rejects: 0


---- tests::no_run_of_spaces_survives stdout ----
proptest: Saving this and future failures in /work/ledger/proptest-regressions/lib.txt
proptest: If this test was run on a CI system, you may wish to add the following line to your copy of the file.
cc c7afbae06dc2d03e8eaa5809f97c8645aebfa0eb5f0ae360ce0f75edbc0f821e

thread 'tests::no_run_of_spaces_survives' (19635) panicked at src/lib.rs:31:5:
Test failed: a run of spaces survived at src/lib.rs:44.
minimal failing input: name = "a  "
	successes: 3
	local rejects: 0
	global rejects: 0


---- tests::name_roundtrips stdout ----
proptest: Saving this and future failures in /work/ledger/proptest-regressions/lib.txt
proptest: If this test was run on a CI system, you may wish to add the following line to your copy of the file.
cc 4519e5b1b7b0e51957e4bc3a95e41e71f84c4063899060dca8751fa2ae6303d9

thread 'tests::name_roundtrips' (19634) panicked at src/lib.rs:31:5:
Test failed: assertion failed: `(left == right)` 
  left: `("", [])`,
 right: `("", [0])` at src/lib.rs:39.
minimal failing input: name = "", tags = [
    0,
]
	successes: 52
	local rejects: 0
	global rejects: 0



failures:
    tests::fee_never_exceeds_amount
    tests::full_rate_is_the_whole_amount
    tests::name_roundtrips
    tests::no_run_of_spaces_survives

test result: FAILED. 1 passed; 4 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s

error: test failed, to rerun pass `--lib`
EOF
}
# The second run of the same tree: proptest replays the seeds it saved, so
# the three "Saving" lines are gone and every `successes` is 0. That is the
# only difference in the real capture, and the recorded input is the same.
pt_replay() { pt_fail | sed -e '/^proptest: /d' -e '/^cc [0-9a-f]\{64\}$/d' -e 's/successes: [0-9]*/successes: 0/'; }
# `cargo test --workspace --quiet full_rate`: the one plain failure alone.
pt_plain() {
  cat <<'EOF'

running 1 test
tests::full_rate_is_the_whole_amount --- FAILED

failures:

---- tests::full_rate_is_the_whole_amount stdout ----

thread 'tests::full_rate_is_the_whole_amount' (19284) panicked at src/lib.rs:55:9:
assertion `left == right` failed
  left: 10000
 right: 9000
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    tests::full_rate_is_the_whole_amount

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 4 filtered out; finished in 0.00s

error: test failed, to rerun pass `--lib`
EOF
}
# `cargo test --workspace --quiet widening`: the one passing property alone.
pt_green() {
  cat <<'EOF'

running 1 test
.
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 4 filtered out; finished in 0.01s

EOF
}
# `-- --nocapture --test-threads=1`: libtest prints no `---- name stdout ----`
# blocks, and each panic goes straight to stderr. Stderr first, then stdout.
pt_nocapture() {
  cat <<'EOF'
proptest: Saving this and future failures in /work/ledger/proptest-regressions/lib.txt
proptest: If this test was run on a CI system, you may wish to add the following line to your copy of the file. (You may need to create it.)
cc 53a42655cb4af2951240af1a0272c9b27ec4835ae438aaa2248c6885102423b6

thread 'tests::fee_never_exceeds_amount' (19659) panicked at src/lib.rs:31:5:
Test failed: assertion failed: fee(amount, bps) <= amount at src/lib.rs:34.
minimal failing input: amount = 180, bps = 9050
	successes: 13
	local rejects: 0
	global rejects: 0

note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

thread 'tests::full_rate_is_the_whole_amount' (19660) panicked at src/lib.rs:55:9:
assertion `left == right` failed
  left: 10000
 right: 9000
proptest: Saving this and future failures in /work/ledger/proptest-regressions/lib.txt
proptest: If this test was run on a CI system, you may wish to add the following line to your copy of the file.
cc 4519e5b1b7b0e51957e4bc3a95e41e71f84c4063899060dca8751fa2ae6303d9

thread 'tests::name_roundtrips' (19661) panicked at src/lib.rs:31:5:
Test failed: assertion failed: `(left == right)` 
  left: `("", [])`,
 right: `("", [0])` at src/lib.rs:39.
minimal failing input: name = "", tags = [
    0,
]
	successes: 52
	local rejects: 0
	global rejects: 0


thread 'tests::no_run_of_spaces_survives' (19662) panicked at src/lib.rs:31:5:
Test failed: a run of spaces survived at src/lib.rs:44.
minimal failing input: name = "a  "
	successes: 0
	local rejects: 0
	global rejects: 0

error: test failed, to rerun pass `--lib`

running 5 tests
tests::fee_never_exceeds_amount --- FAILED
tests::full_rate_is_the_whole_amount --- FAILED
tests::name_roundtrips --- FAILED
tests::no_run_of_spaces_survives --- FAILED
.
failures:

failures:
    tests::fee_never_exceeds_amount
    tests::full_rate_is_the_whole_amount
    tests::name_roundtrips
    tests::no_run_of_spaces_survives

test result: FAILED. 1 passed; 4 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s

EOF
}
# The same crate with one line that does not type-check appended.
pt_build() {
  cat <<'EOF'
error[E0308]: mismatched types
  --> src/lib.rs:58:25
   |
58 | pub fn broken() -> u8 { "x" }
   |                    --   ^^^ expected `u8`, found `&str`
   |                    |
   |                    expected `u8` because of return type

For more information about this error, try `rustc --explain E0308`.
error: could not compile `ledger` (lib test) due to 1 previous error
EOF
}
ptcex() { cex --ingest --tool proptest "$@"; }
ptid() { # $1 = a substring of the test path
  "$FPL_PY" - "$R/.fluxpoint-cex.jsonl" "$1" <<'PY2'
import json, sys
for l in open(sys.argv[1]):
    r = json.loads(l)
    if r["tool"] == "proptest" and sys.argv[2] in r["title"]: print(r["cexId"]); break
PY2
}

mkpt
pt_green | ptcex --exit 0 >/dev/null 2>&1
check "a green cargo test run records nothing" 0 "$(rows)"
pt_fail | ptcex --exit 0 >/dev/null 2>&1
check "exit 0 records nothing whatever was printed" 0 "$(rows)"
pt_fail | ptcex --exit 101 >/dev/null 2>&1
check "three property failures record three counterexamples" 3 "$(rows)"
out="$(cex --list)"
case "$out" in *"[proptest] src/lib.rs:tests::fee_never_exceeds_amount"*)
  ok "a row names the tool, the file and the test path" "selector" ;;
  *) bad "a row names the tool, the file and the test path" "${out:0:80}" ;; esac
case "$out" in *full_rate_is_the_whole_amount*) bad "the plain assertion failure is not in the ledger" "recorded" ;;
  *) ok "the plain assertion failure is not in the ledger" "absent" ;; esac

"$FPL_PY" - "$R/.fluxpoint-cex.jsonl" <<'PY2' && ok "the shrunk input, the reason and the saved seed are recorded" "verbatim" \
  || bad "the shrunk input, the reason and the saved seed are recorded" "wrong payload"
import json, sys
rows = {json.loads(l)["title"]: json.loads(l) for l in open(sys.argv[1])}
f = rows["tests::fee_never_exceeds_amount"]
n = rows["tests::name_roundtrips"]
s = rows["tests::no_run_of_spaces_survives"]
assert f["input"] == "amount = 180, bps = 9050", f["input"]
assert f["kind"] == "property" and f["inputForm"] == "rust-debug" and f["signature"] == "counterexample", f
assert f["iterations"] == 13, f["iterations"]
assert "fee(amount, bps) <= amount" in f["assertion"] and "src/lib.rs:34" in f["assertion"], f["assertion"]
assert f["persistedSeed"] == "cc 53a42655cb4af2951240af1a0272c9b27ec4835ae438aaa2248c6885102423b6", f.get("persistedSeed")
assert f["module"] == "src/lib.rs" and f["selector"] == "src/lib.rs:tests::fee_never_exceeds_amount", f
# Pretty Debug spans lines; the recorded value is the one-line form proptest
# itself writes into proptest-regressions.
assert n["input"] == 'name = "", tags = [0]', repr(n["input"])
assert "left == right" in n["assertion"] and "src/lib.rs:39" in n["assertion"], n["assertion"]
# NOT whitespace-collapsed inside the string: two spaces are the failing case.
assert s["input"] == 'name = "a  "', repr(s["input"])
assert s["assertion"].startswith("a run of spaces survived"), s["assertion"]
PY2
pt_fail | ptcex --exit 101 >/dev/null 2>&1
check "re-ingesting the same run appends nothing" 3 "$(rows)"
# The replay prints no seed and counts no successes. If either were part of
# the identity, the same bug would be a new row on every run after the first.
pt_replay | ptcex --exit 101 >/dev/null 2>&1
check "the replayed run of the same failures appends nothing" 3 "$(rows)"

mkpt
pt_plain | ptcex --exit 101 >/dev/null 2>&1
check "a plain assertion failure is not a counterexample" 0 "$(rows)"
check "and files nothing" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"

mkpt
pt_nocapture | ptcex --exit 101 >/dev/null 2>&1
check "uncaptured output carries the same three counterexamples" 3 "$(rows)"

# ----- what a proptest pin has to survive
ptpin() { # $1 = attribute lines above #[test] (may be empty), $2 = body
  printf 'use ledger::*;\n\n%s#[test]\nfn %s() {\n  %s\n}\n' "$1" "$PID" "$2" >"$R/tests/reg.rs"
  commit p >/dev/null
}
mkpt; pt_fail | ptcex --exit 101 >/dev/null 2>&1; PID="$(ptid fee_never)"
cex --pin "$PID" >/dev/null 2>&1
[ -f "$R/.claude/fluxpoint/cex/$PID.rs.draft" ] \
  && ok "the draft is a Rust test" "rs" || bad "the draft is a Rust test" "missing"
case "$(cat "$R/.claude/fluxpoint/cex/$PID.rs.draft" 2>/dev/null)" in *"in order: 180, 9050"*"proptest-regressions"*|*"proptest-regressions"*"in order: 180, 9050"*)
  ok "and names the literals and where proptest keeps its own seeds" "named" ;;
  *) bad "and names the literals and where proptest keeps its own seeds" "silent" ;; esac
ptpin '' 'assert!(fee(180, 9050) <= 180);'
check "a tracked #[test] carrying both values in order pins" 0 \
  "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
check "and --check is green" 0 "$(rc_of --check)"

mkpt; pt_fail | ptcex --exit 101 >/dev/null 2>&1; PID="$(ptid fee_never)"
ptpin '' 'assert!(fee(181, 9050) <= 181);'
check "a retyped value is refused" 1 "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
ptpin '' 'assert!(fee(9050, 180) <= 180);'
check "the values out of order are refused" 1 "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
ptpin $'#[ignore = "slow"]\n' 'assert!(fee(180, 9050) <= 180);'
check "#[ignore] with a reason is refused (it never runs)" 1 \
  "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
ptpin $'#[should_panic]\n' 'assert!(fee(180, 9050) <= 180);'
check "#[should_panic] is refused (it inverts the oracle)" 1 \
  "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
printf 'use ledger::*;\n\nfn %s() {\n  assert!(fee(180, 9050) <= 180);\n}\n' "$PID" >"$R/tests/reg.rs"; commit plain >/dev/null
check "a plain fn that no test build collects is refused" 1 \
  "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
ptpin '' 'assert!(true);'
check "an assert!(true) body is refused" 1 "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
ptpin '' '// fee(180, 9050)
  assert!(fee(1, 1) <= 1);'
check "the values in a COMMENT are refused" 1 "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
printf 'it("%s regression", () => { expect(fee(180, 9050)).toBe(180); });\n' "$PID" >"$R/tests/reg.test.ts"; commit ts >/dev/null
check "a pin in another tool's language is refused" 1 \
  "$(rc_of --pin "$PID" --file tests/reg.test.ts --test-name "$PID")"

# A string is data: the failing name has two spaces, and one is another value.
mkpt; pt_fail | ptcex --exit 101 >/dev/null 2>&1; PID="$(ptid no_run_of_spaces)"
ptpin '' 'assert!(!normalise("a  ").contains("  "));'
check "a test carrying the exact string pins" 0 "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"
mkpt; pt_fail | ptcex --exit 101 >/dev/null 2>&1; PID="$(ptid no_run_of_spaces)"
ptpin '' 'assert!(!normalise("a ").contains("  "));'
check "the same string retyped shorter is refused" 1 "$(rc_of --pin "$PID" --file tests/reg.rs --test-name "$PID")"

# ----- --check: red on real weakening
mkpt; pt_fail | ptcex --exit 101 >/dev/null 2>&1; PID="$(ptid fee_never)"
ptpin '' 'assert!(fee(180, 9050) <= 180);'
cex --pin "$PID" --file tests/reg.rs --test-name "$PID" >/dev/null 2>&1
git -C "$R" rm -q tests/reg.rs; commit rm >/dev/null
check "a deleted proptest regression is red" 1 "$(rc_of --check)"

# ----- drift is loud and never red
# A failure the parser cannot attach to a test: the input line is there and
# the frame around it is not the one proptest 1.2+ prints under rustc 1.73+.
mkpt
check "a failing input in a frame this parser does not know exits 0" 0 \
  "$(pt_fail | sed '0,/^Test failed: /s//Test falsified: /' | ptcex --exit 101 >/dev/null 2>&1; echo $?)"
check "and files INGEST-FAILED" 1 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
out="$("$FPL_PY" "$INBOX" --root "$R" --list 2>/dev/null || cat "$R/.claude/fluxpoint/inbox.jsonl")"
case "$out" in *"proptest"*) ok "the inbox row names the tool that drifted" "named" ;;
  *) bad "the inbox row names the tool that drifted" "${out:0:60}" ;; esac
check "and nothing half-read is recorded" 0 "$(rows)"
mkpt
pt_build | ptcex --exit 101 >/dev/null 2>&1
check "a build error records nothing" 0 "$(rows)"
check "and files nothing: no test ran" 0 "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkpt
printf 'some other runner said something else entirely\n' | ptcex --exit 1 >/dev/null 2>&1
check "output this parser does not know is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"
mkpt
: | ptcex --exit 101 >/dev/null 2>&1
check "a failing run that printed nothing is a parser break" 1 \
  "$("$FPL_PY" "$INBOX" --root "$R" --count)"

# ----- the wide sweep rotates proptest's own seed and case count
mkpt
mkdir -p "$R/bin"
cat >"$R/bin/cargo" <<'EOF'
#!/usr/bin/env bash
printf '%s %s %s\n' "${PROPTEST_RNG_SEED:-unset}" "${PROPTEST_CASES:-unset}" "$*" >>"$(dirname "$0")/calls"
exit 0
EOF
chmod +x "$R/bin/cargo"
printf '{ "version": 1, "tool": "proptest" }\n' >"$R/.fluxpoint-fuzz.json"
commit fuzz >/dev/null
check "a proptest sweep runs and exits 0" 0 \
  "$( ( cd "$R" && PATH="$R/bin:$PATH" "$FPL_PY" "$CEX" --root "$R" --sweep --seeds 2 --max-success 500 ) >/dev/null 2>&1; echo $?)"
check "each run gets the next seed and the raised case count" \
  "2 500 test --workspace --quiet|3 500 test --workspace --quiet" "$(paste -sd'|' "$R/bin/calls")"

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
