---
name: graph-engineering
description: How to run Graph Engineering at Flux Point — deciding when a loop must become a graph, specifying multi-agent campaigns as a declarative IR in WORK.md with typed contracts and verification tiers, compiling it deterministically to a Claude Code Workflow script, and repairing runs with targeted resume. Use whenever the user mentions graphs, graph engineering, multi-agent work, orchestration, councils, judge panels, fan-out, swarms, wiring or organizing subagents, WORK.md, workflow scripts, or asks to parallelize verified work across agents, even if they never say "graph engineering".
---

# Graph Engineering

Loops made one agent's behavior programmable; graphs make the organization
of agents programmable. The graph is the artifact: a declarative IR
(`WORK.md`), a compiler that turns it into a Workflow script with no model
in the loop, and the same deterministic harness deciding green. A node is
not a prompt — it is a loop-engineered unit with a scoped context packet in
and a typed contract out. The rule that survives every upgrade: the agent
never decides "done"; the harness does.

## Loop or graph

Before a new implementation goal, use `/fluxpoint:grill-me` (Codex:
`$fluxpoint-grill-me`) to research and fill the decision tree, challenge its
assumptions and lock the requirement packet. Routine decisions have explicit
model defaults the user can override. Mutating graph compilation requires
`.fluxpoint-spec.json` and its current lock; every requirement names checks,
and the compiler carries the packet into node context. Schema validation is
structural; the declared tests, model checks or proofs and runtime exercises
establish the behavior. Read-only exploration can precede the locked packet.

Stay in loop mode when the work is one zone, one contract, serial
evidence. Escalate to a graph when two or more hold: independent subtasks
that fail independently; a work-list to fan out over; claims that need
adversarial verification from independent context; cross-zone ownership
(validators vs off-chain vs infra); a budget worth isolating per node. A
work graph of more than ten nodes is scope creep — split the campaign;
the compiler warns past ten. Never build a graph as ceremony around a
single slice.

Two stay-in-loop cases that look graph-shaped and are not. Work whose
*decomposition* is still unknown is loop work: a discovery `repeat`
covers unknown size (known angles, unknown count), never unknown
structure — you cannot fan out over subtasks you cannot yet name, so
explore in loop mode first and graph what the exploration finds. And
work a human steers step-by-step is loop work: the graph's human
machinery (`actor: human`, `confirm`) models permission boundaries
inside an autonomous run, not a person redirecting each move — a graph
whose every edge waits on a person is a loop wearing ceremony.

## The six primitives and their bindings

| Primitive | Meaning | Binding |
|---|---|---|
| Node | one responsibility, its own context | an IR `nodes[]` entry → one `agent()` call; stable roles via `roles` → `agentType` |
| Edge | explicit routing | IR `after` / `foreach`; the compiler derives `pipeline()`/`parallel()` — never a model-improvised hand-off |
| Contract | what a node must produce | `contract: "NameV1"` → `contracts/NameV1.schema.json`, validated structured output |
| Context packet | what a node may believe | the IR `prompt`, with `{{A.x}}` inputs, `{{item.*}}` fan-out, `{{prev}}` predecessor contract or `{{prev.<field>}}` one projected field — never the transcript |
| Reducer | deterministic code between agents | a `reduce` node: `{from, over, dedupeBy, sortBy, order, topK}` → emitted JS, zero spawns. Models for ambiguity, code for plumbing |
| Verification | who decides an edge is green | the `verify` tier, plus `independent: true` nodes that re-derive gates |

**Compress before you reason.** A synthesis node handed every raw fan-out
item pays a reasoning model to do a Set's job. Put a `reduce` node between
fan-out and judgment (dedupe → rank → cut, in that order, each cut named
in the log — no silent caps), or project just the field a consumer needs
with `{{prev.<field>}}` instead of pasting the whole contract. Both are
validated at compile time: a projected field must exist in the
predecessor's contract, and projecting off a node that yields an item
array (a verified sweep, a fan-out, another reduce) is rejected — consume
`{{prev}}` whole or reduce it first.

Durable coordination rides on the executor: every run has a `runId`,
`journal.jsonl` records each node's actual return, resume replays the
longest unchanged prefix, and `record-run.py` writes the provenance
artifact and the Evidence row.

## The IR is the source of truth

