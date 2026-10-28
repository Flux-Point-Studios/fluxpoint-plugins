#!/usr/bin/env bash
# Hook wiring and PostToolUse behavior.
#
# The other suites call the hook scripts directly, which never exercises the
# thing that actually runs in a session: the command strings in hooks.json,
# with ${CLAUDE_PLUGIN_ROOT} expanded. This suite reads those strings out of
# hooks.json and runs them, so a broken path or a renamed script fails here
# instead of silently disarming the harness in someone's session.
#
# It also covers verify-changed.sh, which had no test at all.
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
export CLAUDE_PLUGIN_ROOT="$PLUGIN"
ROOT="$(mktemp -d)"
pass=0; fail=0

ok()   { printf 'PASS  %-52s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad()  { printf 'FAIL  %-52s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

# The command strings the runtime actually executes.
# An event may carry several groups (PostToolUse has one per tool family),
# so both helpers take an optional group index. Defaulting to 0 keeps the
# older cases reading as they did.
hook_cmd() {
  "$FPL_PY" - "$PLUGIN/hooks/hooks.json" "$1" "${2:-0}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
groups = d["hooks"].get(sys.argv[2], [])
i = int(sys.argv[3])
if i < len(groups):
    for h in groups[i]["hooks"]:
        print(h["command"])
        break
PY
}
hook_matcher() {
  "$FPL_PY" - "$PLUGIN/hooks/hooks.json" "$1" "${2:-0}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
groups = d["hooks"].get(sys.argv[2], [])
i = int(sys.argv[3])
if i < len(groups):
    print(groups[i].get("matcher", ""))
PY
}

newrepo() { # $1 = harness exit code, or "none" for no harness
  rm -rf "$ROOT/r"; mkdir -p "$ROOT/r/scripts" "$ROOT/r/src"; cd "$ROOT/r" || exit 1
  git init -q -b main
  if [ "$1" != "none" ]; then
    printf '#!/usr/bin/env bash\nexit %s\n' "$1" >scripts/harness.sh
    chmod +x scripts/harness.sh
  fi
  printf 'x = 1\n' >src/app.py
  git add -A; git -c user.email=t@t -c user.name=t commit -qm base
}

post_input() { printf '{"session_id":"s","cwd":"%s","tool_input":{"file_path":"%s"}}' "$ROOT/r" "$1"; }

# --- 1. every hook command in hooks.json resolves and runs ---
# Every group of every event, not just the first: a second group whose script
# was renamed would otherwise disarm silently.
for spec in "SessionStart 0" "PostToolUse 0" "PostToolUse 1" "PreCompact 0" "Stop 0"; do
  set -- $spec; ev="$1"; idx="$2"
  cmd="$(hook_cmd "$ev" "$idx")"
  [ -n "$cmd" ] || { bad "$ev[$idx]: command present in hooks.json" "missing"; continue; }
  # Expand ${CLAUDE_PLUGIN_ROOT} exactly as the runtime does, then check the
  # target exists before running it.
  target="$(eval "printf '%s' \"$(printf '%s' "$cmd" | sed 's/^bash //')\"")"
  if [ -f "$target" ]; then ok "$ev[$idx]: script exists at plugin root" "$(basename "$target")"
  else bad "$ev[$idx]: script exists at plugin root" "missing $target"; fi
done

# --- 2. the hook commands execute without error on a clean repo ---
newrepo 0
out="$(post_input src/app.py | eval "$(hook_cmd SessionStart)" 2>&1)"; rc=$?
check "SessionStart: runs via its hooks.json command" 0 "$rc"
case "$out" in *"fluxpoint work context"*) ok "SessionStart: injects context" "yes" ;;
  *) bad "SessionStart: injects context" "no" ;; esac

# --- 3. PostToolUse matchers cover the file-writing tools, then Bash ---
m="$(hook_matcher PostToolUse 0)"
check "PostToolUse[0]: matcher is Write|Edit|MultiEdit" "Write|Edit|MultiEdit" "$m"
m="$(hook_matcher PostToolUse 1)"
check "PostToolUse[1]: matcher is Bash" "Bash" "$m"

