#!/usr/bin/env bash
# Shared helpers for fluxpoint hooks. Sourced, never executed.
# JSON handling prefers jq and falls back to "$FPL_PY" so the hooks work on
# machines without jq installed.


# Shared, cached resolution also used by the Python command launcher.
_fpl_lib_dir="${BASH_SOURCE[0]%/*}"
. "$_fpl_lib_dir/python-env.sh"
unset _fpl_lib_dir

# $PWD, not CLAUDE_PROJECT_DIR. Every hook calls fpl_cd_project first, which has already
# resolved the repo — the payload's own cwd, climbed to the git toplevel — so the working
# directory IS the right answer by the time anyone asks. Preferring the project dir here
# undoes that one line later, and in a multi-repo workspace it points at a root that holds
# no state directory at all: the hooks would cd correctly and then read the wrong place.
fpl_state_dir() {
  printf '%s/.claude/fluxpoint' "$PWD"
}

# fpl_sid <raw> — a session id safe to concatenate into file paths. The
# runtime mints UUIDs today, but every consumer here builds paths from the
# value, and an id carrying separators would write outside the state dir.
# Same munge the runtime applies to its own task ids.
fpl_sid() {
  printf '%s' "${1:-nosession}" | tr -c 'A-Za-z0-9_-' '-'
}

# fpl_state_file — the repo's work-state file. WORK.md is current; LOOP.md is
# still honored so repos that have not run /fluxpoint:migrate keep working.
# Prints nothing when neither exists.
fpl_state_file() {
  for f in WORK.md LOOP.md; do
    [ -f "$f" ] && { printf '%s' "$f"; return 0; }
  done
  return 1
}

# fpl_legacy_state_dir — pre-1.0 state lived under .claude/fluxpoint-loop.
fpl_legacy_state_dir() {
  printf '%s/.claude/fluxpoint-loop' "$PWD"
}

# fpl_run_summary <runs/xxx.json> — one line describing a recorded graph run.
fpl_run_summary() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '"\(.runId) \(.outcome), nodes \(.nodesOk)/\(.nodesDead) dead, \(.findings) finding(s), harness \(.harnessExit), red-team \(.redTeam)"' "$1" 2>/dev/null
  else
    "$FPL_PY" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print("%s %s, nodes %s/%s dead, %s finding(s), harness %s, red-team %s" % (
    d.get("runId"), d.get("outcome"), d.get("nodesOk"), d.get("nodesDead"),
    d.get("findings"), d.get("harnessExit"), d.get("redTeam")))' "$1" 2>/dev/null
  fi
}

# fpl_json_get <dotted.key>  — reads JSON on stdin, prints the value or
# nothing. Booleans print as true/false to match jq -r semantics.
fpl_json_get() {
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$1" 'getpath($k / ".") // empty' 2>/dev/null
  else
    "$FPL_PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for part in sys.argv[1].split("."):
    d = d.get(part) if isinstance(d, dict) else None
    if d is None:
        sys.exit(0)
print(d if isinstance(d, str) else json.dumps(d))' "$1" 2>/dev/null
  fi
}

