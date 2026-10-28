# fluxpoint — roadmap: compounding, memory, and proof

This document answers three questions asked of the v1.12 system: what would
make the loop and graph layers compound rather than merely coexist; what is
missing for a graph-engineered system that outlives any one context window;
and how far formal verification can be pushed so that "done" means proved,
not asserted. It was produced the way this plugin says work should be
produced — a fan-out of independent subsystem reviews, adversarially read,
then designed against — and it records the gaps honestly, including the
ones that remained open at each recorded version. The opening diagnosis is
historical; later sections record shipped closures. Use the
[documentation index](../../docs/README.md) and
[graph reference](skills/graph-engineering/SKILL.md) for current operation.

As of the October 2026 documentation audit, concurrent lanes, multi-parent
joins, input/tree replay, structured resume, repair evidence, deterministic
metrics and optional Blaster verification are implemented. Remaining gaps
below retain their stated scope; this file is not the feature inventory.

The v1.12 diagnosis was: **the primitives are right and the circuits are
open.** Nearly every layer produces something a later layer could consume
— findings, refuter objections, counterexamples, seen-sets, decisions,
attested exit codes — and almost none of it flows back. Closing those
circuits is worth more than any new capability, because each closure
multiplies work the system already does instead of adding work it doesn't.

## Part 1 — the open circuits

These were identified as missing connections in the original review. The
status column incorporates subsequent implementations:

| # | Circuit | Recorded status | Cost of the gap |
|---|---|---|---|
| 1 | `runs/*.json` → future campaigns | ~~Write-only.~~ **Closed for findings**: a node's judged items — survivors and the panel's kills with their objections — are filed as `LessonV1` rows by `record-run.py` (Part 3a). Advisor recommendations and per-node objections beyond the top one remain unindexed. | — |
| 2 | Discovery seen-sets → the next sweep | ~~`emit_repeat`'s seen-set is an in-run local.~~ **Closed**: `memory: {seed, emit}` carries the frontier across runs; run two opens where run one stopped. | — |
| 3 | Loop-mode work → Evidence | **Partly closed**: the Stop gate now writes its own `Source: gate` rows and SessionStart labels agent-written rows as assertions (Part 2, slice 6). Still open: `WORK.md` itself is unguarded — `verify-changed.sh` and the hygiene scan both skip `*.md` — so a row can still be edited after the fact, and nothing re-executes a Proof cell. | A forged row is now a *visible* anomaly rather than an invisible one; making it impossible needs the work file guarded. |
| 4 | Loop-mode work → Decisions | **Closed**: `decision.py --record`/`--none` gives loop mode the bus with the `DecisionV1` floors enforced, and a PreCompact hook plus SessionStart tell the post-compaction context when reasoning was lost (Part 3c, slice 8). | — |
| 5 | Prover output → anywhere | **Closed for supported output shapes**: `cex.py` retains Aiken, Dafny, Kani, Apalache and fast-check failures. Optional Blaster retains models and requires exact replay for supported scalar inputs; opaque models remain open. See [Blaster](references/blaster.md). | — |
| 6 | Frozen decisions → the next campaign | ~~`imports` resolved by the orchestrating agent by hand; the emitted guard checked key presence only.~~ **Closed**: the compiler resolves `imports` from recorded runs and embeds the records (see Part 2, slice 1). | — |
| 7 | Real exit codes → claimed exit codes | **Enforced for `prove:<gate>`**: declared gate results require matching, fresh citations. Legacy observational claims retain advisory cross-checking. See [graph execution](commands/graph-run.md). | — |
| 8 | Red-team / proof-audit verdicts → the gate | ~~`proof-auditor`'s `SOUND/WEAKENED` is prose.~~ **Half closed (issue #72)**: `ProofV1` types the verdict (SOUND / WEAKENED / UNPROVEN / NOT-APPLICABLE plus the surface reviewed), the feature campaign binds `proof-auditor` as a gate node halting on WEAKENED, and `record-run.py` files WEAKENED as `BLOCKED-PROOF`, UNPROVEN as INCOMPLETE, and a SOUND over an empty surface as vacuous. Still open: neither verdict reaches `dod-gate.sh` in loop mode. | "Treat WEAKENED as harness-red" is mechanism in graph mode and policy in loop mode. |
| 9 | Evidence → freshness | **Partly closed**: graph replay, structured resume, attested gates and Blaster receipts check their declared source and artifact bindings. Human-authored Evidence prose is still asserted and is not automatically re-executed. | Fresh machine evidence does not make arbitrary historical prose current. |
| 10 | Run artifacts → rates | ~~Runs recorded everything and computed nothing: no spend, no kill rate, no trend — the no-scheduler bet was uninstrumentable.~~ **Closed**: summaries carry `spawned`/`planned`/`spent`, provenance rows carry structured data (round tallies with per-worker unique-new counts, reduce before/after), and `scripts/metrics.py` folds runs + lessons + inbox into per-campaign rates surfaced by `/fluxpoint:status`. | — |

## Part 2 — the trust chain

The enforcement chain is strong at the session boundary (Stop gate,
deterministic harness, hygiene scan) and weakest in the middle, where an
LLM sits between a real verdict and its record. In order of blast radius:

**1. Imports were agent-couriered — closed.** `ledger.py --load` and
`release.py --load` existed, but decisions — the artifact class the system
protects hardest — were resolved by the orchestrating agent by hand into
`args._decisions`, and the emitted guard checked only that *some* record
arrived. A fabricated or stale record satisfied it. As of this roadmap's
first slice, `compile-graph.py` resolves each import from
`.claude/fluxpoint/runs` at compile time (`'latest'` = newest recorded run
carrying the decision, else the named runId), shallow-validates the record
against `DecisionV1`'s required fields, and **embeds the record and its
source runId in the generated script**. No hand-assembled map exists for a
launch to trust; a missing or gutted record fails `--check` in preflight; a
malformed run artifact is a hard error (what `'latest'` names must not
depend on which artifacts happened to parse); a campaign that `decides` an
id it also `imports` is rejected (re-deciding a frozen decision is the
exact overturn imports exist to prevent); and `record-run.py` no longer
re-files imported decisions as fresh rows — the Evidence claim names them
as `honors <id>@<runId>` instead.

**2. Exit codes inside graphs were transcribed, not captured — warn mode
shipped.** The "independent harness gate" node runs `scripts/harness.sh`
itself and then *writes* `{"exit": 0}` into `HarnessCheckV1`, and that
self-reported integer is what `haltWhen` consumes before an irreversible
effect. As of slice 2, a PostToolUse hook on `Bash`
(`scripts/exec-attest.sh` → `scripts/attest.py --record`) records the
runtime's own `tool_response.exit_code` for every command declared in a
committed `.fluxpoint-gates.json`, appending
`{attestId, gate, command, commandSha, exit, logSha256, headSha, when,
sessionId, agent}` to `.claude/fluxpoint/attest.jsonl` — ledger read
discipline, so a malformed row is a hard error rather than a quiet shrink.
`record-run.py` cross-checks every claimed gate exit against that log and
reports `ATTESTED`, `UNATTESTED`, or `MISMATCH`; a mismatch is printed to
stderr, filed as an `attest-mismatch` inbox row, carried into the Evidence
row and the run artifact's `attestation.tally`, and surfaced at
SessionStart. Subagent Bash calls fire the hook too, so gates run inside
graph nodes are attested and the row records which agent ran them.

