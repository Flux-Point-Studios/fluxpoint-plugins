#!/usr/bin/env bash
# Execution attestation: the exit code is minted by the hook, not typed by
# an agent.
#
# The property under test is narrow and load-bearing: a row exists if and
# only if a command the repo *declared* as a gate ran through the Bash tool,
# and the exit it records is the runtime's, not anyone's report of it. The
# laundering cases matter most — `|| true`, a pipe, a different flag — since
# each one changes the exit code the runtime reports and so must not be able
# to borrow a gate's name.
#
# Executed end to end: every case drives attest.py with a real hook payload
# and reads the resulting log, and the cross-check cases run record-run.py
# over a real run summary.
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
ATTEST="$PLUGIN/scripts/attest.py"
RECORD="$PLUGIN/scripts/record-run.py"
INBOX="$PLUGIN/scripts/inbox.py"
ROOT="$(mktemp -d)"
pass=0; fail=0

ok()  { printf 'PASS  %-54s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-54s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

newrepo() {
  rm -rf "$ROOT/r"; mkdir -p "$ROOT/r/scripts"; cd "$ROOT/r" || exit 1
  git init -q -b main
  printf '#!/usr/bin/env bash\nexit 0\n' >scripts/harness.sh
  chmod +x scripts/harness.sh
  git add -A; git -c user.email=t@t -c user.name=t commit -qm base
}

gates() { printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full"}}' \
  >"$ROOT/r/.fluxpoint-gates.json"; }

# A PostToolUse payload for the Bash tool, exactly as the runtime shapes it.
#
# The runtime reports status in the SHAPE of tool_response, never in an
# exit_code field: a success arrives as an object carrying stdout/stderr/
# interrupted, and a failure arrives as a plain string beginning
# "Error: Exit code N". This fixture used to invent an exit_code, which is how
# attest.py came to read a field that has never existed while every test here
# still passed. A fixture that invents its own contract certifies nothing.
payload() { # $1 = command, $2 = exit code, $3 = stdout (optional)
  "$FPL_PY" - "$1" "$2" "${3:-}" <<'PY'
import json, os, sys
cmd, code, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
resp = ({"stdout": out, "stderr": "", "interrupted": False, "isImage": False}
        if code == 0 else f"Error: Exit code {code}\n{out}")
print(json.dumps({
    "session_id": "s1", "cwd": os.environ.get("PAYLOAD_CWD", "."), "hook_event_name": "PostToolUse",
    "tool_name": "Bash", "tool_use_id": "toolu_x",
    "tool_input": {"command": cmd},
    "tool_response": resp,
}))
PY
}

# The same payload carrying an explicit integer exit_code, which the runtime
# does not send today. Kept so the reader keeps honoring it if it ever does.
payload_with_field() { # $1 = command, $2 = exit code
  "$FPL_PY" - "$1" "$2" <<'PY'
import json, sys
print(json.dumps({
    "session_id": "s1", "cwd": ".", "hook_event_name": "PostToolUse",
    "tool_name": "Bash", "tool_use_id": "toolu_x",
    "tool_input": {"command": sys.argv[1]},
    "tool_response": {"exit_code": int(sys.argv[2]), "stdout": "", "stderr": ""},
}))
PY
}

# An interrupted run: the object arrives carrying no status at all.
payload_interrupted() { # $1 = command
  "$FPL_PY" - "$1" <<'PY'
import json, sys
print(json.dumps({
    "session_id": "s1", "tool_name": "Bash", "tool_use_id": "toolu_x",
    "tool_input": {"command": sys.argv[1]},
    "tool_response": {"stdout": "", "stderr": "", "interrupted": True},
}))
PY
}

rows() { [ -f "$ROOT/r/.claude/fluxpoint/attest.jsonl" ] \
  && grep -c . "$ROOT/r/.claude/fluxpoint/attest.jsonl" || echo 0; }
field() { # $1 = jsonl field of the last row
  "$FPL_PY" - "$ROOT/r/.claude/fluxpoint/attest.jsonl" "$1" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
print(rows[-1].get(sys.argv[2], "") if rows else "")
PY
}
rec() { payload "$@" | "$FPL_PY" "$ATTEST" --root "$ROOT/r" --record; }

# --- 1. dormant without a manifest --------------------------------------
newrepo
rec "scripts/harness.sh --full" 0 >/dev/null 2>&1
check "no manifest: nothing is attested" 0 "$(rows)"
check "no manifest: exits clean" 0 "$?"

# --- 2. a declared gate is attested with the runtime's exit -------------
newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
check "a declared gate is attested" 1 "$(rows)"
check "the recorded exit is the runtime's" 0 "$(field exit)"
check "the row names the gate" "harness" "$(field gate)"
[ -n "$(field attestId)" ] && ok "the row carries a citable attestId" "$(field attestId)" \
  || bad "the row carries a citable attestId" "empty"
[ -n "$(field headSha)" ] && ok "the row records the tree it judged" "headSha present" \
  || bad "the row records the tree it judged" "empty"

# A red gate is attested exactly as loudly as a green one.
rec "scripts/harness.sh --full" 1 >/dev/null
check "a red gate is attested too" 2 "$(rows)"
check "the red exit is recorded, not smoothed" 1 "$(field exit)"

# --- 3. invocation spelling that means the same thing -------------------
newrepo; gates
rec "bash scripts/harness.sh   --full" 0 >/dev/null
check "an interpreter prefix still matches" 1 "$(rows)"
rec "./scripts/harness.sh --full" 0 >/dev/null
check "a ./ prefix still matches" 2 "$(rows)"

# --- 4. laundering: anything that changes the exit must not match -------
# This is the case the whole file exists for. `|| true` reports 0 for a red
# harness; a pipe reports the last stage's exit. If either borrowed the
# gate's name, the log would attest a green that never happened.
newrepo; gates
for evil in \
  "scripts/harness.sh --full || true" \
  "scripts/harness.sh --full | tail -1" \
  "scripts/harness.sh --full 2>/dev/null" \
  "scripts/harness.sh --changed README.md" \
  "scripts/harness.sh --full; true" \
  "true # scripts/harness.sh --full"; do
  rec "$evil" 0 >/dev/null 2>&1
done
check "no laundered invocation is attested" 0 "$(rows)"

# --- 4b. a leading `cd` is exit-transparent, so it must NOT disarm the witness
# The trap this closes: `cd /repo && scripts/harness.sh --full` reports the
# HARNESS's exit, because cd is the first command and the gate is the last.
# Refusing it means a green gate run goes unwitnessed while looking witnessed,
# which is the exact failure mode this file exists to prevent — and it cost
# four real runs before anyone noticed.
newrepo; gates
rec "cd $ROOT/r && scripts/harness.sh --full" 0 >/dev/null
check "cd && gate is attested" 1 "$(rows)"
rec "cd $ROOT/r; scripts/harness.sh --full" 0 >/dev/null
check "cd ; gate is attested" 2 "$(rows)"
rec "cd $ROOT/r && bash scripts/harness.sh --full" 0 >/dev/null
check "cd with an interpreter prefix is attested" 3 "$(rows)"
# ...but the cd must land in THIS project, where the gate is declared to run:
# another checkout shares no commit with it, and a subdirectory runs the
# gate somewhere it was not declared.
rm -rf "$ROOT/foreign"; mkdir -p "$ROOT/foreign/scripts"; git -C "$ROOT/foreign" init -q -b main
rec "cd $ROOT/foreign && scripts/harness.sh --full" 0 >/dev/null
rec "cd /some/path; scripts/harness.sh --full" 0 >/dev/null
rec "cd $ROOT/r/scripts && scripts/harness.sh --full" 0 >/dev/null
check "a cd into another checkout, a missing or another directory is not" 3 "$(rows)"

# ...but ONLY a leading cd, and only when the gate is still last. Anything that
# can change the reported exit stays refused, cd or not.
newrepo; gates
for evil in \
  "cd /repo && scripts/harness.sh --full || true" \
  "cd /repo && scripts/harness.sh --full | tail -1" \
  "cd /repo; scripts/harness.sh --full; echo done" \
  "cd /repo && scripts/harness.sh --full && npm publish" \
  "rm -rf /tmp/x && scripts/harness.sh --full" \
  "cd /repo && cd /other && scripts/harness.sh --full"; do
  rec "$evil" 0 >/dev/null 2>&1
done
check "a cd prefix launders nothing else" 0 "$(rows)"

# --- 5. an unreadable exit code is said out loud, never assumed ---------
newrepo; gates
err="$("$FPL_PY" - <<'PY' | "$FPL_PY" "$ATTEST" --root "$ROOT/r" --record 2>&1 >/dev/null
import json
print(json.dumps({"session_id": "s1", "tool_name": "Bash",
                  "tool_input": {"command": "scripts/harness.sh --full"},
                  "tool_response": "ran it"}))
PY
)"
case "$err" in *"no readable exit status"*)
  ok "an unreadable exit is reported, not invented" "said" ;;
  *) bad "an unreadable exit is reported, not invented" "got: ${err:0:40}" ;; esac
