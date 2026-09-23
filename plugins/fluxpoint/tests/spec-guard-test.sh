#!/usr/bin/env bash
# Statement ratchet: what is being proved may not quietly get weaker.
#
# proof-guard covers the proof body; every case here is a weakening it
# cannot see — a conjunct dropped from an `ensures`, a property test
# deleted, a `fail` test flipped to a normal one, a generator narrowed.
# Each keeps the hatch counts flat and every checker exiting 0, which is
# exactly why this ratchet exists.
#
# The other half of the property matters as much: the cases that must NOT
# be red. Reformatting, reordering clauses, moving a file, adding an
# obligation, and a change justified by a Decisions row all stay green — a
# ratchet that cries wolf gets re-baselined blind, which is worse than not
# having one.
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
SG="$PLUGIN/scripts/spec-guard.py"
PG="$PLUGIN/scripts/proof-guard.py"
ROOT="$(mktemp -d)"
pass=0; fail=0

ok()  { printf 'PASS  %-56s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-56s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

sg() { "$FPL_PY" "$SG" --root "$ROOT/r" "$@"; }
rc_of() { sg "$@" >/dev/null 2>&1; echo $?; }

mkrepo() {
  rm -rf "$ROOT/r"; mkdir -p "$ROOT/r/validators" "$ROOT/r/src"
  cd "$ROOT/r" || exit 1
  git init -q -b main
}
commit() { git add -A; git -c user.email=t@t -c user.name=t commit -qm "${1:-x}"; }

write_aiken() {
  cat >validators/vault.ak <<'EOF'
validator {
  fn spend(datum: Data, redeemer: Data, ctx: ScriptContext) -> Bool {
    owner_signed(datum, ctx) && no_double_satisfaction(ctx)
  }
}

test spend_allows_owner() {
  spend(mk_datum(1000), Void, mk_ctx(owner: True))
}

test double_satisfaction_is_rejected(n: Int via bounded_int(1, 99)) {
  !spend(mk_datum(n), Void, mk_ctx(two_scripts: True))
}

test spend_rejects_stranger() fail {
  spend(mk_datum(1000), Void, mk_ctx(owner: False))
}
EOF
}

write_dafny() {
  cat >src/vault.dfy <<'EOF'
method Withdraw(bal: int, amt: int) returns (out: int)
  requires amt > 0
  requires amt <= bal
  ensures out == bal - amt
  ensures out >= 0
{
  out := bal - amt;
}
EOF
}

# ================= 1. dormant, then armed =================================
mkrepo; printf 'print("hi")\n' >app.py; commit
check "no proof files: --check is silent and green" 0 "$(rc_of --check)"

mkrepo; write_aiken; write_dafny; commit
out="$(sg --check)"
case "$out" in *"NOT armed"*) ok "obligations present but unarmed says so" "said" ;;
  *) bad "obligations present but unarmed says so" "silent: ${out:0:40}" ;; esac
check "and does not block bootstrapping" 0 "$(rc_of --check)"

out="$(sg --scan)"
n="$(printf '%s' "$out" | grep -c '^  [ad]')"
check "scan finds every Aiken test and the Dafny method" 4 "$n"
case "$out" in *"double_satisfaction_is_rejected"*)
  ok "the property test is an obligation" "found" ;;
  *) bad "the property test is an obligation" "missing" ;; esac

sg --baseline >/dev/null
check "armed: a fresh baseline is green" 0 "$(rc_of --check)"

# ================= 2. the weakenings a hatch count cannot see =============
base_state() { mkrepo; write_aiken; write_dafny; commit; sg --baseline >/dev/null; }

# A conjunct dropped from a Dafny postcondition.
base_state
"$FPL_PY" - <<'PY'
import re
p = "src/vault.dfy"
s = open(p).read().replace("  ensures out >= 0\n", "")
open(p, "w").write(s)
PY
check "a dropped ensures conjunct is red" 1 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *CHANGED*) ok "and is reported as CHANGED" "named" ;;
  *) bad "and is reported as CHANGED" "not named" ;; esac

# A widened precondition — the hard case is now out of scope.
base_state
sed -i 's/requires amt <= bal/requires amt <= bal + 1/' src/vault.dfy
check "a widened requires is red" 1 "$(rc_of --check)"

# A deleted property test. Nothing else in the system sees this.
base_state
"$FPL_PY" - <<'PY'
p = "validators/vault.ak"
s = open(p).read()
i = s.index("test double_satisfaction_is_rejected")
j = s.index("test spend_rejects_stranger")
open(p, "w").write(s[:i] + s[j:])
PY
check "a deleted property test is red" 1 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *REMOVED*) ok "and is reported as REMOVED" "named" ;;
  *) bad "and is reported as REMOVED" "not named" ;; esac

# A renamed test: the obligation nothing points at any more.
base_state
sed -i 's/test double_satisfaction_is_rejected/test ds_check/' validators/vault.ak
check "a renamed obligation is red" 1 "$(rc_of --check)"

# A negative test flipped positive asserts the opposite of what it did.
base_state
sed -i 's/test spend_rejects_stranger() fail {/test spend_rejects_stranger() {/' \
  validators/vault.ak
check "flipping a fail test is red" 1 "$(rc_of --check)"

# A narrowed generator checks less ground under the same name.
base_state
sed -i 's/bounded_int(1, 99)/bounded_int(1, 2)/' validators/vault.ak
check "a narrowed fuzzer is red" 1 "$(rc_of --check)"

# The whole specification stripped off a method.
base_state
"$FPL_PY" - <<'PY'
p = "src/vault.dfy"
s = "\n".join(l for l in open(p).read().splitlines()
              if not l.strip().startswith(("requires", "ensures")))
open(p, "w").write(s + "\n")
PY
check "a method that lost its entire spec is red" 1 "$(rc_of --check)"

# ================= 3. what must stay green ================================
base_state
sed -i 's/  ensures out == bal - amt/  ensures  out  ==  bal - amt/' src/vault.dfy
check "reformatting is not weakening" 0 "$(rc_of --check)"

base_state
"$FPL_PY" - <<'PY'
p = "src/vault.dfy"
s = open(p).read()
s = s.replace("  requires amt > 0\n  requires amt <= bal\n",
              "  requires amt <= bal\n  requires amt > 0\n")
open(p, "w").write(s)
PY
check "reordering clauses is not weakening" 0 "$(rc_of --check)"

base_state
printf '\ntest another_property(n: Int via bounded_int(1, 9)) {\n  True == True\n}\n' \
  >>validators/vault.ak
check "adding an obligation is always allowed" 0 "$(rc_of --check)"
case "$(sg --check)" in *"1 obligation(s) added"*) ok "and the addition is reported" "said" ;;
  *) bad "and the addition is reported" "silent" ;; esac

# Moving a file relocates every obligation in it without changing any.
base_state
mkdir -p validators/nested && git mv validators/vault.ak validators/nested/vault.ak
check "moving a file is not weakening" 0 "$(rc_of --check)"
case "$(sg --check)" in *moved*) ok "the move is reported as a move" "said" ;;
  *) bad "the move is reported as a move" "silent" ;; esac

# The in-band justification: a Decisions row naming the obligation id.
base_state
sed -i 's/requires amt <= bal/requires amt <= bal + 1/' src/vault.dfy
cat >WORK.md <<'EOF'
# work

## Decisions

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|
| 2026-08-08 01:00 | widen-withdraw-precondition | allow the boundary case | YES | none | dafny:src/vault.dfy:Withdraw is intentionally widened; the caller now enforces the bound and the old clause double-counted it |
EOF
check "a weakening a Decisions row names is allowed" 0 "$(rc_of --check)"
case "$(sg --check)" in *"a Decisions row names it"*) ok "and the justification is echoed" "echoed" ;;
  *) bad "and the justification is echoed" "silent" ;; esac

