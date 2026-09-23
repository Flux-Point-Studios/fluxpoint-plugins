#!/usr/bin/env bash
# Decisions that outlive the context that made them.
#
# `decision.py` gives loop mode the bus that only graph campaigns had, with
# the DecisionV1 floors enforced rather than described — a decision worth
# surviving context death names the alternatives it beat and the best case
# against each, including against the one that won. The compaction-time
# counterpart lives in precompact-test.sh.
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
export CLAUDE_PLUGIN_ROOT="$PLUGIN"
DEC="$PLUGIN/scripts/decision.py"
INJECT="$PLUGIN/scripts/inject-state.sh"
GATE="$PLUGIN/scripts/dod-gate.sh"
ROOT="$(mktemp -d)"
R="$ROOT/r"
pass=0; fail=0

ok()  { printf 'PASS  %-58s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-58s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

GOOD='{
  "question": "how should the vault unlock window be bounded?",
  "options": [
    {"option": "24h", "argued_by": "ops",
     "strongest_objection": "a day may be too slow for incident response"},
    {"option": "72h", "argued_by": "gov",
     "strongest_objection": "three days widens the window an attacker has"}
  ],
  "chosen": "72h",
  "rationale": "the governance timelock already binds operations to 72h, so a shorter unlock buys nothing and adds a race",
  "overturned_prior": true,
  "frozen_by": "genesis",
  "reversible": false,
  "evidence": ["scripts/harness.sh --full exit 0"]
}'

mkrepo() { # $1 = harness exit
  rm -rf "$R"; mkdir -p "$R/scripts" "$R/src"; cd "$R" || exit 1
  git init -q -b main
  printf '#!/usr/bin/env bash\nexit %s\n' "${1:-0}" >scripts/harness.sh
  chmod +x scripts/harness.sh
  printf 'x = 1\n' >src/app.py
  cat >WORK.md <<'EOF'
# work

STATUS: WIP
MODE: loop

## Decisions

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|

## Evidence

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|

## Notes for the next iteration
EOF
  git add -A; git -c user.email=t@t -c user.name=t commit -qm base
  printf '{"session_id":"s","cwd":"%s"}' "$R" | bash "$INJECT" >/dev/null 2>&1
}

dec() { "$FPL_PY" "$DEC" --root "$R" --graph WORK.md "$@"; }
dec_rows() { grep -cE '^\| 20[0-9-]+ [0-9:]+ \| [a-z]' "$R/WORK.md" 2>/dev/null | head -1; }

# ================= 1. the floors are enforced, not described =============
mkrepo
printf '%s' "$GOOD" | dec --record --id vault-window >/dev/null 2>&1
check "a real decision is recorded" 0 "$?"
grep -q '| vault-window | 72h | YES |' "$R/WORK.md" \
  && ok "the row carries the choice and that it overturned the prior" "recorded" \
  || bad "the row carries the choice and that it overturned the prior" "$(grep '^| 20' "$R/WORK.md" | head -1)"

mkrepo
printf '"we picked 72h"' | dec --record >/dev/null 2>&1
check "a bare sentence is refused" 1 "$?"

# Each mutation is written to a file and fed with --result rather than
# piped: under `pipefail` a pipeline inherits decision.py's exit 1, so
# `... | grep -q ... && ok` reports a refusal as a failure and the test lies
# about the code under test.
mutate() { # $1 = python expression mutating `d`
  printf '%s' "$GOOD" | "$FPL_PY" -c "
import json, sys
d = json.load(sys.stdin)
$1
print(json.dumps(d))" >"$R/mut.json"
}

mkrepo
mutate 'd["options"] = d["options"][:1]'
dec --record --result "$R/mut.json" >/dev/null 2>&1
check "one option is a plan, not a decision" 1 "$?"

mutate 'd["options"][1]["strongest_objection"] = "meh"'
out="$(dec --record --result "$R/mut.json" 2>&1)"
case "$out" in *"was not examined"*)
  ok "an option nobody argued against is refused" "refused" ;;
  *) bad "an option nobody argued against is refused" "${out:0:44}" ;; esac