check "and nothing is attested for it" 0 "$(rows)"

# --- 6. a malformed manifest disarms loudly, never silently -------------
newrepo
printf '{"version":1,"gates":{"harness":' >"$ROOT/r/.fluxpoint-gates.json"
err="$(rec "scripts/harness.sh --full" 0 2>&1 >/dev/null)"
case "$err" in *DISARMED*) ok "a malformed manifest says it is disarmed" "said" ;;
  *) bad "a malformed manifest says it is disarmed" "got: ${err:0:40}" ;; esac

printf '{"version":1,"gates":{"harness":"x"},"gate":"typo"}' >"$ROOT/r/.fluxpoint-gates.json"
err="$(rec "x" 0 2>&1 >/dev/null)"
case "$err" in *"unknown field 'gate'"*) ok "a misspelled manifest field is fatal" "rejected" ;;
  *) bad "a misspelled manifest field is fatal" "got: ${err:0:40}" ;; esac

printf '{"version":2,"gates":{"harness":"x"}}' >"$ROOT/r/.fluxpoint-gates.json"
err="$(rec "x" 0 2>&1 >/dev/null)"
case "$err" in *"version must be 1"*) ok "an unknown manifest version is fatal" "rejected" ;;
  *) bad "an unknown manifest version is fatal" "got: ${err:0:40}" ;; esac

printf '{"version":1,"gates":{"Harness":"x"}}' >"$ROOT/r/.fluxpoint-gates.json"
err="$(rec "x" 0 2>&1 >/dev/null)"
case "$err" in *"lowercase kebab-case"*) ok "a gate name must be referenceable" "rejected" ;;
  *) bad "a gate name must be referenceable" "got: ${err:0:40}" ;; esac

# --- 7. a corrupted log is a hard error, never a quiet shrink -----------
newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
printf 'not json\n' >>"$ROOT/r/.claude/fluxpoint/attest.jsonl"
err="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --list 2>&1 >/dev/null)"; rc=$?
case "$err$rc" in *"is not valid JSON"*) ok "a corrupted log fails hard on read" "hard error" ;;
  *) bad "a corrupted log fails hard on read" "rc=$rc ${err:0:40}" ;; esac

# --- 8. the cross-check, end to end through record-run.py --------------
summary() { # $1 = claimed exit
  "$FPL_PY" - "$1" <<'PY'
import json, sys
print(json.dumps({
    "campaign": "c", "outcome": "COMPLETE",
    "results": {"gate": {"exit": int(sys.argv[1]),
                         "command": "scripts/harness.sh --full", "tail": "ok"}},
    "contracts": {"gate": "HarnessCheckV1"},
    "provenance": [{"node": "gate", "status": "OK", "detail": ""}],
}))
PY
}
runrec() { summary "$1" | "$FPL_PY" "$RECORD" --run-id "$2" --graph WORK.md \
  --root "$ROOT/r" --state-dir "$ROOT/r/.claude/fluxpoint/runs" 2>&1; }

newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
out="$(runrec 0 wf-a)"
case "$out" in *"[ATTESTED]"*) ok "a claim matching the log is ATTESTED" "attested" ;;
  *) bad "a claim matching the log is ATTESTED" "got: ${out:0:60}" ;; esac

# The case this layer exists for: the gate really exited 1, the node says 0.
newrepo; gates
rec "scripts/harness.sh --full" 1 >/dev/null
out="$(runrec 0 wf-b)"
case "$out" in *"[MISMATCH]"*) ok "claiming green over an attested red is caught" "caught" ;;
  *) bad "claiming green over an attested red is caught" "got: ${out:0:60}" ;; esac
case "$out" in *"most recent exited 1"*) ok "the mismatch names the real exit" "named" ;;
  *) bad "the mismatch names the real exit" "got: ${out:0:60}" ;; esac
check "a mismatch raises an inbox item" 1 \
  "$("$FPL_PY" "$INBOX" --root "$ROOT/r" --count)"
# Warn mode: loud and filed, but the run is not rewritten yet. The corpus
# earns the enforcing version; a check that starts by failing runs gets
# switched off before it has established what normal looks like.
case "$out" in *"| COMPLETE |"*) ok "warn mode leaves the outcome alone" "COMPLETE" ;;
  *) bad "warn mode leaves the outcome alone" "got: ${out:0:60}" ;; esac
case "$out" in *"CONTRADICT the attested"*) ok "but the Evidence row says so" "in the row" ;;
  *) bad "but the Evidence row says so" "got: ${out:0:60}" ;; esac

# No attestation at all is distinct from a contradiction, and is not a failure.
newrepo; gates
out="$(runrec 0 wf-c)"
case "$out" in *"[UNATTESTED]"*) ok "an unattested gate claim is its own state" "unattested" ;;
  *) bad "an unattested gate claim is its own state" "got: ${out:0:60}" ;; esac
check "and raises nothing for a person" 0 \
  "$("$FPL_PY" "$INBOX" --root "$ROOT/r" --count)"