# A Decisions row about something else must not launder it.
sed -i 's/dafny:src\/vault.dfy:Withdraw/some other unrelated decision/' WORK.md
check "an unrelated Decisions row does not launder it" 1 "$(rc_of --check)"

# ================= 4. the two ratchets share one file =====================
base_state
"$FPL_PY" "$PG" --root "$ROOT/r" --baseline >/dev/null
has_spec() { "$FPL_PY" - "$ROOT/r/.fluxpoint-proof-baseline.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("yes" if d.get("spec", {}).get("obligations") else "no")
PY
}
check "re-arming proof-guard preserves the spec section" "yes" "$(has_spec)"
"$FPL_PY" "$SG" --root "$ROOT/r" --baseline >/dev/null
has_counts() { "$FPL_PY" - "$ROOT/r/.fluxpoint-proof-baseline.json" <<'PY'
import json, sys
print("yes" if "counts" in json.load(open(sys.argv[1])) else "no")
PY
}
check "and re-arming spec-guard preserves the counts" "yes" "$(has_counts)"
check "both ratchets still green afterwards" 0 "$(rc_of --check)"

# ================= 5. languages it does not parse say so ==================
mkrepo; write_aiken
printf 'postulate foo : Set\n' >src/thm.agda
commit; sg --baseline >/dev/null
out="$(sg --check)"
case "$out" in *"NOT covered"*Agda*) ok "a tracked Agda file is named as uncovered" "named" ;;
  *) bad "a tracked Agda file is named as uncovered" "${out:0:50}" ;; esac
check "and does not fail the gate on its own" 0 "$(rc_of --check)"

# ================= 6. Lean, Coq, Isabelle, TLA+ and Kani are covered =====
# Each language gets the same four probes the Aiken and Dafny sections use:
# the statement is found, a weakened statement is CHANGED, a deleted one is
# REMOVED, and a comment or reformat stays green. The fixtures carry a
# commented-out declaration so a scanner reading comments would over-count.
write_lean() {
  mkdir -p src; cat >src/Foo.lean <<'EOF'
/-- The doc comment mentions theorem inside_doc : False, which is prose. -/
theorem add_zero' (n : Nat) : n + 0 = n := by
  simp
-- theorem commented_out : False := sorry
@[simp] lemma two_mul' (n : Nat) : 2 * n = n + n :=
  by omega
theorem cases_ex : ∀ n : Nat, n = n
  | 0 => rfl
  | n + 1 => rfl
def notAnObligation : Nat := 3
EOF
}
write_coq() {
  mkdir -p src; cat >src/Bar.v <<'EOF'
(* Lemma inside_comment : False. *)
Theorem plus_O_n : forall n : nat, 0 + n = n.
Proof. intros n. reflexivity. Qed.
Lemma with_dot_in_name : Nat.add 1 1 = 2.
Proof. reflexivity. Qed.
Definition not_one := 1.
EOF
}
write_isabelle() {
  mkdir -p src; cat >src/Baz.thy <<'EOF'
theory Baz imports Main begin
lemma add_comm': "a + b = (b::nat) + a" by simp
theorem long_one [simp]:
  assumes "x > (0::nat)"
  shows "x + 1 > 1"
  using assms by simp
(* lemma commented: "False" *)
definition foo :: nat where "foo = 1"
lemma in_cart: ‹foo = 1› unfolding foo_def by simp
end
EOF
}
write_tla() {
  mkdir -p specs; cat >specs/Spec.tla <<'EOF'
---- MODULE Spec ----
EXTENDS Naturals
VARIABLE x
Init == x = 0
Next == x' = x + 1
Inv ==
  /\ x >= 0
  /\ x < 100
(* THEOREM Commented == FALSE *)
THEOREM Safety == Spec => []Inv
PROOF OMITTED
Spec == Init /\ [][Next]_x
====
EOF
  cat >specs/Spec.cfg <<'EOF'
SPECIFICATION Spec
INVARIANT Inv
\* PROPERTY Nope
EOF
  printf '[metadata]\nname = x\n' >setup.cfg
}
write_kani() {
  mkdir -p src; cat >src/lib.rs <<'EOF'
// #[kani::proof] fn commented() {}
#[cfg(kani)]
mod verification {
    #[kani::proof]
    #[kani::unwind(3)]
    fn check_add() {
        let a: u8 = kani::any();
        assert!(a.wrapping_add(0) == a);
    }
}
#[kani::requires(x > 0)]
#[kani::ensures(|r| *r > x)]
pub fn bump(x: u32) -> u32 { x + 1 }
fn plain() {}
EOF
}
poly_state() { mkrepo; write_lean; write_coq; write_isabelle; write_tla; write_kani; commit; }

poly_state
out="$(sg --scan)"
for want in "lean:src/Foo.lean:add_zero'" "lean:src/Foo.lean:cases_ex" \
            "coq:src/Bar.v:plus_O_n" "coq:src/Bar.v:with_dot_in_name" \
            "isabelle:src/Baz.thy:long_one" "isabelle:src/Baz.thy:in_cart" \
            "tla:specs/Spec.tla:Safety" "tla:specs/Spec.cfg:Inv" \
            "kani:src/lib.rs:check_add" "kani:src/lib.rs:bump"; do
  case "$out" in *"$want"*) ok "scan finds $want" "found" ;; *) bad "scan finds $want" "missing" ;; esac
done
for absent in commented_out inside_comment "inside_doc" "Commented" "commented()" \
              "Nope" "notAnObligation" "not_one" "plain" "setup.cfg"; do
  case "$out" in *"$absent"*) bad "and does not read $absent" "counted" ;;
    *) ok "and does not read $absent" "ignored" ;; esac
done
case "$out" in *"INVARIANT Inv == /\\ x >= 0 /\\ x < 100"*)
  ok "a TLC invariant is bound to the operator it checks" "bound" ;;
  *) bad "a TLC invariant is bound to the operator it checks" "unbound" ;; esac
case "$out" in *"NOT COVERED"*) bad "nothing is reported uncovered" "reported" ;;
  *) ok "nothing is reported uncovered" "clean" ;; esac
sg --baseline >/dev/null
check "armed over five languages: green" 0 "$(rc_of --check)"

# A Dafny declaration carrying attributes between the keyword and the name
# (`lemma {:axiom} Helper`, `method {:verify false} Skipped`) is its own
# obligation; a scanner that missed the attributes folded their clauses into
# the previous method, which a real file showed.
mkrepo; mkdir -p src; cat >src/vault.dfy <<'EOF'
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
commit
out="$(sg --scan)"
case "$out" in *"dafny:src/vault.dfy:Helper"*"dafny:src/vault.dfy:Skipped"*"dafny:src/vault.dfy:UsesAssume"*)
  ok "Dafny declarations carrying attributes are their own obligations" "found" ;;
  *) bad "Dafny declarations carrying attributes are their own obligations" "${out:0:60}" ;; esac
case "$out" in *"Withdraw"*"x * x"*) bad "and their clauses do not fold into the previous method" "folded" ;;
  *) ok "and their clauses do not fold into the previous method" "separate" ;; esac