One ```json graph-ir fenced block in WORK.md. `graph-run` compiles it
mechanically, so the spec cannot drift from the executor. **Never
hand-edit a compiled `.graph.js`** — edit the IR and recompile. The
compiler rejects, at compile time:

- a node with no contract, or an unknown contract name
- `verifyOver` that is not a field of the node's contract
- an even panel (majority undefined) or a panel with no `verifyOver`
- `verify: harness` — removed; it compiled to nothing while the spec
  claimed the node was checked. Gate on the harness the honest way:
  `mutates: true` on the producer plus an `independent` node that runs it
- an unknown field at any level (a misspelled `verifyOver` used to disable
  verification silently), or `after` whose prompt never uses `{{prev}}` —
  declaration order already sequences nodes, so a consumed-nothing edge is
  a phantom
- `mutates: true` with no `independent: true` node that `verifies` it
- a node verifying itself, or a verifier not marked independent
- `after`/`foreach`/`role` pointing at things that do not exist
- `{{prev}}` with no `after` edge, or `{{seen}}` with no `repeat` block
  (hidden coupling)
- `{{prev.<field>}}` naming a field the predecessor's contract lacks,
  reaching deeper than one hop, or projecting off a node that yields an
  item array — it used to compile clean and die at launch on a
  ReferenceError, which is the exact failure the scope check exists for
- `onRed` outside `halt | drop+log` — a misspelling used to fall back to
  a default silently, weakening the declared failure policy in the
  permissive direction — and `onRed` on a parked node, where nothing can
  die. On fan-out and discovery nodes the field is now real, too: `halt`
  ends the campaign on a dead worker, and a round that lost ANY worker
  never counts toward the dry rule — a finder that keeps crashing must
  not end a sweep looking converged. A spawn the node ceiling declined
  is a different sentence from a death: it files SKIPPED, and under
  `halt` the run ends `BUDGET-EXHAUSTED`, never `NODE-DEAD`
- a `reduce` node with no operation, an `over` that is not a field of its
  source's contract, `over` on a source that already yields items, a
  dedupe/sort key outside the item schema — or over items that declare no
  fields at all, where every key would read `String(undefined)` and
  dedupe would collapse distinct items to one — `order` without `sortBy`,
  `topK` without a ranking, or combined with any agent-node field
- a `repeat` block missing its dry rule, round ceiling, or dedup key, or
  whose dry rule can never fire; a dedup key that is not a field of the
  contract's items
- planned agent calls exceeding `budget.maxNodes`, or no ceiling at all —
  and rounds are priced in, so a four-round discovery loop is costed at
  four rounds, not one
- `irreversible: true` without `confirm` in `requiredArgs`, without an
  earlier `independent` node carrying a `haltWhen`, or combined with
  `foreach`/`repeat`

Those are structural. `/fluxpoint:graph-audit` judges what is left:
scoping, tier-vs-stakes, prompt quality. It runs as a shipped workflow
(`workflows/graph-audit.js`): six lens-scoped auditors, a reduce that merges
their duplicates, and verifiers told to refute each finding, three per
finding at HIGH and above. One auditor per round read as convergence when
it was one reader running out of path, so SOUND now means every lens
returned and nothing survived refutation. Rounds persist under
`.claude/fluxpoint/audits/`, and what earlier rounds fixed or refuted is
passed back as a settled list so a round does not relitigate it. `--quick`
keeps the single auditor for checks between repairs; it never makes a
graph READY.

## Work nobody on the graph can do

The engine used to have two answers for a node it could not complete: halt
the whole campaign, or drop the item and continue with a `null`. Neither is
*"this one is blocked, work the other branches"* — and real deliveries
guarantee that third case. 2-of-3 hardware signing. A withdrawal only an
external counterparty can perform. A 72-hour governance timelock. An
operator wallet with nothing spendable until someone tops it up.

**Park last, not first.** Most work that feels human-only is not: a CLI, an
API, a headless browser, a read-only query, or a generated file the person
only has to sign. Genuine blockers are narrow — key material an agent must
not hold, legal authority, physical possession, another party's own action.
So `release.whyNotAgent` is required and has to name what was ruled out;
`graph-auditor` treats a reason that does not survive contact with the
repo's own tooling as HIGH. Every unnecessary park is a person waiting on
work that could have been finished.

**A block never arrives empty.** When a node does park, the graph spawns one
advisor first, contracted to `DecisionV1` — so a bare "ask the operator"
cannot satisfy it. The advisor is told to attack `whyNotAgent` before
accepting it, and if the step turns out to be automatable its recommendation
*is* that concrete path and the tooling it needs. Otherwise it returns the
best available course of action with the alternatives it rejected and the
strongest objection to each, including to the one it recommends. That lands
in the provenance, the inbox row, and `/fluxpoint:status`, so what reaches a
person is a recommendation with reasoning attached, not a hand-off. The
advisor costs one agent call per parked node and is priced into
`budget.maxNodes`; if the floor declines it, the block says so explicitly
rather than quietly arriving bare.

Mark those `actor: human` or `actor: third-party` with a `release` block:

```json
{ "id": "sign", "actor": "human", "contract": "HarnessCheckV1",
  "prompt": "The campaign has built an unsigned transaction body and needs 2-of-3 hardware signatures before it can submit. Attack the claim that this needs a person before accepting it, then recommend how the operator should proceed.",
  "release": { "instructions": "Sign with 2 of the 3 hardware keys and paste the cardano-cli output.",
               "whyNotAgent": "the keys live on hardware devices held by three people; an agent may never hold them. The unsigned body IS built headlessly by the previous node.",
               "proofContract": "HarnessCheckV1" },
  "wake": { "check": "cardano-cli query tip --mainnet", "everyMinutes": 30 } }
