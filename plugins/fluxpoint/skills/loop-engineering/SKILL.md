---
name: loop-engineering
description: How to run Loop Engineering at Flux Point — choosing between /goal, native /loop, /schedule Routines, and the outer scripts/loop.sh runner, writing verifiable completion conditions, and working with the Stop-hook DoD gate. Use this whenever the user mentions loops, Ralph, /goal, /loop, /schedule, routines, watching or monitoring a build, deploy, or transaction, autonomous or overnight runs, "keep going until green", iteration budgets, or asks the agent to work unattended on a task, even if they never say "loop engineering".
---

# Loop Engineering

Before implementing a new goal, run `/fluxpoint:grill-me` (Codex:
`$fluxpoint-grill-me`). Draft the decisions and checkable requirements, then
lock `.fluxpoint-spec.json`. Model defaults let authorized routine work
proceed while the user retains an override. Put `SPEC: .fluxpoint-spec.json`
in WORK.md and wire `specification.py --run` through the scaffolded harness.
Existing copied harnesses need that integration explicitly; installing a
plugin update cannot edit them. Each slice implements named requirement ids.

The loop is the artifact. Every loop has three parts: a state file
(`WORK.md`, `MODE: loop`), a deterministic harness (`scripts/harness.sh`),
and a driver. The agent never decides "done"; the harness does.

`WORK.md` is shared with graph mode: one Definition of Done, one Merge
policy, one Evidence table, whether the work is decomposed as loop slices
under Plan or as a campaign under Campaign. Repos still carrying a pre-1.0
`LOOP.md` keep working — the hooks honor it — until `/fluxpoint:migrate`
folds it in. When the work stops being one zone with one contract and
serial evidence, escalate per the graph-engineering skill instead of
growing the loop.

## Choosing the driver

- **`/goal <condition>`** — the default for interactive work with a
  verifiable end state. The evaluator model only reads the transcript and
  runs nothing itself, so the condition must name its proof command and the
  proof output must be surfaced in conversation: "scripts/harness.sh --full
  exits 0 and the run is shown" works; "the code is production ready" does
  not. Always append a bound: "or stop after N turns and summarize gaps".