weaken() {  # name, file, sed-expr, expected-word
  poly_state; sg --baseline >/dev/null
  sed -i "$3" "$2"
  check "$1 is red" 1 "$(rc_of --check)"
  case "$(sg --check 2>&1)" in *"$4"*) ok "  and reported as $4" "named" ;;
    *) bad "  and reported as $4" "not named" ;; esac
}
weaken "Lean: a weakened theorem" src/Foo.lean 's/n + 0 = n/n + 0 = n + 0/' CHANGED
weaken "Lean: a deleted theorem" src/Foo.lean '/two_mul/,/omega/d' REMOVED
weaken "Coq: a weakened lemma" src/Bar.v 's/0 + n = n\./0 + n = 0 + n./' CHANGED
weaken "Coq: a deleted theorem" src/Bar.v '/plus_O_n/,/Qed/d' REMOVED
weaken "Isabelle: a weakened lemma" src/Baz.thy 's/shows "x + 1 > 1"/shows "x + 1 > 0"/' CHANGED
weaken "Isabelle: a deleted lemma" src/Baz.thy '/in_cart/d' REMOVED
weaken "TLA+: a weakened invariant body" specs/Spec.tla 's/x < 100/x < 1000/' CHANGED
weaken "TLA+: an INVARIANT dropped from the .cfg" specs/Spec.cfg '/INVARIANT/d' REMOVED
weaken "TLA+: a weakened THEOREM" specs/Spec.tla 's/Spec => \[\]Inv/Spec => Inv/' CHANGED
weaken "Kani: a loosened unwind bound" src/lib.rs 's/unwind(3)/unwind(1)/' CHANGED
weaken "Kani: a dropped contract" src/lib.rs '/kani::ensures/d' CHANGED
weaken "Kani: a deleted harness" src/lib.rs '/#\[kani::proof\]/,/^    }/d' REMOVED

poly_state; sg --baseline >/dev/null
sed -i 's/^theorem add_zero. (n : Nat) : n + 0 = n := by/theorem add_zero'"'"'  (n : Nat)  :  n + 0 = n  := by/' src/Foo.lean
sed -i 's/^-- theorem commented_out.*/-- a different comment/' src/Foo.lean
sed -i 's/(\* Lemma inside_comment : False. \*)/(* nothing *)/' src/Bar.v
sed -i 's/(\* THEOREM Commented == FALSE \*)/(* still a comment *)/' specs/Spec.tla
sed -i 's|^// #\[kani::proof\] fn commented() {}|// another comment|' src/lib.rs
check "reformatting and comment edits stay green across languages" 0 "$(rc_of --check)"
printf '\ntheorem extra (n : Nat) : n = n := rfl\n' >>src/Foo.lean
check "adding a Lean theorem is allowed" 0 "$(rc_of --check)"

# ================= 7. a DoD line that cites an obligation is a claim ======
base_state
cat >WORK.md <<'EOF'
# work

## Definition of Done
- [x] withdraw never overdraws — proof: dafny:src/vault.dfy:Withdraw
- [ ] settle on preview — proof: tx hash
- [ ] later — proof: dafny:src/vault.dfy:NotYetWritten

## Decisions
EOF
check "a checked box citing a live obligation is green" 0 "$(rc_of --check)"
check "an unchecked box citing a missing one is not a claim" 0 "$(rc_of --check)"
sed -i 's/- \[ \] later/- [x] later/' WORK.md
check "checking a box whose obligation does not exist is red" 1 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *NotYetWritten*"DoD CLAIM"*) ok "and names the missing id" "named" ;;
  *) bad "and names the missing id" "silent" ;; esac
sed -i 's/- \[x\] later/- [ ] later/' WORK.md
sed -i 's/  ensures out >= 0\n//' src/vault.dfy
"$FPL_PY" - <<'PY'
p = "src/vault.dfy"
open(p, "w").write(open(p).read().replace("  ensures out >= 0\n", ""))
PY
check "a checked box over a weakened obligation is red (CHANGED)" 1 "$(rc_of --check)"

# ================= 8. the attack taxonomy: unspecified is red =============
TAX="$PLUGIN/templates/attack-taxonomy.json"
# The class ids of one language in the shipped template, read from the file
# both taxonomies live in, so no count below is written down twice.
tax_ids() {
  "$FPL_PY" - "$TAX" "$1" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
for tax in doc.get("taxonomies") or [doc]:
    if tax.get("language", "aiken") == sys.argv[2]:
        print(" ".join(c["id"] for c in tax["classes"]))
PY
}
tax_n() { set -- $(tax_ids "$1"); echo $#; }
waive() {  # language, class id, reason — into that language's own taxonomy
  "$FPL_PY" - "$1" "$2" "$3" <<'PY'
import json, sys
p = ".fluxpoint-attacks.json"
doc = json.load(open(p))
for tax in doc.get("taxonomies") or [doc]:
    if tax.get("language", "aiken") == sys.argv[1]:
        tax.setdefault("waived", {})[sys.argv[2]] = sys.argv[3]
json.dump(doc, open(p, "w"), indent=2)
PY
}
n_aiken="$(tax_n aiken)"
check "the shipped taxonomy names eight eUTxO classes" 8 "$n_aiken"

base_state; cp "$TAX" .fluxpoint-attacks.json
check "an Aiken repo with the manifest and no attack tests is red" 1 "$(rc_of --check)"
out="$(sg --check 2>&1)"
case "$out" in *"UNSPECIFIED"*attack_double_satisfaction*"One output cannot pay"*)
  ok "each class is named with the property to state" "named" ;;
  *) bad "each class is named with the property to state" "${out:0:60}" ;; esac
n_red="$(printf '%s' "$out" | grep -c 'attack:aiken:attack_')"
check "every class is its own finding" "$n_aiken" "$n_red"
case "$(sg --scan)" in *"attack class aiken:attack_datum_hijack: UNSPECIFIED"*)
  ok "--scan reports the taxonomy state" "reported" ;;
  *) bad "--scan reports the taxonomy state" "silent" ;; esac

write_attacks() {  # every class as a property test over a generator
  for cls in $(tax_ids aiken); do
    printf '\ntest %s(n: Int via bounded_int(1, 99)) {\n  !spend(mk_datum(n), Void, mk_ctx(attack: True))\n}\n' "$cls" >>validators/vault.ak
  done
}
write_attacks
check "with a property test per class it is green" 0 "$(rc_of --check)"
sg --baseline >/dev/null
sed -i 's/test attack_foreign_utxo(n: Int via bounded_int(1, 99))/test attack_foreign_utxo()/' validators/vault.ak
case "$(sg --check 2>&1)" in *"attack_foreign_utxo is a unit test"*) ok "a unit test under a class name is noted" "noted" ;;
  *) bad "a unit test under a class name is noted" "silent" ;; esac

base_state; cp "$TAX" .fluxpoint-attacks.json; write_attacks
sed -i '/test attack_unbounded_validity/,/^}/d' validators/vault.ak
waive aiken attack_unbounded_validity "n/a"
check "a waiver without a real reason is red" 1 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"WAIVED WITHOUT A REASON"*) ok "and says so" "said" ;; *) bad "and says so" "silent" ;; esac
waive aiken attack_unbounded_validity "this validator has no time-dependent logic at all; the range is never read"
check "a waiver with a reason is green" 0 "$(rc_of --check)"
case "$(sg --check)" in *"attack_unbounded_validity waived"*) ok "and the waiver is echoed" "echoed" ;;
  *) bad "and the waiver is echoed" "silent" ;; esac

printf '{not json' >.fluxpoint-attacks.json
check "a malformed manifest is red, never a pass" 1 "$(rc_of --check)"
mkrepo; printf 'print(1)\n' >app.py; cp "$TAX" .fluxpoint-attacks.json; commit
check "the manifest in a repo with no Aiken is green with a note" 0 "$(rc_of --check)"
case "$(sg --check)" in *"gates nothing"*) ok "  and the note says it gates nothing" "said" ;;
  *) bad "  and the note says it gates nothing" "silent" ;; esac

