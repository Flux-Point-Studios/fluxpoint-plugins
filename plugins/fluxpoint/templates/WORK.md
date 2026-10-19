# WORK: <one-line goal>

STATUS: ACTIVE
MODE: loop
SPEC: .fluxpoint-spec.json

One goal, one Definition of Done, one Evidence table — whether the work is
decomposed as loop slices, as a graph campaign, or both. MODE is `loop`
(work the Plan), `graph` (run the Campaign), or `both`. Nothing here is
true because an agent said so; every line names its proof.

## Definition of Done
Every line must be provably true, with the proof named. The Stop gate and
the outer loop only trust `scripts/harness.sh --full`; everything else
needs a row in Evidence.

- [ ] `scripts/harness.sh --full` exits 0
- [ ] <acceptance criterion> — proof: <command | tx hash | screenshot>
- [ ] Exercised in the real runtime (browser / device / preview net) — proof recorded
- [ ] Diff carries no single-caller abstractions and no dead code

## Plan
<!-- `- [ ]` todo · `- [x]` done · `- [~] <item> — blockedOn: <who>`
     parked on someone who is not the loop. The loop skips `[~]`;
     re-picking a blocked slice spends the iteration budget on nothing. -->

Loop slices, worked one per iteration per `WORK_PROMPT.md`.

Complete the grill-me decision pass and lock the requirement packet before
implementation. Map each slice to requirement ids and their failing checks.

- [ ] <failing test for the first behavior>
- [ ] <minimum code to flip it green>
- [ ] <next slice>

## Campaign
Graph campaign for this goal. Delete this section in `MODE: loop` repos.
The IR is the single source of truth: `/fluxpoint:graph-run` compiles it
mechanically, so the spec cannot drift from what executes. Compile-check
with `compile-graph.py WORK.md --check`; see `templates/WORK.feature.md`
for a worked council → build → independently-gated campaign.

```json graph-ir
{
  "version": 1,
  "name": "campaign",
  "campaign": "<one line: what this campaign proves or produces>",
  "budget": { "maxNodes": 12, "verifyFloorTokens": 50000, "cacheTtl": "1h", "maxEstimatedTokens": 350000 },
  "defaults": { "effort": "medium" },
  "argDefaults": {
    "target": "the uncommitted diff: git diff HEAD, plus untracked files from git status"
  },
  "roles": { "finder": { "effort": "medium" } },
  "lists": {
    "dimensions": [
      { "key": "correctness", "brief": "wrong output, broken invariants, unhandled edges, boundary math" },
      { "key": "security", "brief": "the red-team surface: eUTxO, oracle, authority, numeric, off-chain, infra" }
    ]
  },
  "nodes": [
    {
      "id": "find",
      "phase": "Find",
      "role": "finder",
      "foreach": "dimensions",
      "prompt": "Review {{A.target}} for {{item.brief}}. Read the code yourself with Read/Grep/Bash. Report only findings with a concrete failure path (inputs/state -> wrong outcome); no style commentary. An empty findings list is a valid, welcome answer.",
      "contract": "FindingsV1",
      "verify": "skeptic:1",
      "verifyOver": "findings",
      "expectItems": 3,
      "onRed": "drop+log"
    }
  ]
}
```

The 350k estimated-token ceiling includes the locked packet and leaves
headroom above the compiler's estimate. Re-run `--check` after replacing this
example with the consuming repository's packet and campaign; runtime prompt
substitutions listed as unpriced still need their own allowance.

## Constraints
- <what must not change on the way there>

## Merge policy
- Auto-merge: <yes | no>. Merge requires all of: CI harness check green,
  red-team VERDICT: SHIP, no unresolved review threads.
- Method: squash; delete branch on merge; sync default branch after.
- Merge-triggers-deploy repos: <park for human | proceed>.
- Standing authorizations: <e.g. starting scripts/loop.sh for Plan items
  in this file needs no further approval>.

## Decisions
Appended automatically by `scripts/record-run.py` — do not hand-edit.
A decision that overturned the prior is the one a fresh context will
silently re-decide the other way.

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|

## Evidence
One table for both modes, and two classes of row. `Source: gate` rows are
written by the Stop hook, which ran the configured harness invocation itself (`--full` unless `FPL_HARNESS_ARGS` says otherwise) and
recorded the exit code, the tree state, and a hash of the log — nobody
writes those by hand. Everything else (`loop` rows per slice, a runId for a
graph run appended by `scripts/record-run.py`) is a claim by whoever wrote
it. A claim without a row is treated as false; a row is not thereby true,
and the SessionStart bootstrap shows the two classes separately for exactly
that reason.

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|

## Notes for the next iteration
<current state, blockers, dead nodes, two alternative paths, which one is
being attacked>
