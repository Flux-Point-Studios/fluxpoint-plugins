# fluxpoint

The Flux Point engineering harness for coding agents; it loads on Claude
Code and on Codex. One Definition of Done, one Evidence discipline, two
drivers over the same contract.

**Loop mode** makes one agent's cycle programmable: session bootstrap,
per-edit verification, and a Stop-hook Definition-of-Done gate that blocks
the stop while `scripts/harness.sh --full` or the hygiene scan is red.

**Graph mode** makes the organization of agents programmable: a declarative
`graph-ir` block in `WORK.md` compiles — deterministically, no model in the
loop — into a Workflow script of typed-contract nodes with verification
tiers, so the spec cannot drift from what executes.

The rule both modes share: the agent never decides "done"; the harness
does.

## Layout

- `hooks/hooks.json` — one hook set for both runtimes: `SessionStart` state
  injection, `UserPromptSubmit` recall (dark by default), a `PreToolUse`
  credential gate on `Bash`, `PostToolUse` scoped verification on file
  writes and execution attestation on `Bash`, a `PreCompact` compaction
  gate, and the `Stop` Definition-of-Done gate.
- `scripts/` — the hooks and what they call.
  - Loop: `lib.sh`, `inject-state.sh`, `verify-changed.sh`, `dod-gate.sh`,
    `precompact.sh`, `prompt-recall.sh`, `evidence.py` (the gate authors
    its own Evidence row; this writes it and executes nothing),
    `decision.py` (Decisions rows with the `DecisionV1` floors enforced),
    `py.sh` (interpreter resolution and UTF-8 stdio).
  - Gates and ratchets: `exec-attest.sh` with `attest.py` (hook-minted exit
    codes for declared gates), `secret-guard.py` (credential gate),
    `proof-guard.py` (escape hatches), `spec-guard.py` (statements),
    `cex.py` (counterexample ledger for Aiken, Dafny, Kani, Apalache and
    fast-check, plus the off-session seed sweep),
    `mutation-guard.py` (mutation score), `seam-guard.py` (module mocks),
    `guard-guard.py` (named guards and the tests that hold them down),
    `pair-guard.py` (relations: co-change, parity, differential, bite and
    `--scan`), `plutus-budget.py` (on-chain size and execution units),
    `blueprint-guard.py` (the off-chain encoder against the CIP-57 schema),
    `recurrence-guard.py` (a lesson learned twice demands a gate).
  - Graph: `compile-graph.py`, `record-run.py`, `ledger.py` (once-only
    guard), `release.py`, `inbox.py` and `wake-check.sh` (the park layer),
    `metrics.py` (per-campaign rates).
  - Memory: `memory.py` (lessons), `recall.py` and `embedder.py` (the
    derived graph and hybrid retrieval).
  - `prompt-audit.py` (advisory prompt hygiene) and `migrate.py` (pre-1.0
    migration: plan, apply, finalize).
- `contracts/` — versioned schemas: `FindingsV1`, `VerdictV1`,
  `HarnessCheckV1`, `DesignV1`, `SliceV1`, `RedTeamV1`, `ProofV1`,
  `DecisionV1`, `LessonV1`, `ExecutionV1`, `TreeCheckV1`.
- `commands/` — `/fluxpoint:init`, `:status`, `:migrate`, `:red-team`,
  `:proof-audit`, `:recall`, `:graph-design`, `:graph-run`, `:graph-audit`,
  `:release`. On Codex each is reached through the matching
  `skills/fluxpoint-<name>/` skill.
- `agents/` — `red-team-reviewer` (adversarial diff review, `RedTeamV1`),
  `proof-auditor` (did the verification get weaker; `ProofV1`, a gate node
  in the feature campaign), `prover` (proof synthesis apart from program
  synthesis; `SliceV1`, a mutator), `graph-auditor` (semantic review of a
  campaign IR; prose, and outside the graph on purpose).
- `skills/` — `loop-engineering` (driver selection, conditions, the gate),
  `graph-engineering` (escalation rule, primitives, tiers, shapes),
  `secret-handling` (derive from a credential's path so it can be worked
  with and never read), and the `fluxpoint-*` Codex entry points, one per
  command.