# --- 3a. the Bash hook attests gate runs and is otherwise dormant ---
bash_input() { # $1 = command, $2 = exit code
  "$FPL_PY" - "$ROOT/r" "$1" "$2" <<'PY'
import json, sys
code = int(sys.argv[3])
resp = ({"stdout": "", "stderr": "", "interrupted": False}
        if code == 0 else f"Error: Exit code {code}\n")
print(json.dumps({"session_id": "s", "cwd": sys.argv[1], "tool_name": "Bash",
                  "tool_use_id": "toolu_x",
                  "tool_input": {"command": sys.argv[2]},
                  "tool_response": resp}))
PY
}
newrepo 0
bash_input "scripts/harness.sh --full" 0 | eval "$(hook_cmd PostToolUse 1)" >/dev/null 2>&1
check "exec-attest: no manifest, no attestation, no error" 0 "$?"
[ -f .claude/fluxpoint/attest.jsonl ] \
  && bad "exec-attest: dormant without a manifest" "wrote a log" \
  || ok "exec-attest: dormant without a manifest" "no log"

printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full"}}' >.fluxpoint-gates.json
bash_input "scripts/harness.sh --full" 1 | eval "$(hook_cmd PostToolUse 1)" >/dev/null 2>&1
check "exec-attest: runs via its hooks.json command" 0 "$?"
if [ -f .claude/fluxpoint/attest.jsonl ] && grep -q '"exit": 1' .claude/fluxpoint/attest.jsonl; then
  ok "exec-attest: the runtime's red exit is what lands" "exit 1 recorded"
else
  bad "exec-attest: the runtime's red exit is what lands" "not recorded"
fi

# A hook that can no longer attest must say so — once, not per command.
printf 'broken' >.fluxpoint-gates.json
out1="$(bash_input "scripts/harness.sh --full" 0 | eval "$(hook_cmd PostToolUse 1)" 2>/dev/null)"
out2="$(bash_input "scripts/harness.sh --full" 0 | eval "$(hook_cmd PostToolUse 1)" 2>/dev/null)"
case "$out1" in *DISARMED*) ok "exec-attest: a broken manifest is announced" "announced" ;;
  *) bad "exec-attest: a broken manifest is announced" "silent: ${out1:0:40}" ;; esac
[ -z "$out2" ] && ok "exec-attest: announced once per session, not per command" "quiet after" \
  || bad "exec-attest: announced once per session, not per command" "repeated"