```

The compiler emits **no spawn** for that node. It reads a release file; if
there is none it reports `BLOCKED` with those instructions, sets the run
`INCOMPLETE`, and keeps going. `instructions` is the entire message the
blocked person gets, so write it for someone with no context — the
compiler rejects an empty one, and rejects a `proofContract` that differs
from the node's contract, because the node yields exactly what was pasted.

Four consequences worth knowing:

- **Blocked is inherited.** A node whose `after` is blocked is blocked too,
  not handed the `null` that reads like a failure. A dependent chain parks
  as a unit; unrelated branches finish.
- **A parked run can never read COMPLETE.** It is `INCOMPLETE`, and the
  Evidence row names the blocked nodes.
- **Releases are proof, not assent.** `/fluxpoint:release` validates what
  the operator pastes against `proofContract` and refuses an adjective. The
  campaign resumes on the strength of that file; one that resumes on
  recollection will eventually resume on a mistake. Never write a release
  on someone's behalf — a fabricated release is strictly worse than a
  stalled campaign, because the stall is visible.
- **Nothing is waiting silently.** Every block, expired wake, and refused
  confirmation lands in `.claude/fluxpoint/inbox.jsonl`;
  `/fluxpoint:status` leads with it and SessionStart injects the count.

`wake` parks a predicate rather than a person: `scripts/wake-check.sh`,
driven by a Routine or by `loop.sh`, runs the due checks and reports which
campaigns can resume. It deliberately does not resume them itself —
re-invoking a graph spends budget and may sit upstream of an irreversible
node, so a human or an explicitly configured Routine makes that call.

In loop mode the same idea is a Plan marker: `- [~] <item> — blockedOn:
<who>`, which the loop skips. Without it, step 1's "pick the first
unchecked item" re-picks a human-blocked slice every iteration and a
72-hour wait spends the entire iteration budget in minutes.

**This is not a scheduler, on purpose.** There is no ready-set, no
topological sort, no `needs`/`priority`. Declaration order plus
park-and-skip-dependents covers a largely serial critical path with
independent slices hanging off it, which is what campaigns actually look
like. A DAG scheduler is the right answer to a problem no graph here has
had yet; build it when one does, not before.

## What a worktree is, and what it is not

`mutates: true` compiles to `isolation: 'worktree'`. Three things about that
are worth knowing before you depend on it, because each has cost a real
campaign a wrong answer rather than an error:

- **You do not choose the base — so every isolated node is told it and
  asserts it.** The compiler emits the literal string and the runtime cuts
  the worktree; nothing in this plugin selects the commit. A node given
  isolation has been observed reading a tree cut from the default branch
  rather than the campaign's, which makes a file an earlier node committed
  simply ABSENT — and nothing inside says so: `git status` is clean and a
  missing file looks like a missing file, so the node returns a confident,
  well-evidenced, wrong report. The graph therefore refuses to start
  without `args._base` (graph-run loads `git rev-parse HEAD` at launch),
  and the compiler injects a preamble ahead of every worktree-isolated
  node's own prompt: run `git rev-parse HEAD` first, check the campaign
  base is an ancestor, and on a mismatch make the mismatch the result
  rather than reporting absent files as findings. A downstream node that
  must read what an upstream node committed should still fetch or check
  out that branch by name rather than assuming it is there.

- **A fan-out that measures declares `isolation: 'measure'`.**
  `parallel()` and `pipeline()` read as isolated units and, for pure
  reasoning, effectively are. The moment a node's output is a *measurement*
  of the tree — a build size, a byte delta, a benchmark — a shared working
  tree makes that number a function of what every sibling is doing, and the
  failure is silent: every node exits 0 with a confident figure. `measure`
  is not the runtime's worktree: the node's injected preamble has it cut a
  frozen `git archive HEAD` snapshot into a temp dir and work only there —
  cheaper than a worktree, immune to the sibling problem, and based on the
  campaign's own HEAD by construction. The compiler warns on a fan-out
  whose prompt looks like it builds or measures with no isolation declared.

- **The shared tree is guarded, not trusted.** "Throw the branch away" in a
  prompt is prose to a model, not a cleanup contract; nothing evaluates it,
  and a gate that already passed was judging a tree that no longer exists.
  Every compiled graph brackets the run with a tree sentinel — a tiny
  schema-forced agent recording `git rev-parse HEAD` and
  `git status --porcelain` at campaign start, before every
  verdict-minting node (`independent` verifiers and `prove:` gates), and at
  campaign end. Mutators run in worktrees and measurers in snapshots, so
  the shared tree must be IDENTICAL at every checkpoint: any drift halts
  the campaign as `TREE-MOVED` with the dirt named, instead of advancing
  verdicts about a tree that is gone. The plugin's own writes are left out
  of the comparison: the state file (`WORK.md`, `LOOP.md`, the graph file
  itself), where the Stop hook appends its Evidence row whenever the
  orchestrator ends a turn, and everything under `.claude/`. The sentinel
  is an `agent()` call, so a resume replays its first reading rather than
  taking one; the launcher's fresh reading in `args._base` (HEAD and
  porcelain, taken at every launch by `/fluxpoint:graph-run`) is compared
  against it, and a resume on a tree that moved since the run it replays
  halts `TREE-MOVED` at launch instead of handing back cached verdicts about
  the old tree. The record rides out in the summary
  as `tree`. `treeGuard: false` in the IR turns it off, on the record.

**Prove the harness in a worktree before you trust a `mutates` node.** Green
in the primary checkout is not green under the isolation the campaign
imposes: `git worktree add` checks out tracked files only, so a gitignored
build artifact or an installed `node_modules` is absent, and a test that
pins an absolute path is false there by construction. Run
`git worktree add --detach` and `bash scripts/harness.sh --full` inside it
once. Otherwise the campaign halts at its verification node blaming the
implementer, which is both wrong and pointed at an innocent node — and the
repair it invites is weakening the assertion that was right.

## Effects that cannot be undone

`mutates: true` buys `isolation: 'worktree'`. That is real containment for
a filesystem write and none whatsoever for a chain write, a published
release, or a destructive migration — the same marker on both reads as
protection it does not provide.

Mark those `irreversible: true`. Three things follow, and the second is
the one that matters:

1. The node refuses to fire unless a human named it in `confirm`. Naming
   the campaign is not naming the effect, so a blanket "yes" carries
   nothing along with it, and refusal is its own outcome
   (`CONFIRM-REQUIRED`), never a warning in a log.
2. **Resume stops double-firing.** Repair-one-node-and-resume is the
   recovery path this skill prescribes, and it is also the operation that
   mints twice: every node after the repair re-runs. Before each
   irreversible spawn the compiled graph checks a ledger keyed by campaign,
   node, and prompt hash; a hit restores the recorded result and logs
   `REPLAYED-FROM-LEDGER` instead of performing the effect. A replayed node
   is filed `REPLAYED`, never `OK` — a ceremony that did not happen must not
   read like one that did.

   **The ledger is local, not committed, and that bounds what it can
   promise.** It lives at `.claude/fluxpoint/irreversible.jsonl`, inside the
   directory `init` and `migrate` add to `.gitignore`, so a fresh clone
   starts with none: `--load` returns `{}` and exits 0, which the guard reads
   as "first run" and the effect fires again. Within one checkout the
   once-only property holds; across checkouts it does not, and no amount of
   care in the graph changes that. If an effect must be once-only for a
   ceremony that more than one machine can reach, the receipt has to outlive
   the working copy — a committed receipt log, or a lock the effect's own
   service holds. Do not read this ledger as that guarantee.
3. The gate must be *ordered before* the effect. The compiler requires an
   earlier `independent` node with a `haltWhen`, because a verifier that
   runs afterwards cannot un-mint an NFT.

   `haltWhen` compares one top-level field to a literal; it cannot reach
   into an array. A contract whose deciding fact lives only inside a list
   needs a top-level field bound to that list, or the gate reads the wrong
   thing. `RedTeamV1` carries one: `worstSeverity` and `worstSeverityRank`
   are bound to `findings[]` and to the verdict, and SHIP over a HIGH or
   CRITICAL finding is refused by the contract, re-derived by the compiled
   graph in code (a halt, before the next node), and re-derived again by
   `record-run.py` (`BLOCKED-REDTEAM`). So `verdict == 'BLOCK'` already
   stops every HIGH and CRITICAL; halt on `worstSeverityRank >= 2` to stop
   on a MEDIUM as well.

Two limits, stated rather than papered over. The sandbox running the
compiled graph has no filesystem, so the ledger row is written from the run
summary afterwards — a crash between the effect landing and the run ending
leaves no record, and the confirm gate is what stands in that window.
And editing a ceremony's prompt changes its key, which re-arms it; that is
deliberate (a different effect deserves a different record) and is the
second reason a human has to name the node every time.

## Choosing a verification tier

By stakes, never by habit. `schema-only` for cheap mechanical output whose
consumer re-reads the source anyway. `skeptic:1` for low-severity claims. `panel:3` for findings that
will cost someone real time. `panel:5` only for CRITICAL. Panels are odd
so majority is defined; refuters are prompted to *refute*, default to
refuted when uncertain, re-read the underlying code themselves, and run at
low effort — cheap skeptics beat expensive believers.

The expensive lesson: verification fan-out dominates cost. A three-finder
review with `panel:3` on every finding is ~30 agent calls. Tier down and
the same campaign costs a fraction with the same guarantees where they
matter.

## Never trust a self-report

A node that writes to the tree may not certify its own work. Mark it
`mutates: true` (which also worktree-isolates it) and give the gate to a
node with `independent: true`, `verifies: "<id>"`, and a `haltWhen` on the
real exit code. That node checks out the branch and re-runs the harness
itself. This is a compiler-enforced invariant because it shipped as a bug
once: the graph trusts exit codes it re-derived, not adjectives it was
told.

Independence of *context* is what the compiler can enforce; fidelity of
*execution* it cannot, because the verifier still types the exit code into
its contract by hand. So declare the commands that decide things in
`.fluxpoint-gates.json`, and a PostToolUse hook records the runtime's own
exit for every one of those runs. `record-run.py` then cross-checks each
claimed gate exit against that log and reports `ATTESTED`, `UNATTESTED`, or
`MISMATCH`. Three things to know: only the exact declared invocation is
attested (a pipe or a trailing `|| true` reports a different exit and is
credited to nothing); an absent attestation is `UNATTESTED`, never a
failure — an executor that does not route through the Bash tool must not
read as guilt; and PostToolUse has been measured not to fire when a Bash
call fails, so the log binds passes and may hold no reds at all. The third
bounds the second: a node claiming exit 0 with no row should have left one,
and `record-run.py` lists exactly those separately as the unverified ones.

**`verify: "prove:<gate>"` turns that observation into enforcement.** The
node returns `ExecutionV1` — `{gate, exit, attestId}` — and cites the
attestation its run produced. The gate name is resolved against the
manifest *at compile time*, so a tier naming nothing refuses to compile;
that is the `verify: harness` lesson, which once priced a tier into the
budget and emitted no check at all. At record time a cited attestation that
does not exist, attests a different gate, or recorded a different exit
files the whole run `TAMPERED-EXECUTION` — a campaign does not get to
report clean when its own verification says its exit codes are not what
happened. A `prove:` node citing nothing is `INCOMPLETE` instead: the
declared verification did not run, which is not the same accusation.

A citation is also bound to the run that cites it. The compiled graph
refuses to start without the launch stamp `/fluxpoint:graph-run` passes in
`args._launch`, and carries it into the summary; a cited row minted before
the stamp, on a different commit than the one the tree guard read, or
already backing another node is `STALE`, which files the run `INCOMPLETE`.
Without that, any earlier row of the same gate and exit — another
campaign's, weeks old — passed as this node's execution. A resume reuses the
stamp of the run it resumes, since its replayed citations predate its own
launch.

Two witnesses cover what one tool call cannot. A gate longer than the
600-second cap runs through `attest.py --run <gate>` (the manifest's
command, resolved from the gate name) started in the background, and
`attest.py --await <token>` collects it in bounded foreground slices; the
runner mints the row itself when the command ends, including a red exit
the hook never sees. CI's own statuses on a commit are `prove:ci`: a
top-level `ci` section in the manifest names the forge and the contexts
that decide, `attest.py --ci --sha <sha> [--wait]` mints a row from the
forge's answer, and pending or unreported contexts mint nothing. Each row
names its witness — `hook`, `wrapper` or `forge`.

Nodes that merely happen to match a declared gate stay observed rather than
enforced. Opting in is what earns the stricter reading, and a check that
starts by failing runs is a check people switch off.

And where a repo declares gates, an `irreversible` node's mandatory earlier
guard **must** use `prove:`. The ordering invariant — gate before effect —
was always sound in structure and hollow in fidelity while the guard typed
its own exit code. An effect nobody can undo may not rest on a number the
node that ran it wrote by hand. `prove:ci` and a `--run`/`--await` gate
qualify, so a merge guarded by CI's statuses or a long suite no longer has
to park on a person for want of a gate that fits one call.

## Canonical shapes

- **Fan-out/verify** (`templates/WORK.md`): dimensions → finders →
  per-finding refuter panel. The default review campaign.
- **Council → build → gate** (`templates/WORK.feature.md`): independent
  designs from stated angles → one node judges them side by side
  (`{{prev}}`, a justified barrier) → a mutator implements → an
  independent node re-runs the harness → the proof-auditor judges whether
  verification got weaker (`ProofV1`, halting on WEAKENED; NOT-APPLICABLE
  when the diff carries no proof surface) → red-team closes.
- **Verified slice** (`templates/WORK.verified.md`, opt-in): program
  synthesis and proof synthesis as separate mutators. The `builder` writes
  the logic and may not touch a statement; the `prover` (`agents/prover.md`,
  contracted to `SliceV1` because its output is edits) writes the
  invariants, lemmas, decreases clauses and property tests, and may add
  obligations but never remove one. Two mutators need two independent
  gates, since `verifies` names one node; the prover's gate is a
  `prove:harness` node so the attested exit, never a typed one, stands
  behind the proofs. Prototype it on a repo with a real proof surface
  before promoting the split into the feature template.
- **Advisor–orchestrator**: a planner node emits the work-list as a typed
  contract; worker nodes consume it. The planner never grades its own plan.
- **Zone defense** (org graph): stable `agents/*.md` roles own domains —
  `red-team-reviewer` owns the adversarial pass, `proof-auditor` owns the
  verification review, `prover` owns proof synthesis — referenced by
  `agentType`, not re-prompted inline. An agent file may declare the contract
  it answers in (`contract: RedTeamV1` or `contract: ProofV1` in its
  frontmatter, or `contract: prose` for one that reports rather than returns
  a schema, as `graph-auditor` does: it reviews the IR itself, and a node
  running it inside the graph it audits would be circular). Binding a node
  to a resolved agent whose declared contract is not the node's is rejected
  at compile time: the name being real is not evidence the answer fits, and
  that mismatch otherwise surfaces as a dead gate late in the run. An
  `agentType` the compiler cannot resolve is only ever a **warning** —
  plugins and built-in types are outside its view, so refusing would reject
  valid IR.
- **Loop-until-dry** (`templates/WORK.discovery.md`): a `repeat` block on a
  finder turns fixed fan-out into unknown-size discovery. Use it when "how
  many are there" is the question rather than an input — audits, sweeps,
  reviews whose coverage the dry rule decides rather than a fixed pass
  count. The prompt asks for findings; the budget floors and the dry rule
  enforce thoroughness, so the prompt never has to exhort it.
- **Pipeline of loops**: each mutating node works one loop slice
  — TDD, `--changed` green per edit, `--full` before returning. The graph
  sequences slices; it never replaces the gate.
- **Escalation ladder** (approximated): most items are cheap to judge and
  a few deserve expensive reasoning. Build it as two stages sharing an
  edge — a `skeptic:1` first pass, then a downstream node (or a second
  campaign seeded by `memory`) that re-judges only the survivors at
  `panel:3`/high `effort` — with a `reduce` node between them cutting to
  the items worth escalating. Runtime uncertainty-routing (an item's own
  confidence deciding its tier mid-run) is deliberately not an IR
  construct yet; see ROADMAP before building it ad hoc.

## Inputs, failure, budget

Inputs are normalized by the generated code (object, JSON string, or bare
string) and logged; required args throw. Declare optional inputs in
`argDefaults` so a default is a stated decision, not a silent substitution
— a wrong-target run looks exactly like a successful one, which makes it
the most expensive failure a graph has.

Failure is local: `onRed: drop+log` drops one item and says so; `halt`
stops the campaign. Dead nodes land in provenance with a reason.

Budget is enforced twice, and neither check is advisory. At compile time
`budget.maxNodes` is a hard ceiling on planned agent calls, priced at the
worst case including discovery rounds. At run time two floors apply:
`verifyFloorTokens` stops verification fan-out (claims left unchecked are
logged UNVERIFIED) and `nodeFloorTokens` stops *work* fan-out before a
node or another discovery round starts — recorded as `SKIPPED` in
provenance and carried into the Evidence row as incomplete coverage. A
campaign that ran out of budget says so; it never reads as a clean sweep.

**Calls are the wrong unit for the bill.** Spend is dominated by whether
each call's prefix was a cache read or a cold prefill, and by how much the
model deliberated — so twenty medium-effort nodes sharing a warm prefix
can cost less than six at `max` running cold, and raising a node's effort
was free as far as `maxNodes` could see. The compiler therefore estimates
every graph in cold-input-token equivalents: a shared prefix per call
(priced at a tenth when warm), work tokens scaled by effort (`low` 0.5×,
`medium` 1×, `high` 1.8×, `xhigh` 2.5×, `max` 3.5×), the whole call
weighted by the model's price, refuters and tree sentinels included. The
text the author writes is priced too, one token per UTF-8 byte like the
packet: each node's prompt with its `{{A.<arg>}}` defaults and `{{item}}`
values expanded, and a park's release instructions in its advisor call.
A fan-out's workers read their shared prompt prefix from cache after the
first; a different node's prompt is never discounted, because the cache
has not seen it. `{{prev}}`, `{{decisions.*}}` and `{{seen}}` are sized
only at run time and are named in the estimate as unpriced. So a prompt
that grows between audit rounds moves the estimate, and the ceiling set
from it can refuse the growth. The
constants are stated assumptions in `compile-graph.py`, and `metrics.py`
folds the estimate against the runtime's own `spent` per run so they get
corrected by evidence rather than argued. `budget.maxEstimatedTokens` is
the ceiling on that estimate and is refused at compile time like
`maxNodes`; the estimate itself prints on every `--check`, in the compiled
script's header, and in the run summary as `estimate` beside `spent`, with
a per-node `profile` (effort, model, calls, tokens) so an effort setting
has a number to answer for.

**The prompt cache is a budget decision, declared as one.** A forked call
shares its parent's cache only on a byte-identical prefix at the same
model and the same effort, and the default time-to-live is five minutes
from the request start — which a parent blocking on a `panel:5`, a
`foreach`, or a discovery round outlives as a matter of course, and a park
outlives by days. So the IR declares what it needs: `budget.cacheTtl`
(`"5m"`, the default, or `"1h"`). Under `1h` the estimate prices
consecutive calls at the same `(model, effort)` as warm; under `5m` only
siblings that dispatch together are, and every sequential hop is a cold
prefill. The runtime sets the TTL per session, never per call, so the
declaration is a requirement on the session that runs the graph —
`/fluxpoint:graph-run` names it in preflight — and the compiled script
logs it at launch. A resume past a parked node is a cold start whatever
the TTL and is priced as one. Every shipped template declares `1h`.

**An effort transition is a cold prefill.** The compiler warns on every
consecutive pair of nodes whose `(model, effort)` differs, and on an inline
`effort` equal to what the role or defaults already give (it changes
nothing and reads as a decision). A park ends the relation: the call after
a human or third-party node is priced cold whatever its key, so a change
placed there costs nothing extra and is not reported. Where a change is
justified, put it where the cache is cold anyway — after a park — and put
same-effort work together; `graph-audit` judges whether
the bump buys anything, because the compiler cannot know a task's shape.
Whether `builder: high` is the right setting at all is a separate
question, and it is asserted today, never measured; see DESIGN-NOTES.

## Discovery loops

A fixed fan-out finds what one pass happens to find. When the size of the
work is unknown, add `repeat` to the finder:

```json
"repeat": { "untilDryRounds": 2, "maxRounds": 4, "dedupeBy": ["file", "line", "claim"] }
```

Rounds re-run until `untilDryRounds` consecutive rounds surface nothing
new, bounded by `maxRounds`. Use `{{seen}}` in the prompt so each round is
told what earlier rounds found and spends itself on new ground.

**`maxRounds` is a backstop, not a thoroughness dial.** The dry rule is
what should end a healthy sweep; the ceiling exists for the loop that
never converges. Set it with headroom — `untilDryRounds + 3` or more — or
the ceiling ends every run and every run reports INCOMPLETE, which trains
readers to ignore the word. The compiler warns when the headroom is under
two rounds.

A generous ceiling is cheap, because it prices risk rather than spend:
`budget.maxNodes` must cover the worst case (rounds are priced in), but
the loop exits the moment it goes dry, so rounds that never run cost
nothing. Raising `maxRounds` from 4 to 6 raises the declared ceiling by
50% and typical spend by roughly zero. Let the token floors, not the round
count, be what actually caps cost.

## Sweeps that compound

A `repeat` block makes one sweep exhaustive; it does nothing for the next
one. Without memory, run two re-finds everything run one found, and pays a
fresh panel to reach verdicts that already exist — which is where the
"verification fan-out dominates cost" lesson actually bites.

`memory` closes that loop:

```json
"memory": { "seed": "defect-sweep", "emit": "defect-sweep" }
```

`emit` files every judged item as a `LessonV1` row in
`.claude/fluxpoint/memory.jsonl` — survivors *and* the panel's kills with
the objection that killed them, which is the half `verifyItems` used to
discard. `seed` hands the next run that frontier. Rows are written by
`record-run.py` from the run's own summary, never by an agent, and a row
whose `provenance.runId` names no recorded run is refused — the opening a
fabricated summary would use to plant durable knowledge.

The compiler rejects: a `memory` block declaring neither side; `seed`
without `repeat` (the seed feeds a sweep's seen-list); `emit` without a
verification tier (filing unjudged output would promote a well-formed guess
to institutional knowledge) or without `verifyOver`, or on a contract whose
items carry no `claim`; `memory.key` alongside `repeat.dedupeBy`, since two
spellings of one identity is how they drift apart; and `memory.classBy` that
is not a non-empty list of real item fields, or that sits on a node emitting
nothing.

**A lesson learned twice is not a lesson. It is a missing gate.** Every
filed row carries `arrivals`, the distinct runs that have filed that
identity, and it is carried onto the superseding row rather than reset by
it: latest-state-wins is right for the claim and was silently wrong for the
count, so a thing learned a third time left a store that looked exactly as
it had the first time. `memory.classBy` declares a second, coarser identity
— the defect's SHAPE — because `file`, `line` and `claim` between them
describe only where a defect is and how it was worded, so one shape found in
three places files three lessons and no join over those fields recovers it.
The class is declared by the finder and never inferred; a class guessed from
claim similarity collapses unrelated lessons, and a gate that fires on
everything is one people learn to skim. At two arrivals `recurrence-guard.py`
stops accepting a restated claim as an answer and demands a command in
`.fluxpoint-recurrence.json` whose real exit code is the lesson's verdict,
run by `harness.sh --full` and reported at session start until it passes.

**A seed is advisory and never suppressive.** It reaches the finder's
prompt; it never enters the dedup set. Seeding the dedup set would silently
drop a re-found item — and a finding that comes back is evidence the lesson
went stale, exactly the regression a sweep is run to catch. So a re-found
item is judged again on its merits, and the cost saving comes from a finder
that knows where the frontier was, not from a loop that refuses to look.

Two rules the compiler enforces because getting them wrong is subtle:

- **Dedup against everything seen, not everything confirmed.** Items enter
  the seen-set before verification. Dedup against survivors instead and
  every judge-rejected finding reappears next round — the loop never
  converges and the panel re-judges the same rejects forever.
- **Hitting `maxRounds` is not exhaustion.** Ending on the ceiling while
  still finding new items is logged `discovery INCOMPLETE, not exhausted`.
  A sweep that stopped early and a sweep that finished are different
  claims and never get blurred into one.

Seeds are loaded ranked, not raw: `scripts/recall.py --format seedmap`
serves the same `{tag: {keys, killed}}` shape as `memory.py --load`, but
ordered by a hybrid of BM25, the memory graph (provenance, kill events,
touched files, campaign membership walked with personalized PageRank),
and — when an embedder key is present — semantic similarity to the
campaign line. Relevance decides what reaches the prompt *first* under a
budget; it never decides what gets judged. Lessons whose tag never
matches the sweep's still surface at SessionStart, ranked against the
work file, so knowledge filed under one campaign reaches the next one
without anyone guessing the tag.

The killed half has its own channel. Declaring

```json
"memory": { "seed": "defect-sweep", "emit": "defect-sweep", "priors": true }
```

hands every refuter the node spawns the seed tag's killed claims with the
objections that killed them — capped at 5 items of 400 chars, framed
explicitly as priors the panel may overturn. The finder's prompt never
carries them: priors are addressed to judges, whose job is to not
re-derive an argument the store already holds, not to the search, whose
job is to look everywhere. The compiler rejects `priors` without a `seed`
(nothing to load) or without a verification tier (nobody to tell).

## Running, repairing, evidence

`/fluxpoint:graph-run` compiles, runs, and records. Watch with
`/workflows`; never poll with sleep. Repair is targeted: fix the one red
node in the IR, recompile, then re-invoke with `resumeFromRunId` — the
unchanged prefix of `agent()` calls returns its memoized results and only
the repaired node onward re-runs. Never restart a mostly-green graph from
zero. Before diagnosing an empty result, read `journal.jsonl`. There is
deliberately no per-node retry knob: recovery is memoized resume plus the
once-only ledger, because an in-run retry loop is a second failure policy
hiding inside the first.

Two caches share a vocabulary and are different mechanisms. **Memoized
resume** is the executor replaying completed `agent()` calls whose
`(prompt, opts)` did not change — a journal lookup, valid for as long as
the run's journal exists. The **prompt cache** is the model API reusing
the prefill of a byte-identical prompt prefix on the same model at the
same effort — a KV cache with a time-to-live measured in minutes. This
skill says "memoized resume" for the first and "prompt cache" or
"warm prefix" for the second; the older wording that used one phrase for
both is retired.

## Observe the graph, not the chat

Tune campaigns against numbers, not transcripts. Every run already
records them: the summary carries `spawned` (agent calls that really went
out) and `declined` (calls the node ceiling refused) against `planned`
(the compile-time worst case) and `spent` (the runtime's own token
meter); discovery rounds carry structured found/fresh/kept tallies with
per-worker unique-new counts keyed by worker identity (fan-out
efficiency: a worker at zero is width without coverage, and a dead
worker reads null, never a shifted neighbor's count); reduces carry
before/after (compression). `scripts/metrics.py` folds `runs/*.json`,
`memory.jsonl`, and `inbox.jsonl` into per-campaign rates — node death
and skip rates, sweep endings split four ways (dry rule, ceiling,
budget-truncated, halted — only the dry rule is convergence), panel kill
rate, inbox pressure — and `/fluxpoint:status` reports the trend block. A
kill rate near 0% means the panels may be decoration; near 100% means
the finders are badly scoped. One number is knowingly absent: per-node
wall-clock (the executor forbids `Date` in workflow scripts to keep
resume deterministic), so the serialization cost of an undeclared edge is
caught at compile time by a warning, not measured at run time — the
compiler flags adjacent top-level nodes that declare no dependency.

Graph green is not done. The campaign still exits through the
loop-engineering ship pipeline — `harness.sh --full`, red-team
`VERDICT: SHIP`, the Merge policy in WORK.md — and the Stop-hook DoD gate
keeps final authority.

## Where the Workflow tool is unavailable

Same IR, same compiled script: run its nodes as parallel subagent calls
with the same contracts, sequencing stages yourself, then record with
`--executor degraded-subagents`. Do not silently downgrade — the Evidence
row says which executor ran the graph.