# fpl_edit_paths — reads a PostToolUse payload on stdin and prints one line
# per file the tool call wrote, as "<op> <path>": A added, U updated, D
# deleted. Two payload shapes reach the file-writing hook. Claude Code's
# Write, Edit and MultiEdit carry tool_input.file_path, printed as-is. Codex's
# apply_patch, which its matcher aliases fire for Write|Edit, carries the
# whole patch in tool_input.command, so the paths are read out of the patch's
# own headers (`*** Add File:`, `*** Update File:`, `*** Move to:`,
# `*** Delete File:`). Those are relative to the directory the patch was
# applied in, and by the time a caller reads them the hook has cd'd to the
# project root, so a path that does not resolve from here is re-based from
# the payload's cwd. Prints nothing for a payload that names no file.
fpl_edit_paths() {
  # The payload is on stdin, so the script rides in as an argument.
  "$FPL_PY" -c '
import json, os, re, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ti = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
fp = ti.get("file_path")
if isinstance(fp, str) and fp.strip():
    print("U " + fp.strip())
    sys.exit(0)
if fp is not None:
    sys.exit(0)
text = ""
for key in ("command", "patch", "input"):
    v = ti.get(key)
    if isinstance(v, str) and "*** " in v:
        text = v
        break
if not text:
    sys.exit(0)
cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else ""
def rebase(p):
    if os.path.isabs(p) or os.path.lexists(p) or not cwd:
        return p
    joined = os.path.join(cwd, p)
    if os.path.lexists(joined):
        try:
            return os.path.relpath(joined, os.getcwd())
        except ValueError:
            return joined
    return p
entries = []
for m in re.finditer(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+?)\s*$", text, re.M):
    kind, path = m.group(1), m.group(2).strip()
    if kind == "Move to":
        if entries and entries[-1][0] == "U":
            entries[-1] = ("U", path)
        continue
    entries.append(({"Add File": "A", "Update File": "U", "Delete File": "D"}[kind], path))
seen = set()
for op, path in entries:
    path = rebase(path)
    if (op, path) in seen:
        continue
    seen.add((op, path))
    print(op + " " + path)
' 2>/dev/null
}

# Move to the directory a hook should act in. Returns non-zero only if it cannot
# reach one at all.
#
# THE PROJECT DIR KEEPS JURISDICTION WHENEVER IT IS ITSELF INSIDE A GIT WORK
# TREE: single-repo sessions, and projects scoped to a subdirectory of a larger
# repo. These hooks are the gate for ONE project, and a session's cwd is
# wherever its shell last cd'd — a dependency checkout, a sibling repo, a
# scratch git init. A Stop gate that followed the cwd there would conclude
# "harness absent" about a repo it was never the gate for, delete the real
# project's arming marker, and wave the stop through — a plain cd would disarm
# the gate, which is the one thing it must never do.
#
# THE PAYLOAD'S OWN cwd WINS only when the project dir cannot be the repo. In a
# MULTI-REPO WORKSPACE the project dir is the workspace root, deliberately not a
# git repo, while the command ran inside one of the repos beneath it — and that
# repo is where the manifests, the harness and the state directory live.
# Resolving the project dir first landed outside every repo and exited before
# reading anything, so attestation, the DoD gate and the per-edit harness were
# all silently dormant for the whole workspace. (secret-guard resolves cwd
# first unconditionally, and rightly: it judges the COMMAND, and the command
# ran in the cwd. These hooks judge a PROJECT.)
#
# Then climb to the git toplevel, because a gate is often run from a
# subdirectory while the artifacts it is judged against sit at the repo root.
# The climb only happens on the workspace path, so it can never walk out of a
# project that sits below a bigger repo's toplevel. Outside a repo, stay put
# and let the caller decide what that means.
_fpl_contained_dir() { # $1 = workspace, $2 = candidate
  "$FPL_PY" - "$1" "$2" <<'PY'
import os
import sys

workspace = os.path.realpath(os.path.abspath(sys.argv[1]))
raw_candidate = sys.argv[2]
if not os.path.isabs(raw_candidate):
    raw_candidate = os.path.join(workspace, raw_candidate)
candidate = os.path.realpath(os.path.abspath(raw_candidate))
try:
    inside = (os.path.normcase(os.path.commonpath((workspace, candidate))) ==
              os.path.normcase(workspace))
except ValueError:
    inside = False
if not inside or not os.path.isdir(candidate):
    raise SystemExit(1)
sys.stdout.write(candidate)
PY
}