# A repo that declared no gates is not nagged about attestation at all.
newrepo
out="$(runrec 0 wf-d)"
case "$out" in *ATTEST*|*UNATTESTED*) bad "no manifest: record-run stays quiet" "spoke" ;;
  *) ok "no manifest: record-run stays quiet" "silent" ;; esac

# The artifact carries the tally, so provenance can be read after the fact.
newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
runrec 0 wf-e >/dev/null
t="$("$FPL_PY" - "$ROOT/r/.claude/fluxpoint/runs/wf-e.json" <<'PY'
import json, sys
print((json.load(open(sys.argv[1])).get("attestation") or {}).get("tally", {}).get("attested"))
PY
)"
check "the run artifact records the attestation tally" 1 "$t"

# ================= 9. prove: tiers are held to the log ===================
# A node declaring `verify: prove:<gate>` opted into being checked against
# the hook's own record. Nodes that merely happen to match a declared gate
# stay observed rather than enforced — the warn-mode lesson still holds for
# everyone who did not opt in.
prove_summary() { # $1 = claimed exit, $2 = cited attestId
  "$FPL_PY" - "$1" "$2" <<'PYEOF'
import json, sys
print(json.dumps({
    "campaign": "c", "outcome": "COMPLETE",
    "results": {"gate": {"gate": "harness", "exit": int(sys.argv[1]),
                         "attestId": sys.argv[2]}},
    "contracts": {"gate": "ExecutionV1"},
    "prove": {"gate": "harness"},
    # The compiled graph stamps its launch; a citation older than it is
    # an execution from another run (#91).
    "launch": {"since": "2000-01-01T00:00:00Z"},
    "provenance": [{"node": "gate", "status": "OK", "detail": ""}],
}))
PYEOF
}
runprove() { prove_summary "$1" "$2" | "$FPL_PY" "$RECORD" --run-id "$3" \
  --graph WORK.md --root "$ROOT/r" --state-dir "$ROOT/r/.claude/fluxpoint/runs" 2>&1; }

newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
ATT="$(field attestId)"
out="$(runprove 0 "$ATT" wf-p1)"
case "$out" in *"[ATTESTED]"*) ok "a prove: node citing a real attestation passes" "attested" ;;
  *) bad "a prove: node citing a real attestation passes" "${out:0:56}" ;; esac
case "$out" in *"| COMPLETE |"*) ok "and the run stays COMPLETE" "COMPLETE" ;;
  *) bad "and the run stays COMPLETE" "${out:0:56}" ;; esac

# The case this layer exists for: the gate really exited 1, the node claims
# 0, and it cites the very attestation that says otherwise.
newrepo; gates
rec "scripts/harness.sh --full" 1 >/dev/null
ATT="$(field attestId)"
out="$(runprove 0 "$ATT" wf-p2)"
case "$out" in *TAMPERED-EXECUTION*)
  ok "claiming green over the attestation it cites is TAMPERED" "caught" ;;
  *) bad "claiming green over the attestation it cites is TAMPERED" "${out:0:56}" ;; esac
case "$out" in *"| COMPLETE |"*)
  bad "and the run may not be filed COMPLETE" "filed clean" ;;
  *) ok "and the run may not be filed COMPLETE" "rewritten" ;; esac

# An attestId nobody minted.
newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
out="$(runprove 0 att_deadbeef1234 wf-p3)"
case "$out" in *TAMPERED-EXECUTION*)
  ok "citing an attestation that does not exist is TAMPERED" "caught" ;;
  *) bad "citing an attestation that does not exist is TAMPERED" "${out:0:56}" ;; esac

# No citation at all: the declared verification did not happen. Not
# tampering — an executor that never routes through the Bash tool leaves no
# rows — but not a clean run either.
newrepo; gates
out="$(runprove 0 "" wf-p4)"
case "$out" in *"[UNATTESTED]"*) ok "a prove: node citing nothing is UNATTESTED" "unattested" ;;
  *) bad "a prove: node citing nothing is UNATTESTED" "${out:0:56}" ;; esac
case "$out" in *"| INCOMPLETE |"*)
  ok "and the run is INCOMPLETE — neither tampered nor clean" "INCOMPLETE" ;;
  *) bad "and the run is INCOMPLETE — neither tampered nor clean" "${out:0:56}" ;; esac

# --- 9. every shape the runtime actually sends -------------------------
# The reason this section exists: with the old fixture inventing exit_code,
# all four of these were unreachable and the whole layer recorded nothing.

newrepo; gates
payload_with_field "scripts/harness.sh --full" 7 \
  | "$FPL_PY" "$ATTEST" --root "$ROOT/r" --record >/dev/null
check "an explicit exit_code is still honored if it arrives" 7 "$(field exit)"

newrepo; gates
rec "scripts/harness.sh --full" 0 "all green" >/dev/null
check "a success object with no exit_code attests 0" 0 "$(field exit)"

newrepo; gates
rec "scripts/harness.sh --full" 3 >/dev/null
check "an Error-Exit-code-N string attests N" 3 "$(field exit)"

newrepo; gates
payload_interrupted "scripts/harness.sh --full" \
  | "$FPL_PY" "$ATTEST" --root "$ROOT/r" --record >/dev/null 2>&1
check "an interrupted command attests nothing, never a pass" 0 "$(rows)"

# A backgrounded launch returns IMMEDIATELY with a success-shaped payload —
# stdout/stderr/interrupted, no exit code — before the gate has run at all,
# and completion arrives later as a task notification, never as another Bash
# PostToolUse. Reading that shape as exit 0 mints a pass that has not
# happened, and record-run trusts what it finds here.
payload_background() { # $1 = command  (deliberate run_in_background: true)
  "$FPL_PY" - "$1" <<'PY'
import json, sys
print(json.dumps({
    "session_id": "s1", "tool_name": "Bash", "tool_use_id": "toolu_x",
    "tool_input": {"command": sys.argv[1], "run_in_background": True},
    "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
}))
PY
}
payload_autobackground() { # $1 = command  (auto-background: id in the response)
  "$FPL_PY" - "$1" <<'PY'
import json, sys
print(json.dumps({
    "session_id": "s1", "tool_name": "Bash", "tool_use_id": "toolu_x",
    "tool_input": {"command": sys.argv[1]},
    "tool_response": {"stdout": "", "stderr": "", "interrupted": False,
                      "backgroundTaskId": "bg_123"},
}))
PY
}
newrepo; gates
payload_background "scripts/harness.sh --full" \
  | "$FPL_PY" "$ATTEST" --root "$ROOT/r" --record >/dev/null 2>&1
check "a backgrounded gate attests nothing — it has not run yet" 0 "$(rows)"
newrepo; gates
payload_autobackground "scripts/harness.sh --full" \
  | "$FPL_PY" "$ATTEST" --root "$ROOT/r" --record >/dev/null 2>&1
check "auto-backgrounded (backgroundTaskId) attests nothing" 0 "$(rows)"

