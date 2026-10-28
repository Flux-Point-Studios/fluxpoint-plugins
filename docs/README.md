# Documentation

Start with the [repository README](../README.md) for installation and the
[development setup guide](development-setup.md) to work on this repository.
The [Flux Point README](../plugins/fluxpoint/README.md) describes the engineering
harness; the [Substrate README](../plugins/substrate/README.md) describes workspace
discovery, manifests, delivered surfaces, memory lint and prose checks.

## Setup and daily use

| Task | Guide |
|---|---|
| Set up and test this checkout | [Development setup](development-setup.md), [contributing](../CONTRIBUTING.md) |
| Install and initialize a consuming repo | [Install](../README.md#install), [init command](../plugins/fluxpoint/commands/init.md) |
| Understand Claude Code and Codex support | [Runtimes](runtimes.md) |
| Specify implementation work | [Spec-first contract](spec-first.md), [packet format](../plugins/fluxpoint/references/spec-format.md), [grill-me](../plugins/fluxpoint/commands/grill-me.md) |
| Drive and finish a loop | [Loop engineering](../plugins/fluxpoint/skills/loop-engineering/SKILL.md) |
| Design a graph, lanes, joins and replay policies | [Graph engineering](../plugins/fluxpoint/skills/graph-engineering/SKILL.md) |
| Launch or resume with pinned artifacts | [Structured resume](../plugins/fluxpoint/references/structured-resume.md), [graph-run](../plugins/fluxpoint/commands/graph-run.md) |
| Diagnose a busy or unhealthy machine | [Machine health and watchdog](box-health.md) |
| Run long commands and manage cancellation | [Long-command lifecycle](../plugins/fluxpoint/references/longrun.md) |
| Inspect status, blockers and metrics | [Status command](../plugins/fluxpoint/commands/status.md) |

## Verification and evidence

| Task | Guide |
|---|---|
| Audit proof strength and assumptions | [Verified work](../plugins/fluxpoint/README.md#verified-work), [proof-audit](../plugins/fluxpoint/commands/proof-audit.md) |
| Configure pinned Lean/Z3 checks | [Blaster setup, trust and replay](../plugins/fluxpoint/references/blaster.md) |
| Recover an interrupted guard mutation | [Guard recovery](guard-recovery.md) |
| Check builder/validator relations | [Relations](../plugins/fluxpoint/README.md#relations-between-artifacts), [Cardano review](../plugins/fluxpoint/references/red-team-cardano.md) |
| Require independent repair evidence | [Repair campaign](../plugins/fluxpoint/templates/WORK.repair.md), [graph repair rules](../plugins/fluxpoint/skills/graph-engineering/SKILL.md) |
| Check production dependency pins | [Deployed gates](../plugins/fluxpoint/references/deployed-gates.md) |
| Bind CI results to commits and attempts | [Forge evidence, source pins and CI identities](../plugins/fluxpoint/references/forge-evidence.md), [fault reruns](../plugins/fluxpoint/references/fault-reruns.md) |
| Check captured provider shapes and counters | [Recordings](../plugins/fluxpoint/references/recordings.md) |
| Measure and tighten deterministic limits | [Metrics, Aiken costs and plateau detection](deterministic-metrics.md), [slice calibration](../plugins/fluxpoint/README.md#calibrating-predicted-gains) |
| Retrieve lessons and decisions | [Memory and recall](memory-recall.md), [recall command](../plugins/fluxpoint/commands/recall.md) |
| Work with credential paths | [Secret handling](../plugins/fluxpoint/skills/secret-handling/SKILL.md), [security policy](../SECURITY.md) |
| Route recognized mail sends through a declared module | [Substrate mail-sender gate](../plugins/substrate/README.md#mail-sender-gate) |

## Reference and maintenance

All command instructions live in [Flux Point commands](../plugins/fluxpoint/commands/)
and [Substrate commands](../plugins/substrate/commands/). Codex skill wrappers
read those same files. [Contracts](../plugins/fluxpoint/contracts/) are the
machine-readable result schemas; [templates](../plugins/fluxpoint/templates/)
are the shipped campaign and harness starters. Run a script with `--help` for
its accepted CLI options.

[Design notes](../plugins/fluxpoint/DESIGN-NOTES.md) and the
[roadmap](../plugins/fluxpoint/ROADMAP.md) preserve historical reasoning and
remaining gaps. Use the operational guides above for current instructions.
The [proof-strength ratchet](../plugins/fluxpoint/README.md) includes Python
typing suppressions and checker configuration; its module help lists categories.
The [initialization guide](../plugins/fluxpoint/commands/init.md) describes
Compact/Midnight attack classes and their property-test or waiver requirements.
The [documentation audit](documentation-audit.md) records the checked surfaces,
corrections and verification limits of the October 2026 review.

Recall corpus configuration, shared schema-specific indexes, detached refresh,
embedding consent, worker briefs and
shared worktree memory are described in [memory recall](memory-recall.md#corpus-configuration-and-safe-refresh).

[Compiled witness identity](witness-identity.md) documents plugin pins across
launch, resume, gate execution and attestation recording.

[Packet reference context](../plugins/fluxpoint/references/spec-format.md#packet-context-by-reference)
reduces repeated scope text while retaining the exact compiled lock identity.