fpl_cd_project() { # $1 = raw hook payload
  local _fpl_p _fpl_r _fpl_workspace=""
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] \
     && git -C "$CLAUDE_PROJECT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || return 1
    return 0
  fi
  _fpl_p="$(printf '%s' "${1:-}" | fpl_json_get cwd)"
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
    [ -d "$CLAUDE_PROJECT_DIR" ] || return 1
    _fpl_workspace="$CLAUDE_PROJECT_DIR"
    [ -n "$_fpl_p" ] || _fpl_p="$CLAUDE_PROJECT_DIR"
    _fpl_p="$(_fpl_contained_dir "$_fpl_workspace" "$_fpl_p")" || return 1
  else
    { [ -n "$_fpl_p" ] && [ -d "$_fpl_p" ]; } || _fpl_p="."
  fi
  cd "$_fpl_p" 2>/dev/null || return 1
  _fpl_r="$(git rev-parse --show-toplevel 2>/dev/null)"
  # A toplevel that cannot be entered must fail the hook, not leave it running
  # in a subdirectory where "scripts/harness.sh is absent" reads as true.
  if [ -n "$_fpl_r" ]; then
    if [ -n "$_fpl_workspace" ]; then
      _fpl_r="$(_fpl_contained_dir "$_fpl_workspace" "$_fpl_r")" || return 1
    fi
    cd "$_fpl_r" 2>/dev/null || return 1
  fi
  return 0
}

# fpl_json_obj key value [key value ...]  — emits a flat JSON object with
# string values, safely escaped.
fpl_json_obj() {
  if command -v jq >/dev/null 2>&1; then
    local args=() filter='{' i=0
    while [ "$#" -gt 0 ]; do
      args+=(--arg "k$i" "$1" --arg "v$i" "$2")
      filter="${filter}(\$k$i): \$v$i,"
      shift 2
      i=$((i + 1))
    done
    jq -cn "${args[@]}" "${filter%,}}"
  else
    "$FPL_PY" -c '
import json, sys
a = sys.argv[1:]
print(json.dumps(dict(zip(a[::2], a[1::2]))))' "$@"
  fi
}

# fpl_memory_sha — a hash of the work file's Decisions and Notes sections.
#
# Those two sections are where reasoning becomes durable: everything else a
# session knows lives in a transcript that compaction summarizes and a
# process exit discards. Hashing them lets a hook answer one question
# deterministically — did anything this session learned reach the disk? —
# without a model reading anything.
fpl_memory_sha() {
  local state
  state="$(fpl_state_file || true)"
  [ -n "$state" ] || { printf 'nostate'; return 0; }
  "$FPL_PY" - "$state" <<'PY' 2>/dev/null || printf 'unknown'
import hashlib, re, sys
try:
    text = open(sys.argv[1], errors="replace").read()
except OSError:
    print("unknown"); raise SystemExit
keep = []
for name in ("Decisions", "Notes"):
    for m in re.finditer(r"^##\s+" + name + r"[^\n]*$(.*?)(?=^##\s|\Z)",
                         text, re.S | re.M | re.I):
        keep.append(m.group(1))
print(hashlib.sha256("".join(keep).encode("utf-8", "replace")).hexdigest()[:12])
PY
}

