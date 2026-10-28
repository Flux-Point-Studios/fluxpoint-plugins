#!/usr/bin/env bash
# Wake parked campaigns whose blocking condition has cleared.
#
# A blocked node is not a stalled one. A 72h governance timelock, an
# operator wallet with no spendable UTxOs, a multi-day reward soak — the
# condition clears on its own schedule, and polling it by hand is exactly
# the "constantly check back in and nudge" this is meant to remove.
#
# Each parked wait carries a predicate the repo wrote. This runs the ones
# that are due, and reports which campaigns are ready to resume. It does
# not resume them itself: re-invoking a graph is an action with a budget
# and, potentially, an irreversible node behind it, so a human or an
# explicitly-configured Routine makes that call.
#
#   wake-check.sh            run due checks, report what is ready
#   wake-check.sh --all      ignore the interval and check everything now
#
# Exit 0 always unless a wait file is malformed: this is a poller, and a
# check that is simply not ready yet is not an error.
set -uo pipefail


. "${BASH_SOURCE[0]%/*}/python-env.sh"

ROOT="${FPL_ROOT:-.}"
WAITS="$ROOT/.claude/fluxpoint/waits"
ALL=0
[ "${1:-}" = "--all" ] && ALL=1

[ -d "$WAITS" ] || { echo "wake-check: nothing parked"; exit 0; }

now=$(date -u +%s)
ready=0; waiting=0; expired=0

for f in "$WAITS"/*.json; do
  [ -e "$f" ] || continue
  # Unit-separated, so a check command containing spaces survives the read.
  IFS=$'\x1f' read -r node check every deadline last runid campaign < <(
    "$FPL_PY" - "$f" <<'PY'
import json, sys
w = json.load(open(sys.argv[1]))
print(w.get("node",""), w.get("check",""), w.get("everyMinutes",60),
      w.get("deadline",""), w.get("lastChecked",0), w.get("runId",""),
      w.get("campaign",""), sep="\x1f")
PY
  ) || { echo "wake-check: $f is malformed" >&2; exit 1; }

  if [ -n "$deadline" ]; then
    dl=$(date -u -d "$deadline" +%s 2>/dev/null || echo 0)
    if [ "$dl" -gt 0 ] && [ "$now" -gt "$dl" ]; then
      echo "wake-check: EXPIRED  $campaign / $node — deadline $deadline passed"
      "$FPL_PY" "$(dirname "$0")/inbox.py" --root "$ROOT" --add \
        --kind wake-expired --node "$node" --campaign "$campaign" \
        --detail "wake deadline $deadline passed with the condition unmet" \
        >/dev/null 2>&1
      expired=$((expired+1))
      continue
    fi
  fi

  due=$(( last + every * 60 ))
  if [ "$ALL" -eq 0 ] && [ "$now" -lt "$due" ]; then
    waiting=$((waiting+1))
    continue
  fi

  if sh -c "$check" >/dev/null 2>&1; then
    echo "wake-check: READY    $campaign / $node"
    echo "    resume: /fluxpoint:graph-run  (resumeFromRunId $runid)"
    "$FPL_PY" "$(dirname "$0")/inbox.py" --root "$ROOT" --add \
      --kind wake-ready --node "$node" --campaign "$campaign" \
      --detail "wake condition met; campaign can resume from $runid" \
      >/dev/null 2>&1
    ready=$((ready+1))
  else
    waiting=$((waiting+1))
  fi
  "$FPL_PY" - "$f" "$now" <<'PY'
import json, sys
p, now = sys.argv[1], int(sys.argv[2])
w = json.load(open(p)); w["lastChecked"] = now
json.dump(w, open(p, "w"), indent=2)
PY
done

echo "wake-check: $ready ready, $waiting still waiting, $expired expired"
exit 0