Two properties worth naming, both pinned by tests. Only the exact declared
invocation matches (whitespace and a leading `bash `/`./` are normalized,
nothing else): `scripts/harness.sh --full || true` reports 0 for a red
harness, so it must not be able to borrow the gate's name — it is attested
as nothing, and shows up as UNATTESTED. And absence is never guilt: an
executor that does not route through the Bash tool produces no rows, which
is reported as its own state rather than as a failure, because the
alternative is a false-red generator.

**Enforcement shipped in slice 10.** `ExecutionV1` (`{gate, exit,
attestId, logSha256}`, `additionalProperties: false`) is the contract a
`verify: "prove:<gate>"` node returns, and the gate name resolves against
the manifest **at compile time** — a tier naming nothing refuses to
compile, which is the removed-`harness`-tier lesson made structural. At
record time a cited attestation that does not exist, attests a different
gate, or recorded a different exit files the run `TAMPERED-EXECUTION`.

Two boundaries kept the promotion honest rather than sweeping:

- **Only nodes that opted in are enforced.** A result that merely happens
  to match a declared gate stays observed and warned about, as before.
  Opting in is what earns the stricter reading; a check that starts by
  failing runs everyone's campaigns is one people switch off.
- **Citing nothing is `INCOMPLETE`, not tampering.** An executor that never
  routes through the Bash tool leaves no attestations, and accusing it
  would make this a false-red generator. The declared verification did not
  run — that is a different sentence from "the exit codes lie", and the
  outcome says so.

The graph also refuses to compile an `irreversible` node whose mandatory
earlier guard reports its own exit code, in any repo that declares gates.
The ordering invariant was always sound in structure and hollow in
fidelity while the guard typed the number itself; an effect nobody can undo
may not rest on that.

**3. Loop-mode evidence was self-certified — closed in slice 6, and not the
way this section proposed.** The design below (an `evidence.py --mint` the
agent runs, which executes a gate and manufactures a row) was written up,
adversarially reviewed from four angles, and rejected. The decisive
objection: *a subprocess an agent launches cannot outrank the agent that
launched it.* It inherits the agent's environment, PATH, and cwd, so
identical argv can produce opposite verdicts; it would have added a fourth
full harness run per iteration; and its attestId would have pointed into
gitignored state, so the committed row's only verifiable content was
unresolvable to a reviewer. It also would have laundered free prose through
a real exit code — `harness --full` green establishes exactly one
proposition, and any sentence in the Claim cell would have inherited it.

**The witness already existed; what was missing was a reader.**
`dod-gate.sh` is a *Stop hook*: the runtime invokes it, at the moment the
agent claims done, and it already re-runs `scripts/harness.sh --full`,
holds the exit code, the log, and the tree state, and already distinguishes
PASS from FAIL from TIMEOUT. So the gate now writes its own Evidence row
(`Source: gate`) through `scripts/evidence.py`, which is a *writer* — it
imports no `subprocess` and executes nothing, and a test pins that. The
proof cell names what was actually established: `harness --full exit <n>
@<sha7> tree:<sha8> dirty:<n> log:<sha8>`. The tree hash matters more than
the commit: in loop mode HEAD is the pre-work commit for the whole
iteration, so a verdict taken before the first edit and one taken after the
last would otherwise be the same row.

Rows are written only where a stop actually happens — green, checkpoint
yield, or timeout. An ordinary blocked stop is a correction, not an outcome.
An unchanged verdict is not repeated, so a long green streak is one row.

`inject-state.sh` then renders the two classes separately with reserved
budgets, so a run of agent-written rows can no longer evict every witnessed
one from the window a fresh context inherits.

**The property, stated honestly:** not "a row cannot be forged" — `WORK.md`
is skipped by both `verify-changed.sh` and the hygiene scan, so an agent can
still type anything into it, including a `gate` row. It is that **there is
now a row class an honest agent never writes**, so a `gate` row in a diff
with no corresponding gate run is a reviewable anomaly, and a fresh context
inherits assertions labelled as assertions. That is a review affordance, not
a containment boundary. Guarding `WORK.md` itself remains open (circuit 3
below).

Also fixed here, and independent: `plugin_script` in the scaffolded harness
ran `find "$HOME/.claude/plugins" … | head -1`, and `find` exits 1 when that
directory does not exist. Under `set -euo pipefail` the pipeline inherited
that status and killed `--full` on its first plugin lookup, with no output —
in exactly the repos the function exists to support, the ones carrying the
harness without the plugin installed.

The rejected design, for the record. `evidence.py --mint` takes a
claim and a command, **executes the command itself** from an allowlist
declared in a committed `.fluxpoint-tools.json`, records the actual exit
and HEAD sha, and splices the row. The agent requests evidence; the script
manufactures it — the loop-mode twin of `record-run.py`. The allowlist is
the same idiom that must eventually contain `wake-check.sh`'s `sh -c`
exposure, so this closes one arbitrary-shell channel while refusing to
open another. Follow-ons, in order: `--replay` (re-execute proof cells of
current rows on a bounded schedule; append REVERIFIED / STALE / REFUTED
status rows, never mutate — supersession, not erasure), validity-graded
SessionStart injection (each row rides in with its replay status and age),
and DoD lines with a `— proof: <cmd>` tail that `dod-gate.sh` re-runs
before honoring a checked box.

**Known bypasses — (a), (b) and (c) closed in slice 7.** The gate now
judges the tree against a **session baseline commit** rather than HEAD.
`inject-state.sh` records the starting commit once per session (only when
absent, so resume and post-compaction cannot forgive commits made before
them), the gate exports it as `FPL_DIFF_BASE`, and it is refreshed on green
— a tree that just passed is the right thing to measure the next one
against. Red and timed-out runs leave it alone, having established nothing.

- (a) **Write-via-Bash then commit is closed.** Verified both ways: with the
  session baseline the gate blocks a red harness; forcing the old HEAD
  baseline lets the same session stop silently. The work prompt tells the
  loop to commit every slice, so this was the normal path, not a clever one.
- (b) **The hygiene scan inherits the same baseline**, so a `TODO` or
  `.unwrap()` committed mid-session is no longer laundered past the gate.
  History older than the session is still CI's job.
- (c) **The trust base is now a list, not one file**: `scripts/harness.sh`
  plus `.fluxpoint-proof-baseline.json`, `.fluxpoint-pairs.json`,
  `.fluxpoint-gates.json`, `.fluxpoint-budget.json`, `.fluxpoint-cex.jsonl`,
  `package.json`, `Makefile`. Any of them changed this session — committed
  or not — is named in the green notice, so re-recording a baseline to
  absorb a rise is said out loud. Still a warning, not a block: making it
  blocking is how a gate gets switched off.
- (d) `wake-check.sh` still runs `sh -c` on predicates that originate in the
  IR. Open; it needs the allowlist treatment `.fluxpoint-gates.json` models.