# fpl_aux_sha <session_id> — a hash of the durable surfaces that live
# OUTSIDE the repo: the project's auto-memory store and the session's task
# board. Together with fpl_memory_sha these are where a session's learning
# survives; a compaction window in which none of the three changed is a
# window whose reasoning exists only in the transcript being summarized.
#
# Paths follow Claude Code's own conventions — projects/<munged-path>/memory
# and tasks/<session-id> under the config dir (CLAUDE_CONFIG_DIR when the
# user relocated it, ~/.claude otherwise) — with the directory passed as an
# argument rather than read from the environment inside python, because on
# Windows the MSYS layer converts POSIX paths in argv but not in exported
# variables. FPL_AUX_HOME exists so tests can fake the whole surface.
# Prints "noaux" when neither directory exists, which compares equal to
# itself and so never blocks anything on its own.
fpl_aux_sha() {
  local cfg
  if [ -n "${FPL_AUX_HOME:-}" ]; then cfg="$FPL_AUX_HOME/.claude"
  else cfg="${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}"; fi
  # CLAUDE_PROJECT_DIR here, unlike fpl_state_dir: this path is not repo state,
  # it is a KEY into Claude Code's own projects/<munged-path> naming, and the
  # runtime munges the directory the session was LAUNCHED from. In a multi-repo
  # workspace the hooks cd into the repo, but the session's auto-memory store
  # stays keyed to the workspace root — $PWD here would hash a directory that
  # does not exist and report the store's flushes as "noaux", blinding the
  # compaction window to exactly the surface it exists to protect.
  "$FPL_PY" - "${CLAUDE_PROJECT_DIR:-$PWD}" "$(fpl_sid "${1:-}")" \
    "$cfg" <<'PY' 2>/dev/null || printf 'noaux'
import hashlib, os, sys
proj, sid, cfg = sys.argv[1], sys.argv[2], sys.argv[3]
munged = "".join(c if c.isalnum() else "-" for c in proj)
h = hashlib.sha256()
found = False
for d in (os.path.join(cfg, "projects", munged, "memory"),
          os.path.join(cfg, "tasks", sid)):
    if not os.path.isdir(d):
        continue
    found = True
    for root, dirs, files in os.walk(d):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            h.update(("%s|%d|%d\n" % (os.path.relpath(p, d).replace(os.sep, "/"),
                                      st.st_size, st.st_mtime_ns)).encode())
print(h.hexdigest()[:12] if found else "noaux")
PY
}