cd /; rm -rf "$ROOT"
# --- Codex: every completed Bash call arrives as ONE string --------------
# Header lines first (`Wall time`, `Process exited with code N`), then
# `Output:` and the output. Passes and failures share the shape, so the exit
# is read from the header, and only from the header.
payload_codex() { # $1 = command, $2 = header line(s) after wall time, $3 = body
  "$FPL_PY" - "$1" "$2" "${3:-harness: green}" <<'PY'
import json, sys
print(json.dumps({
    "session_id": "s1", "cwd": ".", "hook_event_name": "PostToolUse",
    "tool_name": "Bash", "tool_use_id": "call_c",
    "tool_input": {"command": sys.argv[1]},
    "tool_response": "Wall time: 0.0100 seconds\n" + sys.argv[2] + "Output:\n" + sys.argv[3] + "\n",
}))
PY
}
reccodex() { payload_codex "$@" | "$FPL_PY" "$ATTEST" --root "$ROOT/r" --record; }
newrepo; gates
reccodex "scripts/harness.sh --full" $'Process exited with code 0\n' >/dev/null 2>&1
check "codex: 'Process exited with code 0' attests 0" 0 "$(field exit)"
reccodex "scripts/harness.sh --full" $'Process exited with code 3\n' >/dev/null 2>&1
check "codex: a red exit is recorded, not smoothed" 3 "$(field exit)"
reccodex "scripts/harness.sh --full" $'Exit code: 2\n' >/dev/null 2>&1
check "codex: the classic shell tool's 'Exit code: N' attests N" 2 "$(field exit)"
n="$(rows)"
reccodex "scripts/harness.sh --full" "" >/dev/null 2>&1
check "codex: a header with no exit line attests nothing" "$n" "$(rows)"
reccodex "scripts/harness.sh --full" "" $'Process exited with code 0' >/dev/null 2>&1
check "codex: output cannot mint its own exit" "$n" "$(rows)"

# ================= 10. a citation is bound to the run (#91) ==============
# Every check above compares the citation to its row. None compared the row
# to the run: any earlier attestation of the same gate with the same exit —
# another campaign's, weeks old — filed a node that never ran the gate as
# ATTESTED. The run's launch stamp, the commit its tree guard read, and one
# node per execution now bind it.
bound() { # $1 = python dict literal overriding summary fields
  "$FPL_PY" - "$1" <<'PYEOF'
import ast, json, os, sys
s = {"campaign": "c", "outcome": "COMPLETE",
     "results": {"gate": {"gate": "harness", "exit": 0, "attestId": "ATT"}},
     "contracts": {"gate": "ExecutionV1"}, "prove": {"gate": "harness"},
     "launch": {"since": "2000-01-01T00:00:00Z"},
     "provenance": [{"node": "gate", "status": "OK", "detail": ""}]}
s.update(ast.literal_eval(sys.argv[1]))
# The cited id arrives through the environment, so a literal placeholder
# can stand in for it anywhere in the override.
print(json.dumps(s).replace('"ATT"', json.dumps(os.environ.get("ATT", ""))))
PYEOF
}
runbound() { ATT="$ATT" bound "$1" | "$FPL_PY" "$RECORD" --run-id "$2" --graph WORK.md \
  --root "$ROOT/r" --state-dir "$ROOT/r/.claude/fluxpoint/runs" 2>&1; }

newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
ATT="$(field attestId)"
HEAD_SHA="$(git -C "$ROOT/r" rev-parse HEAD)"
out="$(runbound "{'launch': {'since': '2999-01-01T00:00:00Z'}}" wf-f1)"
case "$out" in *"[STALE]"*"before this run launched"*)
  ok "a citation minted before the launch is STALE" "stale" ;;
  *) bad "a citation minted before the launch is STALE" "${out:0:70}" ;; esac
case "$out" in *"| INCOMPLETE |"*)
  ok "  and the run is INCOMPLETE, never ATTESTED-clean" "INCOMPLETE" ;;
  *) bad "  and the run is INCOMPLETE, never ATTESTED-clean" "${out:0:70}" ;; esac
out="$(runbound "{'launch': None}" wf-f2)"
case "$out" in *"[STALE]"*"launch stamp"*)
  ok "a run carrying no launch stamp binds nothing" "stale" ;;
  *) bad "a run carrying no launch stamp binds nothing" "${out:0:70}" ;; esac
out="$(runbound "{'tree': {'baseline': {'head': '0000000000000000000000000000000000000000'}}}" wf-f3)"
case "$out" in *"[STALE]"*"commit"*)
  ok "a citation from another commit than the campaign's is STALE" "stale" ;;
  *) bad "a citation from another commit than the campaign's is STALE" "${out:0:70}" ;; esac
out="$(runbound "{'tree': {'baseline': {'head': '$HEAD_SHA'}}}" wf-f4)"
case "$out" in *"[ATTESTED]"*"| COMPLETE |"*)
  ok "the same commit and a fresh row stay ATTESTED" "attested" ;;
  *) bad "the same commit and a fresh row stay ATTESTED" "${out:0:70}" ;; esac
out="$(runbound "{'results': {'gate': {'gate': 'harness', 'exit': 0, 'attestId': 'ATT'}, 'gate2': {'gate': 'harness', 'exit': 0, 'attestId': 'ATT'}}, 'contracts': {'gate': 'ExecutionV1', 'gate2': 'ExecutionV1'}, 'prove': {'gate': 'harness', 'gate2': 'harness'}}" wf-f5)"
case "$out" in *"[ATTESTED]"*"[STALE]"*"already backs"*)
  ok "one execution cannot verify two nodes" "second is stale" ;;
  *) bad "one execution cannot verify two nodes" "${out:0:90}" ;; esac

# Two runs overlapping on one commit and one gate: a row the OTHER run
# minted after this run's launch passes every check above. The run nonce
# names whose execution a row is.
stamp="$("$FPL_PY" "$ATTEST" --stamp)"
"$FPL_PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if d["since"].endswith("Z") and len(d["nonce"]) == 16 else 1)' "$stamp" \
  && ok "--stamp mints a launch stamp {since, nonce}" "$stamp" \
  || bad "--stamp mints a launch stamp {since, nonce}" "$stamp"
newrepo; gates
rec "FPL_ATTEST_NONCE=run-a scripts/harness.sh --full" 0 >/dev/null
check "a nonce-prefixed gate run is still the declared gate" harness "$(field gate)"
# Quoted text is compared byte for byte, as bash passes it: two spaces in a
# test-name filter is another filter (one that matches nothing, and passes).
"$FPL_PY" -c 'import json,sys; json.dump({"version": 1, "gates": {"harness": "scripts/harness.sh --full", "slow": "node --test --test-name-pattern=\"slow test\" t.test.js"}}, open(sys.argv[1], "w"))' \
  "$ROOT/r/.fluxpoint-gates.json"