# ================= 9. the taxonomy reaches the off-chain builder ==========
# Same manifest, same waiver rule, a second language. An agent never calls
# the validator; it calls the builder, so the builder has its own class list.
# What counts as "specified" differs by language: an Aiken test of that name
# on-chain, a TypeScript test title or declaration off-chain. Each taxonomy
# gates only a repo that carries its language, and a waiver excuses a class
# in the taxonomy it is written in and nowhere else.
n_ts="$(tax_n typescript)"
check "and seven off-chain builder classes" 7 "$n_ts"

write_ts() {  # a builder with one ordinary test and no attack tests
  mkdir -p test src
  printf 'export const build = (n: number): number => n;\n' >src/build.ts
  cat >test/build.test.ts <<'EOF'
import { describe, it, expect } from "vitest";
import { build } from "../src/build";

describe("build", () => {
  it("returns what it was given", () => {
    expect(build(1)).toBe(1);
  });
});
EOF
}
ts_state()   { mkrepo; write_ts; cp "$TAX" .fluxpoint-attacks.json; commit; }
both_state() { mkrepo; write_aiken; write_ts; cp "$TAX" .fluxpoint-attacks.json; commit; }
write_ts_attacks() {  # one fast-check property per off-chain class
  mkdir -p test
  printf 'import fc from "fast-check";\nimport { it } from "vitest";\n' >test/attacks.test.ts
  for cls in $(tax_ids typescript); do
    printf '\nit("%s over generated builds", () => {\n  fc.assert(fc.property(fc.integer(), (n) => n === n));\n});\n' \
      "$cls" >>test/attacks.test.ts
  done
}
plant_on_aiken() {  # the same class id, stated on the on-chain side too
  "$FPL_PY" - "$1" <<'PY'
import json, sys
p = ".fluxpoint-attacks.json"
doc = json.load(open(p))
for tax in doc["taxonomies"]:
    if tax["language"] == "aiken":
        tax["classes"].append({"id": sys.argv[1], "applies": "spend",
                               "property": "the same id, stated on-chain"})
json.dump(doc, open(p, "w"), indent=2)
PY
}

ts_state
check "a TypeScript repo with the manifest and no attack tests is red" 1 "$(rc_of --check)"
out="$(sg --check 2>&1)"
check "every off-chain class is its own finding" "$n_ts" \
  "$(printf '%s' "$out" | grep -c 'attack:typescript:attack_')"
case "$out" in *attack_datum_round_trip*"survives encode then decode"*)
  ok "each names the property to state" "named" ;;
  *) bad "each names the property to state" "${out:0:60}" ;; esac
case "$out" in *"attack:aiken:"*) bad "and the aiken taxonomy gates nothing there" "gated" ;;
  *) ok "and the aiken taxonomy gates nothing there" "dormant" ;; esac
case "$(sg --check)" in *"aiken taxonomy gates nothing"*) ok "  and says so by name" "said" ;;
  *) bad "  and says so by name" "silent" ;; esac

# A test title carrying the class id is what specifies it off-chain.
ts_state
cat >>test/build.test.ts <<'EOF'

it("attack_datum_round_trip: every datum decodes to what was encoded", () => {
  expect(decode(encode(d))).toEqual(d);
});
EOF
commit
out="$(sg --check 2>&1)"
case "$out" in *"attack:typescript:attack_datum_round_trip"*)
  bad "a class id in a test title specifies it" "still red" ;;
  *) ok "a class id in a test title specifies it" "specified" ;; esac
check "  and only that class leaves the finding list" "$((n_ts - 1))" \
  "$(printf '%s' "$out" | grep -c 'attack:typescript:attack_')"
case "$out" in *"attack_datum_round_trip is specified by an example"*)
  ok "  a match in a file without fast-check is noted as an example" "noted" ;;
  *) bad "  a match in a file without fast-check is noted as an example" "silent" ;; esac

# fast-check in the file makes the same match a property over a generator.
ts_state
cat >test/round-trip.test.ts <<'EOF'
import fc from "fast-check";
import { it, expect } from "vitest";
import { decode, encode } from "../src/build";

it(`attack_datum_round_trip over generated datums`, () => {
  fc.assert(fc.property(fc.anything(), (d) => expect(decode(encode(d))).toEqual(d)));
});
EOF
commit
out="$(sg --check 2>&1)"
case "$out" in *"attack:typescript:attack_datum_round_trip"*)
  bad "a template-literal title specifies the class too" "still red" ;;
  *) ok "a template-literal title specifies the class too" "specified" ;; esac
case "$out" in *"is specified by an example"*)
  bad "  and a fast-check file raises no example note" "noted" ;;
  *) ok "  and a fast-check file raises no example note" "clean" ;; esac

# A class id inside a comment is not a test, which is the Aiken rule for a
# commented-out test carried over to // and to /* */.
ts_state
cat >>test/build.test.ts <<'EOF'

// it("attack_stale_protocol_params: params are refetched", () => {});
/*
it("attack_replayable_signed_tx", () => {});
const attack_unbounded_collateral = () => true;
*/
EOF
commit
out="$(sg --check 2>&1)"
for cls in attack_stale_protocol_params attack_replayable_signed_tx attack_unbounded_collateral; do
  case "$out" in *"attack:typescript:$cls"*) ok "a commented-out $cls stays unspecified" "red" ;;
    *) bad "a commented-out $cls stays unspecified" "read as specified" ;; esac
done
check "  so the finding count is unchanged" "$n_ts" \
  "$(printf '%s' "$out" | grep -c 'attack:typescript:attack_')"

# A declaration of that name specifies a class as a title does, and a .ts
# under a test directory is a test file whatever it is called.
ts_state
printf '\nexport async function attack_unvalidated_change_address() { return 1; }\n' \
  >>test/build.test.ts
mkdir -p src/__tests__
printf 'const attack_missing_script_data_hash = () => true;\nexport default attack_missing_script_data_hash;\n' \
  >src/__tests__/hash.ts
commit
out="$(sg --check 2>&1)"
case "$out" in *"attack:typescript:attack_unvalidated_change_address"*)
  bad "a function named for the class specifies it" "still red" ;;
  *) ok "a function named for the class specifies it" "specified" ;; esac
case "$out" in *"attack:typescript:attack_missing_script_data_hash"*)
  bad "a const in a .ts under __tests__ specifies it" "still red" ;;
  *) ok "a const in a .ts under __tests__ specifies it" "specified" ;; esac

ts_state; write_ts_attacks; commit
check "a property per off-chain class is green" 0 "$(rc_of --check)"
case "$(sg --check)" in *"is specified by an example"*)
  bad "  with no example note anywhere" "noted" ;;
  *) ok "  with no example note anywhere" "clean" ;; esac
case "$(sg --scan)" in *"attack class typescript:attack_datum_round_trip: specified"*)
  ok "--scan names the language beside the state" "reported" ;;
  *) bad "--scan names the language beside the state" "silent" ;; esac
case "$(sg --scan)" in *"attack taxonomy aiken: dormant"*)
  ok "  and labels a dormant taxonomy dormant" "labelled" ;;
  *) bad "  and labels a dormant taxonomy dormant" "listed as UNSPECIFIED" ;; esac

