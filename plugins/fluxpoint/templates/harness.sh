#!/usr/bin/env bash
# Flux Point harness contract, the single source of "done":
#   scripts/harness.sh --changed <file>   fast scoped checks after one edit
#   scripts/harness.sh --full             everything the DoD requires
# Exit 0 is green; anything else blocks the Stop-hook gate and the outer
# loop. The auto-detection below is a floor: wire this file to the repo's
# real build, tests, property tests, formal checks, and preview-net
# exercises.
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

mode="${1:---full}"
file="${2:-}"

has() { command -v "$1" >/dev/null 2>&1; }

pm() {
  if [ -f pnpm-lock.yaml ]; then
    echo pnpm
  elif [ -f yarn.lock ]; then
    echo yarn
  else
    echo npm
  fi
}

has_script() {
  if has jq; then
    jq -e --arg s "$1" '.scripts[$s]' package.json >/dev/null 2>&1
  else
    grep -q "\"$1\"" package.json 2>/dev/null
  fi
}

run_script_if_present() {
  # `has_script "$1" && run` would return non-zero when the script is ABSENT,
  # and under `set -e` that aborts the harness in every repo that does not
  # define it. An explicit early return keeps absence a no-op.
  has_script "$1" || return 0
  "$(pm)" run "$1"
}

# Locate a script shipped with the plugin. Prints nothing when the plugin is
# not installed, so a repo carrying this harness stays runnable without it —
# but see need_gate below: "not installed" must never be read as "passed".
#
# Resolution is ordered rather than incidental. `find` guarantees no ordering,
# so `| head -1` picked whichever cached copy the filesystem happened to yield
# first — on a machine with several installed versions that was an ORPHANED
# one, while installed_plugins.json named a newer version as active. Nothing
# misbehaved only because the scripts were byte-identical across those
# versions, which is a coincidence and not a maintained property.
plugin_script() {
  # CLAUDE_PLUGIN_ROOT is set by the runtime whenever the harness runs under a
  # hook — Claude Code and Codex both set it, Codex as PLUGIN_ROOT too — and is
  # version-correct by construction. FPL_PLUGIN_ROOT stays ahead of it as the
  # repo's deliberate override.
  for _root in "${FPL_PLUGIN_ROOT:-}" "${CLAUDE_PLUGIN_ROOT:-}" "${PLUGIN_ROOT:-}"; do
    if [ -n "$_root" ] && [ -f "$_root/scripts/$1" ]; then
      printf '%s\n' "$_root/scripts/$1"
      unset _root
      return 0
    fi
  done
  unset _root
  # `|| true` is load-bearing: find exits 1 when ~/.claude/plugins does not
  # exist, `pipefail` propagates that through the pipe, and `set -e` then
  # killed --full on its first plugin lookup — in exactly the repos this
  # function exists to support, the ones carrying the harness without the
  # plugin installed. It failed with no output at all.
  # Both install roots: Claude Code's plugin cache and Codex's.
  _cands="$( { find "$HOME/.claude/plugins" "${CODEX_HOME:-$HOME/.codex}/plugins/cache" \
               -type f -name "$1" 2>/dev/null || true; } )"
  [ -z "$_cands" ] && { unset _cands; return 0; }
  # A locally-installed marketplace carries no version directory and is the
  # only copy present for that install, so it wins outright. Otherwise take
  # the highest version: `sort -V` is meaningful here precisely because the
  # remaining candidates are all cache paths sharing a prefix up to the
  # version component, which is the comparison that was nondeterministic.
  _mk="$(printf '%s\n' "$_cands" | grep '/marketplaces/' | head -1 || true)"
  if [ -n "$_mk" ]; then
    printf '%s\n' "$_mk"
  else
    printf '%s\n' "$_cands" | sort -V | tail -1
  fi
  unset _cands _mk
}

