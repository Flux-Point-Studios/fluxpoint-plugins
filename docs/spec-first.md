# Spec-first engineering

Status: implementation contract, 2026-09-11.

The asset is the existing WORK.md compiler and harness. A graph that compiles
establishes orchestration structure; it does not establish that its programs
satisfy the user's requirements. Add a decision and requirement boundary before
implementation, then reuse the existing verifier, ratchets and runtime evidence.

## Decisions

| Decision | Options and cost | Baseline | Rationale |
|---|---|---|---|
| Specification | Advisory prose (no enforcement); locked JSON beside WORK.md (one manifest and lock); a new specification language (new parser and toolchain) | Locked JSON | The repo already has Python validators, typed decisions and a deterministic compiler. |
| Interview | Wait on every answer (user effort for routine choices); model fills decisions with overrides (model research and review); skip discovery (unstated assumptions) | Model fills decisions | The user explicitly wants a baseline for every question and escalation only for decisions requiring their authority. |
| Assurance | Tests everywhere (limited exploration); proof everywhere (requires suitable models and toolchains); a declared method per requirement (review each boundary) | Declared method | Existing integrations cover several methods, but a property test is not a universal proof. |
| Compatibility | Break every old loop; preserve existing loops until initialized while requiring specs at the mutating graph CLI | Preserve old loops; gate mutating graph compilation | Existing installations carry copied harnesses. Updating a plugin cannot rewrite them. New init, graph-design and loop instructions require the packet. |

Costs above describe concrete work and risk, not measured time or price estimates.

## Contract

1. Run the grill-me decision pass before implementing a new goal. Research
   answerable facts. Each decision records alternatives, their costs and evidence
   basis, a chosen baseline, rationale, dependencies and whether the choice is
   defaulted, user-confirmed or blocked. Defaulted never means user consent.
2. Write `.fluxpoint-spec.json`. It names the goal, compounded asset, scope,
   exclusions, decisions, requirements, executable checks and resolved challenges.
   Each requirement names decision ids, check ids and a concrete counterexample.
   All references resolve, ids are unique, decision dependencies are acyclic,
   evidence is nonempty, and a blocked decision prevents locking.
3. `specification.py --lock` validates and hashes the packet and any existing
   proof baseline with LF line endings into `.fluxpoint-spec-lock.json`. It also
   binds named obligations to statement hashes from spec-guard and pins JSON
   archives reached by complete text references. References resolve into binding
   text before nodes receive the packet, with the source retained as `scopeRef`.
   The lock separately records check files and optional `harnessInputs` as a
   harness map outside the specification identity. This is a reviewable freeze,
   not an authorization grant or an adversarial security boundary. It runs no
   checks, allowing the tests to be RED before implementation.
4. `--check` rejects absent, malformed, stale or blocked packets. `--run` also
   executes every declared check without a shell, from the repository root,
   with its timeout. Missing executables, errors, nonzero exits and timeouts are
   failures. Checks are noninteractive and receive EOF on stdin. Each check
   runs in a fresh POSIX process group or Windows Job Object; descendants in
   that group/job are terminated on timeout and on early parent exit before
   the next check. Windows assigns a gated launcher before starting the real
   command and verifies the job drains. Cleanup errors stop remaining checks.
   A passing command establishes its declared check only.
5. Checks distinguish `test`, `property`, `model`, `proof` and `runtime`.
   Every check declares scope and assumptions; model/proof checks name tracked
   obligations and execute a non-vacuity witness command. Unsupported proof
   surfaces need a scanner adapter. Adapters must turn unknown, timeout, no
   obligations checked and incomplete exploration into nonzero exits. Naming
   a command `proof` cannot make it a proof; semantic review must inspect it.
   Formal execution also requires the escape-hatch ratchet to be armed and
   every active category ratcheted. A draft can lock before arming; re-lock
   after reviewing and arming the baseline, then run the actual proof checks.
