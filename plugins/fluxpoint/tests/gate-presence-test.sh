#!/usr/bin/env bash
# A gate that cannot run must not read as a gate that passed.
#
# Every gate in templates/harness.sh resolved its script out of
# ~/.claude/plugins and, finding nothing, skipped and still exited 0. The
# environments where that happens are the ones that matter most — CI, cloud
# sessions, fresh clones, and the detached worktrees used for independent
# verification — so "harness green" could describe a run in which the relation
# gate, the proof ratchet, the statement ratchet, the counterexample ledger
# and the mutation ratchet were all absent.
#
# The property under test is that intent decides the direction: a repo whose
# manifest declares a gate goes RED when the gate is missing, a repo that
# declared nothing stays runnable, and the gap can only be accepted on purpose.
#
# Also covers the resolver itself: `find | head -1` guarantees no ordering and
# was observed taking an ORPHANED cached version over the active one.
set -uo pipefail

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS="$PLUGIN/templates/harness.sh"
pass=0; fail=0
ok()  { printf 'PASS  %-54s -> %s\n' "$1" "$2"; pass=$((pass+1)); }
bad() { printf 'FAIL  %-54s -> %s\n' "$1" "$2"; fail=$((fail+1)); }
check(){ [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$3 (wanted $2)"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
EMPTY_DISCOVERY="$WORK/no-plugin"; mkdir -p "$EMPTY_DISCOVERY"

# Keep the caller's HOME/CODEX_HOME intact. Only the resolver's two discovery
# roots are redirected; every other find invocation keeps its real arguments.
# Delegating to real find preserves missing-directory exit/output semantics.
find() {
  if [ -n "${FPL_TEST_PLUGIN_DISCOVERY:-}" ] && [ "$#" -ge 2 ] &&
     [ "$1" = "$HOME/.claude/plugins" ] &&
     [ "$2" = "${CODEX_HOME:-$HOME/.codex}/plugins/cache" ]; then
    shift 2
    command find "$FPL_TEST_PLUGIN_DISCOVERY/.claude/plugins" \
      "$FPL_TEST_PLUGIN_DISCOVERY/.codex/plugins/cache" "$@"
  else
    command find "$@"
  fi
}
export -f find

# A repo carrying the harness, with no plugin reachable from it.
mkrepo() {
  r="$WORK/$1"; rm -rf "$r"; mkdir -p "$r/scripts"
  cp "$HARNESS" "$r/scripts/harness.sh"
  ( cd "$r" && git init -q . && git config user.email t@t && git config user.name t \
    && printf 'x\n' > file.txt && git add -A && git commit -qm init )
}
run_full() {  # $1 = repo, rest = env assignments
  ( cd "$WORK/$1" && env -u FPL_PLUGIN_ROOT -u CLAUDE_PLUGIN_ROOT -u PLUGIN_ROOT \
      FPL_TEST_PLUGIN_DISCOVERY="$EMPTY_DISCOVERY" "${@:2}" bash scripts/harness.sh --full 2>&1 )
}

# ============ 1. a declared gate that cannot run is RED ==================
mkrepo declared
printf '{"pairs": []}\n' > "$WORK/declared/.fluxpoint-pairs.json"
out="$(run_full declared)"; rc=$?
check "a declared gate with no plugin fails the harness" 1 "$rc"
case "$out" in *"pair-guard.py is not installed"*)
    ok "and names the gate and the manifest" "reported" ;;
  *) bad "and names the gate and the manifest" "${out:0:60}" ;; esac
case "$out" in *"cannot report green"*)
    ok "and says why green is unavailable" "reported" ;;
  *) bad "and says why green is unavailable" "${out:0:60}" ;; esac

# The guard ratchet's manifest carries the same weight: a repo that declared
# its money-moving guards must go red when nothing can check them.
mkrepo guarded
printf '{"guards": []}\n' > "$WORK/guarded/.fluxpoint-guards.json"
out="$(run_full guarded)"; rc=$?
check "a declared guards manifest with no plugin is RED" 1 "$rc"
case "$out" in *"guard-guard.py is not installed"*)
    ok "and names guard-guard and its manifest" "reported" ;;
  *) bad "and names guard-guard and its manifest" "${out:0:60}" ;; esac

# ============ 2. the gap is acceptable only on purpose ===================
out="$(run_full declared FPL_ALLOW_MISSING_GATES=1)"; rc=$?
check "an explicit opt-out lets the run continue" 0 "$rc"
case "$out" in *WARNING*) ok "and still says the gate did not run" "warned" ;;
  *) bad "and still says the gate did not run" "${out:0:60}" ;; esac

# ============ 3. a repo that declared nothing stays runnable =============
# The requirement the silent skip existed to serve, kept.
mkrepo bare
out="$(run_full bare)"; rc=$?
check "a repo declaring no gates still runs without the plugin" 0 "$rc"

# ============ 4. the resolver is deterministic ===========================
# Two cached versions plus a marketplace copy, laid out as a real install.
FAKE="$WORK/fake-discovery"
mkdir -p "$FAKE/.claude/plugins/cache/fluxpoint/fluxpoint/1.24.1/scripts" \
         "$FAKE/.claude/plugins/cache/fluxpoint/fluxpoint/1.26.0/scripts" \
         "$FAKE/.claude/plugins/marketplaces/fluxpoint/plugins/fluxpoint/scripts"
