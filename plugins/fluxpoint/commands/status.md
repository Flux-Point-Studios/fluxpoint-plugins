---
description: Report the harness state — gate counters, last harness verdict, WORK.md progress, recorded graph runs — and recommend the single next action.
argument-hint: [none]
---

Report the state of this repo's work, concisely. Read-only; change
nothing.

0. **Lead with what is blocked on a person.**
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" inbox.py --list`. If anything
   is open, the report opens with a **BLOCKED ON YOU** section listing each
   item and how to clear it (`/fluxpoint:release <node>` for a parked node,
   a named confirmation for a refused irreversible one). This goes first
   even when everything else is green: a campaign that parks instead of
   halting is only an improvement if somebody finds out, and an unread
   block is a quieter failure than the halt it replaced. Then
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/wake-check.sh"` if
   `.claude/fluxpoint/waits/` exists, and report anything READY or EXPIRED.
1. Read `.claude/fluxpoint/`: `last-harness`, any `*.blocks` counters, any
   `*.dirty` markers, and every `runs/*.json` (newest first). If the repo
   still has `.claude/fluxpoint-loop/` or `.claude/fluxpoint-graph/`, note
   that `/fluxpoint:migrate` has not been run.
2. Read the work file — `WORK.md`, or `LOOP.md` in a repo that has not
   migrated: the STATUS and MODE lines, checked vs unchecked counts for
   Definition of Done and Plan, and the last few Evidence rows.
3. In `graph` or `both` mode, add:
   - `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" compile-graph.py WORK.md --check`
     — valid or invalid, node count, planned agent calls, budget ceiling.
   - the newest recorded run: runId, outcome, nodes OK/dead, findings,
     harness exit, red-team verdict, and any node whose status is `DEAD`
     with its detail.
3a. If `.fluxpoint-gates.json` exists, add
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" attest.py --list`: declared
   gates, how many attested executions each has, and the newest exit. Call
   out any recorded run whose `attestation.tally` carries `mismatch` — a
   node claimed a gate exit the hook-minted log contradicts, which makes
   that run's verdict untrustworthy regardless of what it reported — or
   `stale`, a node citing an execution from before its run launched, on
   another commit, or already backing another node.
3b. If `.claude/fluxpoint/memory.jsonl` exists, add
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" recurrence-guard.py --scan`:
   what this repo keeps re-learning, and whether each recurrence has a
   command holding it down. Report anything past the threshold as work,
   not as knowledge — the answer to a lesson filed twice is a gate in
   `.fluxpoint-recurrence.json`, and filing it a third time is the
   failure rather than the remedy. Say so plainly if the recommended next
   action is writing that check.
3c. If `runs/` holds more than one artifact, add
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" metrics.py` — the cross-run
   trend block: death and skip rates, agent calls spawned vs planned,
   sweep endings (dry rule / ceiling / budget-truncated / halted — only
   the dry rule is convergence), reduce compression, panel kill rate,
   inbox pressure. These are the numbers to tune a campaign against;
   quote them rather than characterizing them. A kill rate of 0% means
   the panels may be decoration; near 100% means the finders are badly
   scoped — say which reading applies.
4. Summarize in a few lines: harness verdict and its age, gate pressure
   (blocks used out of the max, default 3), plan progress, campaign state,
   and the single most useful next action — fix what is red, work the next
   slice, compile-fix the IR, run the campaign, resume from a runId, or
   ship per the Merge policy. If anything is red, quote the exact failing
   lines rather than paraphrasing them.
5. Never infer that a campaign ran from the presence of a compiled script;
   only a recorded run in `runs/` counts.