# A MULTI-REPO WORKSPACE: the project dir is the workspace root and is NOT a git
# repo, and the gates manifest lives in a repo one level down, where the command
# actually ran. This is the shape the substrate registry is built around — many
# repos under one root — and in it every hook that resolves CLAUDE_PROJECT_DIR
# ahead of the payload's own cwd lands outside the repo and exits before it can
# find anything. secret-guard already settled this ("the payload's own cwd beats
# CLAUDE_PROJECT_DIR"); the shell hooks had not been brought along.
ws="$ROOT/ws"; rm -rf "$ws"; mkdir -p "$ws/repo/scripts"
( cd "$ws/repo" && git init -q -b main \
  && printf '#!/usr/bin/env bash\nexit 0\n' >scripts/harness.sh && chmod +x scripts/harness.sh \
  && printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full"}}' >.fluxpoint-gates.json \
  && git add -A && git -c user.email=t@t -c user.name=t commit -qm base )
ws_input() { # $1 = cwd the command ran in, $2 = command, $3 = exit code
  "$FPL_PY" - "$1" "$2" "$3" <<'WSPY'
import json, sys
code = int(sys.argv[3])
resp = ({"stdout": "", "stderr": "", "interrupted": False}
        if code == 0 else "Error: Exit code %d\n" % code)
print(json.dumps({"session_id": "ws", "cwd": sys.argv[1], "tool_name": "Bash",
                  "tool_use_id": "toolu_ws",
                  "tool_input": {"command": sys.argv[2]},
                  "tool_response": resp}))
WSPY
}
( cd "$ws/repo" && ws_input "$ws/repo" "scripts/harness.sh --full" 0 \
  | CLAUDE_PROJECT_DIR="$ws" eval "$(hook_cmd PostToolUse 1)" >/dev/null 2>&1 )
if [ -f "$ws/repo/.claude/fluxpoint/attest.jsonl" ]; then
  ok "exec-attest: attests in a repo BELOW a non-git project dir" "recorded"
else
  bad "exec-attest: attests in a repo BELOW a non-git project dir" "no log — resolved the workspace root"
fi
# ...and from a session whose shell sits in a SUBDIRECTORY of that repo: the
# hook still finds the repo. The gate itself must run at the project root
# (a bare gate from the subdirectory would run only that subtree), so the
# command cds there, as an agent in a subdirectory does.
rm -rf "$ws/repo/.claude"; mkdir -p "$ws/repo/sub"
( cd "$ws/repo/sub" && ws_input "$ws/repo/sub" "scripts/harness.sh --full" 0 \
  | CLAUDE_PROJECT_DIR="$ws" eval "$(hook_cmd PostToolUse 1)" >/dev/null 2>&1 )
if [ -f "$ws/repo/.claude/fluxpoint/attest.jsonl" ]; then
  bad "exec-attest: a bare gate from a subdirectory is not the project's gate" "recorded"
else
  ok "exec-attest: a bare gate from a subdirectory is not the project's gate" "not recorded"
fi
( cd "$ws/repo/sub" && ws_input "$ws/repo/sub" "cd $ws/repo && scripts/harness.sh --full" 0 \
  | CLAUDE_PROJECT_DIR="$ws" eval "$(hook_cmd PostToolUse 1)" >/dev/null 2>&1 )
if [ -f "$ws/repo/.claude/fluxpoint/attest.jsonl" ]; then
  ok "exec-attest: attests from a subdirectory of the repo" "recorded"
else
  bad "exec-attest: attests from a subdirectory of the repo" "no log"
fi
cd "$ROOT/r" 2>/dev/null || true

# THE INVERSE SHAPES: the payload's cwd must NOT win when the project dir is
# itself inside a git work tree. Both of these worked before fpl_cd_project
# existed and are the gate's ordinary jurisdiction; a cwd-first rule with an
# unbounded toplevel climb turned each into "scripts/harness.sh is absent",
# deleted the arming marker, and waved the stop through.
stop_input() { # $1 = cwd, $2 = session id
  printf '{"session_id":"%s","cwd":"%s"}' "$2" "$1"
}
# A project scoped to a SUBDIRECTORY of a larger repo: cwd and project dir
# agree, and the climb is what walks out of the project.
mono="$ROOT/mono"; rm -rf "$mono"; mkdir -p "$mono/tools/svc/scripts"
( cd "$mono" && git init -q -b main \
  && printf '#!/usr/bin/env bash\nexit 1\n' >tools/svc/scripts/harness.sh \
  && chmod +x tools/svc/scripts/harness.sh && printf 'x = 1\n' >tools/svc/app.py \
  && git add -A && git -c user.email=t@t -c user.name=t commit -qm base )
mkdir -p "$mono/tools/svc/.claude/fluxpoint"; : >"$mono/tools/svc/.claude/fluxpoint/sub.dirty"
out="$(stop_input "$mono/tools/svc" sub \
  | CLAUDE_PROJECT_DIR="$mono/tools/svc" eval "$(hook_cmd Stop)" 2>/dev/null)"
if [ -f "$mono/tools/svc/.claude/fluxpoint/sub.dirty" ] \
   && case "$out" in *'"decision":"block"'*) true ;; *) false ;; esac; then
  ok "dod-gate: a project below a monorepo toplevel keeps jurisdiction" "red harness blocks"
else
  bad "dod-gate: a project below a monorepo toplevel keeps jurisdiction" "climbed out: ${out:0:60}"
fi
# A stop issued while the session shell sits in some OTHER git checkout: the
# gate is the project's, wherever the shell wandered.
proj="$ROOT/proj"; rm -rf "$proj" "$ROOT/foreign"
mkdir -p "$proj/scripts" "$ROOT/foreign"
( cd "$proj" && git init -q -b main \
  && printf '#!/usr/bin/env bash\nexit 1\n' >scripts/harness.sh && chmod +x scripts/harness.sh \
  && printf 'x = 1\n' >app.py \
  && git add -A && git -c user.email=t@t -c user.name=t commit -qm base )
( cd "$ROOT/foreign" && git init -q -b main && printf 'y\n' >f \
  && git add -A && git -c user.email=t@t -c user.name=t commit -qm base )
mkdir -p "$proj/.claude/fluxpoint"; : >"$proj/.claude/fluxpoint/away.dirty"
out="$(stop_input "$ROOT/foreign" away \
  | CLAUDE_PROJECT_DIR="$proj" eval "$(hook_cmd Stop)" 2>/dev/null)"
if [ -f "$proj/.claude/fluxpoint/away.dirty" ] \
   && case "$out" in *'"decision":"block"'*) true ;; *) false ;; esac; then
  ok "dod-gate: a stop from a foreign checkout still judges the project" "red harness blocks"
