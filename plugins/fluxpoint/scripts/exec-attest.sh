#!/usr/bin/env bash
# PostToolUse hook on the Bash tool. Records the exit code of a declared gate
# where it actually happens, so no agent has to be trusted to transcribe it.
#
# This hook never blocks and never fails an edit: attestation is a witness,
# not a gate. It exits 0 in every path, including the ones it complains about.
set -u

[ "${FPL_DISABLE:-0}" = "1" ] && exit 0
here="${0%/*}"; [ "$here" = "$0" ] && here=.
. "$here/lib.sh"

input="$(cat)"
fpl_cd_project "$input" || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Dormant in a repo that declared no gates, exactly like the DoD gate in a
# repo with no harness. Checked before anything is parsed so the common case
# costs one stat.
[ -f .fluxpoint-gates.json ] || exit 0

err="$(printf '%s' "$input" | "$FPL_PY" "$here/attest.py" --record 2>&1 >/dev/null)"
[ -z "$err" ] && exit 0

# Something is wrong with the manifest or the payload, which means gate runs
# are going unattested. Saying it once per session is the point: a layer that
# disarms quietly is worse than one that was never installed. Saying it on
# every Bash call would train people to ignore it.
sid="$(fpl_sid "$(printf '%s' "$input" | fpl_json_get session_id)")"
sd="$(fpl_state_dir)"
marker="$sd/$sid.attest-warned"
if [ ! -f "$marker" ]; then
  mkdir -p "$sd" 2>/dev/null && : >"$marker"
  fpl_json_obj systemMessage "fluxpoint attest: ${err}"
fi
exit 0