n="$(rows)"
rec 'node --test --test-name-pattern="slow  test" t.test.js' 0 >/dev/null
rec "$(printf 'node --test --test-name-pattern="slow\ttest" t.test.js')" 0 >/dev/null
check "whitespace inside quotes is not collapsed into the declared gate" "$n" "$(rows)"
rec 'node --test --test-name-pattern="slow test" t.test.js' 0 >/dev/null
check "  while the declared text itself is the gate" "$((n + 1))" "$(rows)"
# A command with quoting or expansion is compared as written: $(...) nests
# quotes a scanner misreads, so no whitespace in it is collapsed at all.
"$FPL_PY" -c 'import json,sys; json.dump({"version": 1, "gates": {"harness": "scripts/harness.sh --full", "subst": "node --test --test-name-pattern=\"$(echo \"slow test\")\" t.test.js"}}, open(sys.argv[1], "w"))' \
  "$ROOT/r/.fluxpoint-gates.json"
rec 'node --test --test-name-pattern="$(echo "slow  test")" t.test.js' 0 >/dev/null
rec 'node  --test --test-name-pattern="$(echo "slow test")" t.test.js' 0 >/dev/null
check "  and nested quoting is never collapsed into the declared gate" "$((n + 1))" "$(rows)"
rec 'node --test --test-name-pattern="$(echo "slow test")" t.test.js' 0 >/dev/null
check "  while its exact text is the gate" "$((n + 2))" "$(rows)"
gates
rec "FPL_ATTEST_NONCE=run-a scripts/harness.sh --full" 0 >/dev/null
check "  and its row names the run" run-a "$(field nonce)"
ATT="$(field attestId)"
out="$(runbound "{'launch': {'since': '2000-01-01T00:00:00Z', 'nonce': 'run-b'}}" wf-n1)"
case "$out" in *"[STALE]"*"another run minted"*)
  ok "an overlapping run's execution is STALE here" "stale" ;;
  *) bad "an overlapping run's execution is STALE here" "${out:0:70}" ;; esac
out="$(runbound "{'launch': {'since': '2000-01-01T00:00:00Z', 'nonce': 'run-a'}}" wf-n2)"
case "$out" in *"[ATTESTED]"*) ok "  and this run's own is ATTESTED" "attested" ;;
  *) bad "  and this run's own is ATTESTED" "${out:0:70}" ;; esac
rec "FPL_ATTEST_NONCE=x; rm -rf / scripts/harness.sh --full" 0 >/dev/null
check "a nonce cannot carry a second command in" run-a "$(field nonce)"
n="$(rows)"
rec "$(printf 'cd .\nexit 0 && scripts/harness.sh --full')" 0 >/dev/null
check "a line break before the gate is not the gate (exit 0 before it runs)" "$n" "$(rows)"
rec "cd . # && scripts/harness.sh --full" 0 >/dev/null
rec "FPL_ATTEST_NONCE=run-a cd . #&& scripts/harness.sh --full" 0 >/dev/null
rec 'cd $(true) && scripts/harness.sh --full' 0 >/dev/null
check "  nor is a comment, or an expansion, in the cd operand" "$n" "$(rows)"
rec "cd \"$ROOT/r\" && scripts/harness.sh --full" 0 >/dev/null
check "  while a quoted plain path still is" "$((n + 1))" "$(rows)"
# Whitespace bash does not split on: to bash `FPL_ATTEST_NONCE=n<NBSP>gate`
# is one assignment that runs nothing and exits 0.
n="$(rows)"
for ws in $'\xc2\xa0' $'\x0b' $'\x1f' $'\xe2\x80\x83'; do
  rec "FPL_ATTEST_NONCE=run-a${ws}scripts/harness.sh${ws}--full" 0 >/dev/null
done
check "whitespace bash does not split on never reads as a gate" "$n" "$(rows)"
rec 'cd "x\" && scripts/harness.sh --full' 0 >/dev/null
check "  nor does a double-quoted cd whose closing quote bash escapes" "$n" "$(rows)"

# ================= 11. a long gate, run in the background (#96) ==========
# The hook cannot see a backgrounded launch finish, and one foreground call
# is capped at 600 s. attest.py --run executes the DECLARED command itself
# (the agent names a gate, never a command) and mints the row when it ends;
# --await blocks in bounded foreground slices until it does.
newrepo
printf '#!/usr/bin/env bash\nsleep 1\nexit "${GATE_EXIT:-0}"\n' >scripts/harness.sh
gates
tok="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --run harness --detach --nonce run-c 2>&1 | sed -n 's/.* as \(bg_[0-9a-f]*\).*/\1/p' | head -1)"
[ -n "$tok" ] && ok "--run starts a declared gate and names its token" "$tok" \
  || bad "--run starts a declared gate and names its token" "no token"
out="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --await "$tok" --timeout 30 2>&1)"; rc=$?
check "--await returns once the gate has finished" 0 "$rc"
check "  and the row is the gate's, witnessed by the runner" wrapper "$(field witness)"
check "  with the gate's own exit" 0 "$(field exit)"
check "  and the run nonce it was started with" run-c "$(field nonce)"
[ -n "$(field started)" ] && ok "  and the moment it started" "$(field started)" \
  || bad "  and the moment it started" "missing"
BGATT="$(field attestId)"
case "$out" in *"$BGATT"*) ok "  and --await prints the attestId to cite" "$BGATT" ;;
  *) bad "  and --await prints the attestId to cite" "${out:0:60}" ;; esac
out="$(ATT="$BGATT" runbound "{'tree': {'baseline': {'head': '$(git -C "$ROOT/r" rev-parse HEAD)'}}}" wf-bg1)"
case "$out" in *"[ATTESTED]"*) ok "a prove: node citing a runner row is ATTESTED" "attested" ;;
  *) bad "a prove: node citing a runner row is ATTESTED" "${out:0:70}" ;; esac
# The hook never sees a failing Bash call, so a red gate was never
# attested. The runner sees every exit.
GATE_EXIT=3 "$FPL_PY" "$ATTEST" --root "$ROOT/r" --run harness >/dev/null 2>&1; rc=$?
check "a red gate run through the runner exits red" 3 "$rc"
check "  and IS attested, unlike a failing hook call" 3 "$(field exit)"
tok="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --run harness --detach 2>&1 | sed -n 's/.* as \(bg_[0-9a-f]*\).*/\1/p' | head -1)"
"$FPL_PY" "$ATTEST" --root "$ROOT/r" --await "$tok" --timeout 0 >/dev/null 2>&1; rc=$?
check "--await on a gate still running says so (exit 3)" 3 "$rc"
"$FPL_PY" "$ATTEST" --root "$ROOT/r" --await "$tok" --timeout 30 >/dev/null 2>&1
mkdir -p "$ROOT/r/.claude/fluxpoint/attest-bg"
printf '{"token":"bg_dead00000000","gate":"harness","status":"RUNNING","pid":999999,"started":"2026-01-01T00:00:00Z"}' \
  >"$ROOT/r/.claude/fluxpoint/attest-bg/bg_dead00000000.json"
out="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --await bg_dead00000000 --timeout 5 2>&1)"; rc=$?
check "a runner that died attests nothing (exit 4)" 4 "$rc"
"$FPL_PY" "$ATTEST" --root "$ROOT/r" --run 'rm -rf .' >/dev/null 2>&1; rc=$?
check "--run names a gate, never a command" 2 "$rc"

