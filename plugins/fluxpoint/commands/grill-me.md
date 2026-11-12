---
description: Draft and challenge a spec before implementation, with researched options, a model-selected baseline and evidence for every decision. The user can override defaults; genuine authorization boundaries remain blocked.
argument-hint: [goal or existing spec]
---

Build a requirement packet the implementer can work against. This is the
required first pass for a new implementation goal in loop or graph mode.
Claude invokes `/fluxpoint:grill-me`; Codex invokes `$fluxpoint-grill-me`.
Both use these instructions and the same files.

Resolve the plugin root from `CLAUDE_PLUGIN_ROOT`, then `PLUGIN_ROOT`, or
from this command's installed directory. Use `scripts/py.sh` for Python
commands. Read the current WORK.md, existing `.fluxpoint-spec.json`, relevant
code, tests and recorded decisions before proposing anything. For the packet
format, read `references/spec-format.md` under the plugin root.

## Draft answers before escalating

Name the existing asset the goal improves, the observable result, scope,
exclusions, constraints and the failure that would make the result unusable.
Investigate facts using available code, tools, memory and primary sources.
An unavailable fact is unknown; record what was attempted and how it affects
the recommendation. Never invent a measurement or label an estimate as one.

Map the material decisions and their dependencies. Fill each decision yourself:

- Two or more distinct options, including doing nothing when viable. Explain
  the cost in money, time, disk, risk or foreclosed work where relevant. Use
  `basis: measured`, `estimate` or `unknown`, with evidence for that basis.
- One named choice as the baseline. Explain why it beats the alternatives,
  and the strongest objection to every option, including the winner.
- Mark the decision `defaulted` unless the user actually selected it;
  `user-confirmed` requires a real instruction, never silence. Record both
  the choice and its provenance in `record.evidence`.
- Mark a genuinely unresolved authority boundary `blocked`. Still fill the
  recommendation and costs. No default authorizes spend, custody, destructive
  actions or external commitments. Check prior authorization before escalating.

Present the consequential defaults together as options, recommendation and
rationale, making it easy to override a decision by id. Do not interview the
user for facts you can obtain or make routine choices wait for confirmation.
With an asynchronous question tool, keep working on independent material while
optional changes are pending; allow a reasonable reply window before proceeding
under the stated baseline. Without it, show the baseline and proceed within
the authorized task. A required authorization stays pending until answered.

When the user overrides a decision, update it and revisit dependent decisions,
requirements and checks. Preserve unrelated choices. Do not reset discovery.

## Challenge the packet

Before locking, challenge the weakest assumptions and follow each affected
decision branch. Cover observable success, bad inputs, boundary conditions,
failure/recovery, interfaces, compatibility and the verification boundary as
relevant to this goal. Small changes need a small packet; add no irrelevant
decisions to meet a quota. Record each material challenge and its resolution.
An unresolved challenge affecting correctness remains a blocked decision.
An unknown fact need not block if a safe conditional or deny-by-default choice
resolves correctness. State that choice and its limits; escalate only the
remaining decision that actually requires the user.

Write `.fluxpoint-spec.json`. Every requirement has a stable id, a concrete
statement, decision ids, executable check ids and a counterexample that would
violate it. Draft the tests or formal obligations before implementation, run
them, and identify the expected RED behavior. The packet can be locked before
these checks pass; readiness is not completion.

Choose the check by the claim. Use tests for examples, properties for generated
input domains, models for state transitions, and proofs for suitable code or
models with explicit assumptions. Model/proof checks also need an executable
reachability or non-vacuity witness. Record bounds and trusted components in
`scope` and `assumptions`. Reuse the repo's Dafny, Lean, Coq, Kani, Apalache,
Aiken, property-test and relation-check tooling when it fits. A separate model
needs a conformance check against the implementation. A JSON contract, a panel
vote, an exit-zero wrapper or a test name alone proves no semantic property.

For each model/proof check, name its `obligations` using ids from
`spec-guard.py --scan`. The lock records their statement hashes. A supported,
tracked obligation must exist before locking; unsupported proof surfaces need
a scanner adapter before they can claim this binding. Check that the command
actually covers those named obligations and that the witness exercises their
domain. The hash detects statement drift; it does not prove correspondence.
Before a formal `--run`, review and arm `proof-guard.py --baseline`, then
re-lock the packet. The runner requires an armed escape-hatch ratchet and
rejects newly active unratcheted categories. A baseline records existing
assumptions; review those assumptions rather than treating them as proven.

Check adapters must return nonzero on unknown, timeout, missing dependencies,
zero checked obligations and incomplete exploration relative to the declared
scope. Never point a spec check at the full harness that calls it recursively.
Use a focused suite, checker or runtime probe. Inspect what it actually runs.

## Freeze and hand off

Run `bash "$ROOT/scripts/py.sh" specification.py --lock`, then `--check`.
Locking validates the packet and records spec, obligation and proof-baseline hashes;
it neither executes the checks nor grants approval. A blocked decision or
invalid reference must be resolved before implementation. No empty packet,
`--none`, fake assent or stale lock is a substitute.

A directory named in a check's arguments or in `harnessInputs` means the
tracked files under it. Stage a new check file before locking. While an
untracked file that Git does not ignore sits under such a directory, `--lock`
refuses and names it, and `--check` warns about it. Python's compiled caches
in `__pycache__` and a desktop's folder metadata do not count.

A re-lock keeps every path an earlier lock froze. When a recorded decision
removes a check file, take the file out of the check, then run
`--lock --retire <path> --because <decision id or reason>`. The lock records
the path, its last hash and the reason. Deleting the lock drops every frozen
path at once and keeps no record.

Add `SPEC: .fluxpoint-spec.json` to WORK.md. Ensure the repo's harness includes
the specification runner from the current template. Copying a plugin update
does not upgrade a harness previously copied into a consuming repo.

Give the implementer the packet, lock, requirement ids and expected failing
checks. Give the verifier the same frozen packet and require a real re-run.
The compiler embeds the packet in agent context; `specification.py --run` runs
its checks. Revisions to the spec or proof baseline invalidate the lock and
require an explicit, explained revision before continuing. Model defaults
remain changeable; they never become a user's instruction through repetition.

Report the chosen baselines, overrides or genuine blockers, the lock digest,
which checks are still RED, and the next loop slice or graph-design step.
The specification pass is complete when its valid packet is locked and the
expected RED checks are handed off. Implementation is complete only after the
required checks and runtime evidence pass.
