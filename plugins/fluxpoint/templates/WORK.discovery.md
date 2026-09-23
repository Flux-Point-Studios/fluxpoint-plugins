# WORK: surface defects until two consecutive rounds find nothing new

STATUS: DESIGN
MODE: graph

Third canonical campaign: unknown-size discovery. A fixed fan-out finds
what one pass happens to find; a discovery loop keeps going until two
consecutive rounds surface nothing new. Use it for audits and sweeps where
"how many are there" is the question, not an input. Coverage is a
property of the dry rule and the budget floors, so nothing in this file
tells an agent how hard to look: the loop stops when discovery stops, and
a prompt that exhorts coverage buys verbosity and extra tool calls rather
than findings.

Copy over `WORK.md`'s Campaign section to use it.

## Org graph
| Role | Zone owned | Binding | Notes |
|---|---|---|---|
| hunter | read-only discovery | inline prompt, one per modality | each round is told what earlier rounds found |

## Campaign

```json graph-ir
{
  "version": 1,
  "name": "discovery-campaign",
  "campaign": "Sweep for defects until two consecutive rounds find nothing new",
  "budget": { "maxNodes": 126, "verifyFloorTokens": 50000, "nodeFloorTokens": 80000, "cacheTtl": "1h", "maxEstimatedTokens": 2500000 },
  "defaults": { "effort": "medium" },
  "requiredArgs": ["target"],
  "roles": { "hunter": { "effort": "medium" } },
  "lists": {
    "modalities": [
      { "key": "by-contract", "brief": "read each public interface and ask what input breaks its stated promise" },
      { "key": "by-state", "brief": "trace mutable state and find orderings that corrupt it" },
      { "key": "by-boundary", "brief": "hunt the edges: empty, zero, negative, max, unicode, concurrent" }
    ]
  },
  "nodes": [
    {
      "id": "hunt",
      "phase": "Discover",
      "role": "hunter",
      "foreach": "modalities",
      "prompt": "Hunt for defects in {{A.target}} using this modality: {{item.brief}}. Read the code yourself. Report only findings with a concrete failure path (inputs/state -> wrong outcome). Give each finding a defectClass naming its SHAPE rather than its location — the words a defect of the same kind in an unrelated file would also use, like 'hand-maintained-enumeration' or 'unchecked-boundary-crossing'. file, line and claim already say where it is; defectClass is what lets the same shape recurring in three places be counted as three, and reusing an existing class is the point, so prefer a class already in {{seen}} over a new spelling of it. Already surfaced in earlier rounds, do NOT report these again: {{seen}}. Spend this round on ground the earlier rounds did not cover. An empty findings list is a valid answer and ends the sweep.",
      "contract": "FindingsV1",
      "verify": "panel:3",
      "verifyOver": "findings",
      "expectItems": 2,
      "onRed": "drop+log",
      "repeat": {
        "untilDryRounds": 2,
        "maxRounds": 6,
        "dedupeBy": ["file", "line", "claim"]
      },
      "memory": {
        "seed": "defect-sweep",
        "emit": "defect-sweep",
        "classBy": ["defectClass"]
      }
    }
  ]
}
```

## Verification map
- `hunt`: `panel:3` — a sweep's whole value is that its output can be
  trusted without re-deriving it, so findings get a real majority.
- Dedup runs **before** verification, against everything seen rather than
  everything confirmed. Deduping against survivors would resurrect every
  judge-rejected finding each round and the loop would never converge.
- The panel only ever judges genuinely new items, so cost scales with
  discovery, not with rounds.
- `memory` carries the sweep across runs. Every judged item is filed as a
  lesson — survivors *and* the panel's kills with the objection that killed
  them — and the next run seeds from the same tag, so a second sweep opens
  where the first one stopped instead of re-arguing it. Load the seed with
  `memory.py --load --tag defect-sweep` into `args._seen`;
  `/fluxpoint:graph-run` does it.
- Seeds are advisory: they reach the finder's prompt and never the dedup
  set. A re-found item still flows through and is judged again, because a
  finding that comes back is evidence the lesson went stale, and silently
  dropping it would hide the regression the sweep exists to catch.
- `memory.classBy` is the second, coarser identity. `dedupeBy` decides
  whether two findings are the same FINDING; the class decides whether they
  are the same defect SHAPE, which no combination of file, line and claim
  can express — those three describe a defect's location and its wording.
  One shape found in three files files three lessons and one class, and
  `recurrence-guard.py` counts the class: at the second arrival the item
  stops being answerable by another lesson and demands a command whose exit
  code is its verdict. Keep classes few and reused; a class per finding
  counts nothing, and a class covering everything fires on everything.
  The decision is not optional: a node that emits lessons must either
  declare `classBy` or decline it with `"classBy": null` — the compiler
  refuses silence, because an undeclared class is the forgot-to-extend-
  the-list shape the recurrence gate exists to kill.

## Failure policy
- Dry rule: 2 consecutive rounds with nothing new ends the sweep. This is
  what *should* end it.
- Hard ceiling: 6 rounds — a backstop against a loop that never converges,
  not a thoroughness dial. Leave enough headroom above the dry rule
  (>= untilDryRounds + 3) that the ceiling is rarely what stops the sweep;
  the compiler warns when it is too tight. Rounds that never run cost
  nothing, so a generous ceiling is cheap: the `while` exits the moment the
  sweep goes dry.
- Hitting the ceiling while still finding new items is logged as
  `discovery INCOMPLETE, not exhausted`, marks the node INCOMPLETE in
  provenance, and downgrades the campaign outcome to INCOMPLETE. Stopping
  early and finishing are different claims and never get blurred.
- Budget: `nodeFloorTokens` stops a further round before it starts and
  records the sweep as INCOMPLETE rather than half-running a round.
- Halt condition a human can name: two dry rounds, six rounds total, or
  the budget floor — whichever comes first.

`maxNodes` prices the worst case: 3 modalities x (1 finder + 2 expected
items x 3 refuters) x 6 rounds = 126. Typical runs cost far less, because
a converging sweep never reaches its later rounds. `maxEstimatedTokens`
prices the same worst case in tokens (~1.27M under the declared 1-hour
prompt-cache TTL, most of it the refuters' warm reads) and is the ceiling
the bill answers to; `cacheTtl: "1h"` is declared because every round
blocks the parent far longer than the default five-minute cache lives.

## Decisions
Appended automatically by `scripts/record-run.py` — do not hand-edit.
A decision that overturned the prior is the one a fresh context will
silently re-decide the other way.

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|

## Evidence
Appended automatically by `scripts/record-run.py` — do not hand-edit.

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|

## Notes for the next run
<rounds run, whether the sweep went dry or hit its ceiling, what was left>