# ================= 12. CI's own statuses as a gate (#96) =================
# The least forgeable evidence a merge rests on is produced outside the
# agent entirely. attest.py --ci asks the forge for the statuses on a sha
# and mints a row the way the hook does for a local gate; prove:ci cites it.
newrepo
mkdir -p "$ROOT/bin"
cat >"$ROOT/bin/gh" <<'SH'
#!/usr/bin/env bash
# A stand-in forge: the state of every context comes from FAKE_CI.
case "$*" in
  *"/status"*) printf '{"state":"%s","statuses":[{"context":"harness","state":"%s"}]}' \
                 "${FAKE_CI:-success}" "${FAKE_CI:-success}" ;;
  *"/check-runs"*) printf '{"total_count":1,"check_runs":[{"name":"lint","status":"completed","conclusion":"success"}]}' ;;
  *"/pulls/"*) printf '{"head":{"sha":"%s"}}' "${FAKE_HEAD:-abc1234def5678}" ;;
  *) exit 1 ;;
esac
SH
chmod +x "$ROOT/bin/gh"
printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full"},"ci":{"forge":"github","contexts":["harness","lint"]}}' \
  >"$ROOT/r/.fluxpoint-gates.json"
PATH="$ROOT/bin:$PATH" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --ci --sha abc1234 >/dev/null 2>&1; rc=$?
check "--ci over green statuses exits 0" 0 "$rc"
check "  and mints a ci row" ci "$(field gate)"
check "  witnessed by the forge" forge "$(field witness)"
check "  for the sha it was asked about" abc1234 "$(field headSha)"
SHAATT="$(field attestId)"
cici() { # $1 = attestId, $2 = claimed sha ('' for none), $3 = run id
  ATT="$1" bound "{'results': {'merge-gate': {'gate': 'ci', 'exit': 0, 'attestId': 'ATT'$( [ -n "$2" ] && printf ", 'sha': '%s'" "$2")}}, 'contracts': {'merge-gate': 'ExecutionV1'}, 'prove': {'merge-gate': 'ci'}}" \
    | "$FPL_PY" "$RECORD" --run-id "$3" --graph WORK.md --root "$ROOT/r" \
      --state-dir "$ROOT/r/.claude/fluxpoint/runs" 2>&1
}
# A sha the node chose could be CI's verdict on any older green commit.
out="$(cici "$SHAATT" abc1234 wf-ci0)"
case "$out" in *"[STALE]"*"the node named itself"*)
  ok "a forge row for a sha the node chose cannot back prove:ci" "stale" ;;
  *) bad "a forge row for a sha the node chose cannot back prove:ci" "${out:0:70}" ;; esac
PATH="$ROOT/bin:$PATH" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --ci --pr 7 >/dev/null 2>&1; rc=$?
check "--ci --pr asks the forge for the pull request's head" 0 "$rc"
check "  and records which pull request named it" pr:7 "$(field target)"
check "  and the commit the forge named" abc1234def5678 "$(field headSha)"
CIATT="$(field attestId)"
out="$(cici "$CIATT" abc1234def5678 wf-ci1)"
case "$out" in *"[ATTESTED]"*"| COMPLETE |"*)
  ok "a prove:ci node citing the forge row is ATTESTED" "attested" ;;
  *) bad "a prove:ci node citing the forge row is ATTESTED" "${out:0:70}" ;; esac
out="$(cici "$CIATT" "" wf-ci2)"
case "$out" in *"[STALE]"*"does not name the commit"*)
  ok "  but only when the claim names the commit, so the merge can pin it" "stale" ;;
  *) bad "  but only when the claim names the commit, so the merge can pin it" "${out:0:70}" ;; esac
out="$(cici "$CIATT" 0000000aaaa wf-ci3)"
case "$out" in *TAMPERED-EXECUTION*)
  ok "  and a claimed commit the forge did not judge is TAMPERED" "caught" ;;
  *) bad "  and a claimed commit the forge did not judge is TAMPERED" "${out:0:70}" ;; esac
FAKE_CI=failure PATH="$ROOT/bin:$PATH" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --ci --sha abc1234 >/dev/null 2>&1; rc=$?
check "a failing context is a red ci row" 1 "$rc"
check "  recorded, not smoothed" 1 "$(field exit)"
n="$(rows)"
FAKE_CI=pending PATH="$ROOT/bin:$PATH" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --ci --sha abc1234 >/dev/null 2>&1; rc=$?
check "pending statuses are not a verdict (exit 3)" 3 "$rc"
check "  and mint no row" "$n" "$(rows)"
printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full"},"ci":{"forge":"github","contexts":["harness","deploy-preview"]}}' \
  >"$ROOT/r/.fluxpoint-gates.json"
PATH="$ROOT/bin:$PATH" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --ci --sha abc1234 >/dev/null 2>&1; rc=$?
check "a required context the forge never reported is not green" 3 "$rc"
printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full"},"ci":{"forge":"gitlab"}}' \
  >"$ROOT/r/.fluxpoint-gates.json"
"$FPL_PY" "$ATTEST" --root "$ROOT/r" --list >/dev/null 2>&1; rc=$?
check "an unsupported forge is a manifest finding" 1 "$rc"

# ================= 13. review findings on the witnesses ====================
# The instructed form for a declared gate that itself starts with `cd`:
# the nonce goes in front of the whole declared command, or after its cd.
newrepo
printf '{"version":1,"gates":{"harness":"cd scripts && ./harness.sh --full"}}' \
  >"$ROOT/r/.fluxpoint-gates.json"