6. Mutating or irreversible graphs require a locked packet at compilation.
   Embed the packet in their node context and its digest in run summaries.
   Recording such a run checks the digest again. Read-only discovery can run
   without a packet; an existing packet must still be valid. Harness-only fixes
   use `--lock-harness --gate <name>` after an attested pass for that declared
   command at HEAD. This changes no spec identity or compiled bytes. Run records
   and Evidence name the current harness digest and paths differing from its lock.
7. Newly scaffolded WORK.md files declare `SPEC: .fluxpoint-spec.json`.
   The scaffolded harness requires the spec runner when this marker or either
   spec artifact exists. Existing loops without any marker remain legacy;
   init/migration guidance must disclose that upgrading copied files is required.
8. The existing statement ratchet must reject a mere mention of an obligation
   in Decisions. Legacy exceptions require an actual decision table row. Locked
   spec campaigns accept no prose exception; deliberate changes re-lock and
   receive a visible review diff. The proof baseline hash detects rebaselining.
9. Scaffolded formal-tool invocations fail when their detected project requires
   a missing tool. Proof integration availability is not proof execution.
10. Claude uses `/fluxpoint:grill-me`; Codex uses `$fluxpoint-grill-me`.
    One canonical command supplies both. The protocol continues independent
    work while optional changes are pending, and never treats silence as assent
    for money, custody, destructive operations or external commitments.
11. The token estimate and `maxEstimatedTokens` ceiling include the packet,
    identity and preamble at every worker, refuter and advisor call. Price
    this input conservatively at one token per ASCII-serialized byte, apply
    the existing model multiplier, and assume no cache discount for it.
    Actual tree sentinels and deterministic reducers receive no packet.
    This is a stated estimate, not a measured tokenizer count or a context-limit guarantee.
    Template ceilings include packet headroom; recompute against each goal's
    actual locked packet before launching. The starter, consolidation, discovery,
    feature, verified and repair ceilings are 350,000, 600,000, 5,000,000,
    2,500,000, 2,250,000 and 13,000,000 estimated tokens, respectively.

## Implementation validation

Failing tests precede production changes. Exercise the actual CLI and emitted
JavaScript, plus the harness from a temporary git repository. Cover absent and
stale specs, missing references, cycles, blocked decisions, absent cost evidence,
failing and timed-out commands, proof witnesses, missing tools, statement
exception laundering, emitted spec identity and stale run recording. Run the full
repository harness and validate both plugin manifests.

The skill must also survive these review scenarios: routine design with no user
reply; an explicit override; inaccessible facts recorded as unknown; a genuine
authorization boundary; a low-risk non-code task; a claimed proof with no checker.

## Foundations and limits

The upstream [grilling skill](https://github.com/mattpocock/skills/blob/main/skills/productivity/grilling/SKILL.md)
organizes dependent decisions and recommends answers. This adaptation uses the
user's delegated-decision doctrine and does not copy the upstream text or require
a user answer for every routine choice.

[Dafny](https://dafny.org/dafny/DafnyRef/DafnyRef) verifies programs against
specified contracts. [Kani](https://model-checking.github.io/kani/tutorial-loop-unwinding.html)
requires attention to unwinding and completeness. [fast-check model tests](https://fast-check.dev/docs/advanced/model-based-testing/)
exercise implementations against simpler models and shrink failing scenarios.
Reuse the repo's existing integrations; this change installs no new prover and
does not claim a model is equivalent to production code without conformance checks.

The packet cannot prove the user's intent was captured correctly. Independent
review, reachability witnesses, negative cases and actual runtime exercises still
matter. An agent with write access to the spec, lock, compiler and harness can
change all four. CI and review of those diffs remain the external trust boundary.

Parsing and hashing use the same packet byte snapshot, so a concurrent
replacement cannot stamp an old parsed packet with a new packet's digest.
Process cleanup uses [Python process groups](https://docs.python.org/3/library/subprocess.html)
and [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
It is not a sandbox: POSIX programs can deliberately leave the group with
setsid, and service/remote jobs live outside these local boundaries. Checks
must not detach such work; use externally managed containment when required.
