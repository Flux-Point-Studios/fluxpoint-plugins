#!/usr/bin/env bash
# Definition-of-Done contract for this repository.
#
# The plugin in here exists to make "done" a deterministic check rather than
# an agent's self-report. It should be subject to its own rule, so this is
# the same contract fluxpoint:init scaffolds into any other repo.
#
#   --changed <file>   fast, scoped checks for one file
#   --full             everything the DoD requires
#
# exit 0 = green.
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

cd "$(dirname "$0")/.."
PLUGIN="plugins/fluxpoint"
SUB="plugins/substrate"
fail=0

step() { # name, then command
  local name="$1"; shift
  if "$@" >/tmp/fpl-step.log 2>&1; then
    printf '  ok    %s\n' "$name"
  else
    printf '  FAIL  %s\n' "$name"
    sed 's/^/        /' /tmp/fpl-step.log | tail -25
    fail=1
  fi
}

# An advisory step: it runs, its output is shown, and it never flips the
# verdict. The prompt-hygiene audit starts here on purpose — the house
# style is deliberately dense prose, and a gate that begins by failing
# runs on idiom is a gate people learn to route around. A crash (exit 2:
# nothing scanned, unreadable input) is still a failure, because a step
# that ran nothing must not read as a step that found nothing.
advise() { # name, then command
  local name="$1"; shift
  local rc=0
  "$@" >/tmp/fpl-step.log 2>&1 || rc=$?
  if [ "$rc" -eq 2 ]; then
    printf '  FAIL  %s (advisory step could not run)\n' "$name"
    sed 's/^/        /' /tmp/fpl-step.log | tail -25
    fail=1
  else
    printf '  note  %s\n' "$name"
    sed 's/^/        /' /tmp/fpl-step.log | tail -25
  fi
}

check_json() { "$FPL_PY" -m json.tool "$1" >/dev/null; }
check_sh()   { bash -n "$1"; }

# The validator exits 0 on a warning, and a warning nothing fails on is not a
# gate: the plugin.json/marketplace.json version skew rode a validator warning
# through five releases while this step read green over it. Fail on the
# warning marks themselves so every FUTURE validator warning is a gate too,
# not just the one that already bit.
validate_manifests() {
  local out
  out="$(claude plugin validate . 2>&1)" || { printf '%s\n' "$out"; return 1; }
  printf '%s\n' "$out"
  case "$out" in
    *"⚠"* | *"with warnings"*)
      echo "validator emitted warnings; a warning nothing fails on is not a gate" >&2
      return 1 ;;
  esac
  return 0
}
check_py()   { "$FPL_PY" -m py_compile "$1"; }