rec "FPL_ATTEST_NONCE=run-d cd scripts && ./harness.sh --full" 0 >/dev/null
check "a nonce in front of a gate's own leading cd still matches" run-d "$(field nonce)"
rec "cd scripts && FPL_ATTEST_NONCE=run-e ./harness.sh --full" 0 >/dev/null
check "  and so does one after it" run-e "$(field nonce)"
n="$(rows)"
rec "FPL_ATTEST_NONCE=run-f cd scripts && FPL_ATTEST_NONCE=run-g ./harness.sh --full" 0 >/dev/null
check "  but never two nonces" "$n" "$(rows)"
# A gate declared with its own `cd` is identified by that directory too: the
# same command run in another component of the same commit is not it.
rec "cd other && ./harness.sh --full" 0 >/dev/null
check "a declared cd gate run in another directory is not attested" "$n" "$(rows)"
rec "cd $ROOT/r/scripts && ./harness.sh --full" 0 >/dev/null
check "  while an absolute path ending in the declared directory is" "$((n + 1))" "$(rows)"
mkdir -p "$ROOT/foreign/scripts"
rec "cd $ROOT/foreign/scripts && ./harness.sh --full" 0 >/dev/null
check "  but not the same-named directory of another checkout" "$((n + 1))" "$(rows)"
# Where bash RAN is the shell's directory plus the cd: a relative cd, or no
# cd at all, from another checkout is not the project's gate.
PAYLOAD_CWD="$ROOT/foreign" rec "cd scripts && ./harness.sh --full" 0 >/dev/null
check "  nor a relative cd made from another checkout" "$((n + 1))" "$(rows)"
# A quoted ~ is literal to bash; only a bare one is home.
HOME="$ROOT" rec "cd '~/r/scripts' && ./harness.sh --full" 0 >/dev/null
check "  nor a quoted ~, which bash does not expand" "$((n + 1))" "$(rows)"
HOME="$ROOT" rec "cd ~/r/scripts && ./harness.sh --full" 0 >/dev/null
check "  while a bare ~ is home" "$((n + 2))" "$(rows)"
# Bash forms the matcher does not model are refused, not guessed: ~+ ($PWD),
# a relative cd under CDPATH, and a reported directory that is gone.
rec "cd ~+/scripts && ./harness.sh --full" 0 >/dev/null
CDPATH="$ROOT/foreign" rec "cd scripts && ./harness.sh --full" 0 >/dev/null
PAYLOAD_CWD="$ROOT/gone" rec "cd $ROOT/r/scripts && ./harness.sh --full" 0 >/dev/null
check "  nor ~+, a relative cd under CDPATH, or a vanished cwd" "$((n + 2))" "$(rows)"
CDPATH="$ROOT/foreign" rec "cd ./scripts && ./harness.sh --full" 0 >/dev/null
check "  while ./ is immune to CDPATH, as in bash" "$((n + 3))" "$(rows)"
# A declared cd that climbs or is absolute is resolved from the project root.
printf '{"version":1,"gates":{"harness":"cd scripts/.. && scripts/harness.sh --full"}}' \
  >"$ROOT/r/.fluxpoint-gates.json"
rec "cd scripts/.. && scripts/harness.sh --full" 0 >/dev/null
check "a gate declared with cd .. is witnessed where it resolves" "$((n + 4))" "$(rows)"
printf '{"version":1,"gates":{"harness":"cd scripts && ./harness.sh --full"}}' \
  >"$ROOT/r/.fluxpoint-gates.json"
out="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --last harness --nonce run-d 2>&1)"
case "$out" in "$(sed -n 1p "$ROOT/r/.claude/fluxpoint/attest.jsonl" | "$FPL_PY" -c 'import json,sys; print(json.load(sys.stdin)["attestId"])')"*)
  ok "--last prints the attestId a node cites, for its nonce" "${out:0:40}" ;;
  *) bad "--last prints the attestId a node cites, for its nonce" "${out:0:60}" ;; esac
"$FPL_PY" "$ATTEST" --root "$ROOT/r" --last harness --nonce nobody >/dev/null 2>&1; rc=$?
check "  and exits 1 when this run attested nothing" 1 "$rc"

# A gate run through the wrapper from a linked worktree of the campaign
# branch attests into the PROJECT's log, bound to the project's HEAD as the
# hook would be, with the tree it actually ran on recorded beside it.
newrepo; gates
git -C "$ROOT/r" checkout -qb campaign
echo change >"$ROOT/r/f.txt"; git -C "$ROOT/r" add f.txt
git -C "$ROOT/r" -c user.email=t@t -c user.name=t commit -qm campaign
git -C "$ROOT/r" checkout -q main
rm -rf "$ROOT/wt"; git -C "$ROOT/r" worktree add -q --detach "$ROOT/wt" campaign
MAIN_HEAD="$(git -C "$ROOT/r" rev-parse HEAD)"; WT_HEAD="$(git -C "$ROOT/wt" rev-parse HEAD)"
n="$(rows)"
rec "cd $ROOT/wt && scripts/harness.sh --full" 0 >/dev/null
check "the hook witnesses a cd into a linked worktree of this repository" "$((n + 1))" "$(rows)"
(cd "$ROOT/wt" && CLAUDE_PROJECT_DIR="$ROOT/r" "$FPL_PY" "$ATTEST" --run harness --nonce run-w >/dev/null 2>&1)
check "a --run from a worktree lands in the project's log" run-w "$(field nonce)"
check "  bound to the project's HEAD, as the hook binds it" "$MAIN_HEAD" "$(field headSha)"
check "  with the tree it ran on beside it" "$WT_HEAD" "$(field treeSha)"
[ ! -e "$ROOT/wt/.claude/fluxpoint/attest.jsonl" ] && ok "  and nothing in the worktree's own" "clean" \
  || bad "  and nothing in the worktree's own" "a stray log"
# Without CLAUDE_PROJECT_DIR (Codex) a worktree may be the session's own
# project, so nothing is guessed from git: the log stays where it was run,
# and the node is handed the project explicitly instead (--root, stamped).
cp "$ROOT/r/.fluxpoint-gates.json" "$ROOT/wt/"  # a worktree session has its own (committed) manifest
(cd "$ROOT/wt" && env -u CLAUDE_PROJECT_DIR "$FPL_PY" "$ATTEST" --run harness --nonce run-x >/dev/null 2>&1)
[ -f "$ROOT/wt/.claude/fluxpoint/attest.jsonl" ] && grep -q run-x "$ROOT/wt/.claude/fluxpoint/attest.jsonl" \
  && ok "  with no project variable, a worktree session keeps its own log" "its own" \
  || bad "  with no project variable, a worktree session keeps its own log" "moved"
rm -rf "$ROOT/wt/.claude" "$ROOT/wt/.fluxpoint-gates.json"
(cd "$ROOT/wt" && env -u CLAUDE_PROJECT_DIR "$FPL_PY" "$ATTEST" --root "$ROOT/r" --run harness --nonce run-y >/dev/null 2>&1)
check "  and --root names the project from a worktree" run-y "$(field nonce)"
check "    running the gate in the worktree it was called from" "$WT_HEAD" "$(field treeSha)"
stamp="$(cd "$ROOT/r" && env -u CLAUDE_PROJECT_DIR "$FPL_PY" "$ATTEST" --stamp)"
case "$stamp" in *'"root"'*) ok "the launch stamp names the project root" "root" ;;
  *) bad "the launch stamp names the project root" "$stamp" ;; esac
stamp="$(cd / && env -u CLAUDE_PROJECT_DIR "$FPL_PY" "$ATTEST" --stamp --root "$ROOT/r")"
case "$stamp" in *"\"root\": \"$ROOT/r\""*) ok "  and --root names it from anywhere" "root" ;;
  *) bad "  and --root names it from anywhere" "$stamp" ;; esac
printf '{"version":1,"gates":{"unit":"cd .\\npytest -q"}}' >"$ROOT/r/.fluxpoint-gates.json"
out="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --list 2>&1)"; rc=$?
case "$rc:$out" in 1:*"one command line"*) ok "a declared gate with a line break is a manifest finding" "refused" ;;
  *) bad "a declared gate with a line break is a manifest finding" "$rc ${out:0:60}" ;; esac