# A gate whose script is absent is not a gate that passed.
#
# Every gate below was `x="$(plugin_script f.py)"; [ -n "$x" ] && run`, so a
# missing plugin skipped it and the harness still exited 0 — silently, with
# the old "SKIPPED" notice long since refactored away. The environments where
# that happens are the ones that matter most: CI, cloud sessions, fresh clones
# and the detached worktrees used for independent verification. A check that
# does not run reads exactly like a check that passed, which is the defect
# class this harness exists to enforce against.
#
# The repo's intent decides which way it fails. Arming config on disk means
# the repo asked for that gate, so its absence is RED. With no config the gate
# is dormant by design and the run continues, saying so once. Set
# FPL_ALLOW_MISSING_GATES=1 to downgrade the red to a warning — an opt-out
# someone chose, never an absence inferred.
need_gate() {
  FPL_GATE=""
  _script="$1"; shift
  FPL_GATE="$(plugin_script "$_script")"
  if [ -n "$FPL_GATE" ]; then unset _script; return 0; fi
  _armed=""
  for _cfg in "$@"; do
    [ -e "$_cfg" ] && { _armed="$_cfg"; break; }
  done
  if [ -z "$_armed" ]; then
    unset _script _armed _cfg
    return 1
  fi
  if [ "${FPL_ALLOW_MISSING_GATES:-}" = "1" ]; then
    echo "harness: WARNING — $_script is not installed, and $_armed declares it." >&2
    echo "harness:   Running anyway because FPL_ALLOW_MISSING_GATES=1." >&2
    unset _script _armed _cfg
    return 1
  fi
  echo "harness: $_script is not installed, but $_armed declares it." >&2
  echo "harness:   This gate cannot run, so this harness cannot report green." >&2
  echo "harness:   Install the fluxpoint plugin, or set FPL_ALLOW_MISSING_GATES=1" >&2
  echo "harness:   to accept the gap on purpose." >&2
  exit 1
}

changed() {
  case "$file" in
    *.ak)
      if has aiken; then aiken check; fi ;;
    *.rs)
      if has cargo; then cargo check --quiet; fi ;;
    *.ts | *.tsx | *.js | *.jsx | *.mjs)
      if [ -f tsconfig.json ] && [ -f package.json ]; then
        npx --no-install tsc --noEmit
      fi ;;
    *.dfy)
      if has dafny; then dafny verify "$file"; fi ;;
    *.lean)
      if has lake; then lake build; fi ;;
    *.v)
      if has coqc; then coqc -q "$file"; fi ;;
    *.tf)
      if has terraform; then terraform fmt -check "$file"; fi ;;
    *) : ;;
  esac
}