- `templates/` — `harness.sh` (the repo-side contract), `WORK.md`,
  `WORK.feature.md`, `WORK.discovery.md`, `WORK.consolidate.md`,
  `WORK.verified.md` (opt-in: a `prover` node beside the builder),
  `WORK_PROMPT.md`, `loop.sh`, `attack-taxonomy.json` (the attack classes
  a repo must specify, eUTxO and off-chain builder, enforced by spec-guard
  once copied to `.fluxpoint-attacks.json`), `settings.snippet.json` for Claude Code
  and `codex.config.snippet.toml` for Codex.
- `tests/` — one suite per mechanism, all run by the repository's
  `scripts/harness.sh --full`: compiler invariants and field-effect probes,
  codegen injection, the Stop gate and hook wiring driven with both
  runtimes' payload shapes, attestation, every ratchet, the counterexample
  ledger, relations, the park layer, memory and recall, migration against
  pre-1.0 fixtures, the outer loop under both CLIs, and the documented
  claims the code has to keep.

## The two contracts

**Repo side:** `scripts/harness.sh` supporting `--changed <file>` (fast,
scoped) and `--full` (everything the DoD requires), exit 0 = green. The
gate stays dormant in repos that lack it.

**Spec side:** `WORK.md` — goal, `MODE`, Definition of Done, Plan (loop
slices), Campaign (the graph IR), Constraints, Merge policy, and one
Evidence table both modes append to.

## Invariants worth naming

- A node that writes to the tree may not certify its own work. Mark it
  `mutates: true`; a later node with `independent: true` must re-derive
  the verdict. The compiler refuses to build a graph that breaks this.
- Independence of context is enforceable; fidelity of execution needs the
  hook. `verify: "prove:<gate>"` makes a node cite the attestation its run
  produced — a citation that does not exist or disagrees files the run
  `TAMPERED-EXECUTION`, and citing nothing files it `INCOMPLETE` rather
  than accusing an executor that never touched the Bash tool.
- The DoD gate arms on two independent signals — the PostToolUse marker
  and dirtiness re-derived from `git` — because the marker cannot see
  source written through the Bash tool. Dirtiness is measured against the
  commit the session started from, so committing work mid-session
  is not a way to stop being judged; the hygiene scan uses the same
  baseline. It falls back to HEAD when that commit is not an ancestor of
  the current one (a rebase or branch switch), because a gate that fires
  spuriously gets switched off.
- The files that decide what green means — the harness, the proof and pair
  baselines, the gates and budget manifests, `package.json`, `Makefile` —
  are named in the green notice when this session changed any of them.
  A verdict is only as trustworthy as the contract that produced it.
- A gate's exit code is minted by a hook; no agent types it. Declare
  the deciding commands in `.fluxpoint-gates.json`; every run of one is
  recorded to `.claude/fluxpoint/attest.jsonl`, and `record-run.py`
  cross-checks any node that claims a gate exit. Only the exact declared
  invocation attests — `|| true` or a pipe reports a different exit and is
  credited to nothing — and a missing attestation is `UNATTESTED`, never
  a failure.
- Compiled `.graph.js` files are build output. Never hand-edit them; edit
  the IR and recompile.
- Budget is enforced twice and neither check is advisory: `maxNodes` is a
  compile-time ceiling priced at the worst case (discovery rounds
  included), and run-time floors stop verification and work fan-out
  separately. Anything declined is recorded `SKIPPED` and surfaces in
  Evidence as incomplete coverage.
- Calls are the wrong unit for the bill, so the compiler also estimates
  every graph in cold-input-token equivalents — prefix per call, priced
  as a cache read when the previous call shares its `(model, effort)` and
  the declared `cacheTtl` keeps it warm, work scaled by effort, the call
  weighted by model — and `budget.maxEstimatedTokens` is the ceiling on
  that estimate. The constants are stated assumptions in one place;
  `metrics.py` holds them to the runtime's own `spent`. Effort
  transitions between consecutive nodes are warned about as the cold
  prefills they are.
- Ending a discovery sweep on its round ceiling is logged
  `discovery INCOMPLETE, not exhausted` — stopping early and finishing are
  different claims.
- The Stop gate authors its own Evidence rows (`Source: gate`) and no agent
  writes that class. This is a review affordance without containment — `WORK.md`
  is skipped by the PostToolUse hook and the hygiene scan alike, so a row can
  still be edited; what changed is that doing so is now visible in a diff and
  that the bootstrap labels asserted rows as asserted.