gates
git -C "$ROOT/r" worktree remove --force "$ROOT/wt" >/dev/null 2>&1
# From a subdirectory of the project the declared command still runs at the
# project root: `scripts/harness.sh` from scripts/ used to run there.
(cd "$ROOT/r/scripts" && CLAUDE_PROJECT_DIR="$ROOT/r" "$FPL_PY" "$ATTEST" --run harness >/dev/null 2>&1); rc=$?
check "--run from a project subdirectory runs the gate at the root" 0 "$rc"
# A relative --root with --detach: the child starts in that directory and
# used to resolve the root a second time, finding no manifest.
tok="$(cd "$ROOT" && "$FPL_PY" "$ATTEST" --root r --run harness --detach 2>&1 | sed -n 's/.* as \(bg_[0-9a-f]*\).*/\1/p' | head -1)"
(cd "$ROOT" && "$FPL_PY" "$ATTEST" --root r --await "$tok" --timeout 30 >/dev/null 2>&1); rc=$?
check "--detach with a relative --root still runs the gate" 0 "$rc"
# The bash the gate runs under is the one py.sh names, not whichever
# "bash" the OS resolves first (System32's is WSL's on Windows).
printf '#!/usr/bin/env bash\ntouch "%s/used-fpl-bash"\nexec bash "$@"\n' "$ROOT" >"$ROOT/bin-bash"
chmod +x "$ROOT/bin-bash"; rm -f "$ROOT/used-fpl-bash"
FPL_BASH="$ROOT/bin-bash" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --run harness >/dev/null 2>&1
[ -f "$ROOT/used-fpl-bash" ] && ok "--run starts the gate with FPL_BASH" "used" \
  || bad "--run starts the gate with FPL_BASH" "not used"
# A runner that writes DONE between --await's read and its liveness probe
# finished; it did not die.
out="$("$FPL_PY" - "$ATTEST" "$ROOT/r" <<'PYEOF' 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("attest", sys.argv[1])
at = importlib.util.module_from_spec(spec); spec.loader.exec_module(at)
root = sys.argv[2]
at._bg_write(root, {"token": "bg_race0000000", "gate": "harness", "status": "RUNNING",
                    "pid": 999999, "started": at.now()})
def finish(pid, started=""):
    at._bg_write(root, {"token": "bg_race0000000", "gate": "harness", "status": "DONE",
                        "exit": 0, "attestId": "att_race", "started": started})
    return False
at._alive = finish
print("rc", at.await_gate(root, "bg_race0000000", 5))
PYEOF
)"
case "$out" in *"att_race"*"rc 0"*) ok "--await re-reads before calling a runner gone" "done" ;;
  *) bad "--await re-reads before calling a runner gone" "${out:0:70}" ;; esac

# The forge pages its listings (30 by default). A failing check on page two
# of a matrix CI used to be invisible, and the commit attested green.
newrepo
mkdir -p "$ROOT/bin2"
cat >"$ROOT/bin2/gh" <<'SH'
#!/usr/bin/env bash
page="$(printf '%s' "$*" | sed -n 's/.*[?&]page=\([0-9]*\).*/\1/p')"; page="${page:-1}"
per="$(printf '%s' "$*" | sed -n 's/.*per_page=\([0-9]*\).*/\1/p')"; per="${per:-30}"
[ -n "${FAKE_IGNORE_PAGING:-}" ] && { page=1; per=30; }
case "$*" in
  *"/status"*) printf '{"total_count":0,"statuses":[]}' ;;
  *"/check-runs"*)
    start=$(( (page - 1) * per )); end=$(( start + per )); [ "$end" -gt 35 ] && end=35
    printf '{"total_count":35,"check_runs":['
    sep=''
    for i in $(seq "$start" $(( end - 1 ))); do
      c=success; [ "$i" -eq 32 ] && c=failure
      printf '%s{"name":"matrix (%d)","status":"completed","conclusion":"%s"}' "$sep" "$i" "$c"; sep=','
    done
    printf ']}' ;;
  *) exit 1 ;;
esac
SH
chmod +x "$ROOT/bin2/gh"
printf '{"version":1,"gates":{},"ci":{"forge":"github"}}' >"$ROOT/r/.fluxpoint-gates.json"
"$FPL_PY" "$ATTEST" --root "$ROOT/r" --list >/dev/null 2>&1; rc=$?
check "a manifest whose only witness is the forge is accepted" 0 "$rc"
# ...and its prove:ci citations are still checked: a fabricated one is not
# filed clean for want of a local gate.
out="$(ATT=att_fabricated02 cici att_fabricated02 abc1234 wf-cionly)"
case "$out" in *TAMPERED-EXECUTION*) ok "  and a prove:ci citation under it is still checked" "caught" ;;
  *) bad "  and a prove:ci citation under it is still checked" "${out:0:90}" ;; esac
PATH="$ROOT/bin2:$PATH" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --ci --sha abc1234 >/dev/null 2>&1; rc=$?
check "a failing check past the first page is a red ci row" 1 "$rc"
n="$(rows)"
FAKE_IGNORE_PAGING=1 PATH="$ROOT/bin2:$PATH" "$FPL_PY" "$ATTEST" --root "$ROOT/r" --ci --sha abc1234 >/dev/null 2>&1; rc=$?
check "  and a listing shorter than its own total is no verdict" 3 "$rc"
check "  and mints no row" "$n" "$(rows)"

# A manifest the witness refuses mints nothing, so no citation in a run that
# declared prove: nodes can be checked — that run is not clean.
newrepo
printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full","ci":"make ci"}}' \
  >"$ROOT/r/.fluxpoint-gates.json"
out="$("$FPL_PY" "$ATTEST" --root "$ROOT/r" --list 2>&1)"
case "$out" in *"rename this gate"*) ok "a gate named ci is told how to migrate" "rename" ;;
  *) bad "a gate named ci is told how to migrate" "${out:0:70}" ;; esac
out="$(ATT=att_fabricated01 runbound "{}" wf-m1)"
case "$out" in *"| INCOMPLETE |"*) ok "  and a prove: run under a refused manifest is INCOMPLETE" "INCOMPLETE" ;;
  *) bad "  and a prove: run under a refused manifest is INCOMPLETE" "${out:0:90}" ;; esac

# STALE beside a claimed green with no row: both are said.
newrepo; gates
rec "scripts/harness.sh --full" 0 >/dev/null
ATT="$(field attestId)"
out="$(runbound "{'launch': {'since': '2999-01-01T00:00:00Z'}, 'results': {'gate': {'gate': 'harness', 'exit': 0, 'attestId': 'ATT'}, 'gate2': {'gate': 'harness', 'exit': 0}}, 'contracts': {'gate': 'ExecutionV1', 'gate2': 'ExecutionV1'}, 'prove': {'gate': 'harness', 'gate2': 'harness'}}" wf-s1)"
case "$out" in *"claim exit 0 with no row"*) ok "a STALE citation no longer hides a claimed green with no row" "said" ;;
  *) bad "a STALE citation no longer hides a claimed green with no row" "${out:0:90}" ;; esac
case "$out" in *"STALE — "*"UNATTESTED — "*) ok "  and the Evidence claim names both" "both" ;;
  *) bad "  and the Evidence claim names both" "${out:0:90}" ;; esac

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
