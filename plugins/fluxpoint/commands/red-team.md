---
description: Adversarial review of a working change or PR with attack coverage, executable evidence and skeptical reproduction.
argument-hint: [PR URL, base ref, or optional attack focus]
---

Review the change named by the user with `agents/red-team-reviewer.md`.
For a broad change, run `workflows/red-team.js`: independent surface attackers,
duplicate reduction, skeptical refuters and a final identity recheck. Invoking
this command authorizes that review Workflow, as `/fluxpoint:graph-run` does
for a graph run. Codex uses the same stages through subagents. `$ARGUMENTS`
supplies the target or attack focus.

1. Resolve the review target before delegating. For a PR, read its actual
   base/head commits and inspect their merge-base diff, even when the checkout
   is clean. For a user-supplied base ref, resolve it to a commit and compare
   against the current head. Otherwise review `git diff HEAD` plus relevant
   untracked files. Record repository, base/head, changed paths and a content
   fingerprint for uncommitted or untracked inputs. An empty working diff is
   not evidence that a committed PR is safe. Confirm the scratch snapshot
   contains the reviewed changes; never substitute origin/main.
2. Give the reviewer the target, requirement packet/invariants, deployment
   configuration and trust boundaries. Have it enumerate applicable attack
   surfaces and use the shared agent procedure. A focus narrows attack effort;
   excluded surfaces stay visible. Keep experiments in separate scratch copies
   or verified worktrees containing the target state.
3. For a broad change, use the code-review Workflow. A range spanning several
   components or repositories, or too large for one complete attack matrix,
   requires this path. Resolve each base and head to its full Git object id
   and count the actual reviewed diff's files, additions and deletions. For a
   PR, use its merge base as `base`. For working changes, make an isolated
   scratch snapshot containing the relevant tracked and untracked inputs;
   review that commit and preserve its content identity without committing
   to the user's branch. Never turn a dirty working tree into a clean HEAD
   review that omits the change.

   Invoke the Workflow tool with a real JSON args object:

   ```
   {scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/red-team.js",
    args: {pluginRoot: "${CLAUDE_PLUGIN_ROOT}",
           subject: [{repository: "<review checkout>",
                      base: "<resolved base object id>", head: "<resolved head object id>",
                      files: 345, additions: 181038, deletions: 13590}],
           settled: ["<previously fixed or refuted title>"]}}
   ```

   The default lenses are `authority`, `value`, `state`, `input`, `operations`,
   and `composition`, covering the reviewer's five surfaces and a distinct
   composition pass. Each reads `agents/red-team-reviewer.md`, checks the
   same pinned ranges and reports completion, evidence, blockers and whether
   it sampled. Component lenses can replace the defaults with
   `lenses: [{id: "payments", focus: "<component and invariants>"},
   "composition"]`; retain at least one independent surface lens and the
   separate composition lens. The optional `sampled` argument explicitly
   declares caller-selected sampling. Sampling stays visible in the result
   and run record; it never fills in missing or unresolved work.

   A reduce merges claims by failure path, retains every source and the
   highest claimed severity, and cannot drop a claim or merge away two
   different claims from one attacker. Each distinct MEDIUM+ claim receives
   independent `reproduce`, `reachability` and `scope` refuters. LOW receives
   one combined refuter. Refutation needs contradictory evidence from every
   assigned refuter; uncertain, missing or malformed votes leave the claim
   unresolved. A majority reproducing a claim confirms it. Settled titles
   supply context and never count as evidence against a current exploit.

   A no-claims round still runs an independent coverage skeptic. It rebuilds
   representative valid controls and defended attacks, checks the reached
   boundary and looks for false negatives. Missing or incomplete skeptical
   execution blocks SHIP; any exploit it discovers goes through the same
   independent refutation stages.

   After a clean round, a fresh agent rechecks Git objects, current state,
   diff counts and the experiment identities. Drift or a missing recheck
   blocks SHIP. A no-claims default round uses six attackers, an independent
   coverage skeptic and this final checker. A finding-bearing round adds one reducer, three
   refuters per MEDIUM+ claim, and one per LOW. Use available concurrency;
   serial execution has the same coverage obligations. Scheduled agents
   and token consumption are not evidence of completed verification.

   Without Workflow, follow these stages with independent subagents and the
   prompts in `workflows/red-team.js`. Name the executor and preserve the
   same coverage and vote records. If delegation is unavailable, report the
   limitation: a broad gate cannot claim independent multi-lens coverage or
   return SHIP from one inline reader. A narrow review may use one reviewer
   plus an independent skeptic and must report its narrower coverage.
4. Give every claimed exploit, including composition claims, to a skeptic
   for fresh reproduction against the same snapshot. The skeptic checks the
   valid control, attack preconditions and impact. An unavailable skeptic
   leaves the claim unresolved; it cannot erase the claim. If no exploit is
   claimed, inspect coverage evidence and sample defended cases for failures
   that occurred before reaching the intended boundary.
5. Synthesize using the agent's verdict rule. The Workflow returns RedTeamV1
   directly, including top-level `coverage` with `subject` (the repository
   ranges and file/line counts), `attackers`, `lenses`, and `sampled`. Its
   attacker count and lens names include completed primary attackers only;
   planned coverage and failures remain in `review`. A round with zero
   completed attackers omits `coverage` and returns BLOCK. Its
   `review` contains stage counts, every attacker report, confirmed and
   unresolved claims, refutation evidence, blockers and the identity recheck.
   Preserve incomplete coverage and
   unresolved claims as blockers even when findings is empty. Recheck the
   reviewed identity before SHIP. After a fix, reproduce the original attack
   against it, rerun affected coverage and composition, and bind a new verdict.
   A repair PASS also proves the invariant's sibling fields, accepted encodings,
   callers, networks, roles and paths, and attacks the repair's own changed
   behavior. Use the `WORK.repair.md` loop: its compiler reads the actual patch,
   requires every added-hunk coverage row, and spawns separate diff attackers
   and refuters on fresh base archives. No original-attack-only PASS discharges
   a checked commit. This additional small-diff gate preserves the full-range
   review before an irreversible step.
6. Save the result with the campaign's run evidence. In a graph, keep it as a
   RedTeamV1 node result and run `scripts/record-run.py` as usual. The durable
   run artifact records each gate's coverage next to `redTeam`, and its
   Evidence row names range/file counts, attackers, lenses and sampling.
   A SHIP without valid coverage, or with unresolved review blockers, is
   filed `BLOCKED-REDTEAM` even if schema validation was skipped; a CLI
   `--red-team SHIP` flag cannot replace a coverage-bearing review result. The compiled
   multi-lens path binds refutation to the original attacker claims; a reducer
   or collection agent cannot omit one. It records source/stage identities and
   the actual independent votes. `record-run.py` accepts a terminal SHIP over
   an intermediate accusation only when every source returned matching pinned
   coverage and every claim has a complete unanimous refutation receipt.
   Missing or divided votes, source omissions, changed heads and unknown
   severities remain blocking.

Return scope, coverage, findings, blockers and the final SHIP/BLOCK verdict.
Treat BLOCK as harness-red: fix confirmed issues and complete missing checks
within the user's authorized scope. Review does not authorize deployment,
fund transfers or destructive actions.