for v in 1.24.1 1.26.0; do
  printf 'v%s\n' "$v" > "$FAKE/.claude/plugins/cache/fluxpoint/fluxpoint/$v/scripts/pair-guard.py"
done
printf 'marketplace\n' > "$FAKE/.claude/plugins/marketplaces/fluxpoint/plugins/fluxpoint/scripts/pair-guard.py"

resolve() {  # runs plugin_script from the shipped template, nothing else
  ( set -uo pipefail
    # These cases are about fallback ORDER, so the explicit overrides that
    # legitimately short-circuit it have to be out of the way. The runtime exports
    # CLAUDE_PLUGIN_ROOT whenever the harness runs under a hook, and inheriting it made
    # both cases silently assert the override instead of the order — reporting the real
    # installed copy on the developer's machine as if it were the fixture.
    #
    # Latent until v1.30.0, because before that the hooks could not fire in a multi-repo
    # workspace at all, so this suite had only ever run from a bare shell.
    unset CLAUDE_PLUGIN_ROOT FPL_PLUGIN_ROOT PLUGIN_ROOT
    eval "$(sed -n '/^plugin_script() {/,/^}/p' "$HARNESS")"
    FPL_TEST_PLUGIN_DISCOVERY="$1" plugin_script pair-guard.py )
}
got="$(resolve "$FAKE")"
case "$got" in */marketplaces/*)
    ok "a marketplace install wins over cached copies" "marketplaces" ;;
  *) bad "a marketplace install wins over cached copies" "${got##*plugins/}" ;; esac

rm -rf "$FAKE/.claude/plugins/marketplaces"
got="$(resolve "$FAKE")"
case "$got" in *1.26.0*) ok "otherwise the highest cached version wins" "1.26.0" ;;
  *) bad "otherwise the highest cached version wins" "${got##*fluxpoint/}" ;; esac

# The orphan must not win merely by sorting first on disk.
case "$got" in *1.24.1*) bad "the orphaned older version is not picked" "1.24.1" ;;
  *) ok "the orphaned older version is not picked" "skipped" ;; esac

# ============ 5. the runtime's own root is honored =======================
# FPL_PLUGIN_ROOT had no producer anywhere in the plugin, so the first branch
# of the resolver was dead in every scaffolded repo. CLAUDE_PLUGIN_ROOT is set
# by the runtime whenever the harness runs under a hook.
ROOT="$WORK/pluginroot"; mkdir -p "$ROOT/scripts"
printf 'from-claude-root\n' > "$ROOT/scripts/pair-guard.py"
got="$( set -uo pipefail
        unset FPL_PLUGIN_ROOT CLAUDE_PLUGIN_ROOT PLUGIN_ROOT
        eval "$(sed -n '/^plugin_script() {/,/^}/p' "$HARNESS")"
        CLAUDE_PLUGIN_ROOT="$ROOT" plugin_script pair-guard.py )"
case "$got" in "$ROOT"/*) ok "CLAUDE_PLUGIN_ROOT resolves the gate" "honored" ;;
  *) bad "CLAUDE_PLUGIN_ROOT resolves the gate" "${got:-empty}" ;; esac
got="$( set -uo pipefail
        unset FPL_PLUGIN_ROOT CLAUDE_PLUGIN_ROOT PLUGIN_ROOT
        eval "$(sed -n '/^plugin_script() {/,/^}/p' "$HARNESS")"
        PLUGIN_ROOT="$ROOT" plugin_script pair-guard.py )"
case "$got" in "$ROOT"/*) ok "Codex PLUGIN_ROOT resolves the gate" "honored" ;;
  *) bad "Codex PLUGIN_ROOT resolves the gate" "${got:-empty}" ;; esac

# ====== 5. an on-chain repo cannot skip the protocol-limit gate ==========
# The budget gate sits INSIDE `[ -f aiken.toml ] && has aiken`, so listing
# contracts/aiken.toml as one of its arming configs could never arm anything:
# reaching that line already required aiken.toml at the ROOT, and a repo whose
# manifest lives at contracts/ never enters the block at all. The gate was
# therefore armed only by .fluxpoint-budget.json, while the comment above it
# says protocol limits are enforced unconditionally. An Aiken repo without that
# optional file built a validator and skipped the maxTxSize check in silence --
# this harness's own defect, wearing the fix's clothes.
mkrepo onchain
printf 'name = "t/t"\nversion = "0.0.0"\n' > "$WORK/onchain/aiken.toml"
STUB="$WORK/stub"; mkdir -p "$STUB"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/aiken"; chmod +x "$STUB/aiken"
out="$(run_full onchain PATH="$STUB:$PATH")"; rc=$?
check "an aiken repo cannot skip the protocol-limit gate" 1 "$rc"
case "$out" in *"plutus-budget.py is not installed"*)
    ok "and names the budget gate" "reported" ;;
  *) bad "and names the budget gate" "${out:0:70}" ;; esac

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
