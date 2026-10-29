# Documentation audit — 2026-10-27

The main drift was in feature discovery and older status claims. Most advanced
features already had detailed instructions in command files, skills or plugin
references, but the root README and plugin inventory did not expose them.
Several guarantees also needed narrower wording.

This review covers the current working tree, including the Blaster integration
prepared in this task (Flux Point 1.48.0, Substrate 1.9.1). It is not a statement
that those changes have been committed, published or exercised in remote CI.

## Scope and method

The inventory included 71 existing Markdown files: repository documentation,
both plugin READMEs, command instructions, skills and Codex wrappers, agents,
references, templates, historical design records and the Aiken capture fixture.
The documentation index, development setup and this report are additions.

Claims were traced to manifests, hook wiring, script entry points and their
arguments, compiler field registries and emission rules, all 14 contract
schemas, campaign templates, regression suites and both CI configurations.
There are 11 Flux Point commands, five Flux Point agents, four Substrate
commands and 39 public Python CLI entry points in the audited tree. Internal
Python modules and shell/Node entry points were checked with their callers.
Local Markdown targets and heading links were checked separately.

## Corrections

| Surface | Verified drift and correction |
|---|---|
| Root README | Added discoverable links for lanes, joins, replay, structured resume, repairs, health, long commands, deployed pins, forge evidence, fault reruns, recordings, metrics and Blaster. |
| Plugin README | Completed command, agent, script, schema, workflow and template inventories; added graph/repair/operator guides. |
| Setup and install | Added a repository development guide, Python 3.9+ and Bash requirements, explicit Codex installation commands and correct settings-snippet paths. Existing settings must be merged. |
| Runtime contract | Distinguished supported adapter payloads from a live installation smoke test. Documented the Codex subagent fallback and its executor provenance. |
| Substrate | Documented the previously omitted mail-sender hook, its opt-in configuration and static-analysis limits; corrected the runtime guide's claim that its graph CLI reads the hook payload for root selection. |
| Stop and review gates | Documented bounded Stop blocks, strict `prove:<gate>` citations and the fact that loop-mode Stop does not independently consume semantic review verdicts. |
| Blueprint | Made the source-to-blueprint guarantee conditional on a fresh `aiken build` in the consuming harness. |
| Relations | Corrected the introduction to include differential checks alongside co-change and parity. |
| Command paths | Removed first-match recursive cache lookup from nine command guides and two campaign prompts; resolve the loaded plugin installation using runtime roots or the command's location. |
| Deployed gates | Added `--detach` to the example explicitly described as a background task. |
| CI coverage | Replaced claims that the default harness runs every test file with the actual split: wired regressions, separate native Blaster smoke, selected Windows suites and a real health sample. |
| Roadmap | Marked the v1.12 diagnosis as historical; corrected multi-parent joins, strict gate citations, counterexample backends and machine-evidence freshness. |
| Design notes | Marked early APIs as historical and pointed readers from the removed `verify: harness` tier to current `prove:<gate>` instructions. |

## Feature coverage

The following map groups the implementation by user-visible behavior. It also
provides the source and regression entry points for later documentation reviews.
Test filenames below live under `plugins/fluxpoint/tests/` unless specified.

