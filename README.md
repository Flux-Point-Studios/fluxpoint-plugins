# Flux Point plugins

Two plugins for coding agents, packaged for Claude Code and Codex from one
repository.

- **fluxpoint** is an engineering harness. It makes "done" a deterministic
  check: every session runs against a Definition-of-Done gate, the gate
  decides, and the agent's self-report carries no weight.
- **substrate** is a registry for multi-repo workspaces. Each repo declares
  its reusable primitives, a generator compiles them into one graph, and
  every session starts with that graph and its staleness alarms in context.

Both plugins load unchanged on Claude Code and on Codex. The two runtimes
share the hook contract, the skill format and the plugin layout, so one tree
serves both. The differences are listed under [Runtime support](#runtime-support)
and explained in [docs/runtimes.md](docs/runtimes.md).

## Requirements

- `git`, and Python 3 reachable as `python3` or `python`.
- Node 18 or newer for substrate.
- `jq` when available; the hooks fall back to Python without it.
- Claude Code with plugin support, or Codex with plugin support.
- Graph execution (`/fluxpoint:graph-run`) needs Claude Code's Workflow tool.

## Install

Once per machine.

Claude Code:

```
/plugin marketplace add Flux-Point-Studios/fluxpoint-plugins
/plugin install fluxpoint@fluxpoint
/plugin install substrate@fluxpoint
```

Codex:

```
codex plugin marketplace add Flux-Point-Studios/fluxpoint-plugins
```

Then enable `fluxpoint@fluxpoint` and `substrate@fluxpoint` from
`codex /plugins`, or per repository as below.

Once per repository, so every session loads the plugin without a prompt:
commit `templates/settings.snippet.json` into `.claude/settings.json` for
Claude Code, and `templates/codex.config.snippet.toml` into
`.codex/config.toml` for Codex. `/fluxpoint:init` writes whichever the
running agent needs. On Claude Code, `forcedPlugins` in managed settings
covers CI and containers.

## Quick start

```
/fluxpoint:init <one-line goal>      # Claude Code
$fluxpoint-init <one-line goal>      # Codex
```

This copies the harness contract, `WORK.md`, `WORK_PROMPT.md` and
`scripts/loop.sh` into the repo, wires the ignore file and the runtime's
settings, then tailors `scripts/harness.sh` to the repo's stack and iterates
until `--full` exits 0.

From then on every session boots with the branch state, the last harness
verdict and the head of `WORK.md` in context, every edit runs the scoped
checks, and no session ends while the harness is red.

The repo-side contract is one file, `scripts/harness.sh`, with two modes:
`--changed <file>` for fast scoped checks and `--full` for everything the
Definition of Done requires. Exit 0 is green. A repo without the file leaves
the gate dormant.

## What fluxpoint enforces

| Layer | Hook | What happens |
|---|---|---|
| Bootstrap | `SessionStart` | Injects branch state, the last harness verdict, open blockers, recalled context and the head of `WORK.md` on every start, resume, clear and compaction. |
| Per-edit checks | `PostToolUse` on file writes | Runs `scripts/harness.sh --changed <file>`; a red result is fed back for correction at the edit. |
| Credential gate | `PreToolUse` on `Bash` | Refuses a command whose output would be a declared credential file, and permits the same path handed to code that emits public derivations. Dormant without `.fluxpoint-secrets.json`. |
| Attestation | `PostToolUse` on `Bash` | Records the runtime's own exit code for every command declared in `.fluxpoint-gates.json`, so a gate result cannot be typed by an agent. |
| Definition-of-Done gate | `Stop` | When code changed this session, runs `scripts/harness.sh --full` plus a hygiene scan for TODO, FIXME, skipped tests and similar markers. Red blocks the stop, up to `FPL_MAX_BLOCKS` times, then yields with a checkpoint notice. The gate records its own Evidence row. |
| Compaction gate | `PreCompact` | Blocks one compaction when code changed and nothing durable was written, so the reasoning gets written down before the transcript is summarized. |
| Ratchets | inside `--full` | Escape hatches in proof languages and in TypeScript, theorem statements, the assumption set each headline theorem depends on, mutation score, module mocks and named guards may only move in the safe direction against committed baselines. |
| Counterexamples | inside `--full` | Shrunk failing inputs from `aiken check`, `dafny verify`, `cargo kani` (concrete playback), `apalache-mc` (ITF traces) and fast-check (off-chain property tests) are recorded, and each must be pinned to a regression test. A wide sweep over rotating seeds runs off-session, because the gate pins one seed so a shrink stays reproducible. |
| Specification floor | inside `--full` | A checked Definition-of-Done line citing an obligation id must name one that exists and still says what was recorded. On an Aiken repo, every eUTxO attack class in `.fluxpoint-attacks.json` needs a property test of that name or a waiver carrying a reason. A language the repo tracks that the manifest has no taxonomy for is named on every run, and fails once the manifest sets `requireAllLanguages`. |
| Relations | inside `--full` | Declared artifact pairs are checked for co-change, parity and differential agreement. |
| Blueprint conformance | inside `--full` | PlutusData the off-chain builder produced is decoded and checked against the CIP-57 schema the validator declares in `plutus.json`, so a proof on one side of the wire is not read as a guarantee about the other. |
| Review | agents | `red-team-reviewer`, `proof-auditor` and `graph-auditor` end in a typed verdict that the gate consumes. |

The mechanisms behind each row, the invariants they hold and their
limitations are documented in
[plugins/fluxpoint/README.md](plugins/fluxpoint/README.md).

## Graph mode

Implementation starts with `/fluxpoint:grill-me` on Claude Code or
`$fluxpoint-grill-me` on Codex. The model researches the decisions, supplies
options with costs, chooses a baseline and explains its evidence. The user
can override those defaults. The resulting requirement packet is locked
before implementation; each requirement names executable checks.

Mutating graph compilation requires that packet, and newly initialized
loops run its checks through the harness. Existing copied harnesses need
an explicit update. See [the spec-first contract](docs/spec-first.md) and
[packet format](plugins/fluxpoint/references/spec-format.md). Tests, bounded
model checks and proofs retain their declared scope and assumptions.

A campaign is declared as a `graph-ir` block in `WORK.md`: nodes with named
contracts, edges, a verification tier per node and a budget ceiling.
`scripts/compile-graph.py` compiles it deterministically into a Workflow
script and rejects unsound graphs at compile time, including any node that
writes to the tree and certifies its own work. `/fluxpoint:graph-design`
authors and audits the IR; `/fluxpoint:graph-run` compiles, executes and
records provenance. Execution needs Claude Code's Workflow tool. Design,
compile checks and audits run on either runtime.

## Drive a loop

Interactive convergence on Claude Code uses `/goal`; in-session grinding
uses `/loop`. The unattended outer loop runs a fresh agent context per
iteration on either runtime:

```
scripts/loop.sh                                   # Claude Code, attended
AGENT_CLI=codex scripts/loop.sh                   # Codex
MAX_ITER=50 PERMISSION_ARGS="--dangerously-skip-permissions" scripts/loop.sh
                                                  # sandboxed container only
```

Halt an outer loop at any time with `touch .claude/fluxpoint/STOP`. Every
driver works the per-slice contract in `WORK_PROMPT.md`, and a slice ends
merged and cleaned, or explicitly parked. Merge authority is decided once
per repo in the Merge policy section of `WORK.md`.

On Claude Code, cloud Routines can hold standing guardrails such as a nightly
`harness.sh --full` that opens an issue on red. Routines run under your
identity, so keep them to guardrail jobs with no key material.

## Runtime support

| Capability | Claude Code | Codex |
|---|---|---|
| Install | `/plugin marketplace add` and `/plugin install` | `codex plugin marketplace add`, then enable |
| Per-repo enablement | `.claude/settings.json` | `.codex/config.toml` |
| Session bootstrap, per-edit checks, Stop gate, compaction gate, credential gate | yes | yes |
| Attestation of gate runs | passes only; the runtime's PostToolUse does not fire on failure | passes and failures |
| Commands | `/fluxpoint:<name>`, `/substrate:<name>` | `$fluxpoint-<name>`, `$substrate-<name>` |
| Skills | yes | yes |
| Review agents | native subagents | the agent file is handed to a Codex subagent, or run inline |
| Graph execution | Workflow tool | not available; design, compile-check and audit work |
| Outer loop | `claude -p` | `codex exec` |
| In-session drivers (`/goal`, `/loop`, Routines) | yes | no equivalent |
| Memory lint (substrate) | reads the Claude Code memory directory | silent; Codex keeps no such directory |

The hook scripts, state directory (`.claude/fluxpoint/`) and manifests are
identical on both runtimes. [docs/runtimes.md](docs/runtimes.md) records
what each runtime hands the hooks and how the adapter handles the
differences.

## Tuning

| Variable | Default | Meaning |
|---|---|---|
| `FPL_DISABLE=1` | off | Kill switch: every hook becomes a no-op. |
| `FPL_MAX_BLOCKS` | 3 | Consecutive Stop blocks before the gate yields with a checkpoint. |
| `FPL_GATE_TIMEOUT` | 540 | Seconds the Stop gate gives the harness. The hook ceiling is 600. |
| `FPL_HARNESS_ARGS` | `--full` | The harness invocation the Stop gate runs, for repos whose full suite cannot finish inside the ceiling. |
| `FPL_ALLOW_MISSING_GATES=1` | off | Accept a declared gate whose script is not installed. Without it the harness is red, because a check that did not run must not read as one that passed. |
| `FPL_COMPACT_BLOCK=0` | on | Demote the compaction gate to a warning. |
| `FPL_DISTILL=1` | off | The Stop gate also asks for a Decisions row when code changed and nothing was written down. |
| `FPL_RECALL_INJECT=0` | on | Drop the recalled-context section from the session bootstrap. |
| `FPL_MEM_PROMPT=1` | off | Per-prompt recall on `UserPromptSubmit`. |
| `FPL_EMBEDDER`, `FPL_EMBED_MODEL`, `FPL_EMBED_DIMS` | by API key | Embedding provider for recall: `voyage`, `openai`, `gemini` or `none`. Keyless is a supported mode. |
| `FPL_MEMORY_INDEX=0`, `FPL_MEMORY_EMBED=0` | on | Skip the index rebuild, or only the embedding step, after a recorded run. |
| `FPL_PLUGIN_ROOT` | unset | Where the harness resolves gate scripts from. The runtime's own `CLAUDE_PLUGIN_ROOT` is used otherwise, then the install caches. |
| `FPL_PY` | auto | The Python interpreter to use. |
| `FPL_AIKEN_SEED` | 1 | Fuzzer seed for `aiken check` in the scaffolded harness, so a counterexample shrinks the same way each run. |
| `FPL_DAFNY_ARGS` | empty | Extra arguments for `dafny verify`, such as `--extract-counterexample`. |
| `FPL_PROTOCOL_PARAMS` | unset | A `cardano-cli query protocol-parameters` file for the on-chain budget gate. |
| `FPL_PAIR_SEED`, `FPL_PAIR_CASES` | from the manifest | Seed and case count for a relation's differential generator. |
| `FPL_PAIR_AGAINST` | session base | The commit the co-change tier diffs against. |
| `.fluxpoint-hygiene-ignore` | absent | One glob per line, excluded from the hygiene scan only. |
| `AGENT_CLI` | `claude` | Outer loop driver: `claude` or `codex`. |
| `MAX_ITER`, `MAX_TURNS` | 25, 40 | Outer loop budgets; `MAX_TURNS` applies to Claude Code. |
| `PERMISSION_ARGS`, `CODEX_ARGS` | see `loop.sh` | Outer loop permission flags per driver. |

## Memory recall

Lessons, decisions, counterexamples and declared primitives are compiled
into a derived knowledge graph and served through hybrid retrieval: BM25,
optional API embeddings and personalized PageRank, fused by reciprocal rank.
The session bootstrap injects the top results for the work at hand;
`/fluxpoint:recall` serves people. The index is a projection of stores that
already exist, with no new dependency and no daemon. The design, its
evidence and what it refuses to do are in
[docs/memory-recall.md](docs/memory-recall.md).

## Verified work

A prover exits 0 on an assumed lemma exactly as on a proved one, so the
scaffolded harness runs the prover in `--full` and four ratchets hold the
proof surface: escape hatches, theorem statements, the assumptions each
headline theorem depends on, and the on-chain budget for Cardano validators.
Statements are parsed for Aiken, Dafny, Lean, Coq, Isabelle, TLA+ (with the
invariants a TLC config names) and Kani. Escape hatches are counted in those
languages and in TypeScript, where `as any` is the off-chain `sorry`: it
discharges an obligation the type checker had, `tsc` exits 0 either way, and
that layer is the one an autonomous caller actually reaches.
The assumption sets come from the
prover itself — `#print axioms`, `Print Assumptions`, `dafny audit` — so an
axiom laundered in through a helper lemma is red while every hatch count
holds flat; where the toolchain is absent the audit says so and never reads
clean. Two further claims are gated on the same scan: a checked
Definition-of-Done line citing an obligation id, and every eUTxO attack
class an Aiken repo has left unspecified. `/fluxpoint:proof-audit` adds the
semantic pass a counter cannot do.

None of that reaches the other side of the wire. A validator proved correct
still signs whatever the transaction builder constructs, and the builder is
the caller an autonomous agent actually reaches. `blueprint-guard.py`
decodes the PlutusData that builder produced and checks it against the
CIP-57 schema the validator declares in `plutus.json`, which `aiken build`
regenerates from the validator itself. Supported provers and the details
are in
[plugins/fluxpoint/README.md](plugins/fluxpoint/README.md#verified-work).

## Security

- Plugins execute code with the user's privileges through hooks. Treat this
  repository as production infrastructure: protected default branch,
  required review, signed commits.
- Unattended loops belong in a sandboxed container with allow-listed egress
  and no path to key material. Harness exercises settle on preview or
  preprod networks.
- The hygiene scan covers uncommitted and untracked code. Committed history
  is CI's job; run the same harness there.
- A green produced while `scripts/harness.sh` or a baseline file was itself
  modified is reported as such, because a verdict is only as trustworthy as
  the contract that produced it.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Migrating from the split plugins

Repos onboarded before 1.0 carry `LOOP.md`, `GRAPH.md` and two state
directories. `/fluxpoint:migrate` folds them into one `WORK.md`, keeping
every Evidence row, and verifies the harness is still green before removing
anything. Until it runs, the hooks still honor `LOOP.md`.

## Repository layout

```
.claude-plugin/marketplace.json     Claude Code marketplace
.agents/plugins/marketplace.json    Codex marketplace
docs/                               runtimes.md, memory-recall.md
scripts/harness.sh                  this repo's own Definition of Done
plugins/fluxpoint/
├── .claude-plugin/plugin.json      Claude Code manifest
├── plugin.json                     portable manifest (Codex)
├── hooks/hooks.json                one hook set for both runtimes
├── scripts/                        hook scripts, compiler, ratchets, recall
├── contracts/                      versioned JSON schemas
├── commands/                       /fluxpoint:* commands
├── skills/                         loop-engineering, graph-engineering,
│                                   secret-handling, and fluxpoint-* Codex
│                                   entry points for each command
├── agents/                         review and prover agents
├── templates/                      harness.sh, WORK*.md, loop.sh, settings
├── tests/                          the suites scripts/harness.sh runs
├── README.md, DESIGN-NOTES.md, ROADMAP.md
plugins/substrate/                  same shape: scripts/, commands/, skills/,
                                    templates/, tests/, README.md
```

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

`scripts/harness.sh --full` is the Definition of Done for this repository,
and CI runs it on every push and pull request. [CONTRIBUTING.md](CONTRIBUTING.md)
covers the checks, versioning and the conventions the tree follows.
