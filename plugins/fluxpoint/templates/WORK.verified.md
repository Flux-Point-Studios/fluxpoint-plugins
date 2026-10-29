# GRAPH: ship one verified slice, program and proof synthesized apart

STATUS: DESIGN
MODE: graph

Fifth canonical campaign, and an opt-in prototype: the builder writes the
executable logic, a separate `prover` node writes the invariants, lemmas,
termination arguments and property tests that make it verify, and each is
gated by a node that did not write it before the proof-auditor and red-team
close. The split exists because an agent that writes the loop and its
invariant in one breath has an obvious incentive to weaken the specification
until the code it already wrote verifies; `spec-guard.py` catches that after
the fact, and a prover that may add obligations and never remove them
refuses it at the source.

Adopt it on a repo with a real proof surface. The Propose-and-Review
results this shape comes from are Dafny results, where invariant synthesis
is the bottleneck; Aiken validators are mostly straight-line predicate
logic, so on Aiken the prover's whole output is property tests over
`aiken/fuzz`, and the split may earn little. Prototype here first; move
the `prove` node into `WORK.feature.md` only when a campaign shows it paid.

Requires `.fluxpoint-gates.json` declaring `harness` (the `gate-proof` node
attests the prover's exit through `verify: prove:harness`), and
`scripts/harness.sh --full` invoking the prover. Copy over `WORK.md`'s
Campaign section to use it.

## Org graph
| Role | Zone owned | Binding | Notes |
|---|---|---|---|
| builder | executable logic | inline prompt, worktree-isolated | works one loop slice; may not touch statements |
| prover | invariants, lemmas, decreases, property tests | `agentType: prover`, worktree-isolated | adds obligations, never removes one |
| proof | verification review | `agentType: proof-auditor` | reviews the prover's diff; WEAKENED is harness-red |
| red-team | adversarial diff review | `agentType: red-team-reviewer` | closes the campaign |

## Campaign

```json graph-ir
{
  "version": 1,
  "specificationContext": "reference",
  "name": "verified-campaign",
  "campaign": "Verified feature slice: program synthesis and proof synthesis as separate mutators, each independently gated, the proof audited before red-team",
  "budget": {
    "maxNodes": 100,
    "verifyFloorTokens": 50000,
    "cacheTtl": "1h",
    "maxEstimatedTokens": 1300000
  },
  "defaults": {
    "effort": "medium"
  },
  "requiredArgs": [
    "goal"
  ],
  "argDefaults": {
    "constraints": "none beyond WORK.md"
  },
  "roles": {
    "builder": {
      "effort": "high"
    },
    "prover": {
      "agentType": "prover",
      "effort": "high"
    },
    "proof": {
      "agentType": "proof-auditor",
      "effort": "high"
    },
    "red-team": {
      "agentType": "red-team-reviewer",
      "effort": "high"
    }
  },
  "nodes": [
    {
      "id": "build",
      "phase": "Implement",
      "role": "builder",
      "mutates": true,
      "prompt": "Implement the goal \"{{A.goal}}\" as one loop slice. Constraints: {{A.constraints}}. Work TDD strictly per WORK_PROMPT.md: failing test first, minimum code to green, scripts/harness.sh --changed <file> after each edit. Write the executable logic and the tests that exercise it; leave every specification statement \u2014 requires, ensures, invariants, property signatures \u2014 exactly as you found it, and where the prover will need an obligation you could not close, say so in diffSummary rather than weakening a statement to pass. Create and commit on a branch named claude/graph-<short-slug-of-goal>. Run scripts/harness.sh --full and report its real exit code; a red prover is an honest answer here, because the next node exists to close it. Evidence entries are command + observed result.",
      "contract": "SliceV1",
      "verify": "schema-only",
      "onRed": "halt"
    },
    {
      "id": "prove",
      "phase": "Implement",
      "role": "prover",
      "after": "build",
      "mutates": true,
      "prompt": "Proof synthesis for the builder's slice: {{prev}}. Fetch and check out that branch in your worktree, run scripts/harness.sh --full on it first, and close the obligations the diff opened: loop invariants, lemmas, decreases clauses, termination arguments, and on Aiken the property tests over aiken/fuzz that state the properties the logic must hold. Add obligations; never drop, widen, rename or flip one, and never discharge one with assume, {:axiom}, {:verify false}, sorry, Admitted, todo or expect \u2014 spec-guard.py and proof-guard.py run against your diff downstream. Work one obligation at a time, scripts/harness.sh --changed <file> after each edit, --full before you return, committing to the same branch. If the diff opened nothing to prove, add nothing and say so. Return the branch, the obligation ids you added as testsAdded, the harness command and its real exit.",
      "contract": "SliceV1",
      "verify": "schema-only",
      "onRed": "halt"
    },
    {
      "id": "gate",
      "phase": "Gate",
      "after": "prove",
      "independent": true,
      "verifies": "build",
      "prompt": "Independently verify the branch named in this slice: {{prev}}. Check it out into a fresh worktree (git worktree add), run scripts/harness.sh --full YOURSELF, and return the real integer exit code, the exact command, and the last ~20 lines. Do not trust any prior claim about whether it passed \u2014 run it and report what you observe.",
      "contract": "HarnessCheckV1",
      "haltWhen": "exit != 0",
      "haltReason": "independent harness re-run disagrees with the implementers; the graph never argues with the harness",
      "onRed": "halt"
    },
    {
      "id": "gate-proof",
      "phase": "Gate",
      "after": "gate",
      "independent": true,
      "verifies": "prove",
      "verify": "prove:harness",
      "prompt": "The independent harness run reported: {{prev}}. Now attest the prover's exit rather than typing it: from the repository root of a fresh worktree of the campaign branch, run exactly the command .fluxpoint-gates.json declares for the gate named harness, through the Bash tool, so the PostToolUse hook records the runtime's own exit. Read .claude/fluxpoint/attest.jsonl afterwards and return ExecutionV1: gate \"harness\", the exit the hook recorded, and that row's attestId (and logSha256 when present). Cite the row your run produced; a claim citing nothing files this run INCOMPLETE, and a citation the log contradicts files it TAMPERED-EXECUTION.",
      "contract": "ExecutionV1",
      "haltWhen": "exit != 0",
      "haltReason": "the attested prover run is red; the proofs the prover wrote do not verify",
      "onRed": "halt"
    },
    {
      "id": "proof-audit",
      "phase": "Gate",
      "role": "proof",
      "after": "gate-proof",
      "prompt": "Audit the proof synthesis on the campaign branch, starting from the attested prover run: {{prev}}. Check the branch out into a worktree of your own (git worktree add --detach) and run every experiment there; the shared checkout is guarded and must not change. Resolve the loaded fluxpoint plugin root from CLAUDE_PLUGIN_ROOT, then PLUGIN_ROOT, or the installed agent file location and run its scripts/py.sh proof-guard.py --check and spec-guard.py --check against the branch, then mutation-guard.py --report where .fluxpoint-mutation.json exists and plutus-budget.py --report where plutus.json exists. Then your checklist over the prover's diff in particular: an invariant that is trivially preserved because it says nothing, a lemma that restates its goal, a property test whose generator cannot reach the interesting case, a decreases clause on a measure that never changes, an obligation closed by an escape hatch the ratchet did not count. Name every file, obligation and suite you reviewed in surface. UNPROVEN when no checker could run; never SOUND on a static read; NOT-APPLICABLE only if the diff carries no proof surface at all.",
      "contract": "ProofV1",
      "verify": "schema-only",
      "haltWhen": "verdict == 'WEAKENED'",
      "haltReason": "the proof-auditor found the verification weaker while the checker stayed green; WEAKENED is harness-red and the campaign does not ship over it",
      "onRed": "halt"
    },
    {
      "id": "red-team-lenses",
      "phase": "Gate",
      "role": "red-team",
      "after": "proof-audit",
      "prompt": "Review only this adversarial lens: {{item.brief}}. Pin the campaign branch and default-branch base to exact commits before attacking, measure changed files/additions/deletions with git, and report top-level coverage for your actual lens id {{item.key}}, attackers 1 and sampled explicitly. Apply your full checklist within that lens and independent skeptical reproduction. The composition lens attacks sequences across components. Keep findings executable and include minimal-case artifact paths. Put missing work and unresolved claims in review.blockers. Prior proof verdict: {{prev}}. Return RedTeamV1. A lens may BLOCK with findings for independent refutation; never claim complete coverage for work you sampled.",
      "contract": "RedTeamV1",
      "verify": "schema-only",
      "onRed": "halt",
      "foreach": "red-team-lenses",
      "independent": true,
      "verifies": "build",
      "isolation": "worktree"
    },
    {
      "id": "red-team-distinct",
      "phase": "Gate",
      "reduce": {
        "from": "red-team-lenses",
        "over": "findings",
        "dedupeBy": [
          "finding",
          "exploit_path"
        ]
      }
    },
    {
      "id": "red-team-refute",
      "phase": "Gate",
      "after": "red-team-distinct",
      "prompt": "Read these distinct executable claims: {{prev}}. Return them without dropping unresolved claims in findings so three independent refuters can test reproduction, reachability and scope. Preserve exact snapshot, minimal-case paths and valid controls in each exploit_path. Return BLOCK while claims remain; this is an intermediate collection, not the final gate.",
      "contract": "RedTeamV1",
      "verify": "panel:3",
      "verifyOver": "findings",
      "expectItems": 12,
      "onRed": "halt"
    },
    {
      "id": "red-team",
      "phase": "Gate",
      "role": "red-team",
      "after": "red-team-refute",
      "prompt": "Bind the final adversarial verdict after three independent refuters per claim. Surviving or unverified claims: {{prev}}. The compiler supplies raw coverage and blockers for every lens. Require every one of the six lenses (authority, value, state, input, operations, composition), exact matching repository base/head identities and measured diff counts; report completed attacker count and sampled truthfully. Inspect missing work, unresolved claims, dead or unverified refuters, inconsistent snapshots and coverage blockers; any of those means BLOCK even when findings are empty. Do not reintroduce refuted claims without contradictory executable evidence. Independently sample defended cases and check composition for false negatives, delegating the skeptic. Return RedTeamV1 with top-level coverage and a final snapshot recheck. SHIP only after the whole review is complete.",
      "contract": "RedTeamV1",
      "verify": "schema-only",
      "haltWhen": "verdict == 'BLOCK'",
      "haltReason": "red-team blocked the diff; a BLOCK verdict is harness-red and the campaign does not ship over it",
      "onRed": "halt",
      "independent": true,
      "verifies": "red-team-lenses"
    }
  ],
  "lists": {
    "red-team-lenses": [
      {
        "key": "authority",
        "brief": "authority and identity boundaries"
      },
      {
        "key": "value",
        "brief": "value, arithmetic and conservation"
      },
      {
        "key": "state",
        "brief": "state and provenance, replay and partial writes"
      },
      {
        "key": "input",
        "brief": "input decoding and execution boundaries"
      },
      {
        "key": "operations",
        "brief": "operations, infrastructure and resource exhaustion"
      },
      {
        "key": "composition",
        "brief": "composition across all changed components and shared resources"
      }
    ]
  }
}
```

## Verification map
- `build` and `prove` both mutate, so both run worktree-isolated and neither
  may certify itself. `verifies` names one node, which is why there are two
  gates: `gate` re-runs the harness for the builder's slice, `gate-proof`
  attests it for the prover's. The second harness run is the price of a
  second mutator, paid on purpose rather than folded into one gate that
  would then be vouching for work it never named.
- `gate-proof` uses `verify: prove:harness`, so its exit code is the hook's
  record and never the node's typing: a proof obligation closed by a
  transcribed exit is the exact laundering this template exists to refuse.
  Requires `.fluxpoint-gates.json` to declare `harness`.
- `proof-audit` reads the prover's diff specifically. WEAKENED halts;
  UNPROVEN files the run INCOMPLETE; a SOUND with an empty surface is
  reported as vacuous by `record-run.py`.
- `red-team-lenses` covers six independent surfaces, including composition;
  `red-team-distinct` deduplicates; `red-team-refute` runs three refuters per
  claim; the separate `red-team` gate binds their results and raw coverage.
  BLOCK, incomplete coverage and unverified claims stop the campaign.

## Failure policy
- Any node returning nothing halts (`onRed: halt`).
- Halt conditions a human can name: either harness gate red, a proof-audit
  verdict of WEAKENED, a red-team verdict of BLOCK.
- Budget: up to 100 work seats, including the independent attack/refutation
  calls. The cost ceiling is 2.25M estimated tokens, including the expanded
  locked packet, prompt text and cold prefills under the declared 1-hour
  prompt-cache TTL. Re-run `--check` with the consuming repository's packet
  and expected findings; named unpriced runtime substitutions need their own
  allowance beyond the printed estimate.

## Decisions
Appended automatically by `scripts/record-run.py` — do not hand-edit.

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|

## Evidence
Appended automatically by `scripts/record-run.py` — do not hand-edit.

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|

## Notes for the next run
<what the prover closed and could not, what the auditor found, resume point>