else
  bad "dod-gate: a stop from a foreign checkout still judges the project" "gate skipped: ${out:0:60}"
fi
cd "$ROOT/r" 2>/dev/null || true

# --- 4. verify-changed.sh: the previously untested hook ---
newrepo 0
printf 'y = 2\n' >>src/app.py
post_input src/app.py | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
check "verify-changed: green harness exits 0" 0 "$?"
[ -f .claude/fluxpoint/s.dirty ] && ok "verify-changed: marks the session dirty" "marker written" \
  || bad "verify-changed: marks the session dirty" "no marker"

newrepo 1
printf 'y = 2\n' >>src/app.py
err="$(post_input src/app.py | eval "$(hook_cmd PostToolUse)" 2>&1 >/dev/null)"; rc=$?
check "verify-changed: red harness exits 2 (feeds Claude)" 2 "$rc"
case "$err" in *"harness --changed RED"*) ok "verify-changed: red output names the file" "yes" ;;
  *) bad "verify-changed: red output names the file" "got: ${err:0:40}" ;; esac

# A red harness must still arm the gate, or a failed edit could be walked away
# from by never touching Write again.
[ -f .claude/fluxpoint/s.dirty ] && ok "verify-changed: arms the gate even when red" "marker written" \
  || bad "verify-changed: arms the gate even when red" "no marker"

# --- 4a. Codex shapes: apply_patch carries the paths in the patch text ---
# Codex fires this same group for apply_patch (its matcher aliases cover
# Edit|Write) with no file_path at all: tool_input.command is the patch.
patch_input() { # $1 = header kind, $2 = path
  "$FPL_PY" - "$ROOT/r" "$1" "$2" <<'PY'
import json, sys
body = "*** Begin Patch\n*** %s: %s\n@@\n-x = 1\n+x = 2\n*** End Patch\n" % (sys.argv[2], sys.argv[3])
print(json.dumps({"session_id": "s", "cwd": sys.argv[1], "hook_event_name": "PostToolUse",
                  "tool_name": "apply_patch", "tool_use_id": "call_x",
                  "tool_input": {"command": body}, "tool_response": {"ok": True}}))
PY
}
newrepo 0
printf 'y = 2\n' >>src/app.py
patch_input "Update File" src/app.py | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
check "apply_patch: green harness exits 0" 0 "$?"
[ -f .claude/fluxpoint/s.dirty ] && ok "apply_patch: marks the session dirty" "marker written" \
  || bad "apply_patch: marks the session dirty" "no marker"

newrepo 1
printf 'y = 2\n' >>src/app.py
err="$(patch_input "Update File" src/app.py | eval "$(hook_cmd PostToolUse)" 2>&1 >/dev/null)"; rc=$?
check "apply_patch: red harness exits 2" 2 "$rc"
case "$err" in *"harness --changed RED for src/app.py"*) ok "apply_patch: red output names the patched file" "yes" ;;
  *) bad "apply_patch: red output names the patched file" "got: ${err:0:60}" ;; esac

# A deletion changes the tree, so it arms the gate, but there is nothing left
# to check and a missing file must not read as a red check.
newrepo 1
rm -f src/app.py
patch_input "Delete File" src/app.py | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ -f .claude/fluxpoint/s.dirty ]; then
  ok "apply_patch: a deletion arms the gate without a red check" "armed, exit 0"
else
  bad "apply_patch: a deletion arms the gate without a red check" "rc=$rc"
fi

newrepo 1
printf 'notes\n' >README.md
patch_input "Update File" README.md | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ ! -f .claude/fluxpoint/s.dirty ]; then
  ok "apply_patch: a docs-only patch does not arm the gate" "no run, no marker"
else
  bad "apply_patch: a docs-only patch does not arm the gate" "rc=$rc"
fi

# The Bash attestation reads Codex's string-shaped response too.
newrepo 0
printf '{"version":1,"gates":{"harness":"scripts/harness.sh --full"}}' >.fluxpoint-gates.json
"$FPL_PY" - "$ROOT/r" <<'PY' | eval "$(hook_cmd PostToolUse 1)" >/dev/null 2>&1
import json, sys
print(json.dumps({"session_id": "s", "cwd": sys.argv[1], "hook_event_name": "PostToolUse",
                  "tool_name": "Bash", "tool_use_id": "call_y",
                  "tool_input": {"command": "scripts/harness.sh --full"},
                  "tool_response": "Wall time: 0.0200 seconds\nProcess exited with code 1\nOutput:\nharness: RED\n"}))