full() {
  if need_gate specification.py .fluxpoint-spec.json .fluxpoint-spec-lock.json; then
    "$FPL_PY" "$FPL_GATE" --run --if-present
  elif [ -f WORK.md ] && grep -Eq $'^SPEC:[ \t]*[^ \t\r]' WORK.md; then
    # Any packet WORK.md names, as specification.py reads it: the runner
    # checks the declared packet, so its absence cannot pass for "no spec".
    echo "harness: specification.py is required by WORK.md but is unavailable" >&2
    return 1
  fi
  if [ -f aiken.toml ]; then
    has aiken || { echo "harness: Aiken project requires aiken; checks did not run" >&2; return 1; }
    aiken fmt --check .
    # `aiken check` has no --json flag: it emits structured JSON whenever
    # stdout is not a TTY and sends every diagnostic to stderr, so a plain
    # redirect buys the machine form for free and the operator still sees
    # the Compiling/Summary lines.
    #
    # Why a file and not `aiken check | cex.py --ingest`: this script runs
    # under `set -euo pipefail`, where a pipeline reports the last non-zero
    # status. A parser bug in the recorder would then be indistinguishable
    # from a failed proof, and the pipeline would abort before anything
    # downstream ran. Capture, record, re-raise — the prover's exit code
    # stays the gate and the recorder never gets a vote.
    #
    # The seed is fixed so shrinking is reproducible: aiken draws a random
    # u32 per run otherwise, and the same bug then shrinks to a different
    # value each time, which would file a new counterexample per run.
    aiken_out="$(mktemp)"
    aiken_rc=0
    aiken check --seed "${FPL_AIKEN_SEED:-1}" >"$aiken_out" || aiken_rc=$?
    if [ "$aiken_rc" -ne 0 ]; then
      # Unconditionally, before anything else can drop it: in a repo that
      # carries this harness without the plugin installed, this file is the
      # only record of which test failed and why.
      cat "$aiken_out" >&2
    fi
    cx="$(plugin_script cex.py)"
    if [ -n "$cx" ]; then
      "$FPL_PY" "$cx" --ingest --tool aiken --from "$aiken_out" \
        --exit "$aiken_rc" || true
    fi
    rm -f "$aiken_out"
    if [ "$aiken_rc" -ne 0 ]; then return "$aiken_rc"; fi
    # `check` typechecks and runs tests; `build` is what actually produces the
    # on-chain artifact, and it can fail where check passes. A validator that
    # will not build is not done.
    aiken build
    # Correct and submittable are different properties. A validator larger
    # than maxTxSize cannot go on chain at all, and the prover has nothing
    # to say about it. Protocol limits are enforced unconditionally; set a
    # headroom target in .fluxpoint-budget.json when you want one.
    # Armed by aiken.toml, not contracts/aiken.toml: reaching this line already
    # required aiken.toml at the ROOT, so the contracts/ path could never arm
    # anything. Arming on the manifest that got us here is what makes the
    # sentence above true — a repo that just built a validator cannot report
    # green without someone having checked it fits on chain.
    # .fluxpoint-budget.json stays first so the more specific declaration is
    # the one named when a repo has set a headroom target.
    if need_gate plutus-budget.py .fluxpoint-budget.json aiken.toml; then
      "$FPL_PY" "$FPL_GATE" --check ${FPL_PROTOCOL_PARAMS:+--params "$FPL_PROTOCOL_PARAMS"}
    fi
  fi
  # Provers run in --full, not only per-file. A gate that decides "done"
  # without invoking the prover is not a gate.
  if [ -n "$(git ls-files -- '*.dfy' 2>/dev/null)" ]; then
    has dafny || { echo "harness: Dafny sources require dafny; verification did not run" >&2; return 1; }
    # Same shape as the aiken block above: capture, record, re-raise, with
    # the prover's exit code staying the gate. Dafny prints a model for the
    # first failing assertion only under --extract-counterexample; put it
    # (and whatever else this repo's verify needs) in FPL_DAFNY_ARGS so the
    # harness stays the one place the prover is invoked. The output is
    # echoed unconditionally: in a repo carrying this harness without the
    # plugin, it is the only record of what failed.
    #
    # The prover takes a project file or the .dfy files themselves; a bare
    # `.` is refused by Dafny 4.9 ("neither a recognized option nor a Dafny
    # input file"), and a `**/*.dfy` glob without globstar only ever saw one
    # directory level. Tracked files are the honest list either way.
    dafny_out="$(mktemp)"
    dafny_rc=0
    if [ -f dfyconfig.toml ]; then
      # shellcheck disable=SC2086
      dafny verify ${FPL_DAFNY_ARGS:-} dfyconfig.toml >"$dafny_out" 2>&1 || dafny_rc=$?
    else
      # shellcheck disable=SC2086,SC2046
      dafny verify ${FPL_DAFNY_ARGS:-} $(git ls-files -- '*.dfy') >"$dafny_out" 2>&1 || dafny_rc=$?
    fi
    cat "$dafny_out"
    cx="$(plugin_script cex.py)"
    if [ -n "$cx" ]; then
      "$FPL_PY" "$cx" --ingest --tool dafny --from "$dafny_out" \
        --exit "$dafny_rc" || true
    fi
    rm -f "$dafny_out"
    if [ "$dafny_rc" -ne 0 ]; then return "$dafny_rc"; fi
  fi
  # Apalache runs only where the repo names its spec and invariant: there is
  # no manifest to detect, and `apalache-mc check` without --inv checks
  # nothing worth recording. FPL_APALACHE_ARGS carries them, e.g.
  # `--inv=Inv Spec.tla`. Same shape again — capture, record, re-raise, the
  # checker's exit code staying the gate (12 on a violation) and the
  # recorder never getting a vote. The trace itself lands under
  # _apalache-out/ and the recorder reads it from the path the checker
  # prints; the output is echoed unconditionally for the repo that carries
  # this harness without the plugin.
  if [ -n "${FPL_APALACHE_ARGS:-}" ]; then
    has apalache-mc || { echo "harness: Apalache check requires apalache-mc; verification did not run" >&2; return 1; }
    ap_out="$(mktemp)"
    ap_rc=0
    # shellcheck disable=SC2086
    apalache-mc check $FPL_APALACHE_ARGS >"$ap_out" 2>&1 || ap_rc=$?
    cat "$ap_out"
    cx="$(plugin_script cex.py)"
    if [ -n "$cx" ]; then
      "$FPL_PY" "$cx" --ingest --tool apalache --from "$ap_out" \
        --exit "$ap_rc" || true
    fi
    rm -f "$ap_out"
    if [ "$ap_rc" -ne 0 ]; then return "$ap_rc"; fi
  fi
  if [ -f lakefile.lean ] || [ -f lakefile.toml ]; then
    has lake || { echo "harness: Lean project requires lake; verification did not run" >&2; return 1; }
    lake build
  fi
  if [ -f _CoqProject ]; then
    has coq_makefile || { echo "harness: Coq project requires coq_makefile; verification did not run" >&2; return 1; }
    coq_makefile -f _CoqProject -o CoqMakefile && make -f CoqMakefile
  fi
  if [ -f Cargo.toml ]; then
    has cargo || { echo "harness: Rust project requires cargo; checks did not run" >&2; return 1; }
    cargo fmt --all -- --check
    if cargo clippy --version >/dev/null 2>&1; then
      cargo clippy --all-targets --quiet -- -D warnings
    fi
    cargo test --quiet
    # Kani proof harnesses, when the crate declares any. Same shape as the
    # aiken and dafny blocks: capture,
    # record, re-raise, with the prover's exit code staying the gate and
    # the recorder never getting a vote. Put `-Z concrete-playback
    # --concrete-playback=print` (and whatever else these proofs need) in
    # FPL_KANI_ARGS so the ledger gets the interpreted input values and not
    # only the failed check; `=inplace` writes that playback test into the
    # source instead, where — renamed to carry the cexId and tracked — it is
    # a legal pin target. The output is echoed unconditionally: without the
    # plugin it is the only record of what failed.
    if grep -rqs 'kani::proof' src; then
      has cargo-kani || { echo "harness: Kani proofs require cargo-kani; verification did not run" >&2; return 1; }
      kani_out="$(mktemp)"
      kani_rc=0
      # shellcheck disable=SC2086
      cargo kani ${FPL_KANI_ARGS:-} >"$kani_out" 2>&1 || kani_rc=$?
      cat "$kani_out"
      cx="$(plugin_script cex.py)"
      if [ -n "$cx" ]; then
        "$FPL_PY" "$cx" --ingest --tool kani --from "$kani_out" \
          --exit "$kani_rc" || true
      fi
      rm -f "$kani_out"
      if [ "$kani_rc" -ne 0 ]; then return "$kani_rc"; fi
    fi
  fi
  if [ -f package.json ]; then
    run_script_if_present typecheck
    run_script_if_present lint
    # The test run is captured the way the provers are. A fast-check failure
    # prints a shrunk counterexample with the `seed` and `path` that replay
    # it, and that is the most reusable thing an off-chain property test
    # produces — it otherwise scrolls out of the terminal. Capture, echo,
    # record, re-raise: the runner's exit code stays the gate and the
    # recorder never gets a vote. Only the vitest reporter is parsed; any
    # other shape files a loud INGEST-FAILED rather than reading as green.
    if has_script test; then
      js_out="$(mktemp)"
      js_rc=0
      "$(pm)" run test >"$js_out" 2>&1 || js_rc=$?
      cat "$js_out"
      cx="$(plugin_script cex.py)"
      if [ -n "$cx" ]; then
        "$FPL_PY" "$cx" --ingest --tool fastcheck --from "$js_out" \
          --exit "$js_rc" || true
      fi
      rm -f "$js_out"
      if [ "$js_rc" -ne 0 ]; then return "$js_rc"; fi
    fi
  fi
  if has terraform && compgen -G '*.tf' >/dev/null; then
    terraform fmt -check -recursive
  fi
  # Proof-strength ratchet. A prover exits 0 on an assumed lemma exactly as it
  # does on a proved one, so the count of escape hatches may fall but never
  # rise. Dormant in repos with no proof-language files.
  if need_gate proof-guard.py .fluxpoint-proof-baseline.json; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
  # Statement ratchet. The hatch counts above police proof bodies; this
  # polices what is being proved, because dropping a conjunct from an
  # `ensures` or deleting a property test moves no count and keeps every
  # checker green. It also holds the DoD's `— proof:` claims to the scan,
  # audits headline theorems' axioms when the baseline names any, and
  # requires every class in .fluxpoint-attacks.json to be specified, so the
  # attack manifest arms it on its own. Dormant until one of them exists.
  if need_gate spec-guard.py .fluxpoint-proof-baseline.json .fluxpoint-attacks.json; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
  # Seam ratchet. The mutation score below asks whether the tests can fail;
  # this asks whether they reach the code at all — a mock of a module you own
  # walls it off and asserts a contract nothing verifies, and a mutant behind
  # that wall reports the same green either way. Dormant until armed with
  # --baseline.
  if need_gate seam-guard.py .fluxpoint-proof-baseline.json; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
  # Counterexample ledger. A prover's shrunk failing input is the most
  # reusable thing it produces and it lives in a log the next command
  # overwrites. This fails when a pinned counterexample has lost the
  # regression test that carries it. Dormant with nothing recorded.
  if need_gate cex.py .fluxpoint-cex.jsonl; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
  # Blueprint conformance. Every gate above judges the on-chain predicate.
  # This one judges the off-chain encoder against the CIP-57 schema the
  # validator itself declares, because a validator proved correct still
  # signs whatever the builder constructs, and the caller an agent reaches
  # is the builder. Outside the aiken block on purpose: a repo may commit
  # plutus.json and check it in CI without building there. Dormant until
  # a corpus is declared in .fluxpoint-blueprint.json.
  if need_gate blueprint-guard.py .fluxpoint-blueprint.json plutus.json; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
  # Mutation score. Every check above asks whether the tests pass; this asks
  # whether they can fail. Cheap here on purpose — it re-runs nothing and
  # only asks whether a measurement exists and still describes this tree.
  # The expensive `--measure` belongs off-session, on a Routine.
  if need_gate mutation-guard.py .fluxpoint-mutation.json .fluxpoint-proof-baseline.json; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
  # Guard ratchet. Everything above judges proofs and tests; this judges the
  # GUARDS — the lines that stop money moving wrongly — structurally: the
  # guard is still in the code and its proof still names it. The expensive
  # --verify (prove the guard bites by disabling it) belongs off-session.
  # Gated on the manifest existing because guard-guard is deliberately loud
  # when invoked without one — "register your guards or state you have none"
  # is the right answer to a person, and the wrong one to every repo that
  # never declared any.
  if [ -f .fluxpoint-guards.json ] \
     && need_gate guard-guard.py .fluxpoint-guards.json; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
  # Relation gate. Every check above measures one artifact; the defects that
  # cost the most are relationships between two, and a suite stays green
  # because each half is individually correct. Dormant without a manifest.
  # --check runs co-change, every parity command, and every differential
  # (generated inputs through both implementations, bytes compared);
  # FPL_PAIR_CASES caps the differential when this gate needs to be quick.
  # The expensive --verify (mutate each reader, require the parity to go
  # RED, restore) belongs off-session, on a Routine, like guard-guard.
  #
  # `if`, not `[ -n "$x" ] && cmd`: as the LAST statement of a function under
  # `set -e`, that form returns 1 when the variable is empty, so a repo whose
  # plugin is not installed failed --full for no reason at all.
  if need_gate pair-guard.py .fluxpoint-pairs.json; then
    # Fall back to the session baseline the Stop gate already exports.
    # pair-guard diffs against HEAD by default, so an agent that committed
    # its slice — which WORK_PROMPT.md step 5 tells it to do — empties
    # `git diff HEAD`, and the co-change tier reports "no changes to
    # compare" over work sitting right there in the commit. A pair with no
    # parity command is then enforced by nothing. Every other check in the
    # gate judges against where the session started; this one opted out by
    # omission, not by design.
    pair_base="${FPL_PAIR_AGAINST:-${FPL_DIFF_BASE:-}}"
    "$FPL_PY" "$FPL_GATE" --check ${pair_base:+--against "$pair_base"}
  fi
  # Recurrence gate. Every check above judges the tree; this one judges what
  # the repo keeps re-learning. A lesson filed a second time is not a
  # duplicate to collapse, it is a missing gate — so past the threshold the
  # item stops being satisfiable by another lesson and demands a command
  # whose exit code is its verdict, executed here rather than reported.
  # Armed by the lesson store itself, so it cannot be dodged by deleting the
  # manifest, and dormant in every repo that has filed none.
  if need_gate recurrence-guard.py .fluxpoint-recurrence.json \
       .claude/fluxpoint/memory.jsonl; then
    "$FPL_PY" "$FPL_GATE" --check
  fi
}

case "$mode" in
  --changed) changed ;;
  --full) full ;;
  *)
    echo "usage: harness.sh --changed <file> | --full" >&2
    exit 64
    ;;
esac