mutate 'd["rationale"] = "it is better"'
dec --record --result "$R/mut.json" >/dev/null 2>&1
check "a lazy rationale is refused" 1 "$?"

# The subtle one: a well-formed record whose chosen value was never on the
# table. Every floor passes and the decision is still incoherent.
mutate 'd["chosen"] = "48h"'
out="$(dec --record --result "$R/mut.json" 2>&1)"
case "$out" in *"not one of the options"*)
  ok "choosing something never considered is refused" "refused" ;;
  *) bad "choosing something never considered is refused" "${out:0:44}" ;; esac

# ================= 2. silence can be declared, but not faked =============
mkrepo
dec --none "n/a" --session s >/dev/null 2>&1
check "a throwaway --none reason is refused" 2 "$?"
dec --none "this slice was a mechanical rename with no choice to make" --session s >/dev/null 2>&1
check "a real one is recorded" 0 "$?"
[ -f "$R/.claude/fluxpoint/s.nodecision" ] \
  && ok "and silence becomes a statement on disk" "marker written" \
  || bad "and silence becomes a statement on disk" "missing"

# ================= 3. no table, no silent loss ===========================
mkrepo
printf '# nothing here\n' >"$R/BARE.md"
printf '%s' "$GOOD" | dec --record --graph BARE.md >/dev/null 2>&1
check "a file with no Decisions table is reported, not written" 3 "$?"

# ================= 4. the distill check is opt-in ========================
mkrepo 0
printf 'y = 2\n' >>src/app.py
gate() { printf '{"session_id":"s","cwd":"%s"}' "$R" | bash "$GATE" 2>/dev/null; }
out="$(gate)"
case "$out" in *"nothing was written down"*)
  bad "the distill check is off by default" "blocked without being asked" ;;
  *) ok "the distill check is off by default" "silent" ;; esac

mkrepo 0
printf 'y = 2\n' >>src/app.py
out="$(printf '{"session_id":"s","cwd":"%s"}' "$R" | FPL_DISTILL=1 bash "$GATE" 2>/dev/null)"
case "$out" in *"nothing was written down"*)
  ok "armed, it blocks a green stop that recorded nothing" "blocked" ;;
  *) bad "armed, it blocks a green stop that recorded nothing" "allowed" ;; esac

mkrepo 0
printf 'y = 2\n' >>src/app.py
printf '%s' "$GOOD" | dec --record --id vault-window >/dev/null 2>&1
out="$(printf '{"session_id":"s","cwd":"%s"}' "$R" | FPL_DISTILL=1 bash "$GATE" 2>/dev/null)"
case "$out" in *"nothing was written down"*)
  bad "armed, a recorded decision clears it" "still blocked" ;;
  *) ok "armed, a recorded decision clears it" "allowed" ;; esac

mkrepo 0
printf 'y = 2\n' >>src/app.py
dec --none "a mechanical rename with no choice in it" --session s >/dev/null 2>&1
out="$(printf '{"session_id":"s","cwd":"%s"}' "$R" | FPL_DISTILL=1 bash "$GATE" 2>/dev/null)"
case "$out" in *"nothing was written down"*)
  bad "armed, declared silence clears it too" "still blocked" ;;
  *) ok "armed, declared silence clears it too" "allowed" ;; esac

# ================= 5. the whole record survives, not one row (#98) =========
# The row is an index: cells are cut to fit a table. The validated record —
# the question, every option with its objection, the evidence — used to be
# written nowhere, so the conditions that mattered were the part a fresh
# context could not recover.
LONG='{
  "question": "should same-block claims wait for the risk-bucket campaign?",
  "options": [
    {"option": "options 1 and 3 together, started only after the risk-bucket campaign is fully done",
     "argued_by": "operator",
     "strongest_objection": "delays the distribution partner by a full campaign"},
    {"option": "option 1 alone", "argued_by": "model",
     "strongest_objection": "makes the same block likely but cannot guarantee it"}
  ],
  "chosen": "options 1 and 3 together, started only after the risk-bucket campaign is fully done",
  "rationale": "The operator chose it, citing the distribution partner'"'"'s wish for same-block payout: option 1 alone makes the same block likely but cannot guarantee it, and running both before the risk-bucket campaign lands would double the migration window the auditors signed off on.",
  "overturned_prior": true,
  "frozen_by": "none",
  "reversible": true,
  "evidence": ["operator, verbatim: start only after the risk-bucket campaign is fully done"]
}'
mkrepo
printf '%s' "$LONG" | dec --record --id same-block-claims >/dev/null 2>&1
check "a long decision is recorded" 0 "$?"
grep -q '| same-block-claims |' "$R/WORK.md" \
  && ok "the row carries the id whole, so it can be looked up" "indexed" \
  || bad "the row carries the id whole, so it can be looked up" "$(grep '^| 20' "$R/WORK.md" | head -1)"
