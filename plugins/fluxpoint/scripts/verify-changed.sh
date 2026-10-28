#!/usr/bin/env bash
# PostToolUse hook on the file-writing tools: Write|Edit|MultiEdit under
# Claude Code, and apply_patch under Codex, whose matcher aliases fire this
# same group. Marks the session dirty for the Stop gate and runs the repo's
# fast scoped checks; exit 2 feeds stderr back to the agent so failures get
# corrected at the edit, not at the gate.
set -u
[ "${FPL_DISABLE:-0}" = "1" ] && exit 0
here="${0%/*}"; [ "$here" = "$0" ] && here=.
. "$here/lib.sh"

input="$(cat)"
fpl_cd_project "$input" || exit 0

edits="$(printf '%s' "$input" | fpl_edit_paths)"
[ -n "$edits" ] || exit 0

# Only a path inside the project changes the project. An orchestrator's
# scratch scripts outside the repository used to arm the gate, and the
# gate then wrote its Evidence row into the very tree a live graph was
# guarding — halting the run for a mutation nothing in it made (#94).
# Resolved in Python so symlinked temp dirs and Windows drive paths
# compare the way the filesystem does, not the way the strings look.
edits="$(printf '%s\n' "$edits" | "$FPL_PY" -c '
import os, sys
root = os.path.realpath(os.getcwd())
for line in sys.stdin.read().splitlines():
    op, _, path = line.partition(" ")
    if not path:
        continue
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    try:
        inside = os.path.commonpath([root, full]) == root
    except ValueError:  # another drive on Windows
        inside = False
    if inside:
        print(line)
' 2>/dev/null)"
[ -n "$edits" ] || exit 0

# Docs, the work file and local state never arm the gate or run the harness:
# a patch that touches only those is not code changing. A deleted file has
# nothing to check, and a path the patch names but the tree does not carry
# must not read as a red check.
arm=0; todo=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  op="${line%% *}"; f="${line#* }"
  case "$f" in
    *.md | */.claude/* | .claude/*) continue ;;
  esac
  arm=1
  [ "$op" = "D" ] && continue
  [ -f "$f" ] || continue
  todo="${todo}${f}
"
done <<EOF
$edits
EOF
[ "$arm" -eq 1 ] || exit 0

sid="$(printf '%s' "$input" | fpl_json_get session_id)"
sid="${sid:-nosession}"
sd="$(fpl_state_dir)"
mkdir -p "$sd"
: >"$sd/$sid.dirty"

[ -f scripts/harness.sh ] || exit 0
log="$sd/changed.log"
rc=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if ! bash scripts/harness.sh --changed "$f" >"$log" 2>&1; then
    {
      printf 'harness --changed RED for %s (last 30 lines):\n' "$f"
      tail -n 30 "$log"
    } >&2
    rc=2
  fi
done <<EOF
$todo
EOF
exit $rc