# The waiver rule holds per taxonomy: a reason of at least MIN_REASON.
ts_state; write_ts_attacks; sed -i '/it("attack_unbounded_collateral/d' test/attacks.test.ts; commit
check "one off-chain class left unspecified is red" 1 "$(rc_of --check)"
waive typescript attack_unbounded_collateral "n/a"
check "  waived with a too-short reason, still red" 1 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"attack:typescript:attack_unbounded_collateral"*"WAIVED WITHOUT A REASON"*)
  ok "  naming the class and what a waiver owes" "said" ;;
  *) bad "  naming the class and what a waiver owes" "silent" ;; esac
waive typescript attack_unbounded_collateral "this service never selects collateral; the wallet provider attaches it"
check "  waived with a real reason, green" 0 "$(rc_of --check)"
case "$(sg --check)" in *"typescript:attack_unbounded_collateral waived"*)
  ok "  and the waiver is echoed" "echoed" ;;
  *) bad "  and the waiver is echoed" "silent" ;; esac

# A repo carrying both languages answers for both taxonomies.
both_state
out="$(sg --check 2>&1)"
check "a repo carrying both languages is gated by both" "$((n_aiken + n_ts))" \
  "$(printf '%s' "$out" | grep -c 'attack:[a-z]*:attack_')"
case "$out" in *"gates nothing"*) bad "  and neither taxonomy is dormant" "dormant" ;;
  *) ok "  and neither taxonomy is dormant" "both live" ;; esac

# A waiver is scoped to the taxonomy it sits in. The same id excused on one
# side leaves the other side owed.
both_state; plant_on_aiken attack_datum_round_trip
waive aiken attack_datum_round_trip "the validator reads this datum as opaque bytes and never decodes it"
out="$(sg --check 2>&1)"
case "$out" in *"attack:typescript:attack_datum_round_trip"*)
  ok "an aiken waiver does not excuse the typescript class" "still red" ;;
  *) bad "an aiken waiver does not excuse the typescript class" "excused" ;; esac
case "$out" in *"attack:aiken:attack_datum_round_trip"*)
  bad "  while its own class is waived" "still red" ;;
  *) ok "  while its own class is waived" "waived" ;; esac

both_state; plant_on_aiken attack_datum_round_trip
waive typescript attack_datum_round_trip "the builder never constructs this datum; the indexer hands it over whole"
out="$(sg --check 2>&1)"
case "$out" in *"attack:aiken:attack_datum_round_trip"*)
  ok "and a typescript waiver does not excuse the aiken class" "still red" ;;
  *) bad "and a typescript waiver does not excuse the aiken class" "excused" ;; esac
case "$out" in *"attack:typescript:attack_datum_round_trip"*)
  bad "  while its own class is waived" "still red" ;;
  *) ok "  while its own class is waived" "waived" ;; esac

# The other direction: a validator-only repo answers for the eUTxO classes
# alone.
base_state; cp "$TAX" .fluxpoint-attacks.json
out="$(sg --check 2>&1)"
case "$out" in *"attack:typescript:"*) bad "an Aiken-only repo is not gated off-chain" "gated" ;;
  *) ok "an Aiken-only repo is not gated off-chain" "dormant" ;; esac
case "$(sg --check)" in *"typescript taxonomy gates nothing"*)
  ok "  saying the typescript taxonomy is dormant" "said" ;;
  *) bad "  saying the typescript taxonomy is dormant" "silent" ;; esac
check "  while its own classes are still owed" 1 "$(rc_of --check)"

# Backward compatibility: the single-object manifest that shipped in 1.38.0
# may already sit in a repo, and behaves exactly as it did.
legacy_tax() {  # the aiken taxonomy, written back in the 1.38.0 shape
  "$FPL_PY" - "$TAX" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
tax = [t for t in doc["taxonomies"] if t["language"] == "aiken"][0]
json.dump({"version": 1, "language": "aiken", "note": tax["note"],
           "classes": tax["classes"], "waived": {}},
          open(".fluxpoint-attacks.json", "w"), indent=2)
PY
}
base_state; legacy_tax
check "the 1.38.0 single-object manifest is still read" 1 "$(rc_of --check)"
out="$(sg --check 2>&1)"
check "  gating exactly the classes it names" "$n_aiken" \
  "$(printf '%s' "$out" | grep -c 'attack:aiken:attack_')"
case "$out" in *"attack:typescript:"*) bad "  and no others" "off-chain classes appeared" ;;
  *) ok "  and no others" "clean" ;; esac
base_state; legacy_tax; write_attacks
check "  a property test per class turns it green" 0 "$(rc_of --check)"
case "$(sg --scan)" in *"attack class aiken:attack_foreign_utxo: specified"*)
  ok "  and --scan reads it as the aiken taxonomy" "reported" ;;
  *) bad "  and --scan reads it as the aiken taxonomy" "silent" ;; esac
mkrepo; printf 'print(1)\n' >app.py; legacy_tax; commit
check "  in a repo with no Aiken it is green" 0 "$(rc_of --check)"
case "$(sg --check)" in *"gates nothing"*) ok "    with the note that it gates nothing" "said" ;;
  *) bad "    with the note that it gates nothing" "silent" ;; esac

# A manifest in neither shape is a problem with a name, never a pass.
base_state
printf '{"version": 1, "taxonomies": []}\n' >.fluxpoint-attacks.json
check "an empty taxonomies list is red" 1 "$(rc_of --check)"
printf '{"version": 1, "taxonomies": [{"language": "rust", "classes": []}]}\n' >.fluxpoint-attacks.json
# A language with no rule is NOT a malformed manifest; see 9b. The shape of
# the entry is still checked, which is what the cases around this one cover.
check "a taxonomy in a language with no rule is uncovered, not red" 0 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"NOT COVERED"*rust*) ok "  naming the language" "named" ;;
  *) bad "  naming the language" "silent" ;; esac
printf '{"version": 1, "taxonomies": [{"language": "typescript", "classes": [{"nope": 1}]}]}\n' \
  >.fluxpoint-attacks.json
check "a class carrying no id is red" 1 "$(rc_of --check)"
printf '{"version": 1, "note": "no classes anywhere"}\n' >.fluxpoint-attacks.json
check "a manifest in neither shape is red" 1 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *UNREADABLE*taxonomies*) ok "  and says what it needed" "named" ;;
  *) bad "  and says what it needed" "silent" ;; esac
printf '[]\n' >.fluxpoint-attacks.json
check "a manifest that is not an object is red" 1 "$(rc_of --check)"

# ========= 9b. a language this guard has not learned is UNCOVERED =========
# The manifest is a repo's declaration of what it answers for; this script
# is what lags behind it. Reddening a forward-looking declaration teaches
# people to delete classes, which is the opposite of the taxonomy's point.
# Skipping it silently is worse: the classes would read as covered when
# nothing looked at them. So it is reported, exactly as a tracked Agda file
# already is — named every run, counted as covered never.
unknown_lang() {  # $1 = the language string to write
  mkrepo; write_aiken
  "$FPL_PY" - "$1" <<'PY'
import json, sys
json.dump({"version": 1, "taxonomies": [
    {"language": "aiken",
     "classes": [{"id": "attack_double_satisfaction", "property": "p"}], "waived": {}},
    {"language": sys.argv[1],
     "classes": [{"id": "attack_reentrancy", "property": "p"},
                 {"id": "attack_overflow", "property": "p"}], "waived": {}}]},
    open(".fluxpoint-attacks.json", "w"), indent=2)
PY
  printf '\ntest attack_double_satisfaction(n: Int via bounded_int(1, 9)) {\n  !spend(mk_datum(n), Void, mk_ctx(a: True))\n}\n' >>validators/vault.ak
  commit
}