[ -f "$R/.claude/fluxpoint/decisions.jsonl" ] \
  && ok "the full record is kept beside the row" "decisions.jsonl" \
  || bad "the full record is kept beside the row" "missing"
out="$(dec --show same-block-claims 2>&1)"
case "$out" in *"started only after the risk-bucket campaign is fully done"*"delays the distribution partner"*"operator, verbatim"*)
  ok "--show returns the chosen option, objections and evidence" "whole" ;;
  *) bad "--show returns the chosen option, objections and evidence" "${out:0:60}" ;; esac
case "$out" in *"auditors signed off on."*)
  ok "  and the rationale the row cut" "whole" ;;
  *) bad "  and the rationale the row cut" "${out:0:60}" ;; esac
js="$(dec --show same-block-claims --json 2>/dev/null)"
"$FPL_PY" -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if len(r["options"])==2 and r["overturned_prior"] is True else 1)' "$js" \
  && ok "--show --json is the validated DecisionV1" "json" \
  || bad "--show --json is the validated DecisionV1" "${js:0:60}"
ctx="$(printf '{"session_id":"s2","cwd":"%s"}' "$R" | bash "$INJECT" 2>/dev/null)"
case "$ctx" in *"decision.py --show same-block-claims"*)
  ok "SessionStart points a cut row at its full record" "pointer" ;;
  *) bad "SessionStart points a cut row at its full record" "no pointer" ;; esac

printf '%s' "$GOOD" | dec --record --id same-block-claims >/dev/null 2>&1
out="$(dec --show same-block-claims 2>&1)"
case "$out" in *"1 earlier version"*"chosen: 72h"*)
  ok "a re-decision shows the newest and counts the earlier" "newest" ;;
  *) bad "a re-decision shows the newest and counts the earlier" "${out:0:60}" ;; esac
dec --show nobody-decided-this >/dev/null 2>&1
check "--show on an id nobody recorded is an error" 1 "$?"
printf '%s' "$GOOD" | dec --record --id 'Vault Window!' >/dev/null 2>&1
check "an --id that cannot be looked up is refused" 1 "$?"

# A graph campaign's decisions live in its run artifact; --show finds them.
mkdir -p "$R/.claude/fluxpoint/runs"
printf '%s' "$GOOD" | "$FPL_PY" -c '
import json, sys
rec = json.load(sys.stdin)
json.dump({"runId": "wf_dec", "when": "2026-09-01 10:00",
           "summary": {"decisions": {"vault-window": rec}}}, open(sys.argv[1], "w"))' \
  "$R/.claude/fluxpoint/runs/wf_dec.json"
out="$(dec --show vault-window 2>&1)"
case "$out" in *"wf_dec"*"three days widens"*)
  ok "--show finds a decision a graph run filed" "run artifact" ;;
  *) bad "--show finds a decision a graph run filed" "${out:0:60}" ;; esac

# An honoring run carries the decision it IMPORTED in its decisions map as
# provenance. Read as a fresh ruling, it was dated by that run and hid a
# newer operator ruling recorded while the run was parked.
printf '%s' "$GOOD" | "$FPL_PY" -c '
import json, sys
rec = json.load(sys.stdin); rec["chosen"] = "72h-imported-copy"
json.dump({"runId": "wf_hon", "when": "2030-01-01 10:00", "recordedAt": "2030-01-01 10:00:00",
           "summary": {"decisions": {"vault-window": rec},
                       "decisionsImported": {"vault-window": "dec_old"}}}, open(sys.argv[1], "w"))' \
  "$R/.claude/fluxpoint/runs/wf_hon.json"
