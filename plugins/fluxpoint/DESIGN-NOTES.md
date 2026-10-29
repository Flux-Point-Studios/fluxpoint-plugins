# fluxpoint — design notes

## Definition of Done boundary (decision #105)

Keep the shared Definition of Done at the project's declared engineering and
merge boundary. Do not introduce a universal `merged-unverified` state or
infer field success from a green pre-merge suite. The plugin serves libraries,
on-chain scripts and hosted applications; it has no shared deployment target,
telemetry source, rollout authority or safe rollback action. A generic state
without those observations would claim a lifecycle it cannot verify.

Projects that need field verification declare a separate deployment campaign
with their own attested check, observation window and rollback criteria.
Record deployment and observed outcomes there, and leave that campaign open
until its own Definition of Done passes or its rollback is recorded. A merge
campaign's completion certifies only its stated scope. A feature flag's owner,
kill-switch or ramp purpose, expiry and retirement check belong to that
project's deployment specification. Generic deterministic metrics can ratchet
flag debt when the project declares an exact count; the plugin does not infer
which flags are safe to retire.

Reconsider this boundary when a concrete integration supplies deployment
identity, reproducible field checks and a measured rollback workflow that
cannot already be expressed as an attested campaign.

## Historical design record

The sections below describe the versions named in their headings, beginning
with v0.1. They preserve earlier APIs and decisions rather than serving as
current setup instructions. For the current graph fields and verification
tiers, use [graph engineering](skills/graph-engineering/SKILL.md); the
[documentation index](../../docs/README.md) links the other operational guides.
In particular, the early `verify: harness` tier below was removed; declared
gate execution now uses `verify: "prove:<gate>"` and attested evidence.

These notes record what the v0.1 smoke test proved, the redesign it
pointed to, and how that redesign landed. Not bound by prior conventions —
the goal was the best design, not symmetry with what came before.

## Status: all eight recommendations shipped (1–7 in v0.2.0, 8 in v1.0.0),
plus the two gaps v1.0 named, closed in v1.1.0

