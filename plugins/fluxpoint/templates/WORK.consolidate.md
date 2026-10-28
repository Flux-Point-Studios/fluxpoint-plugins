# WORK: consolidate the lesson store for one tag

STATUS: DESIGN
MODE: graph

Fourth canonical campaign: memory curation as an ordinary verified run —
no daemon, no bespoke writer, no hand edits. Episodic lessons accumulate
one sweep at a time, so near-duplicates pile up under different dedupe
keys, claims drift from the code they describe, and kills go stale when
the file they judged has since changed. This campaign reads the store,
proposes the consolidated state, has a skeptic attack each proposal, and
lets `record-run.py` file the survivors through the same single-writer
path every other lesson takes. Supersession stays append-only: re-filing
an identity refreshes it, nothing is ever rewritten, and the history of
what was believed when stays readable.

Copy over `WORK.md`'s Campaign section to use it, and set the tag in all
three places (`memory.seed`, `memory.emit`, the prompt) to the stream you
are consolidating — the compiler requires them to be literal kebab-case
tags, not arguments.

## Org graph
| Role | Zone owned | Binding | Notes |
|---|---|---|---|
| curator | read-only over `.claude/fluxpoint/` | inline prompt | proposes; never files — record-run.py stays the only writer |

## Campaign

```json graph-ir
{
  "version": 1,
  "name": "memory-consolidation",
  "campaign": "Consolidate the defect-sweep lesson stream: one canonical row per real finding, stale kills re-examined, drifted claims restated against the current code",
  "budget": { "maxNodes": 40, "verifyFloorTokens": 40000, "nodeFloorTokens": 60000, "cacheTtl": "1h", "maxEstimatedTokens": 600000 },
  "defaults": { "effort": "medium" },
  "roles": { "curator": { "effort": "medium" } },
  "nodes": [
    {
      "id": "curate",
      "phase": "Consolidate",
      "role": "curator",
      "prompt": "Curate this repo's lesson store for the tag defect-sweep. Read .claude/fluxpoint/memory.jsonl yourself — append-only JSONL of LessonV1 rows, identity is tag|dedupeKey, the latest row per identity wins, and each row carries arrivals/classArrivals counting the distinct runs that have filed it — and compare each surviving lesson against the CURRENT code it describes. Propose consolidations as findings, one per item: (a) near-duplicates — two identities describing one defect — restated as a single canonical claim carrying the file and the strongest evidence from both; (b) claims whose code has changed enough that the lesson is misleading as written, restated against what the code does today; (c) killed lessons whose objection no longer holds because the guard or type that killed them moved, restated as live claims. Give every finding a defectClass naming the SHAPE the lesson is an instance of, and carry the EXISTING class forward when the rows you are merging already have one — merging two identities into one canonical claim is exactly how a recurrence gets laundered into a single first-time lesson, and the class is what survives that. Each finding needs file (the file the lesson is actually about), claim (the canonical statement, standing alone, >= 20 chars) and a concrete failure_path. Do NOT propose deletions — the store is append-only and a replaced identity stays readable. Already proposed this run: {{seen}}. An empty findings list is a valid answer and ends the pass.",
      "contract": "FindingsV1",
      "verify": "skeptic:1",
      "verifyOver": "findings",
      "expectItems": 4,
      "onRed": "drop+log",
      "repeat": {
        "untilDryRounds": 1,
        "maxRounds": 3,
        "dedupeBy": ["file", "claim"]
      },
      "memory": {
        "seed": "defect-sweep",
        "emit": "defect-sweep",
        "priors": true,
        "classBy": ["defectClass"]
      }
    }
  ]
}
```

## Verification map
- `curate`: `skeptic:1` — every proposal is attacked before it can become
  a filed row; `memory.emit` refuses to file unjudged output by
  construction, so the tier is load-bearing, not decorative.
- `memory.priors` rides the tag's killed lessons into the skeptic's
  prompt: the objection that killed a claim once is exactly the argument a
  consolidation skeptic needs in hand, framed as a prior it may overturn.
- Filing integrity is not this graph's job and cannot be: rows land only
  when `record-run.py` files the run's summary, after the run artifact
  exists, through `memory.py`'s provenance check — the same path that
  stops a fabricated summary from planting durable knowledge. A
  consolidation row is distinguishable in the store by its provenance
  runId, and `/fluxpoint:recall stats` shows the store's state after.
- Supersession semantics do the merging: a proposal re-filed under an
  existing identity (same file and claim) refreshes that identity; a
  restated claim files a new canonical identity while the old row stays,
  append-only, with its history intact. Nothing here deletes, and recall's
  latest-wins collapse plus near-duplicate dedupe do the rest at read
  time.
- What consolidation must NOT collapse is the count. Merging two identities
  into one canonical claim is the single move that can turn a recurrence
  back into a first-time lesson, and it is why `classBy` is declared here as
  well as on the sweep: the restated row starts its own instance count and
  inherits the class one, so the recurrence gate still sees it. A class that
  has arrived twice is not answerable by a better-worded claim — it is
  answerable only by a command in `.fluxpoint-recurrence.json` whose exit
  code is the lesson's verdict.

## Failure policy
- Dry rule: one pass with nothing new to propose ends the campaign — a
  curated store should go dry immediately on the second look.
- Hard ceiling: 3 rounds. Hitting it while still proposing is logged
  `discovery INCOMPLETE, not exhausted` and downgrades the outcome —
  a store that keeps yielding consolidations after three passes needs a
  human looking at why, not a longer loop.
- Budget: `nodeFloorTokens` declines a further pass rather than
  half-running one; declined work files SKIPPED and the Evidence row says
  coverage was incomplete.
- Run it on a schedule if the repo runs many sweeps — a cloud Routine
  invoking `/fluxpoint:graph-run` on this file monthly is the shape the
  roadmap names — or by hand when `/fluxpoint:recall stats` shows the
  store outgrowing its usefulness.

`maxNodes` prices the worst case: 3 rounds x (1 curator + 4 expected
items x 1 skeptic) = 15, with headroom for re-proposals. Typical runs
cost one round. `maxEstimatedTokens` (600k) bounds the same worst case in
tokens, including the expanded locked packet, under the declared 1-hour
prompt-cache TTL. Re-run `--check` with the consuming repository's packet
and expected item counts; the shipped ceiling leaves headroom above this
repository's estimate and is not a measured bill.

## Decisions
Appended automatically by `scripts/record-run.py` — do not hand-edit.

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|

## Evidence
Appended automatically by `scripts/record-run.py` — do not hand-edit.

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|

## Notes for the next run
<what was consolidated, what the skeptic killed, whether the store went dry>