unknown_lang rust
check "a language this guard has not learned does not fail the gate" 0 "$(rc_of --check)"
out="$(sg --check 2>&1)"
case "$out" in *"NOT COVERED: taxonomy 'rust'"*"2 class(es) unchecked"*)
  ok "  it is named, with what it cost" "named" ;;
  *) bad "  it is named, with what it cost" "${out:0:70}" ;; esac
case "$out" in *"aiken, typescript only"*) ok "  and the known languages are listed beside it" "listed" ;;
  *) bad "  and the known languages are listed beside it" "silent" ;; esac
case "$out" in *"attack:rust:"*) bad "  its classes are not findings" "red" ;;
  *) ok "  its classes are not findings" "unchecked" ;; esac
out="$(sg --scan)"
case "$out" in *"attack class rust:attack_reentrancy: UNCHECKED"*)
  ok "  --scan calls them UNCHECKED, a state of their own" "UNCHECKED" ;;
  *) bad "  --scan calls them UNCHECKED, a state of their own" "${out:0:70}" ;; esac
case "$out" in *"rust:attack_overflow: specified"*|*"rust:attack_overflow: UNSPECIFIED"*)
  bad "  never folded into specified or unspecified" "folded" ;;
  *) ok "  never folded into specified or unspecified" "distinct" ;; esac
# The language it DOES know keeps gating in the same manifest.
case "$(sg --check 2>&1)" in *"attack:aiken:"*) bad "  the known taxonomy still gates" "silent" ;;
  *) ok "  the known taxonomy still gates" "gating" ;; esac

# The one real risk the change introduces: a typo un-gates a language that
# WAS covered, quietly. The near miss is named so it cannot pass a reader.
unknown_lang typescrpit
check "a transposed language still does not fail the gate" 0 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"Did you mean 'typescript'?"*)
  ok "  but the near miss is named" "hinted" ;;
  *) bad "  but the near miss is named" "no hint" ;; esac
unknown_lang typescripts
case "$(sg --check 2>&1)" in *"Did you mean 'typescript'?"*)
  ok "a one-character-longer language is hinted too" "hinted" ;;
  *) bad "a one-character-longer language is hinted too" "no hint" ;; esac
unknown_lang haskell
case "$(sg --check 2>&1)" in *"Did you mean"*) bad "a genuinely different language gets no hint" "hinted" ;;
  *) ok "a genuinely different language gets no hint" "no hint" ;; esac

# Shape is still a hard error: this change is about vocabulary, not rigour.
mkrepo; write_aiken
printf '{"version": 1, "taxonomies": [{"language": "rust", "classes": [{"nope": 1}]}]}\n' \
  >.fluxpoint-attacks.json
commit
check "an unknown language with a malformed class is still red" 1 "$(rc_of --check)"
printf '{"version": 1, "taxonomies": [{"classes": []}]}\n' >.fluxpoint-attacks.json
commit
check "a taxonomy with no language at all is still red" 1 "$(rc_of --check)"

# ====== 9c. a tracked language the manifest has no taxonomy for ===========
# Every case above runs a taxonomy against the repo. This is the other
# direction: the repo against the manifest. A repo of validators whose
# manifest carries only the typescript half had eight eUTxO classes nobody
# checked and a green gate that said nothing about them. A surface nobody
# checked must never read as one that passed, so it is named every run: a
# note by default, since reddening a repo for a manifest it has not finished
# writing makes a gate people delete, and a failure once the manifest sets
# requireAllLanguages. `languages` declares the halves a repo gates on
# purpose, and a tracked language it leaves out is named as excluded.
only_tax() {  # $1 = the template language to keep; then key=<json> pairs
  "$FPL_PY" - "$TAX" "$@" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
doc["taxonomies"] = [t for t in doc["taxonomies"] if t["language"] == sys.argv[2]]
for kv in sys.argv[3:]:
    k, v = kv.split("=", 1)
    doc[k] = json.loads(v)
json.dump(doc, open(".fluxpoint-attacks.json", "w"), indent=2)
PY
}
set_key() {  # key, <json> — into the manifest already in the repo
  "$FPL_PY" - "$1" "$2" <<'PY'
import json, sys
p = ".fluxpoint-attacks.json"
doc = json.load(open(p))
doc[sys.argv[1]] = json.loads(sys.argv[2])
json.dump(doc, open(p, "w"), indent=2)
PY
}
# The first three ids of a language in the shipped template, then how many
# more, which is what the note is expected to name.
first3() { set -- $(tax_ids "$1"); printf '%s, %s, %s, +%d more' "$1" "$2" "$3" "$(($# - 3))"; }
# Both languages tracked, every builder class specified, and the aiken half
# missing from the manifest: the exact repo the issue describes.
issue_state() { mkrepo; write_aiken; write_ts; write_ts_attacks; only_tax typescript "$@"; commit; }

issue_state
check "a tracked language with no taxonomy does not fail the gate" 0 "$(rc_of --check)"
out="$(sg --check 2>&1)"
case "$out" in *"NO TAXONOMY: this repo tracks Aiken but .fluxpoint-attacks.json declares no aiken taxonomy"*)
  ok "  it is named NO TAXONOMY, with the language" "named" ;;
  *) bad "  it is named NO TAXONOMY, with the language" "${out:0:70}" ;; esac
case "$out" in *"$n_aiken known eUTxO classes are ungated"*) ok "  with how many classes it leaves ungated" "counted" ;;
  *) bad "  with how many classes it leaves ungated" "no count" ;; esac
case "$out" in *"($(first3 aiken))"*) ok "  and the first three of them by id" "listed" ;;
  *) bad "  and the first three of them by id" "not listed" ;; esac
case "$out" in *"templates/attack-taxonomy.json"*) ok "  and where to copy them from" "said" ;;
  *) bad "  and where to copy them from" "silent" ;; esac
case "$out" in *"attack:aiken"*) bad "  no aiken class becomes a finding" "red" ;;
  *) ok "  no aiken class becomes a finding" "note only" ;; esac
case "$(sg --scan)" in *"attack taxonomy aiken: NO TAXONOMY"*)
  ok "--scan reports the missing taxonomy too" "reported" ;;
  *) bad "--scan reports the missing taxonomy too" "silent" ;; esac

issue_state 'requireAllLanguages=true'
check "requireAllLanguages makes it a failure" 1 "$(rc_of --check)"
case "$(sg --check 2>&1 >/dev/null)" in *"attack:aiken: NO TAXONOMY"*"$n_aiken known eUTxO classes"*)
  ok "  the finding names the language and the cost" "named" ;;
  *) bad "  the finding names the language and the cost" "silent" ;; esac
issue_state 'requireAllLanguages=false'
check "requireAllLanguages false keeps it a note" 0 "$(rc_of --check)"

# The other direction: a builder the manifest carries only the aiken half for.
mkrepo; write_ts; only_tax aiken; commit
check "a TypeScript repo with only the aiken taxonomy stays green" 0 "$(rc_of --check)"
out="$(sg --check 2>&1)"
case "$out" in *"NO TAXONOMY: this repo tracks TypeScript tests but"*"declares no typescript taxonomy"*"$n_ts known off-chain builder classes are ungated ($(first3 typescript))"*)
  ok "  but names the missing typescript taxonomy" "named" ;;
  *) bad "  but names the missing typescript taxonomy" "${out:0:70}" ;; esac
case "$out" in *"gates nothing here"*) ok "  beside the dormant aiken note" "said" ;;
  *) bad "  beside the dormant aiken note" "silent" ;; esac
set_key requireAllLanguages true
check "  and requireAllLanguages reds it" 1 "$(rc_of --check)"