Two properties of the baseline worth knowing. It **falls back to HEAD**
whenever the recorded commit is not an ancestor of the current one — a
rebase or a branch switch would otherwise diff the tree against unrelated
history, arming the gate and flooding the hygiene scan with findings nobody
introduced; a gate that cries wolf is worse than the bypass it closed. And
it **depends on SessionStart having run**: with no recorded baseline the
gate degrades to the previous HEAD comparison, which is the old behavior
rather than a new hole, but it means `FPL_DISABLE=1` on the SessionStart
hook alone weakens the Stop gate too.

## Part 3 — the memory layer

The context strategy already matches where the field converged: durable
state in versioned files, a curated always-present injection at
SessionStart, isolated node contexts whose typed contracts define what
survives the boundary. What is missing is the tier discipline and the
consolidation process. In the standard taxonomy: SessionStart injection is
core memory, `runs/` is episodic memory, Evidence/Decisions are semantic
memory — and nothing performs consolidation, so episodic never becomes
semantic without an agent volunteering it.

**3a. Lessons: `memory.jsonl` + `LessonV1` + IR `memory:` — shipped in
slice 3.** What landed matches the design below with one deliberate
narrowing, and it is the safety-critical part: **a seed reaches the
finder's prompt and never the dedup set.** Seeding the dedup set — the
literal reading of "pre-loads the seen-set" — would silently drop a
re-found item, and a finding that comes back is evidence the lesson went
stale, which is exactly the regression a sweep exists to catch. So the cost
saving comes from a finder that knows where the frontier was, not from a
loop that refuses to look, and a re-found item is judged again on its
merits. Also enforced beyond the original sketch: `memory.emit` requires a
verification tier, because filing unjudged finder output would promote a
well-formed guess to institutional knowledge. Still open from this
proposal: killed-lesson claims are loaded (`memory.py --load` returns them)
but do not yet ride into refuter prompts as priors, and there is no
`--since <git-rev>` staleness filter.