- A lesson seeded from an earlier run is advisory: it reaches a finder's
  prompt, never the dedup set. A re-found item is judged again rather than
  dropped, because a finding that comes back is evidence the lesson went
  stale.
- Destructive migration is code. `migrate.py` separates
  plan/apply/finalize so nothing is deleted before the result is checked,
  and `--finalize` refuses when `WORK.md` carries fewer Evidence rows than
  the sources did.
- `budget.maxNodes` is counted at run time as well as compile time. Every
  emitted agent call goes through one `spawn()` helper, because the
  compile-time estimate leans on `expectItems`, which is a guess.
- **WORK.md is untrusted input.** The compiler turns it into JavaScript that
  is then executed, so every interpolated value is an injection surface.
  Free text goes through `js_str`/`js_template`; anything emitted as a JS
  *identifier* (node ids, `lists` keys) is constrained by `IDENT` at
  validation; halt literals are re-emitted from their parsed value rather
  than pasted from the source. `tests/security-test.py` compiles real
  payloads and executes the output to prove they stay inert.
- Every field the IR accepts must demonstrably change what the compiler
  produces. `tests/emission-test.py` probes each one and fails on a field
  that changes nothing, because the recurring defect here was never a wrong
  output — it was a silent one. Unknown fields are compile errors for the
  same reason.

## Relations between artifacts

Every gate above measures one artifact. The defects that cost the most are
relationships between two: an on-chain predicate tightened without its
off-chain builder, a migration and the schema it assumes, both ends of a
wire format. Each side passes its own tests — the suite is green *because*
each half is individually correct — and the pair is what breaks.

`scripts/pair-guard.py` checks declared pairs from a repo-owned
`.fluxpoint-pairs.json`, in two tiers. **Co-change** reports a diff that
moves one side and not the other; it is a smoke alarm and proves only that
somebody touched both files. **Parity** runs a declared command whose job
is to evaluate the two together, making the relation itself an exit code —
that is the real check, and `--list` names every pair that lacks one rather
than letting a half-checked relation read as covered. Only the consuming
repo can write parity vectors; the plugin supplies the slot.

A one-sided edit is often legitimate -- a paired file gets touched for a
reason the relation has nothing to do with -- and co-change had no way to
record that somebody checked. It does now: `.fluxpoint-pair-acks.json`, also
repo-owned, carries `{pair, source_sha, why}`. The ack is **content-addressed**
over the current bytes of the source files that tripped the alarm, so the next
edit to them re-opens it with nobody having to remember; a `why` under 60
characters is refused, because the ack's whole content is the claim that a
human read the mirror and found it unaffected. An ack that discharged nothing
is reported as spent rather than left to read as coverage. It clears
**co-change only** -- a failing parity is a broken relation and stays fatal.

Three more primitives came out of a seven-round review campaign on a mainnet
keeper, where every HIGH finding was a hand-written reader disagreeing with
the parser it claimed to mirror, and the parity that should have caught it
compared two identical strings:

- **`differential`** is the parity nobody has to hand-roll: a `generator`
  emits one input per line under `FPL_PAIR_SEED`/`FPL_PAIR_CASES`, `sourceRun`
  and `mirrorRun` read each one on stdin, and the two `(exit, stdout,
  stderr)` triples must be identical, bytes compared, error text and key
  order included, no normalisation. It runs inside `--check`; a generator
  that fails, emits too few cases, or emits one input repeated is refused
  rather than read as agreement, and a side that is killed or not found is
  reported as that, never as a divergence. `--seed`/`--cases` override the
  manifest so the line that reported a divergence reproduces it.
- **`bite`** makes a parity prove it can fail. `--verify` runs the checks
  intact, mutates the reader (`file`, `find`, `replace`, with the anchor
  required to match exactly once), requires the checks to go RED, and
  restores — reading the file back after the write and after the restore,
  because an anchor that misses is a no-op that reads as a survivor and a
  restore that did not restore corrupts every later measurement. An optional
  `compile` runs before and after each step with its exit printed on its own
  line, since a test runner that compiles in a global setup reports zero
  failures for a mutant that does not compile. `--check` and `--list` name a
  parity with no bite as unproven; `--verify` fails it.
- **`authority`** names the funds-moving side, so a divergence reads as *the
  reader diverged from the authority* rather than *they differ*, and a bite
  that would mutate the authority to prove a reader notices is refused.