# A language the repo does not carry needs no taxonomy.
mkrepo; write_ts; write_ts_attacks; only_tax typescript 'requireAllLanguages=true'; commit
check "a repo with no Aiken needs no aiken taxonomy, even strict" 0 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"NO TAXONOMY"*) bad "  and says nothing about one" "noted" ;;
  *) ok "  and says nothing about one" "silent" ;; esac
base_state; only_tax aiken 'requireAllLanguages=true'; write_attacks
check "a repo with no TypeScript tests needs no typescript taxonomy" 0 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"NO TAXONOMY"*) bad "  and says nothing about one" "noted" ;;
  *) ok "  and says nothing about one" "silent" ;; esac
mkrepo; write_aiken; write_attacks; write_ts; write_ts_attacks
cp "$TAX" .fluxpoint-attacks.json; set_key requireAllLanguages true; commit
check "both halves carried and specified: green under requireAllLanguages" 0 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"NO TAXONOMY"*) bad "  with no gap named" "noted" ;;
  *) ok "  with no gap named" "none" ;; esac

# A manifest carrying only a taxonomy this guard has not learned still owes
# the language it has learned and the repo tracks.
mkrepo; write_aiken
printf '{"version": 1, "taxonomies": [{"language": "rust", "classes": [{"id": "attack_x"}]}]}\n' \
  >.fluxpoint-attacks.json
commit
out="$(sg --check 2>&1)"
case "$out" in *"NOT COVERED: taxonomy 'rust'"*"NO TAXONOMY: this repo tracks Aiken"*)
  ok "an unlearned taxonomy alone still names the missing aiken one" "both named" ;;
  *) bad "an unlearned taxonomy alone still names the missing aiken one" "${out:0:70}" ;; esac

# `languages` says which halves this repo gates, on purpose. A tracked
# language it leaves out is silenced, never failed, and still named.
issue_state 'languages=["typescript"]'
check "a language left out of 'languages' does not fail the gate" 0 "$(rc_of --check)"
set_key requireAllLanguages true
check "  not even under requireAllLanguages" 0 "$(rc_of --check)"
out="$(sg --check 2>&1)"
case "$out" in *"NO TAXONOMY"*) bad "  and raises no NO TAXONOMY line" "noted" ;;
  *) ok "  and raises no NO TAXONOMY line" "silenced" ;; esac
case "$out" in *"aiken excluded by declaration: this repo tracks Aiken"*"$n_aiken known eUTxO classes are not gated"*) ok "  but is named as excluded by declaration" "named" ;;
  *) bad "  but is named as excluded by declaration" "silent" ;; esac
check "  in exactly one line" 1 "$(printf '%s\n' "$out" | grep -c 'excluded by declaration')"
case "$(sg --scan)" in *"attack taxonomy aiken: "*"excluded by declaration"*)
  ok "  --scan names the exclusion too" "named" ;;
  *) bad "  --scan names the exclusion too" "silent" ;; esac

# Listing a language with no taxonomy is a contradiction: declared gated,
# gated by nothing. It is reported, and fails under requireAllLanguages.
issue_state 'languages=["typescript", "aiken"]'
check "a listed language with no taxonomy stays a note by default" 0 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"NO TAXONOMY: 'languages' lists aiken"*"declares no aiken taxonomy"*)
  ok "  naming the contradiction" "named" ;;
  *) bad "  naming the contradiction" "silent" ;; esac
set_key requireAllLanguages true
check "  and fails under requireAllLanguages" 1 "$(rc_of --check)"
mkrepo; write_ts; write_ts_attacks; only_tax typescript 'languages=["typescript", "aiken"]' 'requireAllLanguages=true'; commit
check "a listed language the repo does not carry fails nothing" 0 "$(rc_of --check)"
case "$(sg --check 2>&1)" in *"'languages' lists aiken"*"no aiken taxonomy"*"nothing is ungated"*)
  ok "  but the contradiction is still named" "named" ;;
  *) bad "  but the contradiction is still named" "silent" ;; esac

# A list never switches off a taxonomy the manifest writes out in full.
both_state; set_key languages '["typescript"]'
out="$(sg --check 2>&1)"
check "a taxonomy left out of 'languages' still gates" "$n_aiken" \
  "$(printf '%s' "$out" | grep -c 'attack:aiken:attack_')"
case "$out" in *"the aiken taxonomy gates although 'languages' does not list aiken"*)
  ok "  and the mismatch is named" "named" ;;
  *) bad "  and the mismatch is named" "silent" ;; esac

# Both fields are read as strictly as the rest of the manifest: a value this
# guard cannot read is a gate that decides nothing, so it is red and named.
bad_key() {  # label, key, <json>, text the finding must carry
  issue_state "$2=$3"
  check "$1 is red" 1 "$(rc_of --check)"
  case "$(sg --check 2>&1)" in *UNREADABLE*"$4"*) ok "  and names what is wrong" "named" ;;
    *) bad "  and names what is wrong" "silent" ;; esac
}
bad_key "requireAllLanguages as a string" requireAllLanguages '"yes"' "'requireAllLanguages' must be true or false"
bad_key "requireAllLanguages as null" requireAllLanguages 'null' "'requireAllLanguages' must be true or false"
bad_key "languages as a bare string" languages '"typescript"' "'languages' must be a non-empty list"
bad_key "an empty languages list" languages '[]' "'languages' must be a non-empty list"
bad_key "a non-string in languages" languages '["typescript", 1]' "'languages' must be a non-empty list"
bad_key "a language listed twice" languages '["typescript", "typescript"]' "names 'typescript' twice"
bad_key "an unknown language in languages" languages '["typescript", "haskell"]' "names 'haskell'"
issue_state 'languages=["typescrpit"]'
case "$(sg --check 2>&1)" in *UNREADABLE*"(did you mean 'typescript'?)"*)
  ok "a misspelt language in languages is hinted" "hinted" ;;
  *) bad "a misspelt language in languages is hinted" "no hint" ;; esac
# The language of a taxonomy the manifest itself declares may be listed, even
# one this guard has not learned: that taxonomy is reported NOT COVERED on
# its own, and a forward-looking declaration is not a malformed one.
mkrepo; write_aiken
printf '{"version": 1, "languages": ["aiken", "rust"], "taxonomies": [{"language": "rust", "classes": [{"id": "attack_x"}]}]}\n' \
  >.fluxpoint-attacks.json
commit
check "listing an unlearned language the manifest declares is not red" 0 "$(rc_of --check)"