The original design, for reference. A new
append-only `.claude/fluxpoint/memory.jsonl` owned by `scripts/memory.py`
(ledger read discipline; identity `tag|dedupeKey`, latest-state-wins like
the inbox). `record-run.py` grows a filing pass symmetric to its
ledger/inbox passes: every `FindingsV1` node's items become `LessonV1`
rows — survivors with the objections the panel already keeps, killed items
with their kill count and top objection (today discarded by
`verifyItems`' filter) — mechanically, keyed by the node's own `dedupeBy`,
provenance `{runId, node}` required and checked against `runs/`. The IR
gains `memory: {seed: <tag>, emit: <tag>}` (closed-registry, emission-test
probe like every field): `seed` pre-loads a discovery node's seen-set and
`{{seen}}` from disk via `memory.py --load`, and killed-lesson claims ride
into refuter prompts as priors. The Evidence row gains "seeded N prior
keys, M new beyond the prior frontier", so an incremental sweep is
distinguishable from a fresh one. This is the single most multiplicative
change available: `untilDryRounds` converges against cumulative knowledge,
so the same ceiling buys monotonically deeper coverage every run, and
panels stop re-litigating what earlier panels killed. Seeds are advisory
(they suppress re-reporting cost, never auto-kill a re-found item), and
`--since <git-rev>` drops lessons older than the last touch of the files
they name, so a stale kill cannot hide a regression.

**3b. Counterexamples: `.fluxpoint-cex.jsonl` — shipped in slice 5.**
Research settled the parser question outright: `aiken check` has no `--json`
flag because it emits structured JSON whenever stdout is not a TTY, sending
diagnostics to stderr — so the harness captures it with a plain redirect and
nothing parses a terminal box. (The published `--show-json-schema` is wrong
in two ways that break a schema-driven parser: it declares a `kind`
discriminator the binary never writes, and names the array `test` where real
output says `tests`. Kinds are told apart by field presence.)

Four corrections to the sketch below, each from an adversarial pass and each
now pinned by tests:

- **The store is `.fluxpoint-cex.jsonl` at the repo root, not under
  `.claude/fluxpoint/`.** That directory is gitignored by this plugin's own
  installer, so a ratchet placed there exists only on the machine that found
  the bug — CI and every teammate would take the dormant path and report
  green. Every other ratchet (`.fluxpoint-proof-baseline.json`,
  `.fluxpoint-pairs.json`, `.fluxpoint-gates.json`) is already at root; only
  ephemeral run state belongs under `.claude/`.
- **A pin cannot be generated, so it is verified instead.** The JSON carries
  the counterexample but not the test's parameter type or the predicate it
  broke, so a generated `.ak` would be a guess — and a wrong guess drops a
  non-compiling file into a tree that was merely red. The agent writes the
  test; `cex.py` refuses to record it unless the recorded literals are in
  *that test's own body*, in order, on token boundaries, outside comments
  and `@"..."` strings, in a test that is neither `fail`-annotated (which
  inverts the oracle: it passes because the bug is unfixed) nor hollow (bare
  boolean, self-comparison, or a value bound then ignored). Whole-file
  containment would have been vacuous for the modal counterexample — `0`,
  `True`, `[]`.
- **Containment is over literal leaves, not the whole string.** Opaque types
  render as something unwritable: `Dict([(#"ab", True)])` must be built with
  `dict.from_list(...)`, so demanding the word `Dict` would make a
  legitimate pin impossible. The data inside it is still required.
- **The harness fixes the seed** (`--seed "${FPL_AIKEN_SEED:-1}"`). Aiken
  draws a random u32 per run, so the same bug shrinks to a different value
  each time and dedupe would never fire.

Also fixed here, found while wiring: `[ -n "$x" ] && cmd` as the **last**
statement of a function returns 1 under `set -e`, so the shipped template's
final pair-guard line failed `--full` outright in any repo without the
plugin installed. Converted to an `if`.

Not yet claimed, and named in the docstring: a pinned test is recorded as
*carrying* the counterexample, not re-run against the un-fixed code to prove
it would have caught it. That is why `signature` is already in the schema.

**Dafny joined the ledger (issue #70).** `TOOLS` became a registry — one
parser per prover, one ledger, one pinning discipline — and the second entry
is Dafny, which jumped the queue below because it was already first-class in
the other two ratchets. The parser reads the literal shape
`dafny verify --extract-counterexample` prints (a `Counterexample for first
failing assertion:` heading, then `file.dfy(l,c): initial state:` blocks of
`name : type = value` lines) and Dafny 4's `assume name == value && …;`
rendering of the same model, keeps the initial state as the method's inputs,
and records later failures — Dafny models the first one only — as assertions
with no input. Resolution and type errors print in the same `Error:` shape
and are refused as counterexamples; a `Counterexample` heading with nothing
readable under it files INGEST-FAILED, named by prover. A Dafny pin is a
tracked `.dfy` declaration carrying the literals in order, refused when it is
`{:verify false}`, `{:axiom}`, an `assume`, or `assert true`. The scaffolded
harness captures `dafny verify` the way it captures `aiken check`, with
`FPL_DAFNY_ARGS` carrying `--extract-counterexample`.

*fast-check landed in slice 24 (issue #80), and with it the seed sweep
(issue #82).* Off-chain property tests produce the same artifact as a
prover: a shrunk counterexample with the `seed` and `path` that replay it.
They now enter the same ledger under the same pin discipline, and the
scaffolded harness captures the `test` script the way it captures the
provers. Only the vitest reporter is parsed; a bare `fc.assert` throw and
Jest's frame are named rather than guessed at. A vitest run is the first
input whose failures are not all counterexamples, so a plain assertion
failure in the same run is deliberately not recorded: it carries no
generated input, and minting one would put a value in the ledger that no
generator produced.

Capturing it exposed what pinning the gate's seed had cost. `--seed 1`
makes a shrink reproducible and lets this ledger dedupe, and the price —
every run exploring the same cases forever, at the tool's default
iteration count — had never been written down. `cex.py --sweep` is the
other half of the split `mutation-guard` already makes: it runs the prover
over a rotating seed window at a raised `--max-success`, starting where the
last sweep stopped, ingests what it finds through the parser the gate
already uses, and stamps `{from, to, found, headSha, when}` into
`.fluxpoint-fuzz.json`. `--check` reports what the last sweep covered and
how many commits ago, loud by default and fatal only under
`failWhenStale`. Both flags were read off `aiken check --help` on a real
v1.1.9 binary rather than assumed.

*Kani and Apalache landed in slice 21.* `cargo kani` output is read per
harness — the FAILURE checks with their locations, the `Failed Checks`
summary, and the concrete-playback block's interpreted values in harness
order, with the byte-vector form kept beside them so Kani's own
`--concrete-playback=inplace` test is a legal pin once it is renamed to
carry the cexId and tracked. A build error records nothing and says so; a
summary counting failures the parser could not extract is INGEST-FAILED.
Apalache's ITF trace is read from the path its stdout names or directly,
every state rendered in TLA+ spelling (`#bigint`, `#set`, `#map`, `#tup`,
records), the first state being what a pin must carry — an `Init`
operator the invariant can be re-checked from — and an `ASSUME` refused
as inverted. The scaffolded harness runs `cargo kani` when the crate
declares a harness and `cargo-kani` is installed (`FPL_KANI_ARGS` carries
the playback flags), and `apalache-mc check` only when `FPL_APALACHE_ARGS`
names the spec and invariant. Both fixtures are captured from real runs
(Kani 0.67.0 on CBMC 6.8.0, Apalache 0.47.2, 2026-09-11) and both provers
were driven through the scaffolded harness on real installs. The captures
corrected the documentation in three places: Apalache names its trace
files `violation1.*`, its ITF `#meta` carries no `source` (the spec name
now comes from the `_apalache-out/<Spec.tla>/` directory, so the same
trace dedupes across timestamped runs), and Dafny 4.9.1's `dafny verify`
prints ` Related counterexample:` with the literal left of the `==`, which
the Dafny parser had read as a bare assertion until the real output became
the fixture. The same session found that `dafny verify .` is refused by
the real CLI and that a `**/*.dfy` arm glob saw one directory level, so
the harness template now hands the prover its project file or the tracked
`.dfy` files.

The original design, for reference. `cex.py --ingest` parses prover
output tee'd from the harness (aiken's shrunk counterexample block first;
Dafny models second, shipped; Kani traces and Apalache ITF later — parser
drift is a loud INGEST-FAILED inbox row, never a silent drop). `--pin <id>` generates a concrete
regression test from the *recorded* input (never from agent prose) and
`--check` in `--full` fails when a pinned counterexample has lost its test
file. A pinned cex is the one memory artifact that re-verifies itself
forever, because its regression runs in every future `harness.sh --full` —
the "claim without a row is false" axiom finally given its converse for
prover claims. Repair campaigns get their canonical shape: gate red → cex
ingested → the implementer's context packet *is* the minimal failing
input → gate re-run → pin on green.

**3c. Loop-mode decision capture + flush-before-death — shipped in slice 8,
with the PreCompact half redesigned.** `decision.py --record` and `--none`
landed as described, enforcing the `DecisionV1` floors rather than
describing them — including one the schema implies but never stated: a
`chosen` value that was never among the options is refused, because every
floor can pass and the decision still be incoherent.

The PreCompact hook did **not** land as sketched. The sketch had it "tell
the summarizer what is already durable", which assumes a hook's stdout is
injected into the compaction summary — and that is **undocumented**. A hook
whose entire value rested on an undocumented mechanism would be inert
without ever saying so, which is the `verify: harness` failure class this
plugin removed from its own compiler.

So the hook's value rests on a mechanism this plugin already depends on. It
answers one deterministic question at compaction time — had anything this
window learned reached the disk? — by hashing the durable surfaces (the
work file's Decisions/Notes, the project's memory store, the session's task
board) against a per-window snapshot, and writes the answer to
`<session>.compacted`. SessionStart then reads that marker and tells the
post-compaction context, in the same injection that already carries branch
state and the harness verdict, that the reasoning behind the current diff
is gone and to re-derive rather than assume. The stdout line is still
emitted: a bonus if the summarizer sees it, costing nothing if it does not.

v1.29 hardened this from a warning into a gate. An unflushed window now
blocks compaction ONCE (exit 2, reason on stderr) so the flush happens
before the transcript is discarded; the block is bounded per window — the
retry always proceeds and re-seeds the window — so two consecutive attempts
can never both block and the wedge the original design feared is
structurally impossible. Non-git roots are in scope (the old git-guard exit
made the hook inert in exactly the long non-repo sessions where compaction
hurts most), and `FPL_COMPACT_BLOCK=0` demotes it back to warn-only.

`FPL_DISTILL=1` arms the Stop-gate check, and it is off by default for the
reason the slice-6 review made explicit: a check that starts by blocking
stops is one people disable, taking the rest of the gate with it.

The original design, for reference.
`decision.py --record` validates a piped `DecisionV1` against the existing
schema and splices it under the Decisions header with `Source: loop` — the
schema's anti-lazy floors (two options with real objections, rationale ≥40)
now police loop mode too; `--none <reason>` makes silence explicit. A new
PreCompact hook tells the summarizer exactly what is already durable
("WORK.md and .claude/fluxpoint/* are authoritative; decisions and failed
approaches not yet there must be preserved verbatim"), which turns
compaction from the largest memory-loss event into a non-event for
anything that matters. Optionally, behind `FPL_DISTILL=1` until the
false-positive rate is measured: the Stop gate blocks once when code
changed but the Decisions/Notes hash didn't and no `--none` marker exists —
"done" extends from code-green to memory-flushed.

**3e. Hybrid recall: the memory graph + embeddings layer — shipped in
slice 18.** The tier taxonomy above named the stores; what stayed missing
was a way back into them that did not require guessing an exact
`tag|dedupeKey`. The design question was researched properly — Graphiti's
architecture read at source level, the 2024–2026 agent-memory evidence
base surveyed (HippoRAG 2's ablations, LazyGraphRAG's indexing-cost
result, the verbatim-beats-extracted ablation, the LoCoMo vendor-eval
collapse), three independent designs drawn and adversarially judged — and
the verdict was unanimous on the load-bearing point: **Graphiti's read
path is worth adopting and its write path is not.** Its LLM
extraction/dedup/resolution pipeline exists because it ingests
unstructured chat; these stores are already typed, provenanced, and
append-only, so extraction would add cost and nondeterminism for negative
value. What shipped in `recall.py` + `embedder.py`:

- a **derived knowledge graph** — lessons, runs, campaigns, IR nodes,
  tags, files, counterexamples; edges for provenance, kill events
  (`KILLED_BY` stays permanently valid — GRIT's formulation, so "kills
  are priors, never suppressors" holds in the type system), supersession
  (weight zero — never walked), path touches (resolve-or-drop, drops
  counted) — compiled byte-identically from the stores, exactly as the
  IR compiler treats WORK.md;
- **bi-temporal serving** derived from the append chains (current by
  default, `--as-of` for the version that held then), plus build-time
  stale-kill annotation (the `--since` idea from 3a, landed as a mark
  rather than a filter);
- **hybrid retrieval**: BM25 (k1=1.2, b=0.4, identifier-aware tokenizer)
  over query and files-touched legs, optional embedding cosine, and
  personalized PageRank (damping 0.5, specificity-weighted seeds) fused
  with weighted RRF (k=60), boosted by re-establishment count and mild
  recency decay on lessons only;
- an **embedder quarantine** (`voyage`/`openai`/`none`, key-resolved,
  unknown value fatal, content-hash cached, unit-normalized, brute-force
  dot product) sanctioned as the single non-deterministic input in the
  memory layer, able only to reorder advisory output;
- **budgeted injection**: a SessionStart section across every indexed
  document kind (offline, never rebuilding, stale-and-absent modes named,
  `FPL_RECALL_INJECT=0`),
  relevance-ordered seed maps for `/fluxpoint:graph-run` in
  `memory.py --load`'s exact shape, and `/fluxpoint:recall` for humans.

The follow-on slice (19) closed most of what this design left open:

- **Killed priors reach refuter prompts.** `memory.priors: true` (closed
  registry, emission-probed) builds a capped priors block — 5 killed
  claims, 400 chars each, each with the objection that killed it — from
  the seed tag's loaded lessons and hands it to every refuter the node
  spawns, framed as priors the panel may overturn. The finder's prompt
  stays clean: priors are addressed to judges, not to the search.
- **Decisions are graph nodes.** Projected from `runs/*.json`
  `summary.decisions` — the one row class with a stable machine identity
  (the id the compiler already resolves `imports` by), so the sha-of-row
  churn problem never arises; WORK.md's Evidence rows stay out until that
  identity question has an answer.
- **Substrate primitives join the walk.** `substrate-graph.mjs --json`
  emits the workspace graph machine-readably (deterministic, sanitized,
  exit 1 on manifest problems), and recall ingests the repo's own
  `substrate.json` — sanitized as hostile input, skipped by name on
  damage — so a lesson touching a primitive's file sits two hops from the
  primitive and one more from its consumers.
- **The per-prompt hook ships dark.** A UserPromptSubmit hook exists but
  is gated behind `FPL_MEM_PROMPT=1` (default off), offline,
  never-rebuilding, capped at 3 items / 1200 bytes, with a precision
  floor: two genuinely independent retrieval legs, or a lexical match on
  at least two informative query tokens. The graph leg never counts as
  corroboration — with only a prompt to go on, its seeds come from the
  lexical leg's own top ranks, so lex + graph is one signal counted
  twice, which the adversarial review caught before it shipped.
  It stays dark until an artifact-grounded eval shows lift — the
  strongest external result (SWE-ContextBench) says wrongly retrieved
  memories cost more than none, and that is a bet, not a default.
- **Consolidation is a canonical campaign.** `WORK.consolidate.md` (3d's
  template half): a curator proposes merges/restatements as findings, a
  skeptic attacks each one with the killed priors in hand, and
  record-run.py files survivors through the single-writer path.
  Supersession stays append-only; nothing deletes.

Still open: `evidence.py --replay` and validity-graded injection (the
other half of slice 11); WORK.md Evidence rows as graph nodes (stable
identity unsolved); cross-repo federation (unscheduled, needs its own
Decisions row).

**3d. Consolidation as an ordinary campaign.** A shipped
`WORK.consolidate.md` template — read recent runs and `memory.jsonl` →
propose merges/supersessions → `skeptic:1` refutation → a `mutates` node
appends via `memory.py`, verified by an `independent` node re-reading the
store — run by a scheduled Routine. Memory curation thereby produces
provenance, spends declared budget, and passes independent verification
like any other work; no bespoke daemon. Supersession is bi-temporal
(append an invalidating row; never mutate), which preserves the
append-only property everywhere. Cross-repo federation comes last and
allowlisted: a committed `.fluxpoint-federation.json` maps aliases to repo
paths (the compiler rejects an alias not in the manifest — the wake-check
lesson applied in advance), org-scope lessons are injected as advisory
context only, never as gate inputs, and promotion to org scope happens
only through the consolidation campaign's refute-and-verify path.

## Part 4 — formal verification

A prover's exit code is the strongest gate this harness can hold, and
agents gaming provers is measured behavior, not speculation. There are
four escape routes; `proof-guard.py` closes the first. The roadmap closes
the rest, and every mechanism lands as an exit code the existing gate
already consumes.

*Route 1 reaches off-chain, shipped in slice 22 (issue #78).* The whole of
Part 4 assumed the prover was the gate worth gaming, which was true while
the threat was a human reading validators. It is the wrong assumption
against an autonomous caller, which does not call the validator at all: it
calls the API, the indexer and the transaction builder, and a provably
correct validator signs whatever that builder hands it. So the hatch
ratchet now counts TypeScript, on the argument route 1 already makes. A
type checker is a prover with a weak logic; `as any`, `as unknown as T`,
the `!` assertion, `@ts-ignore` and a tsconfig with `"strict": false` each
discharge an obligation it had, and `tsc` exits 0 on all of them.
`@ts-expect-error` is deliberately uncounted: it fails the build once the
error it names is fixed, so it cannot rot in place, and counting it would
push people toward the suppression that can. The rest of the off-chain
surface is #80 (fast-check in the counterexample ledger) and #81 (the
builder's own attack taxonomy).

*The blueprint half, shipped in slice 23 (issue #79).* `plutus-budget.py`
read `plutus.json` for exactly one field, `compiledCode`, and used it for
script size. The blueprint also carries the datum and redeemer *schemas*,
and nothing held the off-chain encoder to them. `blueprint-guard.py`
decodes the PlutusData a builder produced and asks the blueprint whether
the validator would recognise it. The blueprint is the right oracle
because `aiken build` regenerates it from the validator, so it cannot
drift from the on-chain truth the way a hand-kept type does.

Three honesty rules, since a conformance checker that guesses is worse
than none: a schema construct the parser does not know is reported
UNCHECKED rather than conformant; a `$ref` that does not resolve is an
error rather than an empty schema everything satisfies; and an opaque
`Data` schema conforms by definition but every conforming value is told
how many fields rode through on that opacity. An opaque schema does not
red by default — that would make the gate unadoptable in the repos that
need it most — and `requireTyped` turns those into failures once a repo
is ready.

Building it surfaced a defect in the ratchet itself, latent since the
first category and now fixed with regression cases: `--check` read a
category absent from the baseline as a recorded zero, so **every category
this plugin has ever added was a red gate for every repo that armed before
it** — on an upgrade they did not ask for, over a diff they did not write.
An absent key now reads as new, is reported and counted, and ratchets from
the next `--baseline` on.

**4a. `spec-guard.py` — the statement ratchet (route 2: weaken the
theorem) — shipped in slice 4.** What landed covers Aiken and Dafny:
Aiken test and property signatures (name, fuzzer types, `fail` polarity)
and Dafny `requires`/`ensures`/`invariant` clauses per declaration, hashed
into the `spec` section of `.fluxpoint-proof-baseline.json` (each guard now
preserves the other's section, or arming one would disarm the other).
`--check` reds on a changed or removed obligation unless a Decisions row
names its id; additions are free; reformatting, clause reordering, and file
moves are not weakenings and stay green.

*Slice 21 closed the rest of 4a.* Lean and Coq theorem statements, Isabelle
lemma statements, TLA+ `THEOREM`s together with every `INVARIANT` and
`PROPERTY` a TLC `.cfg` names (bound to the operator definition it checks,
so deleting the config line and editing the operator are both visible),
and every Rust fn carrying a `kani::` attribute are parsed; what remains
uncovered (Agda, F*, Alloy) is still named. `--axioms` records the
prover's own assumption listing for headline theorems — `#print axioms`
through `lake env lean`, `Print Assumptions` through `coqc` with the
module path resolved from `_CoqProject`, `dafny audit --report-format
text` with line numbers dropped so a moved declaration is not a new
assumption — and `--check` reds on a new axiom in a headline's set while
fewer is always allowed; a missing toolchain prints NOT RUN by name and
never reads clean, and a listing the parser cannot read is red. The DoD
`— proof: <obligation id>` tail is enforced: a checked box whose obligation
does not exist is red, and one whose obligation changed is caught by the
statement hash. The plugin's own harness runs without the provers, so
the audit parsers are pinned through shims — but the shims print what
Lean 4.15.0, Coq 8.18.0 and Dafny 4.9.1 printed on 2026-09-11, and each
audit was driven against the real install through the scaffolded harness,
including a red the moment a `sorry` entered a green Lean theorem. The
real runs corrected two guesses: Dafny 4.9.1 spells the report format
`txt` and prints its ordinary warnings beside the finding rows, which are
now told apart by the `file(l,c):Name:` shape; and a Dafny declaration
carrying an attribute (`lemma {:axiom} Helper`) was invisible to the
statement scanner, which folded its clauses into the previous method.

Building it surfaced a live bug in the sibling guard, now fixed with
regression cases: `proof-guard.py` matched Aiken parameter lists with a
`[^)]*` class, which stops at the first `)`. A property test's own fuzzer
contains one — `n: Int via bounded_int(1, 99)` — so **every parameterised
property test was invisible to the vacuous-test scan**, which is exactly
where a gutted body is least likely to be re-read. Constant predicates
taking a tuple were missed the same way. Both now match by counting parens.

The original design, for reference. proof-guard polices proof *bodies*;
the statements are unguarded — a dropped `ensures` conjunct, a widened `requires`, or a
deleted property keeps every hatch count flat and every prover green, and
the proof-auditor's own checklist calls this the most common way verified
code regresses. `--scan` extracts every obligation statement per tool
(Dafny requires/ensures/invariant per method, Lean/Coq theorem signatures,
Aiken test + property names with their fuzzer types), normalizes and
hashes each into a `spec` section of the existing baseline file. `--check`
fails when a baselined statement changed or disappeared without a
Decisions row whose evidence names the obligation id — weakening a spec
becomes a frozen, re-injected, justified decision or a red gate. Adding
obligations is always free. `--axioms` runs the prover's own assumption
audit (`#print axioms`, `Print Assumptions`, Dafny's listing) over
baselined headline theorems: a *new* axiom in a dependency set is red even
when counts are flat, closing assumption laundering; where the toolchain
is absent the output says "axiom audit not run" rather than reading clean.
With both ratchets armed, the LLM auditor's residue shrinks to what only
judgment can see — properties proved about unreachable states — which is
the correct division of labor.

**4b. Attestation (route 3: transcribe the verdict).** Part 2, item 2.
Composes: `prove:spec-guard` as the ordered pre-merge gate makes
spec-freeze a graph-shape property, and an irreversible ceremony's
mandatory earlier `haltWhen` guard finally consumes a machine-minted exit.

**4c. `mutation-guard.py` — the judge of the judge — shipped in slice 9.**
The split that made it work is between the two halves: `--measure` is the
expensive run (minutes to hours, one compile-and-test per mutant) and is
where the ratchet lives; `--check` is cheap enough for `--full` and re-runs
nothing, asking only whether a measurement exists and still describes this
tree. `--measure` belongs on the wake/Routine layer.

Both directions ratchet, and the second is the one a ratio alone misses: a
score can hold flat while coverage shrinks, so the absolute survivor count
may fall but never rise. `--accept` re-records a weaker score and demands a
written reason that is kept in the committed record. `unviable` and
`timeout` outcomes are excluded from the denominator — a mutant that never
compiled tested nothing, and folding those in would let a build that got
slower look like a suite that got better.

`--from` records an `outcomes.json` a CI job already produced instead of
re-running the tool, which is both the common CI shape and what makes the
ratchet testable without a Rust toolchain.

Staleness is deliberately **not** fatal by default. It is named with how
stale, and raised in the inbox so somebody schedules the re-run — a gate
that reds because an expensive job has not been re-run yet is one people
switch off, and it would take the rest of the harness with it. A repo that
wants it fatal sets `failWhenStale`. A measurement taken at a commit the
repo no longer has is reported as its own state, distinct from merely old.
Toolchains this guard cannot parse are named rather than silently skipped.

The adapter was then verified against a real run of cargo-mutants 27.1.0
rather than shipped on documentation, and that caught four things
documentation alone would not have:

- The `outcomes` array **includes the baseline run**, whose `scenario` is
  the bare string `"Baseline"` while every mutant's is an object — so it is
  neither uniform nor one-entry-per-mutant. The top-level counters are the
  source of truth instead.
- A survivor's line is `span.start.line`, not a `line` field; `function.span`
  is the whole enclosing function.
- A **failed baseline still writes an `outcomes.json`**, full of zeroes.
  Read as counters that is indistinguishable from a clean sweep, and would
  publish a perfect score for a build that never ran.
- **`cargo mutants --check` exits 0 having measured nothing** — it only
  compiles mutants, filing every one as `Success` with nothing caught or
  missed. A gate trusting that exit reports a green mutation run.

All four are refused by name. The schema has also churned across releases
(`cargo_result` → `process_status`, `line` folded into a `function`
submessage plus `span`, the `failure` counter removed), so an
`outcomes.json` from a version this parser has not been read against is
refused rather than parsed on optimism. Scoring follows the convention
Stryker publishes — `detected = caught + timeout`, `valid` excludes
`unviable` — with timeouts recorded separately, since a timeout usually
means the limit is wrong rather than that a test did its job.

`#[mutants::skip]` joined `proof-guard`'s ratcheted hatch categories (along
with `mutants::exclude_re`, and matched loosely enough to catch the
`cfg_attr`-nested form the tool also honors — cargo-mutants emits no count
of skipped mutants anywhere, so grepping the source is the only way to see
them):
excusing a mutant is excusing a change no test has to notice, which is the
same move as excusing a proof obligation, and it should not be free to
sprinkle wherever this goes red.

The original design, for reference. In loop-mode TDD the same agent writes the code and its judge;
proof-guard's structural scans catch only the unambiguous `{ True }`
vacuities. Mutation score is the one metric that cannot be faked by merely
executing code. Dispatch by toolchain (cargo-mutants, mutmut, Stryker; for
Aiken a first-party operator set — negate comparisons, swap `&&`/`||`,
replace an arm body with `True` — as source-rewrite plus `aiken check` per
mutant, feasible because proof-guard already brace-parses Aiken).
`--measure` stamps `{score, survivors, headSha, ts}` into the baseline;
`--check` fails on a score below the ratcheted floor *or* a stamp more
than N commits stale — the plutus-budget honesty rule: unmeasured is loud,
never a silent pass. Expensive measurement runs ride the existing
wake/Routine layer off-session. Survivors are counted absolutely as well
as by ratio (deleting tests to shrink the mutant surface must not raise
the score), and a `mutants-allow` annotation is itself a proof-guard hatch
category, so allowlisting ratchets too.

**4d. The attack taxonomy as scaffolded obligations (route 0: never
specify the property).** For Cardano work, `/fluxpoint:init` on an Aiken
repo should scaffold the known validator vulnerability classes as named
property tests over `aiken/fuzz` — double satisfaction (count validation
tokens across *all* inputs and outputs), datum hijack (output address
checks), token-name confusion, unbounded-value — whose names *are*
spec-guard obligation ids. A Cardano campaign then starts with the attack
taxonomy already baselined as hashed, prover-checked obligations, and the
red-team agent audits the residue instead of re-deriving the taxonomy. The
assurance ladder maps onto the existing tiers cleanly — automated tooling
= exit-code gates in `--full`; in-depth audit = skeptic/panel tiers plus
the reviewer agents; full FV = proof against the Plutus metatheory — and
the attested chain (statement hash → prover exit → mutation score →
Evidence row) is reconstructible certification evidence rather than a
vibe.

*Extended off-chain in slice 25 (issue #81).* The taxonomy was written as
if the validator were the only thing with an attack surface. The builder
has its own enumerable list, and it is the one an autonomous caller
reaches: an unvalidated change address, a datum that does not survive
encode-then-decode, protocol parameters cached past an epoch boundary, a
replayable signed body, coin selection over a wallet an attacker grew, a
transaction whose redeemer set and its committing hash disagree,
collateral with no ceiling. The manifest now carries one taxonomy per
language and each gates only a repo that tracks its language, so neither
half shouts at a repo it does not apply to.

*Shipped in slice 21, as a manifest rather than generated Aiken.* A
scaffolded `.ak` file that does not compile would drop a red file into a
tree that was merely unspecified, and nothing here can compile Aiken to
prove otherwise, so the taxonomy ships as data: `templates/attack-taxonomy.json`
names eight classes (double satisfaction, datum hijack, token-name
confusion, unbounded value, staking-credential substitution, foreign
UTxOs, unbounded validity, arbitrary mint), each with the property to
state and the generator to fuzz it with. `/fluxpoint:init` copies it to
`.fluxpoint-attacks.json`, and `spec-guard.py --check` is red for every
class that has neither an Aiken test of that exact name nor a waiver in
the manifest's `waived` object with a reason of at least twenty
characters; a class specified as a unit test rather than a property over a
generator is noted. The manifest arms the gate on its own, without a
baseline, so an onboarded Aiken repo cannot report green with the
taxonomy unspecified. Once written, the tests are spec-guard obligations
and their signatures ratchet like any other.

**Spec-first campaign shape.** A canonical `WORK.verified.md`: a spec node
authors obligations (frozen via spec-guard, adversarially refuted by a
panel — once implementation cannot move the target, the spec *is* the
attack surface); the implementer `mutates` against them; a `prove:` gate
attests the prover's exit; red-team closes. The verification condition
becomes the Definition of Done before any implementation exists — "the
harness decides done" taken to its logical end. Stated honestly: formal
verification moves trust to the spec rather than eliminating it, which is
why the ratchet, the adversarial spec review, and the mutation floor are
the load-bearing pieces, not the prover invocation.

*Shipped as an opt-in prototype (issue #71):* `templates/WORK.verified.md`
carries the Propose-and-Review split — a `builder` that may not touch a
statement, a `prover` (`agents/prover.md`, contracted to `SliceV1`, a
mutator) that adds invariants, lemmas, decreases clauses and property tests
and may never remove an obligation, two independent gates because
`verifies` names one node, the prover's gate attested through
`prove:harness`, then `proof-audit` over the prover's diff and red-team.
The spec node and the adversarial spec panel are still to come, and the
split stays out of the default feature template until a Dafny-bearing
campaign shows it pays; on Aiken the prover's output reduces to property
tests, and the template says so.

## Part 5 — build order

Ordered by leverage over effort; every slice lands green through the
existing harness and each is independently shippable.

| # | Slice | Status |
|---|---|---|
| 1 | Compile-time `imports` resolution + embedding (`resolve_imports`, `DECISIONS_IMPORTED`, record-run dedup, re-decide invariant) | **shipped** with this document |
| 2 | `exec-attest.sh` + `.fluxpoint-gates.json` + record-run cross-check in warn mode | **shipped** |
| 3 | `memory.py` + record-run filing of findings/objections + `memory.seed` frontier seeding | **shipped** |
| 4 | `spec-guard.py` for Aiken + Dafny statements, wired into `templates/harness.sh --full` | **shipped** |
| 5 | `cex.py --ingest/--pin/--check` for `aiken check` output | **shipped** |
| 6 | Gate-authored Evidence rows + classed injection (replaced `--mint`; see Part 2) | **shipped** |
| 7 | Stop-gate branch-point dirtiness + baseline/trust-base modified-contract warnings | **shipped** |
| 8 | `decision.py` + PreCompact hook (distill gate behind `FPL_DISTILL=1`) | **shipped** |
| 9 | `mutation-guard.py` wrapping cargo-mutants, floor + staleness stamp | **shipped** |
| 10 | `ExecutionV1` + `verify: "prove:<gate>"` tier + TAMPERED-EXECUTION enforcement | **shipped** |
| 11 | `WORK.consolidate.md` + consolidation Routine; evidence `--replay` + graded injection | |
| 12 | Attack-taxonomy scaffolding in `/fluxpoint:init` for Aiken repos; `WORK.verified.md` | **shipped**: the template as an opt-in prototype in slice 20, the taxonomy as `.fluxpoint-attacks.json` enforced by spec-guard in slice 21 |
| 13 | Cross-repo federation (`.fluxpoint-federation.json`, org-scope lessons) | |
| 14 | `{{prev.<field>}}` projection (validated single-hop, bracket-emitted; the compile-clean/launch-dead trap closed) + `reduce` nodes (deterministic dedupe/rank/cut between agents, zero spawns, cuts named) | **shipped** |
| 15 | `onRed` as a closed registry, enforced on fan-out and discovery (a dead worker can halt; a round that lost any worker never reads as dry; a ceiling-declined spawn files SKIPPED and halts as BUDGET-EXHAUSTED, never NODE-DEAD) | **shipped** |
| 16 | Graph metrics: `spawned`/`planned`/`spent` in summaries, structured provenance data, `scripts/metrics.py`, scope-creep and serialization warnings (circuit 10) | **shipped** |
| 17 | Hygiene scan no longer flags the proof ratchet's OWN baseline — `.fluxpoint-proof-baseline.json` records a count for every marker the scan hunts, so `"lean.sorry": 0` (the evidence a repo is clean) read as a proof hole and turned the gate red | **shipped** |
| 18 | Hybrid recall (Part 3e): derived memory graph + pluggable embeddings + BM25 + PPR fused with weighted RRF; bi-temporal serving, stale-kill marks, relevance-ordered seed maps, SessionStart recall section, `/fluxpoint:recall` | **shipped** |
| 19 | Recall follow-ons (Part 3e): `memory.priors` refuter wiring with emission probe; decision nodes from run artifacts; `substrate-graph.mjs --json` + primitive ingestion; gemini provider; dark-launched per-prompt hook (`FPL_MEM_PROMPT=1`); `WORK.consolidate.md` (3d's template half) | **shipped** |
| 20 | Issues #62–#72 closed together (v1.36.0): relation gate `differential` / `bite` / `authority` / `--scan` (#62); Dafny in the counterexample ledger (#70); `ProofV1` + the `proof-audit` node in the feature campaign, `record-run` filing WEAKENED as `BLOCKED-PROOF` (#72); the `prover` role and opt-in `WORK.verified.md` (#71); the token estimate, `maxEstimatedTokens`, `cacheTtl`, effort-transition warnings and the estimate/profile instrumentation (#64–#67, the sweep itself still to run); `prompt-audit.py` as an advisory harness step (#68); the cached-prefix wording and the two thoroughness boosters retired (#69) | **shipped** |
| 21 | Part 4 off the roadmap (v1.38.0): spec-guard parses Lean, Coq, Isabelle, TLA+ theorems plus the invariants a TLC `.cfg` names, and Kani harnesses and contracts; `--axioms` records each headline theorem's assumption set from the prover's own listing and reds on a new one; DoD `— proof: <obligation id>` tails are enforced; the attack taxonomy ships as `templates/attack-taxonomy.json`, copied to `.fluxpoint-attacks.json` by init and red per unspecified class (4d, slice 12); Kani concrete playback and Apalache ITF traces join the counterexample ledger under the same pin discipline (3b); every new parser verified against the real toolchain (Lean 4.15.0, Coq 8.18.0, Dafny 4.9.1, Kani 0.67.0, Apalache 0.47.2) and the fixtures captured from those runs | **shipped** |
| 22 | Route 1 reaches off-chain (#78): `proof-guard.py` counts TypeScript escape hatches (`ts.any`, `ts.double_cast`, `ts.non_null`, `ts.ts_ignore`, `flags.types_off`), with strings stripped before counting and the `@ts-` directives read from the raw line; `@ts-expect-error` deliberately uncounted; plain JavaScript does not arm the ratchet. Also fixes the latent defect that made every category this plugin ever added a red gate for repos that armed before it | **shipped** |
| 23 | Blueprint conformance (#79): `blueprint-guard.py` decodes PlutusData and checks it against the CIP-57 schema `plutus.json` declares — `--scan` names every opaque schema, `--conform` checks hex against a purpose, `--check` runs the corpora in `.fluxpoint-blueprint.json` and reds on a refused value, a stale manifest or an unresolvable `$ref`. Stdlib-only CBOR reader verified against `cbor2` on 24 values; fixtures are real blueprints from the aiken compiler's own repository | **shipped** |
| 24 | Off-chain properties in the ledger (#80) and the seed sweep (#82): `cex.py` parses fast-check failures out of a vitest run — shrunk counterexample, seed and replay path — with TypeScript pins refusing `.skip`/`.todo`/`.fails`, and a plain assertion failure deliberately not minted as a counterexample; `--sweep` runs a rotating seed window at a raised `--max-success` off-session and stamps what it covered, with `--check` reporting staleness | **shipped** |
| 25 | The builder's own attack taxonomy (#81): `.fluxpoint-attacks.json` carries one taxonomy per language, the 1.38.0 single-object shape still read as one; seven TypeScript classes specified by a test title carrying the class id, with an example noted as weaker than a fast-check property; each taxonomy dormant in a repo that does not track its language, and waivers scoped so an Aiken waiver cannot excuse a builder class | **shipped** |

## Part 6 — named absences

Things the graph layer does not do, said here so their absence is a
decision rather than an oversight. Each names its trigger for being
built.

- **Per-node token spend.** The runtime meters the run (`budget.spent()`)
  and never a node, so the per-node `profile` in every summary carries
  the compiler's *estimate* beside a run-level `spent`. Build the per-node
  fold in `metrics.py` when the runtime exposes per-call usage; the
  effort sweep DESIGN-NOTES describes can start on run-level numbers.
- **Per-node wall-clock and critical-path latency.** The executor forbids
  `Date` in workflow scripts (nondeterminism breaks resume), so the
  emitted code cannot timestamp itself. The compiler warns about the
  shape that costs latency — adjacent top-level nodes with no declared
  dependency serialize — but the cost itself is unmeasured. Build when
  the runtime's journal exposes its own timestamps: record-run.py then
  folds them per node with no change to emitted code.
- **Runtime uncertainty-routed escalation.** The escalation-ladder shape
  is approximated in the skill (skeptic:1 pass → reduce → high-effort
  re-judge of survivors); an `escalateWhen` that routes one item by its
  own confidence mid-run is not an IR construct. Build when a campaign
  shows the two-stage approximation re-judging enough cheap survivors to
  matter; the `effort`/`model` plumbing it needs already exists.
- **Multi-parent joins — shipped.** `after` accepts multiple parents, for
  example `after: ["gate-engine", "gate-publisher"]`. Named lanes execute
  independent chains concurrently. See [lanes, replay and rejudging](skills/graph-engineering/SKILL.md#independent-lanes-replay-and-rejudging).
- **`foreach` over a produced list.** Fan-out ranges over static IR
  `lists` only; a planner node's output cannot become a work-list edge.
  The advisor–orchestrator shape passes the plan through `{{prev}}`
  instead. Multi-parent joins have shipped; dynamically produced work lists
  remain a separate unsupported shape.
- **Per-node retry.** Refused, not pending: recovery is memoized resume
  (the executor replaying completed `agent()` calls from the run journal,
  a different mechanism from the model's prompt cache) plus the once-only
  ledger, and an in-run retry loop would be a second failure policy hiding
  inside the first. A flaky node is repaired and resumed, not retried
  until it confesses.

Every proposal above follows the house idiom — closed field registries
with emission probes, append-only stores whose reads hard-fail on
corruption, deterministic scripts deciding green, incompleteness that
names itself — so each one strengthens the invariants it touches instead
of adding a parallel mechanism beside them.