- **`/loop`** (native, v2.1.72+) — in-session grinding and polling. With
  an interval (`/loop 5m <prompt>`) it re-fires on a timer; with no
  interval it self-paces, choosing each delay itself and ending the loop
  once the stop condition in the prompt provably holds. Loop-work form:
  `/loop work the next slice per WORK_PROMPT.md; stop only when
  scripts/harness.sh --full exits 0 and WORK.md reads STATUS: DONE`.
  It is a scheduler, not a Stop hook, so the DoD gate stays the sole stop
  authority: every iteration must end green before the next fires.
  Session-scoped; Esc cancels a pending iteration; loops expire after
  seven days. Prefer it over the older ralph-loop plugin, whose second
  Stop hook contends with the gate. For pure watching (CI, a preview-net
  tx, the outer loop's logs), ask for a dynamic watch and Claude may run
  the Monitor tool — a background script whose output streams into the
  session — which beats interval polling on tokens and latency. There is
  no /monitor slash command; Monitor is a tool Claude reaches for.
- **`/schedule` (Routines)** — cloud-hosted standing guardrails that run
  with the laptop closed. Triggers: a schedule (cron floor one hour, daily
  run caps by plan), a per-routine HTTPS endpoint, or GitHub events,
  combinable on one routine. Each run clones the repo fresh and pushes
  only to claude/-prefixed branches; create conversationally with
  /schedule in-session (API and GitHub triggers are added at
  claude.ai/code/routines), manage with /schedule list|update|run. Right
  jobs: nightly harness --full drift checks, PR-triggered red-team passes,
  docs drift. Routines run autonomously under the account's identity, so
  prompts must be self-contained and fail loudly — open an issue on red,
  never end silent. No key material, no mainnet paths, no governed repos.
- **`scripts/loop.sh`** — the outer Ralph for multi-hour unattended runs:
  fresh context per iteration, state in WORK.md and git, promotion gated by
  the harness. Unattended means a sandboxed container with allow-listed
  egress, zero reachable key material, and preview networks only. Choose
  it over Routines when the campaign needs the exact local toolchain
  (aiken, dafny, preview-net egress) or the repo cannot leave self-hosted
  infrastructure.

## Writing conditions that hold up

Four parts, always: one measurable end state, a stated proof command, the
constraints that must not change, and a turn or time bound. Example:
"scripts/harness.sh --full exits 0, no test file deleted or skipped, diff
touches at most 15 files, or stop after 30 turns and report gaps."

## Working with the DoD gate

The Stop hook blocks stops while `scripts/harness.sh --full` or the hygiene
scan is red, up to FPL_MAX_BLOCKS (default 3) consecutive times, then
yields with a checkpoint notice. The correct responses, in order: fix what
is red; never weaken the harness, delete tests, or edit WORK.md's
Definition of Done to reach green; on the third block, write a checkpoint —
what failed, what was attempted, two alternative paths — and hand control
back to the user.

## The ship pipeline

Green is not done. A slice ends in exactly one of two states: merged and
cleaned (squash, branch deleted, default branch synced) or explicitly
parked per the repo's Merge policy in WORK.md — never at "PR opened,
awaiting someone". Before any merge, red-team the diff; VERDICT: BLOCK is
harness-red. Prefer the deterministic form: run scripts/harness.sh --full
as a required CI check alongside the red-team verdict, then
`gh pr merge --auto --squash --delete-branch` — GitHub executes "merge if
green" against the named checks, so the merge decision never rests on the
agent's self-report of green. Repos where merge triggers a deploy get
their policy decided once, per repo, in WORK.md, not renegotiated per PR.

## Verified work

A prover is the best thing that can sit behind this harness: `aiken check`,
`dafny verify`, `lake build`, `coqc` all give a crisp exit code, which beats
"the code looks production-ready" by a mile. But exit 0 is weaker than it
looks — every prover ships a way to discharge an obligation without proving
it, and an agent told to "make it pass" will find it.

Five rules, in order of how often they are broken:

1. **The prover runs in `--full`, not only in `--changed`.** Per-file
   checking on edit is feedback; the gate that decides done must invoke the
   prover over the whole project. A harness that verifies only what was
   touched will certify a repo it never checked.
2. **Escape hatches ratchet.** `scripts/proof-guard.py --baseline` records
   `todo`, `expect`, `assume`, `{:axiom}`, `sorry`, `Admitted`,
   `verifier::external_body`, `--skip-tests` and friends into a committed
   file; `--check` fails when any category rises. Proving something you
   previously assumed lowers the count and is always allowed. Raising one
   is a diff a human has to justify.
   **Off-chain counts too.** A type checker is a prover with a weak logic,
   so `as any`, `as unknown as T`, the `!` assertion, `@ts-ignore` and a
   tsconfig with `"strict": false` ratchet as `ts.*` categories. Each
   discharges an obligation `tsc` had, and `tsc` exits 0 on all of them.
   That surface is where an autonomous caller actually reaches a protocol,
   which is why it is gated rather than trusted. `@ts-expect-error` is
   deliberately uncounted: it fails the build once the error it names is
   fixed, so it cannot rot in place.
   **And the statements ratchet too.** An agent blocked from adding an
   `assume` has an easier move: weaken the theorem. `scripts/spec-guard.py`
   hashes what is being proved — Dafny `requires`/`ensures`/`invariant`
   clauses per declaration, Aiken test and property signatures with their
   fuzzers and their `fail` polarity, Lean and Coq theorem statements,
   Isabelle lemma statements, TLA+ theorems and the invariants a TLC
   `.cfg` names, Kani harnesses and contracts — into the same baseline
   file, and `--check` fails when a recorded obligation changed or
   vanished. Adding obligations is free; changing one needs a Decisions
   row naming its obligation id, or a re-recorded baseline. Neither
   ratchet makes weakening impossible; both make it a reviewed diff
   instead of an invisible one.
   The same scan backs three more claims. `--baseline --axioms --headline
   <id>` records what the prover itself says a headline theorem depends on
   (`#print axioms`, `Print Assumptions`, `dafny audit`), and `--check` is
   red when a new axiom enters that set even though every hatch count held
   flat — an assumption laundered through a helper lemma is still an
   assumption. A checked `- [x]` line in the Definition of Done whose
   `— proof:` tail names an obligation id is red unless that obligation
   exists and is unchanged. And an Aiken repo carrying
   `.fluxpoint-attacks.json` is red for every attack class in it that has
   neither a property test of that name nor a waiver with a reason, so
   never specifying the property is a gate too.
   Two weakenings add no hatch at all, so both are counted structurally:
   `test t() { True }` is flagged as a test that cannot fail, and `fn
   check(..) -> Bool { True }` as a predicate that decides nothing — which
   is what a `todo` usually becomes when someone is told to make the count
   go down. The ratchet is still a floor, not a ceiling — a theorem that
   lost a conjunct, a property about an unreachable state, a generator that
   cannot produce the interesting case, or a `fail` test that trips an
   earlier guard than the one it is named for all leave the counts
   untouched, which is why rule 3 exists.
3. **A counterexample is evidence — keep it.** The shrunk failing input a
   property test produces is the most reusable thing a prover makes, and it
   lives in a log the next command overwrites. In an Aiken repo the
   scaffolded harness captures `aiken check`'s JSON (there is no `--json`
   flag: it emits JSON whenever stdout is not a TTY) and `scripts/cex.py`
   records each failure to the committed `.fluxpoint-cex.jsonl`. A Dafny
   repo's `dafny verify` model, a Rust crate's `cargo kani` concrete
   playback and an Apalache ITF trace are captured the same way, with
   `FPL_DAFNY_ARGS`, `FPL_KANI_ARGS` and `FPL_APALACHE_ARGS` carrying what
   each prover needs to print its input. Off-chain counts too: a
   fast-check failure under vitest carries a shrunk counterexample with
   the `seed` and `path` that replay it, and the scaffolded harness
   captures the `test` script the same way. A plain assertion failure in
   the same run is deliberately not recorded, because it carries no
   generated input and minting one would put a value in the ledger that
   no generator produced. Pin one
   with `cex.py --pin <cexId> --file <path> --test-name <name>` once you
   have written a real regression test; the pin is refused unless the
   recorded value is physically in that test's body, on token boundaries,
   outside comments and strings, in a test that is neither `fail`-annotated
   nor hollow. `--check` then fails if a pinned counterexample loses its
   test. Releasing one costs a `--reason` and a Decisions row naming the
   cexId — the same price spec-guard charges to forgive an obligation.
   What this does not yet claim: the pinned test is recorded as *carrying*
   the counterexample, not re-run against the un-fixed code to prove it
   would have caught it.