PY
if [ -f .claude/fluxpoint/attest.jsonl ] && grep -q '"exit": 1' .claude/fluxpoint/attest.jsonl; then
  ok "exec-attest: reads Codex's string-shaped exit" "exit 1 recorded"
else
  bad "exec-attest: reads Codex's string-shaped exit" "not recorded"
fi

# --- 5. skipped paths do not arm the gate or run the harness ---
for skip in README.md .claude/settings.json docs/notes.md; do
  newrepo 1
  mkdir -p "$(dirname "$skip")" 2>/dev/null
  printf 'text\n' >"$skip"
  post_input "$skip" | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
  rc=$?
  if [ "$rc" -eq 0 ] && [ ! -f .claude/fluxpoint/s.dirty ]; then
    ok "verify-changed: skips $skip" "no run, no marker"
  else
    bad "verify-changed: skips $skip" "rc=$rc marker=$([ -f .claude/fluxpoint/s.dirty ] && echo yes || echo no)"
  fi
done

# --- 5a. only paths inside the project arm the gate (#94) ---
# An orchestrator writes scratch scripts outside the repository all the
# time; arming on them made the gate write an Evidence row into the tree a
# live graph was guarding.
newrepo 1
mkdir -p "$ROOT/scratch"
printf 'print(1)\n' >"$ROOT/scratch/patch.py"
post_input "$ROOT/scratch/patch.py" | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ ! -f .claude/fluxpoint/s.dirty ]; then
  ok "verify-changed: a write outside the project does not arm" "no run, no marker"
else
  bad "verify-changed: a write outside the project does not arm" "rc=$rc"
fi
post_input "../scratch/patch.py" | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ ! -f .claude/fluxpoint/s.dirty ]; then
  ok "verify-changed: nor does a relative path that escapes it" "no run, no marker"
else
  bad "verify-changed: nor does a relative path that escapes it" "rc=$rc"
fi
newrepo 0
printf 'y = 2\n' >>src/app.py
post_input "$ROOT/r/src/app.py" | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
[ -f .claude/fluxpoint/s.dirty ] && ok "verify-changed: an absolute path inside still arms" "marker written" \
  || bad "verify-changed: an absolute path inside still arms" "no marker"

# --- 6. no harness in the repo: mark dirty, stay silent, never fail the edit ---
newrepo none
printf 'y = 2\n' >>src/app.py
post_input src/app.py | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
check "verify-changed: harmless when no harness exists" 0 "$?"
[ -f .claude/fluxpoint/s.dirty ] && ok "verify-changed: still arms the gate" "marker written" \
  || bad "verify-changed: still arms the gate" "no marker"

# --- 7. the kill switch disarms every hook ---
newrepo 1
printf 'y = 2\n' >>src/app.py
FPL_DISABLE=1 bash -c "$(hook_cmd PostToolUse)" <<<"$(post_input src/app.py)" >/dev/null 2>&1
check "FPL_DISABLE=1: PostToolUse becomes a no-op" 0 "$?"
printf '{"session_id":"s","cwd":"%s"}' "$ROOT/r" >"$ROOT/stopin"
out="$(FPL_DISABLE=1 bash -c "$(hook_cmd Stop)" <"$ROOT/stopin" 2>&1)"
[ -z "$out" ] && ok "FPL_DISABLE=1: Stop gate becomes a no-op" "silent" \
  || bad "FPL_DISABLE=1: Stop gate becomes a no-op" "spoke"

# --- 8. non-git directory: hooks must not explode ---
rm -rf "$ROOT/plain"; mkdir -p "$ROOT/plain"; cd "$ROOT/plain"
printf '{"session_id":"s","cwd":"%s","tool_input":{"file_path":"a.py"}}' "$ROOT/plain" \
  | eval "$(hook_cmd PostToolUse)" >/dev/null 2>&1
check "non-git dir: PostToolUse exits cleanly" 0 "$?"
printf '{"session_id":"s","cwd":"%s"}' "$ROOT/plain" | eval "$(hook_cmd Stop)" >/dev/null 2>&1
check "non-git dir: Stop gate exits cleanly" 0 "$?"

cd /; rm -rf "$ROOT"
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