| # | Recommendation | Shipped as |
|---|---|---|
| 1 | Declarative graph IR | ```json graph-ir block in WORK.md + `scripts/compile-graph.py` |
| 2 | Verification tiers | `verify: schema-only \| harness \| skeptic:N \| panel:N`, low-effort refuters, `verifyFloorTokens` |
| 3 | No self-report gates | compile-time invariant: `mutates` requires an `independent` node that `verifies` it |
| 4 | Named versioned contracts | `contracts/*.schema.json`, referenced by name, inlined at compile |
| 5 | Provenance as build artifact | `scripts/record-run.py` → `runs/<runId>.json` + auto-appended Evidence row; `/fluxpoint:status` |
| 6 | Org graph = real subagents | IR `roles` → `agentType`; `red-team-reviewer` wired in the feature campaign |
| 7 | Budget declared and enforced | `budget.maxNodes` rejected at compile time; run-time verification floor logs what it skips |
| 8 | Unify loop + graph into one plugin | v1.0.0: one `fluxpoint` plugin, one `WORK.md`, one Evidence table, `/fluxpoint:migrate` |

Recommendation 8 shipped in v1.0.0 after being parked once. The parked
rationale was that merging changes install granularity for every consuming
repo and rewrites the public surface — true, and the reason it needed a
real migration path rather than a rename. What made it worth doing: both
plugins had grown their own Evidence table, their own settings snippet,
and their own copy of the ship-pipeline prose, and the graph side already
depended on the loop side for `red-team-reviewer` and `harness.sh`. Two
plugins that cannot be installed independently are one plugin.

What unification actually bought:
- one `WORK.md` — a single Definition of Done, Merge policy, and Evidence
  table, whether work is decomposed as loop slices (Plan) or a campaign
  (Campaign). Previously a repo running both had two DoDs that could
  disagree.
- one Evidence discipline: `| When | Source | Outcome | Claim | Proof |`,
  where `Source` is `loop` or a runId.
- one state directory, one settings snippet, one install.
- `/fluxpoint:migrate`, which folds pre-1.0 repos over without losing an
  Evidence row, and hooks that keep honoring `LOOP.md` until it runs.

## v1.1.0: the two gaps v1.0 shipped with

Both were named honestly at v1.0 rather than discovered later, and both
were real:

**Unknown-size discovery.** The IR could only express fixed fan-out, so an
audit found whatever one pass happened to find. A `repeat` block
(`untilDryRounds`, `maxRounds`, `dedupeBy`) turns a finder into a
loop-until-dry sweep, with `{{seen}}` telling each round what earlier
rounds surfaced. Two subtleties are compiler-enforced because they are
easy to get wrong and quiet when wrong: items enter the seen-set **before**
verification (dedup against survivors and every judge-rejected finding
returns next round, so the loop never converges), and ending on the round
ceiling is logged `discovery INCOMPLETE, not exhausted` rather than
reading as a finished sweep.

**Budget enforced where the tokens actually go.** `maxNodes` was a
compile-time ceiling priced at one round, and the only run-time floor
guarded verification. Now rounds are priced into the ceiling (a four-round
loop costs four rounds), and `nodeFloorTokens` stops *work* fan-out —
a node, or another discovery round — before it starts. Declined work is
recorded `SKIPPED` in provenance and surfaces in the Evidence row as
incomplete coverage, so a campaign that ran out of budget can never be
read as a clean sweep.

### The ceiling is a backstop, not a dial (v1.1.1)

The first real discovery run ended on its ceiling with findings still
arriving each round (3 -> 4 -> 4 new, not decaying). The instinct is to
raise the default; the actual lesson is that `maxRounds` had become the
binding constraint instead of the dry rule, so every sweep would report
INCOMPLETE and the word would stop carrying information.

Two changes, because the number alone would only move the problem:
- the shipped template's ceiling went 4 -> 6 (`maxNodes` 84 -> 126),
- and the compiler now warns when `maxRounds - untilDryRounds < 2`, so a
  too-tight ceiling is caught per repo rather than depending on a default
  being right everywhere. It also warns on discovery with no verification
  tier, since sweep output tends to get consumed as fact.

A generous ceiling is nearly free: it raises the declared worst case,
which is what `maxNodes` must cover, but the loop exits the moment it goes
dry, so unrun rounds cost nothing. Cost is capped by the token floors, not
the round count.

Verified: 52 compiler invariant tests, 7 gate-bypass regression cases,
20 unified-state and compatibility tests; all three canonical campaigns
compile to syntactically valid Workflow scripts; the provenance path
writes a real artifact and Evidence row; and compiled campaigns have been
executed end-to-end on the Workflow engine.

## Effort is asserted, never measured (issues #64–#67)

The shipped roles run `builder: high` and `red-team: high`, `architect:
medium`, refuters at `low`. None of those numbers came from a measurement;
they came from a sentence in this file ("cheap skeptics beat expensive
believers") and from taste. The cost article's nearest analogue to a
campaign — an agentic coding benchmark — cut spend by about half by
dropping effort to `medium` and constraining output, and also shows
high effort degrading answers when there is no more evidence to find. It
is plausible that `red-team: high` is right (its job is to keep looking)
and `builder: high` is not. Guessing which is what has to stop.

What v1.36 changed is the instrumentation, and only that:

- the compiler prices every graph (`estimate_tokens`: prefix per call,
  warm or cold by the declared `cacheTtl` and the `(model, effort)` of the
  call before it, work scaled by `EFFORT_MULT`, the call weighted by
  `MODEL_MULT`, and since v1.43 each node's own prompt text by its bytes)
  and refuses a graph over `budget.maxEstimatedTokens`;
- every compiled script carries `ESTIMATE` and a per-node `PROFILE`
  (effort, model, calls, estimated tokens) into its summary beside
  `spent`;
- `metrics.py` folds `estimated` against `spent` per campaign and reports
  the effort mix, so a sweep has a per-role number to compare.

What it did not do, and cannot do from inside a repository: run the
sweep. The evaluation `hillclimb` needs is a graded score, and
`scripts/harness.sh --full` is a binary Definition-of-Done signal — it
discriminates green from red and nothing finer. The candidate score is
the one `metrics.py` already computes per campaign: harness green rate
across repeated runs of one template, over `spent`, with node death rate
and panel kill rate as tie-breakers, swept per role over `effort` and
`model` (a stronger model at `low` against the default at `high` is the
case never tried). Every constant in the cost model is a stated
assumption until that sweep has run; a flat curve would itself be the
finding, and would mean the task is not bound by thinking compute and the
defaults should come down. Until then the templates keep their
settings, and every one of them is labelled as asserted.

v1.43 settles the evaluation question and builds everything around the
sweep except the spend. `scripts/sweep.py --plan` writes one graph file per
setting under test (the role changed, nothing else; a role no node uses,
or one a node overrides inline, is refused, because a knob that does not
reach its calls sweeps noise), prices and validates each against the same
packet the compiler would load, and draws a train/test split of the cases at
random, stratified by tag, never by score, with the test fraction honored
over the whole set; a plan that holds out nothing is refused, since no rep
count can then compare two variants. `--score` grades
recorded runs per variant — a pass is COMPLETE with no red harness, no
BLOCK and no WEAKENED — with Wilson intervals on the test split as the
headline, calls the curve flat when every interval overlaps, and
`--hillclimb DIR` exports the runs in the layout the claude-api hillclimb
flow reads (`baseline/` for the unmodified setting, `v<N>/` for the rest,
transcripts for the train split only). Each compiled summary now carries its launch `inputs`, so a
run is matched to its case.

The arithmetic that settles whether the harness can serve as the
evaluation: it can, but only at sample sizes a binary score forces. A pass
rate over n runs is known to about ±1/sqrt(n). Planned over the feature
template (four variants, twelve cases, three reps, about 32.6M estimated
tokens), the four held-out cases give twelve test runs per variant and
resolve differences of about 40 points; seeing a 20-point difference takes
about 93 test runs per variant, a 10-point one about 388. So a first sweep
can only find a large effect or a flat curve, and the flat curve is the
finding most worth having. A finer-grained score — node death rate, panel
kill rate, confirmed findings per run — would lower those counts, and is
the next thing to build if the first sweep comes back flat and ambiguous.

Still not done: running it needs an authenticated campaign runner with access
to the requested models and usage metering.

Review on 2026-10-14: issue #67 remains open. The attached cloud environment
reports current observations with no configured secrets, runtime variables,
capabilities or outbound identities. Claude Code 2.1.300 was installed locally
to run the real plugin validator; `claude auth status` reports `loggedIn: false`
and `authMethod: none`. The Codex CLI is present, but does not establish access to the requested Claude model
and effort grid or its billing meter. No held-out model runs or actual bill
were produced, so no role default was changed and no measured curve is claimed.

The deterministic metric ratchet added for #101 supplies exact graded counts
and can now measure real pinned Aiken unit-test costs (#102). It does not by
itself show that an effort choice improves coding quality. A useful calibration
still needs campaign-specific held-out tasks, the unchanged full correctness
harness, per-run task scores, and actual token/cost observations across the
architect, builder and red-team effort/model grid. The existing sweep planner,
train/test split and scorer prepare those runs; the unavailable authenticated
runner and usage meter are the concrete blocker to completing #67 here.

### Measured on 2026-10-29 (v1.49.0)

The sweep has now run, on role-shaped calls rather than whole campaigns.
`bench/effort/` holds 26 graded cases: 12 builder (hidden acceptance tests,
eight single-module specs and four changes across a small codebase), 8
red-team (two or three seeded defects plus decoys, F1 scored) and 6
architect (files the change needs and the risks it must name). Each
(model, effort, case, rep) was one authenticated headless `claude -p` call
with `--model` and `--effort`. That is 3 models (Opus 5.5, Sonnet 5.5,
Haiku 5.5), 3 efforts, 26 cases and 3 reps: 702 calls, no errors, every one
served by the model it asked for. The meter reported $32.03 in total. Cost
is the list price the CLI reports. The split is `sweep.py`'s, and 10 cases
were held out. The raw rows, the split and the report are in
`bench/effort/results/2026-10-29/`.

Held-out score and cost per call on Opus, the default session model, with
the shipped setting marked:

| Role | low | medium | high | Cost at low / medium, relative to the shipped setting |
|---|---|---|---|---|
| builder (15 test runs each) | 1.000 | 1.000 | 1.000 (shipped) | 0.65 / 0.87 |
| red-team (9) | 0.978 | 0.941 | 0.978 (shipped) | 0.56 / 0.86 |
| architect (6) | 0.867 | 0.927 (shipped) | 0.827 | 0.80 / 1.00 |

What the curve says:

- **Builder is flat.** Every one of the nine settings scored 1.000 on the
  held-out cases, Haiku at `low` included. Haiku `low` costs 3% of the
  shipped Opus `high`. At this size of slice the builder is not bound by
  thinking compute, which is the finding the issue named.
- **Red-team effort is flat on Opus, but model choice is not.** On Opus,
  `low` equals `high` on the held-out cases (+0.000, with the paired
  interval at zero) for 56% of the cost. Every Sonnet and Haiku setting
  lost 13 to 24 points on the held-out cases. Their train scores matched
  Opus, so a cheaper model looked safe on the cases a tuner can read and
  failed on the ones it cannot.
- **Architect is not flat, and `high` over-thinks.** The shipped `medium`
  is the best setting measured. Opus `high` is 0.10 worse on every held-out
  pair. Opus `low` is 0.06 worse. Every Sonnet and Haiku setting is 0.05 to
  0.23 worse.
- **The train-only pick failed where model choice mattered.** The report's
  pick rule chooses the cheapest setting within 0.05 of the baseline on
  train. It chose Haiku for all three roles. That held for the builder, and
  lost 0.15 on held-out architect and red-team cases. The held-out split is
  what caught this.

What changed: `WORK.feature.md` now runs `builder` and `red-team` at
`medium` instead of `high`. `medium` is within the held-out interval of
`high` for both roles and cheaper. `low` held as well, but the bench calls
are smaller than a campaign slice, where the red-team's procedure (coverage
matrix, attacks with controls, skeptic) is much longer than a case prompt,
so the default moves one step rather than two. `architect` stays at
`medium`, the measured best. No template pins a model. The red-team result
argues for keeping Opus there, and the builder result argues that a builder
on a smaller model is worth a campaign-level trial.

Limits of this measurement:

- One role call in isolation is not a campaign. There is no council, no
  judges, no repair round and no harness gate. The cases are small enough
  that one call finishes them.
- The held-out sets are small: 5 builder, 3 red-team and 2 architect cases.
  The bootstrap resamples cases, so with two architect cases its interval
  is narrow only because it has few cases to draw from. Read the architect
  row as direction, not as a measured margin.
- Before the recorded sweep, pilot runs exposed unseeded real defects in
  five red-team fixtures and a ceiling in the builder cases. The fixtures
  were corrected, and the four multi-file builder cases added, before any
  recorded run. No fixture changed during or after it.
- `compile-graph.py`'s `EFFORT_MULT` and `MODEL_MULT` are still assumptions;
  the bench measures cost per call, not the compiler's per-node estimate.

## What the smoke test established

*(Historical record from v0.1/v0.2. Filenames are as they were then:
`GRAPH.md` and `LOOP.md` became `WORK.md` in v1.0.0, and the hand-written
`review.graph.js` / `feature.graph.js` templates became compiled output.)*

Onboarded a scratch repo (`smoke-ledger`: a lovelace ledger, unittest
suite, real `scripts/harness.sh`), ran `graph-init`, then exercised the
executor:

- **Ping graph** — two independent nodes counted tracked files; `agree:
  true` (10 = 10). Nodes, schema contracts, and the executor wiring work.
- **`review.graph.js`** — 12 agents (3 dimension finders → 3-refuter
  panel per finding), 0 errors, empty-result handling exercised (2 finders
  found nothing), majority filtering and severity sort exercised. The
  full fan-out/verify machine ran end to end and produced **verified real
  findings** — including a HIGH fail-open in the sibling loop plugin's Stop
  gate. The approach has teeth.

Two defects surfaced, both now fixed in the templates:

1. **Silent wrong-target.** `args.target` arrived as a JSON *string*;
   `review.graph.js` fell back to its default target instead of failing
   loudly, and reviewed the wrong repo. Fix: normalize `args`
   (object | JSON string | bare string) and `log()` the resolved target.
2. **Self-report gate.** `feature.graph.js` gated red-team on
   `slice.harnessExit` — the *implementer node's own reported exit code*.
   The node that wrote the code graded its own pass. Fix: an independent
   `harness-verify` node checks out the branch and re-runs the harness
   itself; the gate keys on that.

One cost signal: **570k subagent tokens, ~25 min, 12 agents** for a single
review. Verification fan-out (9 of 12 agents were refuters) dominates both.

## The core tension

v0.1 made `GRAPH.md` a prose spec that a model hand-compiled into a
`.graph.js` script. That transcription step is itself the source of the
bug class the `graph-auditor` exists to police ("does the script mirror
the spec?"). The highest-leverage redesign removes the transcription step.

## Prioritized redesign

### 1. Declarative graph IR — single source of truth (highest leverage)
Replace prose→script transcription with a declarative block inside the
work file (fenced ```yaml/```json) that `graph-run` compiles *mechanically*
to the Workflow script. Nodes, edges, contracts, and verifiers become
data:

```yaml
nodes:
  - id: find-correctness
    role: finder            # -> agents/*.md or inline prompt
    context: [diff, src/**] # spans/refs, never the transcript
    contract: FindingV1     # named schema (see 4)
    verify: panel:3         # tier (see 2)
    onRed: drop+log
edges:
  - {from: find-*, to: verify, when: "findings>0"}
```

Kills "spec diverges from script" entirely, turns `graph-audit` into a
mostly-static check, and makes evidence/resume trivial to wire. This is
the single biggest upgrade.

### 2. Verification tiers, not one-size panels (biggest cost win)
A per-edge policy — `schema-only | harness | skeptic:1 | panel:3 |
panel:5` — chosen by stakes (severity, blast radius, reversibility).
Default cheap; escalate to a full panel only for HIGH/CRITICAL. v0.1
hardcodes `panel:3` on every finding (9 refuters for 3 findings); tiering
plus cheap models/effort for refuters (`opts.model`, `opts.effort`,
already supported, unused) is most of the 570k tokens back.

### 3. Never trust a node's self-report — a checked invariant
The `feature.graph.js` bug generalizes: every gate must be re-derived by a
node that did not produce the artifact. Make independent verification a
first-class edge type in the IR and an auditor invariant, not a convention
a template can quietly violate.

### 4. Contracts as versioned, named artifacts
`contracts/*.schema.json` (`FindingV1`, `VerdictV1`, `SliceV1`,
`DesignV1`) referenced by name from nodes. Harden once, reuse everywhere;
they become the typed interface between org-graph roles, and the auditor
checks schema strength in one place instead of per template.

### 5. Provenance as a build artifact, not a manual table
`graph-run` writes `.claude/fluxpoint/runs/<runId>.json` (nodes,
contracts returned, verifier exits, verdicts, token cost) and
auto-appends the WORK.md Evidence row. No more hand-run shell to stamp
evidence. Feeds `/fluxpoint:status`.

### 6. Org graph = real installed subagents
Stable roles become actual `agents/*.md` with pinned contracts; the work
graph references them by name; zone ownership becomes enforceable (a node
in the `src` zone can only be the src-owner role). Makes "zone defense"
real instead of a markdown table.

### 7. Budget as a declared, enforced input
The WORK.md `BUDGET` line becomes the token target the runner enforces —
scaling fan-out and verification tiers to fit, failing at design time if
the campaign cannot fit. v0.1 ignores `budget` entirely.

### 8. (Bigger call) Unify loop + graph into one plugin, two drivers
One shared harness contract, one `WORK.md` state model with a mode line,
one Evidence discipline, one review family (red-team + graph-audit).
Removes the duplicated settings snippets, Evidence tables, and
ship-pipeline prose now copied across two plugins. Presented as an option,
not a mandate — the split may be worth keeping for install granularity.

## Safety note surfaced by the smoke test (loop plugin)

The Stop gate is bypassable. `.dirty` — the marker `dod-gate.sh:22`
requires before it runs the harness — is written only by the
`Write|Edit|MultiEdit` PostToolUse hook (`verify-changed.sh:24`). Source
edits made through the **Bash tool** (`cat > f`, `sed -i`, `git apply`)
never arm it, so the gate exits 0 with the harness never run. Same disease
as the graph self-report bug: trusting a signal the agent can route
around. Fix: re-derive dirtiness from `git status --porcelain` at Stop
time instead of a hook-written marker. Recommended as a fluxpoint-loop
follow-up (v0.1.1 closed the exec-bit vector, not this one).