And for the mirror nobody declared, **`--scan`** proposes manifest entries
from the same non-trivial error message thrown in two modules, overlapping
thrown-message sets, and a comment or docstring claiming to mirror or
delegate — JSON on stdout, evidence in each `why`, exit 0 always. It is a
suggestion list; the parity, differential and bite are the repo's to write.

## Blocked is a state

A node no agent can run — 2-of-3 signing, a third party's withdrawal, a
72h timelock — used to leave the engine two options: halt everything, or
drop it and continue with a `null`. Neither is "work the other branches".

Parking is a last resort: `release.whyNotAgent` must name what makes the
step impossible for an agent, and `graph-auditor` attacks that reason,
because a CLI, an API or a headless browser covers most of what feels
human-only. `actor: human` / `actor: third-party` emits no worker spawn.
Absent a release the node reports BLOCKED — but never empty: one advisor
runs first, contracted to `DecisionV1` so a bare "ask the operator" cannot
satisfy it, told to challenge `whyNotAgent` before accepting it and to
return the concrete automatable path if one exists, otherwise the
recommended course with the rejected alternatives and the strongest
objection to each. The run goes INCOMPLETE and continues, and dependents
inherit BLOCKED rather than a null that reads like a failure. `/fluxpoint:release`
validates the operator's proof against the node's `proofContract` and
refuses an adjective — the campaign resumes on that file, so it is not a
formality. Every block, expired `wake` deadline and refused confirmation
lands in an inbox that `/fluxpoint:status` leads with and SessionStart
counts, because a campaign that parks quietly is a worse failure than one
that halts loudly. Deliberately not a DAG scheduler.

## Verified work

A prover behind this harness is the best case for it: `aiken check`,
`dafny verify`, `lake build`, `coqc` give a crisp exit code. But exit 0 is
weaker than it looks — every prover ships a way to discharge a goal without
proving it, and a checker exits 0 on an assumed lemma exactly as on a
proved one.

- **The prover runs in `--full`.** Per-file checking on edit is feedback;
  the gate that decides done must invoke the prover over the project.
- **`scripts/proof-guard.py` ratchets escape hatches.** `--baseline`
  records `todo`/`expect` (Aiken), `assume`/`{:axiom}` (Dafny), `sorry`
  (Lean/Isabelle), `Admitted` (Coq), `#[verifier::external_body]` (Verus),
  `ASSUME` (TLA+), and verification-disabling CLI flags into a committed
  file. `--check` fails when a category rises. Falling is always allowed.
  TypeScript ratchets on the same argument: a type checker is a prover with
  a weak logic, and `as any`, `as unknown as T`, the `!` assertion,
  `@ts-ignore` and a tsconfig with `"strict": false` each discharge an
  obligation `tsc` had while it exits 0 regardless. Off-chain code is the
  surface an autonomous caller actually reaches, so it is gated rather than
  trusted. `@ts-expect-error` is deliberately uncounted, because it fails
  the build once the error it names is fixed and so cannot rot in place.
  A category this plugin adds after a repo armed is reported as new and
  left unratcheted until the next `--baseline`, so an upgrade never reds a
  diff nobody wrote.
- **`scripts/spec-guard.py` ratchets the statements.** Hatch counts police
  proof bodies; this hashes what is being proved, so a dropped `ensures`
  conjunct or a deleted property test is a red gate unless a Decisions row
  names the obligation id. Coverage: Aiken test and property signatures,
  Dafny `requires`/`ensures`/`invariant` clauses, Lean and Coq theorem
  statements, Isabelle lemma statements, TLA+ `THEOREM`s plus every
  `INVARIANT` and `PROPERTY` a TLC `.cfg` names bound to the operator it
  checks, and every Rust fn carrying a `kani::` attribute. Three more
  checks ride on the same scan: `--axioms` records the prover's own
  assumption listing for headline theorems (`#print axioms`, `Print
  Assumptions`, `dafny audit`) and reds on a new axiom in the dependency
  set, which closes laundering an assumption through a helper lemma; a
  checked `- [x]` line in the Definition of Done with a `— proof: <obligation
  id>` tail must name an obligation that exists and is unchanged; and
  `.fluxpoint-attacks.json`, copied from `templates/attack-taxonomy.json`
  by `/fluxpoint:init`, names the attack classes a repo must specify, one
  taxonomy per language: the eUTxO classes a validator answers for, and
  the builder classes the off-chain TypeScript answers for. It is red for
  every class with neither a test of that name nor a waiver with a reason,
  each taxonomy gates only a repo that tracks its language, and a waiver
  is scoped to its own taxonomy. A tracked language whose taxonomy is
  missing from the manifest is named `NO TAXONOMY` with the classes the
  template holds for it; that is a note until the manifest sets
  `"requireAllLanguages": true`, and a `"languages"` list names the halves
  a repo gates on purpose, printing any tracked language it leaves out as
  excluded by declaration. Where a prover is
  not on PATH the axiom audit says NOT RUN rather than reading clean.
