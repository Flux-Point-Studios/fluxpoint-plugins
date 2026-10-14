Read WORK.md in the repo root. It is the loop's single source of state.

Before implementation, read its `.fluxpoint-spec.json` and validate the lock
with the plugin's `scripts/py.sh specification.py --check`. If absent, stale
or unrelated to the goal, perform the grill-me decision pass first. Model
defaults are changeable recommendations, never user approval. Work from the
requirement ids and their declared checks; do not weaken the packet or
rebaseline proofs to make an implementation pass.

Each iteration of this loop starts with a fresh context. Work exactly one
slice per iteration, and a slice is not finished until it is shipped or
explicitly parked:

1. Pick the first unchecked item under Plan. **Skip any item marked
   `- [~]`** — that is parked on someone who is not you, and its
   `blockedOn:` names who. Re-picking a blocked slice every iteration is
   how a 72h wait spends a whole iteration budget in minutes and produces
   nothing. If every remaining item is `- [~]`, stop and say so rather
   than burning the budget; that is a checkpoint, not a failure. If Plan
   is empty, derive the next smallest slice from the Definition of Done
   and add it first.
   If the slice you are on turns out to need someone else — a signature,
   a third party, a timelock — mark it `- [~] <item> — blockedOn: <who,
   and what would unblock it>` and move to the next unblocked one.
   If this slice declares a target in `.fluxpoint-metrics.json`, record the
   prediction **before changing code** with
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" slice-metric.py --start --slice <unique-id> --role <role> --metric <name> --predicted-delta <positive-improvement>`.
   This measures the starting value through metric-guard's repeated command;
   the prediction is in the metric's units, positive for the gain you expect.
2. TDD, strictly: write the failing test, run it, confirm the exact
   expected failure, then write the minimum code to flip it green.
3. Run scripts/harness.sh --full. Red output is the work list; fix it.
   For a target-metric slice, finish with
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" slice-metric.py --end --slice <same-id>`.
   It records the ending measurement and measured/predicted gain. Do not
   rewrite the forecast after seeing the result; changing the metric's
   definition requires a new slice. Status reports each role's calibration.
4. Append one row to Evidence for anything claimed — `| When (UTC) |
   Source | Outcome | Claim | Proof |`, with `Source: loop` for slices
   (graph runs fill their own rows with a runId): the command run and its
   result, a tx hash, a log excerpt. Unproven claims do not count.
   **Never write a `Source: gate` row.** That class belongs to the Stop
   hook, which runs the harness itself and records what it saw; a `gate`
   row you wrote is a forged one, and it is visible as such in the diff.
   Your rows are read as assertions — the bootstrap labels them that way to
   the next context — so name the command and its exit rather than
   describing the outcome in prose.
5. Check the finished Plan item off. Commit test and code together with a
   message naming the slice.
6. Ship it per the Merge policy in WORK.md: push the branch, open or
   update the PR, then red-team the diff (/fluxpoint:red-team, or
   apply the adversarial checklist inline: eUTxO, oracle, authority,
   numeric, off-chain, infra) and post the verdict on the PR.
   VERDICT: BLOCK is harness-red — fix the findings before anything else.
   On SHIP: if policy allows auto-merge, merge (squash), delete the
   branch, and sync the default branch locally; otherwise park the PR
   with a one-line handoff comment and move on. Never end an iteration
   with an unreviewed PR.
7. Update Notes for the next iteration: current state, blockers, and if
   blocked, two alternative paths and which one to attack next.
   If this slice made a real choice — one with more than one defensible
   answer — record it with
   `scripts/decision.py --record < decision.json` (DecisionV1: the options,
   the best case against each including the winner, the rationale). The
   code and the tests survive this iteration; why you chose this and not
   that does not, unless it is written down. If nothing was decided, say
   so once with `--none "<why>"` rather than leaving silence to be read as
   an oversight.

Set `STATUS: DONE` on line 3 of WORK.md only when every Definition of Done
line is checked with proof recorded. Never edit the Definition of Done,
the Merge policy, or delete tests to get there. If the same approach has
failed three iterations in a row, write a checkpoint summary in Notes
instead of retrying it a fourth time.