out="$(dec --show vault-window 2>&1)"
case "$out" in *"72h-imported-copy"*|*"wf_hon"*)
  bad "an imported copy is provenance, not a newer version" "${out:0:60}" ;;
  *) ok "an imported copy is provenance, not a newer version" "skipped" ;; esac

# An id derived from the question must be one --show can look up, and two
# questions must never share one.
mkrepo
printf '%s' "$GOOD" | "$FPL_PY" -c 'import json,sys; d=json.load(sys.stdin); d["question"]="2FA: TOTP or WebAuthn for operator logins?"; print(json.dumps(d))' \
  | dec --record >/dev/null 2>&1
check "a question starting with a digit still records" 0 "$?"
dec --show d-2fa-totp-or-webauthn-for-operator-login >/dev/null 2>&1
check "  under an id --show finds" 0 "$?"
printf '%s' "$GOOD" | "$FPL_PY" -c 'import json,sys; d=json.load(sys.stdin); d["question"]="保险库的解锁窗口应该设置为多长时间才合适？"; print(json.dumps(d))' \
  | dec --record >/dev/null 2>&1
check "a question with no ASCII letters still records" 0 "$?"
for q in "Should we use Postgres or SQLite for the session store?" \
         "Should we use Postgres or SQLite for the analytics warehouse?"; do
  printf '%s' "$GOOD" | Q="$q" "$FPL_PY" -c 'import json,os,sys; d=json.load(sys.stdin); d["question"]=os.environ["Q"]; print(json.dumps(d))' \
    | dec --record >/dev/null 2>&1
done
ids="$("$FPL_PY" -c 'import json,sys; print(" ".join(sorted({json.loads(l)["id"] for l in open(sys.argv[1]) if "Postgres" in l})))' "$R/.claude/fluxpoint/decisions.jsonl")"
case "$ids" in *" "*) ok "two questions sharing a 40-char prefix keep two ids" "$ids" ;;
  *) bad "two questions sharing a 40-char prefix keep two ids" "$ids" ;; esac
printf '%s' "$GOOD" | dec --record --id 9lives >/dev/null 2>"$ROOT/err"
case "$(cat "$ROOT/err")" in *"start with a letter"*) ok "a bad --id says what is wrong with it" "said" ;;
  *) bad "a bad --id says what is wrong with it" "$(head -c 80 "$ROOT/err")" ;; esac

# A cut row whose record this clone never kept (it predates 1.43, or was
# recorded in another clone) gets no pointer that answers "no record".
mkrepo
"$FPL_PY" - "$R/WORK.md" <<'PYEOF'
import sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
row = "| 2026-01-01 00:00 | legacy-window | 24h | no | none | " + "x" * 80 + "… |\n"
d = t.index("## Decisions")
sep = t.index("|\n|", d)
i = t.index("\n", sep + 2) + 1
open(p, "w", encoding="utf-8").write(t[:i] + row + t[i:])
PYEOF
ctx="$(printf '{"session_id":"s3","cwd":"%s"}' "$R" | bash "$INJECT" 2>/dev/null)"
case "$ctx" in *"decision.py --show legacy-window"*)
  bad "a cut row with no kept record gets no --show pointer" "pointer" ;;
  *"legacy-window's whole record is not kept"*) ok "a cut row with no kept record gets no --show pointer" "says so" ;;
  *) bad "a cut row with no kept record gets no --show pointer" "${ctx:0:80}" ;; esac

mkrepo
printf '# nothing here\n' >"$R/BARE.md"
printf '%s' "$GOOD" | dec --record --id vault-window --graph BARE.md >/dev/null 2>&1
check "no table: still reported" 3 "$?"
grep -q '"vault-window"' "$R/.claude/fluxpoint/decisions.jsonl" 2>/dev/null \
  && ok "  but the record itself is not lost" "kept" \
  || bad "  but the record itself is not lost" "dropped"

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