- **`scripts/plutus-budget.py` gates submittability.** Correct and
  submittable are different properties and only one has a prover:
  `aiken check` is green on a validator too large to go on chain. It reads
  `compiledCode` out of the plutus.json blueprint, folds the entries a
  multi-purpose validator repeats, and fails on the protocol's `maxTxSize`
  unconditionally — that limit is a protocol fact. Headroom is the
  part you configure (`.fluxpoint-budget.json`), and `--params` takes real
  `cardano-cli query protocol-parameters` output so the constants here are a
  fallback rather than the authority. Execution units cannot be derived from
  a compiled artifact — they are a property of evaluating a script against a
  transaction — so measured values go in the same file and an absent
  measurement is reported as unmeasured rather than passed.
- **`scripts/blueprint-guard.py` holds the builder to the validator.** Every
  gate above judges the on-chain predicate. A validator proved correct still
  signs whatever the transaction builder constructs, and the builder is what
  an autonomous caller actually reaches, so a proof on one side of the wire
  guarantees nothing about the other. `plutus.json` is the artifact both
  sides can be held to: `aiken build` regenerates it from the validator, so
  it cannot drift, and CIP-57 already says what a legal datum and redeemer
  look like. `--conform` decodes PlutusData the builder produced and asks
  the blueprint whether the validator would recognise it; `--check` runs the
  corpora declared in `.fluxpoint-blueprint.json` and reds on a value the
  schema refuses, a manifest naming a validator the blueprint lacks, or a
  `$ref` that does not resolve. `--scan` names every schema that is opaque
  `Data`, because a validator taking untyped data has no specification to
  hold anyone to and the gate is blind there. A schema the checker cannot
  read is reported UNCHECKED and never as conformant.
- **`/fluxpoint:proof-audit`** runs the ratchet, checks the prover is
  actually in `--full`, then the `proof-auditor` agent for what a count
  cannot see: vacuity, specification drift, assumption laundering, tests
  that cannot fail, negative tests that fail for the wrong reason, unproved
  surface, on-chain budgets, solver `unknown` read as success.

It also counts two weakenings that add no hatch for a line scan to find:
Aiken tests that cannot fail (a body that is a bare boolean, a
self-comparison, or empty) and predicates that decide nothing (`fn
credential_matches(..) -> Bool { True }` — the call site still reads as a
checked conjunction). Both catch the same trade: swapping a `todo` for
`True` lowers `aiken.todo` to zero and looks like progress. Only
unambiguous cases are flagged, and a pre-existing one is absorbed by the
baseline rather than indicting the repo; a false positive would train
people to ignore the ratchet. The ratchet remains a floor: a
theorem that lost a conjunct, or a `fail` test that trips an earlier guard
than the one it is named for, still needs the semantic pass.

## Migrating from the split plugins

`/fluxpoint:migrate` folds `LOOP.md` and `GRAPH.md` into one `WORK.md`,
moves local state under `.claude/fluxpoint/`, and rewires settings. Until
then the hooks still honor `LOOP.md`, so nothing breaks mid-migration.

Requires `git` and Python 3, reached as either `python3` or `python` — the
latter is what a standard Windows install provides. Scripts are invoked
through `scripts/py.sh`, which resolves the name and pins UTF-8 stdio. `jq`
preferred, with a Python fallback built into the hooks. Graph execution
additionally needs Claude Code's Workflow tool; design, compile checks and
audits run on Codex as well.

Installation for both runtimes, the support matrix and the tuning
variables are in the repository README; what each runtime hands the hooks
is in `docs/runtimes.md`.