_fpl_skip_path() {
  case "$1" in
    *.md|*.svg|*.min.js|*.lock|package-lock.json|pnpm-lock.yaml|yarn.lock) return 0 ;;
    .claude/*|*/.claude/*|node_modules/*|*/node_modules/*) return 0 ;;
    # The proof ratchet's own baseline NAMES every marker the scan hunts —
    # "lean.sorry": 0 is a count of zero holes, and reading it as a hole makes
    # the guard flag the evidence that it is clean.
    .fluxpoint-proof-baseline.json|*/.fluxpoint-proof-baseline.json) return 0 ;;
    target/*|*/target/*|dist/*|*/dist/*|build/*|*/build/*|.git/*) return 0 ;;
    # A Python repo that has not gitignored its virtualenv otherwise has the
    # hygiene scan walk site-packages.
    .venv/*|*/.venv/*|venv/*|*/venv/*|__pycache__/*|*/__pycache__/*) return 0 ;;
  esac
  return 1
}

# fpl_base_file <session_id> — where this session's gate baseline lives.
fpl_base_file() {
  printf '%s/%s.base' "$(fpl_state_dir)" "${1:-nosession}"
}

# fpl_base_ref <session_id> — the commit the gate compares the tree against.
#
# HEAD is the wrong baseline and it was a real bypass: an agent that writes
# source through the Bash tool (which fires no PostToolUse marker), commits,
# and stops leaves a tree identical to HEAD, so the gate found nothing to
# judge and the harness never ran. The work file's own prompt tells the loop
# to commit every slice, which made that the normal path rather than a
# clever one.
#
# So the baseline is the commit this session started from — or the last one
# the gate passed — and work committed mid-session is still work the gate
# has not seen.
#
# Falls back to HEAD whenever the recorded base cannot describe the current
# branch: a stale sha, a rebase, or a branch switch would otherwise diff the
# tree against unrelated history, arming the gate and flooding the hygiene
# scan with findings nobody introduced. A gate that cries wolf gets turned
# off, which is a worse outcome than the bypass it was closing.
fpl_base_ref() {
  "$FPL_PY" "${BASH_SOURCE[0]%/*}/session-baseline.py" "$(fpl_base_file "$1")"
}

# fpl_set_base <session_id> [ref] — record a baseline. Written at
# SessionStart only when absent (so compaction and resume cannot silently
# forgive commits made before them) and refreshed by the gate on green,
# because a tree that just passed is the right thing to measure the next one
# against.
fpl_set_base() {
  local sd f
  sd="$(fpl_state_dir)"
  f="$(fpl_base_file "$1")"
  git rev-parse --verify -q HEAD >/dev/null 2>&1 || return 0
  mkdir -p "$sd" 2>/dev/null || return 0
  git rev-parse "${2:-HEAD}" 2>/dev/null >"$f"
  git rev-parse HEAD 2>/dev/null >"$f.head"
  # Keep this anchor until the next PASS. An imported checkout can move the
  # diff baseline, but must not erase which commits this session authored.
  git reflog show --format=%H HEAD 2>/dev/null | wc -l | tr -d ' ' >"$f.reflog-count"
}

# fpl_code_dirty — succeeds when the tree carries code the DoD gate should
# judge: anything added, modified or renamed since the session baseline, or
# untracked, that survives _fpl_skip_path. Re-derived from git rather than
# read from a marker, so source edits made through the Bash tool (cat >,
# sed -i, git apply, tee) arm the gate exactly like Write/Edit/MultiEdit do —
# committed or not.
fpl_code_dirty() {
  local f base="${FPL_DIFF_BASE:-HEAD}"
  if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      _fpl_skip_path "$f" && continue
      return 0
    done < <(git diff "$base" --name-only --diff-filter=ACMRD 2>/dev/null)
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    _fpl_skip_path "$f" && continue
    return 0
  done < <(git ls-files --others --exclude-standard 2>/dev/null)
  return 1
}

# The files whose contents decide what "green" means. A verdict is only as
# trustworthy as the contract that produced it, and every one of these can
# turn a red run green without touching a line of the code under test.
FPL_TRUST_BASE="${FPL_TRUST_BASE:-}
scripts/harness.sh
.claude/settings.json
.claude/settings.local.json
.fluxpoint-hygiene-allow
.fluxpoint-hygiene-ignore
.fluxpoint-proof-baseline.json
.fluxpoint-pairs.json
.fluxpoint-gates.json
.fluxpoint-budget.json
.fluxpoint-cex.jsonl
.fluxpoint-metrics.json
.fluxpoint-metrics-baseline.json
package.json
Makefile"

# fpl_trust_base_modified — prints each trust-base file changed since the
# session baseline or untracked. Empty output means the contract that
# produced this verdict is the committed one.
#
# Diffing against the baseline rather than HEAD is what makes this real: an
# agent that re-records a proof baseline to absorb a rise, or edits the
# harness to exit 0, and then commits it, would otherwise show a clean tree.
fpl_trust_base_modified() {
  local f base="${FPL_DIFF_BASE:-HEAD}" hit
  { printf '%s\n' "$FPL_TRUST_BASE"; fpl_packet_trust_paths; } | sort -u | while IFS= read -r f; do
    [ -n "$f" ] || continue
    hit=""
    if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
      hit="$(git diff "$base" --name-only -- "$f" 2>/dev/null)"
    fi
    [ -n "$hit" ] || hit="$(git ls-files --others --exclude-standard -- "$f" 2>/dev/null)"
    [ -n "$hit" ] && printf '%s\n' "$hit"
  done
}

# Locked packets can name repo-defined harness checks in addition to the
# fixed trust base. Read every declared packet, including campaign packets.
fpl_packet_trust_paths() {
  "$FPL_PY" - "${BASH_SOURCE[0]%/*}" <<'FPLPY' 2>/dev/null
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import specification
root = pathlib.Path.cwd()
for packet in specification.declared_packets(root):
    try:
        lock = specification.packet_paths(root, packet)[1]
        data = json.loads(lock.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("harness", {}), dict):
            continue
        for path in data.get("harness", {}):
            try:
                print(specification.in_repo(path, "locked harness input").as_posix())
            except (ValueError, TypeError):
                continue
    except (OSError, ValueError, TypeError):
        continue
FPLPY
}

# Resolve a repo's gate contract after fpl_cd_project. Explicit process env
# wins, then local settings, shared settings, and the documented default.
fpl_harness_args() {
  if [ "${FPL_HARNESS_ARGS+x}" = x ]; then
    printf '%s' "$FPL_HARNESS_ARGS"
    return
  fi
  "$FPL_PY" - <<'FPLPY'
import json
for path in (".claude/settings.local.json", ".claude/settings.json"):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get("env", {}).get("FPL_HARNESS_ARGS")
        if isinstance(value, str):
            print(value)
            break
    except (OSError, ValueError, AttributeError):
        continue
else:
    print("--full")
FPLPY
}

# fpl_harness_modified — kept as the narrow question ("is the harness itself
# changed"), still asked by name in the gate's regression suite.
fpl_harness_modified() {
  local base="${FPL_DIFF_BASE:-HEAD}"
  if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
    [ -n "$(git diff "$base" --name-only -- scripts/harness.sh 2>/dev/null)" ] && return 0
  fi
  [ -n "$(git ls-files --others --exclude-standard -- scripts/harness.sh 2>/dev/null)" ] && return 0
  return 1
}

# Hygiene scan per the Flux Point Definition of Done. Scans lines added since
# the session baseline — committed this session or not — plus untracked
# files. History older than the session is CI's job; work this session
# committed is not, or committing would launder a marker past the gate.
# Emits one "file: line" per finding.
# _fpl_hygiene_ignored — is this path excluded from the HYGIENE SCAN only?
#
# Read by fpl_scan_hygiene and deliberately NOT by _fpl_skip_path, because
# _fpl_skip_path is shared with fpl_code_dirty, the predicate that ARMS the
# gate. A repo-extensible skip list wired into that predicate would be a
# gate-disarming primitive: `src/**` in a file, and the DoD gate silently
# stops firing. That is a far worse failure than the one being fixed, so the
# arming predicate stays untouchable and this narrows only what the scan
# reads.
#
# .fluxpoint-hygiene-ignore is one glob per line, # for comments. It is in
# FPL_TRUST_BASE, so widening the scan's blind spot is itself reported on a
# green run rather than being a silent edit.
_fpl_hygiene_ignored() {
  local pat
  [ -f .fluxpoint-hygiene-ignore ] || return 1
  while IFS= read -r pat; do
    # This list is hand-authored, so on Windows it arrives CRLF. Without the
    # strip the CR lands INSIDE the glob, `scratch/*<CR>` matches nothing, and
    # the file silently does nothing while the gate keeps blocking — the fix
    # looks applied and the behaviour never changes.
    pat="${pat%$'\r'}"
    case "$pat" in ""|\#*) continue ;; esac
    # shellcheck disable=SC2254 - the pattern is a glob on purpose
    case "$1" in $pat) return 0 ;; esac
  done < .fluxpoint-hygiene-ignore
  return 1
}

fpl_scan_hygiene() {
  local f added base="${FPL_DIFF_BASE:-HEAD}"
  local filter="${BASH_SOURCE[0]%/*}/hygiene-filter.py"
  if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
    while IFS= read -r -d '' f; do
      [ -n "$f" ] || continue
      _fpl_skip_path "$f" && continue
      case "$f" in .fluxpoint-hygiene-ignore|.fluxpoint-hygiene-allow) continue ;; esac
      added="$(git diff "$base" -U0 --no-color -- "$f" | sed -n '/^+++ /d; s/^+//p')"
      [ -n "$added" ] || continue
      printf '%s\n' "$added" | "$FPL_PY" "$filter" "$f"
    done < <(git diff "$base" --name-only -z --diff-filter=ACMR 2>/dev/null)
  fi
  while IFS= read -r -d '' f; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    _fpl_skip_path "$f" && continue
    case "$f" in .fluxpoint-hygiene-ignore|.fluxpoint-hygiene-allow) continue ;; esac
    _fpl_hygiene_ignored "$f" && continue
    "$FPL_PY" "$filter" "$f" <"$f"
  done < <(git ls-files -z --others --exclude-standard 2>/dev/null)
}