4. **Can the tests fail?** Every gate above asks whether the suite passes.
   Mutation score asks whether it can fail: break the implementation on
   purpose and count how many broken versions the suite notices. It is the
   one measure that cannot be faked by executing code, and it matters most
   in loop mode, where the same agent writes the code and the thing that
   grades it. Declare the tool in `.fluxpoint-mutation.json`;
   `mutation-guard.py --measure` runs it and is where the ratchet lives —
   red when the score falls *or* the survivor count rises, because a ratio
   can hold flat while coverage shrinks. `--check` is the cheap half wired
   into `--full`: it re-runs nothing and only asks whether a measurement
   exists and still describes this tree. A mutation run costs minutes to
   hours, so `--measure` belongs off-session on a Routine; staleness is
   named and raised in the inbox but does not fail the build unless the
   repo asks, because a gate that reds over an un-run expensive job is one
   people switch off.
5. **Green is not stronger.** `/fluxpoint:proof-audit` runs the ratchet and
   then the `proof-auditor` agent, which looks for what a count cannot see:
   a theorem whose statement got weaker, a property proved about an
   unreachable state, an Aiken `test` that cannot fail, a validator with no
   test at all, a solver `unknown` read as success. `VERDICT: WEAKENED` is
   harness-red.

For Cardano specifically, correctness is necessary and not sufficient:
script size and execution-unit budgets, min-ADA, and datum size decide
whether a proved-correct validator can actually be submitted. Script size
is already gated — the scaffolded harness runs `scripts/plutus-budget.py`
after `aiken build`, which fails on the protocol `maxTxSize` whatever you
have configured, and on a tighter headroom target if you set
`maxScriptBytes` in `.fluxpoint-budget.json`. Point it at real
`cardano-cli query protocol-parameters` output via `FPL_PROTOCOL_PARAMS`
when you target anything but mainnet. Execution units are not derivable
from a compiled script, so record measured values from your own
transaction-building tests under `exUnits` in that file; until you do, the
gate reports them unmeasured rather than met.

## Writing down what you decided

Compaction is the largest memory-loss event a session has, and a process
exit is the second. Both destroy the same thing: the reasoning. The code
survives, the tests survive, and *why this and not that* does not — so the
next context re-decides it, often the other way.

Record a real choice with `scripts/decision.py --record`, which validates
against `DecisionV1` before writing: at least two options, each with the
best case against it (including against the one that won — an option nobody
argued against was not examined), and a rationale long enough that a lazy
sentence cannot satisfy it. The whole record — question, every option
with its objection, evidence — is kept in `.claude/fluxpoint/decisions.jsonl`,
and a row indexing it lands in the Decisions table, which SessionStart
injects into every future context. The row's cells are cut to fit; a cut
row points at `decision.py --show <id>`, which prints the record whole, and
a later campaign's `imports` bind the record itself, not the row. Give an
operator's ruling an `--id` and paste their words into `evidence` verbatim:
the condition that decides a ruling is usually the part a cell cuts. If a
slice genuinely decided nothing, say so:
`decision.py --none "<why>" --session <id>` makes silence a statement
rather than an absence.

A decision has no verdict to witness — unlike an Evidence row, it is
inherently an assertion, and no gate could certify it. What the floors buy
is not proof but survival in a form the next context can act on.

The PreCompact hook records, at the moment of compaction, whether anything
had been written down at all; if code changed and nothing had,
SessionStart tells the next context the reasoning is gone and to re-derive
rather than assume. Set `FPL_DISTILL=1` to make the Stop gate ask for a
decision (or an explicit `--none`) before a green stop — off by default,
because a check that starts by blocking stops is one people switch off.

## Evidence discipline

Every completion claim gets a row in WORK.md's single Evidence table
(`| When (UTC) | Source | Outcome | Claim | Proof |`; `Source: loop` for
slices, a runId for graph campaigns): the command
and its exit status, the tx hash on preview or preprod, the log excerpt. A
claim without evidence is treated as false.