# Two manifest sets describe each plugin: .claude-plugin/plugin.json, which
# Claude Code reads, and the portable plugin.json Codex reads. The validator
# step above catches version skew inside Claude's pair; this catches it
# across the pair the validator cannot see, and holds the Codex surface
# complete — every command needs its Codex entry point under skills/.
manifests_in_sync() {
  "$FPL_PY" - "$PLUGIN" "$SUB" .claude-plugin/marketplace.json .agents/plugins/marketplace.json <<'PY'
import json, os, sys
fail = 0
mk = json.load(open(sys.argv[3], encoding="utf-8"))
cmk = json.load(open(sys.argv[4], encoding="utf-8"))
for root in sys.argv[1:3]:
    a = json.load(open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8"))
    b = json.load(open(os.path.join(root, "plugin.json"), encoding="utf-8"))
    for key in ("name", "version", "description"):
        if a.get(key) != b.get(key):
            print(f"{root}: {key} differs between .claude-plugin/plugin.json and plugin.json"); fail = 1
    entry = next((e for e in mk["plugins"] if e.get("name") == a["name"]), None)
    if not entry or entry.get("version") != a["version"]:
        print(f"{root}: .claude-plugin/marketplace.json does not carry version {a['version']}"); fail = 1
    if not any(e.get("name") == a["name"] for e in cmk.get("plugins", [])):
        print(f"{root}: missing from .agents/plugins/marketplace.json"); fail = 1
    if b.get("$schema") != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json":
        print(f"{root}: plugin.json is not a portable Agent Plugins manifest"); fail = 1
    for fn in sorted(os.listdir(os.path.join(root, "commands"))):
        if not fn.endswith(".md"):
            continue
        skill = os.path.join(root, "skills", f"{a['name']}-{fn[:-3]}", "SKILL.md")
        if not os.path.isfile(skill):
            print(f"{root}: command {fn} has no Codex entry point at {skill}"); fail = 1
sys.exit(fail)
PY
}

compile_templates() {
  local t n=0
  for t in "$PLUGIN"/templates/WORK*.md; do
    [ -f "$t" ] || continue
    grep -q '```json graph-ir' "$t" || continue
    n=$((n + 1))
    "$FPL_PY" "$PLUGIN/scripts/compile-graph.py" "$t" -o /tmp/fpl-compiled.js || return 1
    # Generated scripts must be syntactically valid under the runtime's
    # async wrapper, or the graph fails at launch instead of at compile.
    {
      echo 'const agent=0,parallel=0,pipeline=0,log=0,phase=0,args=0,budget=0,workflow=0;(async () => {'
      sed 's/^export const meta/const meta/' /tmp/fpl-compiled.js
      echo '})()'
    } >/tmp/fpl-wrapped.mjs
    node --check /tmp/fpl-wrapped.mjs || return 1
  done
  # A green that compiled nothing is not a green: if the templates are ever
  # renamed or moved, this check must fail rather than silently pass.
  if [ "$n" -lt 2 ]; then
    echo "expected at least 2 campaign templates with an IR block, compiled $n" >&2
    return 1
  fi
}

# A shipped workflow is hand-written, not compiled, but the runtime wraps it
# the same way: checked under the same async wrapper, or a syntax error
# surfaces when someone runs the command rather than here.
check_workflow() {
  {
    echo 'const agent=0,parallel=0,pipeline=0,log=0,phase=0,args=0,budget=0,workflow=0;(async () => {'
    sed 's/^export const meta/const meta/' "$1"
    echo '})()'
  } >/tmp/fpl-workflow.mjs
  node --check /tmp/fpl-workflow.mjs
}

check_workflows() {
  local f n=0
  for f in "$PLUGIN"/workflows/*.js; do
    [ -f "$f" ] || continue
    n=$((n + 1))
    check_workflow "$f" || { echo "$f does not parse under the async wrapper" >&2; return 1; }
  done
  # A green that checked nothing is not a green.
  if [ "$n" -lt 1 ]; then
    echo "expected at least 1 shipped workflow under $PLUGIN/workflows, checked $n" >&2
    return 1
  fi
}

# Commands and agents are run as literal instructions, so a `python3` written
# into one is reached by no shell resolver — and `python3` is absent from a
# standard Windows install, which made every slash command a no-op there.
# Invocations go through scripts/py.sh, which also pins UTF-8 stdio.
portable_invocations() {
  local hits
  hits=$(grep -rn 'python3[^`]*\.py' --include='*.md' "$PLUGIN" || true)
  if [ -n "$hits" ]; then
    echo "$hits" >&2
    echo "hardcoded python3 invocation; route it through scripts/py.sh" >&2
    return 1
  fi
  [ -x "$PLUGIN/scripts/py.sh" ] || {
    echo "scripts/py.sh missing or not executable" >&2
    return 1
  }
}

substrate_tests() {
  local n
  n=$(ls "$SUB"/tests/*.test.mjs 2>/dev/null | wc -l)
  # A green that ran nothing is not a green: if the suites are ever renamed
  # or moved, this check must fail rather than silently pass.
  if [ "$n" -lt 1 ]; then
    echo "expected at least 1 substrate test suite, found $n" >&2
    return 1
  fi
  node --test "$SUB"/tests/*.test.mjs
}

case "${1:---full}" in
  --changed)
    f="${2:?usage: harness.sh --changed <file>}"
    # The per-edit hook passes the tool's file_path, which is absolute; every
    # pattern below is relative to the repository root, so an absolute path
    # matched none of them and the scoped checks never ran from the hook.
    case "$f" in
      /* | [A-Za-z]:[\\/]*)
        rel="$("$FPL_PY" -c 'import os, sys
p = os.path.relpath(os.path.abspath(sys.argv[1]), os.getcwd())
print(p.replace(os.sep, "/"))' "$f" 2>/dev/null || true)"
        case "$rel" in "" | ../* | ..) : ;; *) f="$rel" ;; esac ;;
    esac
    case "$f" in
      *.json) step "json: $f" check_json "$f" ;;
      *.sh)   step "bash -n: $f" check_sh "$f" ;;
      *.py)   step "py_compile: $f" check_py "$f" ;;
      *.mjs)  step "node --check: $f" node --check "$f" ;;
      *)      : ;;
    esac
    # Any change under the plugin can break compilation; keep it cheap but
    # not blind.
    case "$f" in
      "$PLUGIN"/scripts/*.py | "$PLUGIN"/contracts/*.json | "$PLUGIN"/templates/WORK*.md)
        step "templates compile" compile_templates ;;
      "$SUB"/*)
        step "substrate suites" substrate_tests ;;
    esac
    case "$f" in
      "$PLUGIN"/scripts/recall.py | "$PLUGIN"/scripts/recall_sources.py | "$PLUGIN"/scripts/embedder.py)
        step "hybrid recall pipeline" "$FPL_PY" "$PLUGIN/tests/recall-test.py"
        step "recall source maintenance" "$FPL_PY" "$PLUGIN/tests/recall-maintenance-test.py"
        step "embedder quarantine" "$FPL_PY" "$PLUGIN/tests/embedder-test.py" ;;
      "$PLUGIN"/scripts/inject-state.sh | "$PLUGIN"/scripts/prompt-recall.sh)
        step "hybrid recall pipeline" "$FPL_PY" "$PLUGIN/tests/recall-test.py"
        step "warm prompt recall" "$FPL_PY" "$PLUGIN/tests/prompt-recall-daemon-test.py" ;;
      "$PLUGIN"/scripts/prompt-recall-daemon.py | "$PLUGIN"/scripts/prompt-recall-client.mjs)
        step "warm prompt recall" "$FPL_PY" "$PLUGIN/tests/prompt-recall-daemon-test.py" ;;
      "$PLUGIN"/scripts/memory.py | "$PLUGIN"/scripts/recurrence-guard.py)
        step "recurrence gate" "$FPL_PY" "$PLUGIN/tests/recurrence-test.py"
        step "lessons across runs" "$FPL_PY" "$PLUGIN/tests/memory-test.py" ;;
      "$PLUGIN"/scripts/pair-guard.py)
        step "relation gate" bash "$PLUGIN/tests/pair-test.sh"
        step "relation gate: differential + bite + scan" bash "$PLUGIN/tests/pair-verify-test.sh" ;;
      "$PLUGIN"/scripts/cex.py)
        step "counterexample ledger" bash "$PLUGIN/tests/cex-test.sh" ;;
      "$PLUGIN"/scripts/blueprint-guard.py)
        step "blueprint conformance" bash "$PLUGIN/tests/blueprint-test.sh" ;;
      "$PLUGIN"/contracts/RedTeamV1.schema.json)
        step "red-team severity reaches the gate" "$FPL_PY" "$PLUGIN/tests/redteam-contract-test.py" ;;
      "$PLUGIN"/workflows/*.js)
        step "node --check (async wrapper): $f" check_workflow "$f"
        step "graph-audit workflow" "$FPL_PY" "$PLUGIN/tests/audit-workflow-test.py" ;;
      # Reached only by hand: the per-edit hook skips *.md edits.
      "$PLUGIN"/commands/graph-audit.md)
        step "graph-audit workflow" "$FPL_PY" "$PLUGIN/tests/audit-workflow-test.py" ;;
    esac
    ;;
  --full)
    echo "fluxpoint harness --full"
    for f in .claude-plugin/marketplace.json .agents/plugins/marketplace.json \
             "$PLUGIN"/.claude-plugin/plugin.json "$PLUGIN"/plugin.json \
             "$PLUGIN"/contracts/*.json "$PLUGIN"/hooks/hooks.json \
             "$PLUGIN"/templates/settings.snippet.json \
             "$SUB"/.claude-plugin/plugin.json "$SUB"/plugin.json "$SUB"/hooks/hooks.json; do
      [ -f "$f" ] && step "json: ${f#"$PLUGIN"/}" check_json "$f"
    done
    for f in "$PLUGIN"/scripts/*.sh "$PLUGIN"/templates/*.sh scripts/*.sh; do
      [ -f "$f" ] && step "bash -n: $(basename "$f")" check_sh "$f"
    done
    for f in "$PLUGIN"/scripts/*.py; do
      [ -f "$f" ] && step "py_compile: $(basename "$f")" check_py "$f"
    done
    step "manifests validate" validate_manifests
    step "manifests agree across runtimes" manifests_in_sync
    step "portable interpreter invocations" portable_invocations
    step "locked specification + executed requirements" "$FPL_PY" "$PLUGIN/scripts/specification.py" --run
    step "Blaster locked requirements" "$FPL_PY" "$PLUGIN/scripts/specification.py" --spec .fluxpoint-spec.blaster.json --run
    step "templates compile + emit valid JS" compile_templates
    step "shipped workflows parse under the async wrapper" check_workflows
    step "graph-audit workflow: lenses, reduce, refutation (executed)" \
      "$FPL_PY" "$PLUGIN/tests/audit-workflow-test.py"
    step "compiler invariants" "$FPL_PY" "$PLUGIN/tests/compile-test.py"
    step "graph lanes, replay and rejudgment" "$FPL_PY" "$PLUGIN/tests/graph-replay-test.py"
    step "journal and transcript replay provenance" "$FPL_PY" "$PLUGIN/tests/replay-memo-test.py"
    step "compiler and execution cleanup regressions" "$FPL_PY" "$PLUGIN/tests/execution-cleanup-test.py"
    step "attested harness state and content dirt" "$FPL_PY" "$PLUGIN/tests/harness-dirt-test.py"
    step "carried resume arguments and evidence pins" "$FPL_PY" "$PLUGIN/tests/resume-test.py"
    step "transitive packet inputs and repair preflight" "$FPL_PY" "$PLUGIN/tests/packet-inputs-test.py"
    step "recurring classes force reframe" "$FPL_PY" "$PLUGIN/tests/recurring-class-test.py"
    step "agentType resolution + contract" "$FPL_PY" "$PLUGIN/tests/agenttype-test.py"
    step "proof verdict reaches the record (executed)" "$FPL_PY" "$PLUGIN/tests/proof-verdict-test.py"
    step "red-team severity reaches the gate (executed)" "$FPL_PY" "$PLUGIN/tests/redteam-contract-test.py"
    step "red-team multi-lens coverage" "$FPL_PY" "$PLUGIN/tests/redteam-workflow-test.py"
    step "canonical run records and Evidence failures" "$FPL_PY" "$PLUGIN/tests/record-location-test.py"
    step "unchecked repair commit windows" "$FPL_PY" "$PLUGIN/tests/unchecked-repairs-test.py"
    step "repair sibling coverage and diff-scoped attack receipts" "$FPL_PY" "$PLUGIN/tests/repair-proof-test.py"
    step "recorded authorization context" "$FPL_PY" "$PLUGIN/tests/authorization-context-test.py"
    step "packet edit projection and prose pins" "$FPL_PY" "$PLUGIN/tests/packet-projection-test.py"
    step "machine health observations" "$FPL_PY" "$PLUGIN/tests/box-health-test.py"
    step "Windows machine health adapter" "$FPL_PY" "$PLUGIN/tests/box-health-windows-test.py"
    step "versioned graph launch requirements" "$FPL_PY" "$PLUGIN/tests/launch-requirements-test.py"
    step "bounded log regex matching" "$FPL_PY" "$PLUGIN/tests/regex-wait-test.py"
    step "emission coverage" "$FPL_PY" "$PLUGIN/tests/emission-test.py"
    step "cost model: estimate, ceiling, cache TTL (executed)" "$FPL_PY" "$PLUGIN/tests/cost-test.py"
    step "codegen injection + red-team regressions" "$FPL_PY" "$PLUGIN/tests/security-test.py"
    step "stop-gate regression" bash "$PLUGIN/tests/gate-test.sh"
    step "gate regression scenarios" "$FPL_PY" "$PLUGIN/tests/gate-regression-test.py"
    step "hygiene scope (executed)" bash "$PLUGIN/tests/hygiene-scope-test.sh"
    step "gate-authored evidence" bash "$PLUGIN/tests/evidence-test.sh"
    step "hook wiring + PostToolUse" bash "$PLUGIN/tests/hooks-test.sh"
    step "decisions that outlive context" bash "$PLUGIN/tests/decision-test.sh"
    step "compaction gate" bash "$PLUGIN/tests/precompact-test.sh"
    step "migration against pre-1.0 fixtures" bash "$PLUGIN/tests/migrate-test.sh"
    step "STATUS vocabulary" "$FPL_PY" "$PLUGIN/tests/status-vocab-test.py"
    step "proof-strength ratchet" bash "$PLUGIN/tests/proof-guard-test.sh"
    step "Python type escape hatches" "$FPL_PY" "$PLUGIN/tests/python-hatches-test.py"
    step "Shared recall corpus indexes" "$FPL_PY" "$PLUGIN/tests/recall-shared-index-test.py"
    step "Campaign schema and record upgrades" "$FPL_PY" "$PLUGIN/tests/campaign-upgrade-test.py"
    step "Codex memory corpus scoping" "$FPL_PY" "$PLUGIN/tests/codex-memory-test.py"
    step "Midnight attack taxonomy" "$FPL_PY" "$PLUGIN/tests/compact-taxonomy-test.py"
    step "statement ratchet" bash "$PLUGIN/tests/spec-guard-test.sh"
    step "counterexample ledger (executed)" bash "$PLUGIN/tests/cex-test.sh"
    step "mutation ratchet (executed)" bash "$PLUGIN/tests/mutation-test.sh"
    step "deterministic metrics + safe tightening (executed)" "$FPL_PY" "$PLUGIN/tests/metric-ratchet-test.py"
    step "pinned Aiken execution metrics (executed)" "$FPL_PY" "$PLUGIN/tests/aiken-metrics-test.py"
    step "Blaster result trust, freshness and concrete replay" "$FPL_PY" "$PLUGIN/tests/blaster-test.py"
    step "seam ratchet (executed)" bash "$PLUGIN/tests/seam-guard-test.sh"
    step "provider recordings conformance (executed)" "$FPL_PY" "$PLUGIN/tests/recordings-test.py"
    step "guard ratchet (executed)" bash "$PLUGIN/tests/guard-guard-test.sh"
    step "on-chain budget gate" bash "$PLUGIN/tests/budget-test.sh"
    step "blueprint conformance (executed)" bash "$PLUGIN/tests/blueprint-test.sh"
    step "once-only ledger (executed)" "$FPL_PY" "$PLUGIN/tests/ledger-test.py"
    step "execution attestation (executed)" bash "$PLUGIN/tests/attest-test.sh"
    step "retained crash attempts and reference reruns" "$FPL_PY" "$PLUGIN/tests/fault-attempts-test.py"
    step "forge coverage and provider launch regressions" "$FPL_PY" "$PLUGIN/tests/forge-coverage-test.py"
    step "forge stored identities and deployed witness regressions" "$FPL_PY" "$PLUGIN/tests/forge-identity-test.py"
    step "forge witness identity and coverage" "$FPL_PY" "$PLUGIN/tests/forge-evidence-test.py"
    step "content-bound red retention and interrupted fault executions" "$FPL_PY" "$PLUGIN/tests/fault-retention-test.py"
    step "forge and local gate race preserves red attempts" "$FPL_PY" "$PLUGIN/tests/forge-fault-race-test.py"
    step "shared long-command runner (executed)" "$FPL_PY" "$PLUGIN/tests/longrun-test.py"
    step "deployed dependency resolution" "$FPL_PY" "$PLUGIN/tests/deployed-test.py"
    step "documented claims match the code" "$FPL_PY" "$PLUGIN/tests/doc-claims-test.py"
    step "prompt hygiene scanner (executed)" "$FPL_PY" "$PLUGIN/tests/prompt-audit-test.py"
    advise "prompt hygiene over agents, commands, skills, templates (advisory)" \
      "$FPL_PY" "$PLUGIN/scripts/prompt-audit.py"
    step "lessons across runs (executed)" "$FPL_PY" "$PLUGIN/tests/memory-test.py"
    step "recurrence gate (executed)" "$FPL_PY" "$PLUGIN/tests/recurrence-test.py"
    step "packet reference context (executed)" "$FPL_PY" "$PLUGIN/tests/specification-reference-test.py"
    step "compiled witness identity (executed)" "$FPL_PY" "$PLUGIN/tests/plugin-identity-test.py"
    step "hybrid recall pipeline (executed)" "$FPL_PY" "$PLUGIN/tests/recall-test.py"
    step "warm prompt recall (executed)" "$FPL_PY" "$PLUGIN/tests/prompt-recall-daemon-test.py"
    step "recall source maintenance (executed)" "$FPL_PY" "$PLUGIN/tests/recall-maintenance-test.py"
    step "durable memory storage (executed)" "$FPL_PY" "$PLUGIN/tests/memory-storage-test.py"
    step "embedder quarantine (executed)" "$FPL_PY" "$PLUGIN/tests/embedder-test.py"
    step "graph metrics aggregator (executed)" "$FPL_PY" "$PLUGIN/tests/metrics-test.py"
    step "repair origin replay + resources + live tallies (executed)" "$FPL_PY" "$PLUGIN/tests/repair-origin-test.py"
    step "slice gain calibration" "$FPL_PY" "$PLUGIN/tests/slice-calibration-test.py"
    step "effort sweep: plan, split, score (executed)" "$FPL_PY" "$PLUGIN/tests/sweep-test.py"
    step "relation gate" bash "$PLUGIN/tests/pair-test.sh"
    step "relation gate: differential + bite + scan (executed)" bash "$PLUGIN/tests/pair-verify-test.sh"
    step "gate presence + resolver (executed)" bash "$PLUGIN/tests/gate-presence-test.sh"
    step "credential gate (executed)" bash "$PLUGIN/tests/secret-guard-test.sh"
    step "secret-handling skill shapes (executed)" "$FPL_PY" "$PLUGIN/tests/secret-handling-test.py"
    step "park layer (executed)" "$FPL_PY" "$PLUGIN/tests/park-test.py"
    step "Workflow stall and kill observer" "$FPL_PY" "$PLUGIN/tests/watchdog-test.py"
    step "unified state + compatibility" bash "$PLUGIN/tests/unify-test.sh"
    step "interpreter resolution" bash "$PLUGIN/tests/interpreter-test.sh"
    step "outer runner + co-change base" bash "$PLUGIN/tests/loop-runner-test.sh"
    for f in "$SUB"/scripts/*.mjs; do
      [ -f "$f" ] && step "substrate: node --check $(basename "$f")" node --check "$f"
    done
    step "substrate: registry graph + staleness suites" substrate_tests
    ;;
  *)
    echo "usage: harness.sh --changed <file> | --full" >&2
    exit 2
    ;;
esac

if [ "$fail" -ne 0 ]; then
  echo "harness: RED"
  exit 1
fi
echo "harness: green"
exit 0
