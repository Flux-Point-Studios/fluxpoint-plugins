#!/usr/bin/env bash
# Outer Ralph loop: a fresh agent context per iteration; all state lives in
# WORK.md and git. Promotion is decided by the harness, never by the agent.
# Unattended runs belong in a sandboxed container with allow-listed egress
# and zero reachable key material; only there is
#   PERMISSION_ARGS="--dangerously-skip-permissions"
# acceptable. Halt any time with: touch .claude/fluxpoint/STOP
set -euo pipefail

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

MAX_ITER="${MAX_ITER:-25}"
# Which agent CLI drives an iteration: `claude` (Claude Code, headless) or
# `codex` (Codex, non-interactive). Both read WORK_PROMPT.md, and neither
# decides promotion: the harness below does.
AGENT_CLI="${AGENT_CLI:-claude}"
MAX_TURNS="${MAX_TURNS:-40}"                                         # claude
PERMISSION_ARGS="${PERMISSION_ARGS:---permission-mode acceptEdits}"  # claude
# Codex takes its approval and sandbox settings as config overrides. The
# default edits inside the workspace without prompting, which is what a
# non-interactive run needs. In a sandboxed container ONLY:
#   CODEX_ARGS="--dangerously-bypass-approvals-and-sandbox"
CODEX_ARGS="${CODEX_ARGS:--c approval_policy=never -c sandbox_mode=workspace-write}"
case "$AGENT_CLI" in
  claude | codex) ;;
  *) echo "loop: AGENT_CLI must be claude or codex, got '$AGENT_CLI'" >&2; exit 2 ;;
esac

run_agent() { # $1 = log path
  case "$AGENT_CLI" in
    claude)
      # shellcheck disable=SC2086
      claude -p "$(cat WORK_PROMPT.md)" $PERMISSION_ARGS --max-turns "$MAX_TURNS" \
        --output-format stream-json --verbose >"$1" 2>&1 ;;
    codex)
      # `-` reads the prompt from stdin; --json streams events like claude's
      # stream-json does, so the two logs are read the same way.
      # shellcheck disable=SC2086
      codex exec $CODEX_ARGS --json - <WORK_PROMPT.md >"$1" 2>&1 ;;
  esac
}

sd=".claude/fluxpoint"
mkdir -p "$sd/logs"
rm -f "$sd/STOP"

# A metric is optional. Resolve the helper through the same plugin roots as
# the harness; a declared metric with no helper is an explicit setup error.
loop_metric=""
if [ -f .fluxpoint-metrics.json ] && "$FPL_PY" -c \
  'import json,sys; sys.exit(0 if json.load(open(".fluxpoint-metrics.json")).get("metrics") else 1)'; then
  for _root in "${FPL_PLUGIN_ROOT:-}" "${CLAUDE_PLUGIN_ROOT:-}" "${PLUGIN_ROOT:-}"; do
    if [ -n "$_root" ] && [ -f "$_root/scripts/loop-metric.py" ]; then
      loop_metric="$_root/scripts/loop-metric.py"; break
    fi
  done
  if [ -z "$loop_metric" ]; then
    echo "loop: metrics declared but loop-metric.py unavailable; set FPL_PLUGIN_ROOT" >&2
    exit 2
  fi
fi
PLATEAU_WINDOW="${PLATEAU_WINDOW:-3}"
PLATEAU_MIN="${PLATEAU_MIN:-1}"
record_metric() {
  local args=(--window "$PLATEAU_WINDOW" --minimum "$PLATEAU_MIN")
  [ -n "${PLATEAU_METRIC:-}" ] && args+=(--metric "$PLATEAU_METRIC")
  "$FPL_PY" "$loop_metric" "${args[@]}" "$@"
}
# Capture the pre-iteration value so W means W completed iterations.
[ -z "$loop_metric" ] || record_metric --reset || true
stop_kind=budget-exhausted
stop_detail="outer loop spent its iteration budget with the harness red or STATUS not DONE"

for ((i = 1; i <= MAX_ITER; i++)); do
  if [ -f "$sd/STOP" ]; then
    echo "loop: STOP file present, halting after $((i - 1)) iteration(s)"
    exit 0
  fi
  echo "loop: iteration $i/$MAX_ITER"
  # Where this iteration started. The commit below empties `git diff HEAD`,
  # so without a base the co-change tier judges an empty diff and every
  # declared pair reports "no changes to compare" — silently, and for the
  # one class of pair that has no parity command to fall back on.
  # `|| true` because an unborn HEAD must not kill the runner.
  iter_base="$(git rev-parse HEAD 2>/dev/null || true)"
  run_agent "$sd/logs/iter-$i.jsonl" ||
    echo "loop: $AGENT_CLI exited non-zero on iteration $i (see logs)" >&2
  git add -A
  git commit -q -m "loop: iteration $i" || true
  if FPL_PAIR_AGAINST="${FPL_PAIR_AGAINST:-$iter_base}" bash scripts/harness.sh --full \
     && grep -q '^STATUS: DONE' WORK.md; then
    echo "loop: harness green and STATUS DONE after iteration $i"
    exit 0
  fi
  if [ -n "$loop_metric" ]; then
    metric_rc=0
    record_metric || metric_rc=$?
    if [ "$metric_rc" -eq 10 ]; then
      stop_kind=plateau
      stop_detail="plateau: declared metric improved less than $PLATEAU_MIN over $PLATEAU_WINDOW iterations"
      echo "loop: $stop_detail" >&2
      break
    fi
  fi
done
# iteration budget exhausted or the declared metric reached a plateau.
echo "loop: ${stop_detail:-iteration budget exhausted, harness red or STATUS still ACTIVE. Checkpoint.}" >&2
# An unattended run that gives up silently is a run nobody learns about
# until they wonder why nothing shipped.
# `|| true` is load-bearing, and templates/harness.sh documents the same
# trap: find exits 1 when ~/.claude/plugins is absent, pipefail carries
# that through the pipe, and set -e then kills the runner ON THIS LINE —
# before the notification these three lines exist to send, and with the
# same exit 1 the intended path would have produced, so nothing looks
# wrong. The deterministic trigger is a machine with no plugins dir:
# exactly the sandboxed container this file's own header recommends.
# `find` guarantees no ordering, so `head -1` took whichever cached copy the
# filesystem yielded first — observed to be an ORPHANED version while a newer
# one was the active install. Prefer a root the runtime already set, then the
# marketplace copy, then the highest cached version.
ib=""
for _root in "${FPL_PLUGIN_ROOT:-}" "${CLAUDE_PLUGIN_ROOT:-}" "${PLUGIN_ROOT:-}"; do
  [ -n "$_root" ] && [ -f "$_root/scripts/inbox.py" ] && { ib="$_root/scripts/inbox.py"; break; }
done
if [ -z "$ib" ]; then
  # Both install roots: Claude Code's plugin cache and Codex's.
  _ic="$({ find "$HOME/.claude/plugins" "${CODEX_HOME:-$HOME/.codex}/plugins/cache" \
           -type f -name inbox.py 2>/dev/null || true; })"
  if [ -n "$_ic" ]; then
    ib="$(printf '%s\n' "$_ic" | grep '/marketplaces/' | head -1 || true)"
    [ -z "$ib" ] && ib="$(printf '%s\n' "$_ic" | sort -V | tail -1)"
  fi
  unset _ic
fi
unset _root
[ -n "$ib" ] && "$FPL_PY" "$ib" --add --kind "${stop_kind:-budget-exhausted}" \
  --detail "${stop_detail:-outer loop spent its iteration budget with the harness red or STATUS not DONE}" >/dev/null 2>&1
exit 1