# ================= 10. --axioms: the prover's own assumption audit ========
# The provers are not installed where this suite runs, so each toolchain is
# a shim on PATH that prints the listing its real counterpart printed for the
# same probe — captured 2026-09-11 from Lean 4.15.0 (`lake env lean` on a
# `#print axioms` file), Coq 8.18.0 (`coqc` on a `Print Assumptions` file,
# module path from _CoqProject) and Dafny 4.9.1 (`dafny audit
# --report-format txt`), and pinned against the same three parsers end to
# end on those installs.
mkshims() {
  mkdir -p "$ROOT/bin"
  cat >"$ROOT/bin/lake" <<'EOF'
#!/usr/bin/env bash
# lake env lean <probe>
[ "$1" = env ] && [ "$2" = lean ] || { echo "shim: unexpected $*" >&2; exit 2; }
name="$(sed -n 's/^#print axioms \(.*\)$/\1/p' "$3")"
if [ -n "${SHIM_LEAN_GARBAGE:-}" ]; then echo "error: unknown package 'Foo'"; exit 1; fi
if [ -z "${SHIM_LEAN_AXIOMS:-}" ]; then echo "'$name' does not depend on any axioms"
else echo "'$name' depends on axioms: [${SHIM_LEAN_AXIOMS}]"; fi
EOF
  cat >"$ROOT/bin/coqc" <<'EOF'
#!/usr/bin/env bash
for last; do :; done
name="$(sed -n 's/^Print Assumptions \(.*\)\.$/\1/p' "$last")"
[ -n "$name" ] || { echo "shim: no Print Assumptions in $last" >&2; exit 2; }
if [ -z "${SHIM_COQ_AXIOMS:-}" ]; then echo "Closed under the global context"
else printf 'Axioms:\n'; for a in $SHIM_COQ_AXIOMS; do printf '%s : forall P : Prop, P\n' "$a"; done; fi
EOF
  cat >"$ROOT/bin/dafny" <<'EOF'
#!/usr/bin/env bash
# dafny audit --report-format txt <files>, as Dafny 4.9.1 prints it: the
# ordinary warnings first, one `file(l,c):Name: message` row per finding,
# then the completion lines. 4.9.1 rejects `text` for the format.
[ "$1" = audit ] || { echo "shim: unexpected $*" >&2; exit 2; }
if [ "$2" != --report-format ] || [ "$3" != txt ]; then
  printf "Argument '%s' not recognized. Must be one of:\n\t'html'\n\t'txt'\n" "$3"; exit 1
fi
printf 'src/vault.dfy(22,4): Warning: assume statement has no {:axiom} annotation\n'
printf '%b' "${SHIM_DAFNY_AUDIT:-}"
printf 'Dafny auditor completed with %s findings\n\nDafny program verifier did not attempt verification\n' \
  "$(printf '%b' "${SHIM_DAFNY_AUDIT:-}" | grep -c .)"
EOF
  chmod +x "$ROOT/bin/"*
}
ax_state() {
  poly_state
  cat >src/Bar.v <<'EOF'
Theorem plus_O_n : forall n : nat, 0 + n = n.
Proof. intros n. reflexivity. Qed.
EOF
  printf -- '-R src Vault\n' >_CoqProject
  write_dafny; commit; sg --baseline >/dev/null
}
mkshims
export SHIM_LEAN_AXIOMS="propext, Classical.choice"
export SHIM_COQ_AXIOMS="classic"
export SHIM_DAFNY_AUDIT='src/vault.dfy(10,17):Helper: Declaration has explicit `{:axiom}` attribute. Possible mitigation: Provide a proof or test.\n'
HEADS="--headline lean:src/Foo.lean:add_zero' --headline coq:src/Bar.v:plus_O_n --headline dafny:*"
with_shims() { PATH="$ROOT/bin:$PATH" "$FPL_PY" "$SG" --root "$ROOT/r" "$@"; }
rc_shims() { with_shims "$@" >/dev/null 2>&1; echo $?; }
# A PATH with git and the interpreter and nothing else, so the "toolchain
# absent" case holds on a machine where a real prover happens to be installed.
mkdir -p "$ROOT/nobin"
for _t in git "$FPL_PY" sh bash env sed grep; do
  _p="$(command -v "$_t")"; [ -n "$_p" ] && ln -sf "$_p" "$ROOT/nobin/$(basename "$_t")"
done
without_tools() { PATH="$ROOT/nobin" "$FPL_PY" "$SG" --root "$ROOT/r" "$@"; }
rc_none() { without_tools "$@" >/dev/null 2>&1; echo $?; }

ax_state
check "a headline that is not an obligation is refused" 1 "$(rc_shims --baseline --axioms --headline lean:src/Foo.lean:nope)"
check "recording the assumption sets" 0 "$(rc_shims --baseline --axioms $HEADS)"
rec="$("$FPL_PY" -c 'import json; d=json.load(open(".fluxpoint-proof-baseline.json")); print(",".join(d["axioms"]["recorded"]["lean:src/Foo.lean:add_zero'"'"'"]))')"
check "  Lean's listing is recorded verbatim" "Classical.choice,propext" "$rec"
rec="$("$FPL_PY" -c 'import json; d=json.load(open(".fluxpoint-proof-baseline.json")); print(len(d["axioms"]["recorded"]["dafny:*"]))')"
check "  dafny audit finding rows are recorded, warnings are not" 1 "$rec"
check "same assumptions: green" 0 "$(rc_shims --check)"
check "a new axiom under a headline is red" 1 "$(SHIM_LEAN_AXIOMS='propext, Classical.choice, sorryAx' rc_shims --check)"
case "$(SHIM_LEAN_AXIOMS='propext, Classical.choice, sorryAx' with_shims --check 2>&1)" in
  *"NEW AXIOM"*sorryAx*) ok "  and names it" "named" ;; *) bad "  and names it" "silent" ;; esac
check "fewer axioms is green" 0 "$(SHIM_LEAN_AXIOMS=propext rc_shims --check)"
check "a new dafny audit row is red" 1 "$(SHIM_DAFNY_AUDIT='src/vault.dfy(10,17):Helper: Declaration has explicit `{:axiom}` attribute. Possible mitigation: Provide a proof or test.\nsrc/vault.dfy(13,25):Skipped: Declaration has `{:verify false}` attribute. Possible mitigation: Remove `{:verify false}` attribute and prove if possible.\n' rc_shims --check)"
check "the same row at another line is not new" 0 "$(SHIM_DAFNY_AUDIT='src/vault.dfy(40,17):Helper: Declaration has explicit `{:axiom}` attribute. Possible mitigation: Provide a proof or test.\n' rc_shims --check)"
check "a new Coq assumption is red" 1 "$(SHIM_COQ_AXIOMS='classic functional_extensionality' rc_shims --check)"
check "--check --axioms runs only the audit" 0 "$(rc_shims --check --axioms)"
sed -i 's/x + 1 > 1/x + 1 > 0/' src/Baz.thy
check "  (a weakened statement elsewhere does not enter it)" 0 "$(rc_shims --check --axioms)"
check "  while the full --check still sees it" 1 "$(rc_shims --check)"
ax_state; with_shims --baseline --axioms $HEADS >/dev/null
sg --baseline >/dev/null
has_ax="$("$FPL_PY" -c 'import json; print("yes" if "axioms" in json.load(open(".fluxpoint-proof-baseline.json")) else "no")')"
check "re-recording statements preserves the axiom section" yes "$has_ax"
check "without the toolchains --check is green and says NOT RUN" 0 "$(rc_none --check)"
case "$(without_tools --check)" in *"AXIOM AUDIT NOT RUN"*"lake is not on PATH"*) ok "  naming the missing tool" "named" ;;
  *) bad "  naming the missing tool" "silent" ;; esac
check "a listing that cannot be read is red, never clean" 1 "$(SHIM_LEAN_GARBAGE=1 rc_shims --check)"
case "$(SHIM_LEAN_GARBAGE=1 with_shims --check 2>&1)" in *"AXIOM AUDIT UNREADABLE"*) ok "  and says UNREADABLE" "said" ;;
  *) bad "  and says UNREADABLE" "silent" ;; esac
rm _CoqProject
case "$(with_shims --check 2>&1)" in *"NOT RUN"*"_CoqProject"*) ok "Coq without _CoqProject is NOT RUN, naming why" "named" ;;
  *) bad "Coq without _CoqProject is NOT RUN, naming why" "silent" ;; esac
case "$(with_shims --scan --axioms 2>&1)" in *"add_zero'"*"Classical.choice"*) ok "--scan --axioms prints each headline's set" "printed" ;;
  *) bad "--scan --axioms prints each headline's set" "silent" ;; esac
unset SHIM_LEAN_AXIOMS SHIM_COQ_AXIOMS SHIM_DAFNY_AUDIT

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