| Implemented behavior | Current documentation | Implementation and regression evidence |
|---|---|---|
| Bootstrap, scoped checks, Stop, compaction and evidence | [Root README](../README.md), [loop engineering](../plugins/fluxpoint/skills/loop-engineering/SKILL.md) | `lib.sh`, hook scripts, `evidence.py`; `hooks-test.sh`, `gate-test.sh`, `gate-regression-test.py`, `evidence-test.sh`, `precompact-test.sh` |
| Requirement packets, decisions and input closure | [Spec-first](spec-first.md), [packet format](../plugins/fluxpoint/references/spec-format.md) | `specification.py`, `packet_inputs.py`, `decision.py`; `specification-test.py`, `packet-inputs-test.py`, `packet-projection-test.py`, `decision-test.sh` |
| Typed graphs, independence, budgets, lanes and joins | [Graph reference](../plugins/fluxpoint/skills/graph-engineering/SKILL.md) | `compile-graph.py`; `compile-test.py`, `emission-test.py`, `security-test.py`, `cost-test.py`, `graph-replay-test.py` |
| Structured launch, captured prefix, input/tree replay and rejudging | [Structured resume](../plugins/fluxpoint/references/structured-resume.md), [graph-run](../plugins/fluxpoint/commands/graph-run.md) | `resume.py`, `replay_memo.py`, `run_artifacts.py`, `record-run.py`; `captured-prefix-test.py`, `resume-test.py`, `replay-memo-test.py`, `launch-requirements-test.py`, `graph-replay-test.py`; optional actual Workflow acceptance `captured-prefix-live.py` with a scripted localhost provider |
| Repair coverage, native witnesses, historical migration, recurrence and unchecked commits | [Repair template](../plugins/fluxpoint/templates/WORK.repair.md), [graph reference](../plugins/fluxpoint/skills/graph-engineering/SKILL.md) | `repair-proof.py`, `repair-replay.py`, `repair_diff.py`, `repair_journal.py`, `repair_checks.py`, `unchecked-repairs.py`, `recurrence-guard.py`; `repair-proof-test.py`, `repair-origin-test.py`, `unchecked-repairs-test.py`, `recurring-class-test.py`, `recurrence-test.py` |
| Red-team, proof and graph reviews | [Red-team](../plugins/fluxpoint/commands/red-team.md), [proof-audit](../plugins/fluxpoint/commands/proof-audit.md), [graph-audit](../plugins/fluxpoint/commands/graph-audit.md) | Agents, workflows and typed contracts; `redteam-workflow-test.py`, `proof-verdict-test.py`, `audit-workflow-test.py` |
| Gate attestation, deployed pins and commit-bound CI evidence | [Deployed gates](../plugins/fluxpoint/references/deployed-gates.md), [forge evidence](../plugins/fluxpoint/references/forge-evidence.md), [fault reruns](../plugins/fluxpoint/references/fault-reruns.md) | `attest.py`, `record-run.py`; `attest-test.sh`, `deployed-test.py`, `forge-evidence-test.py`, `fault-attempts-test.py`, `forge-fault-race-test.py` |
| Long-command ownership, logs, waits and cancellation | [Long commands](../plugins/fluxpoint/references/longrun.md) | `longrun.py`, `background.py`, `process_tree.py`; `longrun-test.py`, `regex-wait-test.py` |
| Machine health, watchdog and durable inbox | [Machine health](box-health.md), [graph-run](../plugins/fluxpoint/commands/graph-run.md) | `box-health.py`, `watchdog.py`, `inbox.py`, `wake-check.sh`; `box-health-test.py`, `box-health-windows-test.py`, `park-test.py` |
| Proof strength, statements, axioms, taxonomy and counterexamples | [Verified work](../plugins/fluxpoint/README.md#verified-work), [proof-audit](../plugins/fluxpoint/commands/proof-audit.md) | `proof-guard.py`, `spec-guard.py`, `cex.py`; `proof-guard-test.sh`, `spec-guard-test.sh`, `cex-test.sh` |
| Optional Lean/SMT checks, trust and exact scalar replay | [Blaster setup](../plugins/fluxpoint/references/blaster.md) | `blaster.py`, bridge/model templates, `BlasterVerificationV1`; `blaster-test.py`, separate `blaster-live.py` |
| Artifact relations, mutation, seams, guards, blueprint and budgets | [Plugin README](../plugins/fluxpoint/README.md), [recordings](../plugins/fluxpoint/references/recordings.md) | `pair-guard.py`, `mutation-guard.py`, `seam-guard.py`, `guard-guard.py`, `blueprint-guard.py`, `plutus-budget.py`; corresponding pair, mutation, seam, guard, blueprint, budget and recordings suites |
| Deterministic measurements, tightening, plateau and calibration | [Metrics](deterministic-metrics.md), [slice calibration](../plugins/fluxpoint/README.md#calibrating-predicted-gains) | `metric-guard.py`, `loop-metric.py`, `slice-metric.py`, `aiken-metrics.py`, `metrics.py`, `sweep.py`; `metric-ratchet-test.py`, `aiken-metrics-test.py`, `slice-calibration-test.py`, `metrics-test.py`, `sweep-test.py` |
| Lessons, recall and optional embeddings | [Memory and recall](memory-recall.md), [recall](../plugins/fluxpoint/commands/recall.md) | `memory.py`, `recall.py`, `embedder.py`; `memory-test.py`, `recall-test.py`, `embedder-test.py` |
| Secrets, release, migration and prompt hygiene | [Secret handling](../plugins/fluxpoint/skills/secret-handling/SKILL.md), [release](../plugins/fluxpoint/commands/release.md), [migration](../plugins/fluxpoint/commands/migrate.md) | `secret-guard.py`, `release.py`, `migrate.py`, `prompt-audit.py`; corresponding secrets, park, migration and prompt audit suites |
| Substrate discovery, primitives, delivery, staleness, lint and mail-sender routing | [Substrate README](../plugins/substrate/README.md), [commands](../plugins/substrate/commands/) | Substrate Node scripts, including `send-chokepoint.mjs`, doctrine/writing templates and `plugins/substrate/tests/`, including `send-chokepoint.test.mjs` |

## Verification and limits

Validation completed in the Linux development environment:

- `bash scripts/harness.sh --full`: **180 passing steps, zero failures**,
  including the 24 documentation-claims checks, locked requirement packets,
  template compilation, hook regressions and Substrate suites.
- Compiler cost checks: **90 passed**, including every campaign template
  against its existing budget ceiling.
- Local Markdown links: **148 targets across 74 files**, zero missing files
  or heading anchors.
- `git diff --check` passed. The prose scan found no high-severity findings
  in the edited overview/setup guides. The full harness retained six existing
  advisory prompt findings; they do not fail the gate.

No production feature was added by this documentation pass. Historical records,
fixture provenance and quoted early APIs were preserved with explicit context.
The detailed spec, recordings, metrics, resume, forge, fault and memory guides
remain the primary explanations of their existing features.

This audit checks consistency with the repository. It does not establish remote
CI success, live hook delivery on every runtime version, sustained native
Windows load behavior, hermetic solver execution, kernel proof reconstruction
or conformance of a Cardano ledger model. Blaster's scope and solver trust remain
explicit. Existing consuming repositories must deliberately update copied
harnesses to adopt newer gates; installing a plugin update does not rewrite them.

Compiler execution cleanup regressions are exercised by `plugins/fluxpoint/tests/execution-cleanup-test.py`: parked descendants, unique wait records, reducer ancestry, probe validation on emission, irreversible prompt reuse, await argument order, local citation commit identity and pre-start journal failures.

`harness-dirt-test.py` exercises attestation after a tracked graph STATUS update, inserted Evidence/Decisions rows, untracked and tracked compiled scripts, source/config/IR drift and a dirty linked worktree. The committed graph remains the content boundary; existing rows and prompts are never exempted.
