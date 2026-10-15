# GRAPH: ship one feature slice, council-designed and independently gated

STATUS: DESIGN

Second canonical campaign: a council designs, one loop-engineered node
implements, a node that did NOT write the code re-runs the harness, the
proof-auditor judges whether verification got weaker, and red-team closes.
Copy over `WORK.md` to use it, or keep both and point `graph-run` at this
file.

Uses the loop side of the plugin: the gate resolves `proof-auditor` and
`red-team-reviewer` via `agentType`, and the implement node assumes
`scripts/harness.sh` exists.

## Org graph
| Role | Zone owned | Binding | Notes |
|---|---|---|---|
| architect | design proposals | inline prompt | one per keyed angle, no repo writes |
| builder | implementation | inline prompt, worktree-isolated | works one loop slice |
| proof | verification review | `agentType: proof-auditor` | WEAKENED is harness-red; NOT-APPLICABLE when the diff carries no proof surface |
| red-team | adversarial diff review | `agentType: red-team-reviewer` | verdict gates the merge |

## Work graph

```json graph-ir
{
  "version": 1,
  "name": "feature-campaign",
  "campaign": "Council-designed, loop-implemented, independently gated feature slice",
  "budget": {
    "maxNodes": 100,
    "verifyFloorTokens": 50000,
    "cacheTtl": "1h",
    "maxEstimatedTokens": 1350000
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
    "architect": {
      "effort": "medium"
    },
    "builder": {
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
  "lists": {
    "angles": [
      {
        "key": "smallest-diff",
        "brief": "the minimum slice that ships observable value"
      },
      {
        "key": "risk-first",
        "brief": "neutralize the scariest failure mode before anything else"
      },
      {
        "key": "contract-first",
        "brief": "pin the tests and interfaces before any implementation"
      }
    ],
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
  },
  "nodes": [
    {
      "id": "design",
      "phase": "Council",
      "role": "architect",
      "foreach": "angles",
      "prompt": "Design an implementation for the goal \"{{A.goal}}\" from exactly this angle: {{item.brief}}. Constraints: {{A.constraints}}. Read the repo first; ground every plan step in real files. Plan steps must be TDD-shaped: each names the failing test before the code.",
      "contract": "DesignV1",
      "verify": "schema-only",
      "onRed": "drop+log"
    },
    {
      "id": "choose",
      "phase": "Council",
      "after": "design",
      "prompt": "Judge these keyed candidate designs independently for the goal \"{{A.goal}}\" on TDD-ability, blast radius, fit with WORK.md and honesty of their risks. Candidates: {{prev}}. Return JudgmentV1: winner is one exact surviving key or none; ranking lists every allowed key exactly once with winner first; rationale explains why it wins; strongest_objection maps every key to its best objection, including the chosen design and none. The compiler tallies three independent seats and builds DecisionV1 only after a strict majority. Barrier justified: judging requires all candidates at once.",
      "contract": "DecisionV1",
      "decides": "implementation-approach",
      "effort": "high",
      "verify": "judges:3",
      "onRed": "halt"
    },
    {
      "id": "build",
      "phase": "Implement",
      "role": "builder",
      "after": "choose",
      "mutates": true,
      "honors": [
        "implementation-approach"
      ],
      "prompt": "Implement the winning candidate's original design as one loop slice: {{winner}}. The exact-key council decision is {{prev}}. The frozen decision that binds this work is {{decisions.implementation-approach}} \u2014 if the implementation cannot honor it, stop and say so rather than quietly choosing differently. Goal: \"{{A.goal}}\". Constraints: {{A.constraints}}. Work TDD strictly per WORK_PROMPT.md: failing test first, minimum code to green, scripts/harness.sh --changed <file> after each edit. Create and commit on a branch named claude/graph-<short-slug-of-goal>, test and code together. Run scripts/harness.sh --full and report its real exit code; never weaken the harness or delete tests to reach green. Evidence entries are command + observed result.",
      "contract": "SliceV1",
      "verify": "schema-only",
      "onRed": "halt"
    },
    {
      "id": "gate",
      "phase": "Gate",
      "after": "build",
      "independent": true,
      "verifies": "build",
      "prompt": "Independently verify the branch named in this slice: {{prev}}. Check it out into a fresh worktree (git worktree add), run scripts/harness.sh --full YOURSELF, and return the real integer exit code, the exact command, and the last ~20 lines. Do not trust any prior claim about whether it passed \u2014 run it and report what you observe.",
      "contract": "HarnessCheckV1",
      "haltWhen": "exit != 0",
      "haltReason": "independent harness re-run disagrees with the implementer; the graph never argues with the harness",
      "onRed": "halt"
    },
    {
      "id": "proof-audit",
      "phase": "Gate",
      "role": "proof",
      "after": "gate",
      "prompt": "Audit the verification work on the branch this campaign built, starting from the independent harness result: {{prev}}. Check the branch out into a worktree of your own (git worktree add --detach) and run every experiment there; the shared checkout is guarded and must not change. Resolve the fluxpoint plugin root (${CLAUDE_PLUGIN_ROOT}, else `find ~/.claude/plugins -type d -name fluxpoint | head -1`) and run its scripts/py.sh proof-guard.py --scan and spec-guard.py --scan against the branch first. If the diff touches no proof-language file (.ak, .dfy, .lean, .v, .thy, .tla, verified Rust) and no test or property suite, return verdict NOT-APPLICABLE with an empty surface and stop. Otherwise run the sequence /fluxpoint:proof-audit prescribes: both ratchets with --check, mutation-guard.py --report where .fluxpoint-mutation.json exists, confirm scripts/harness.sh --full invokes the prover, plutus-budget.py --report where plutus.json exists, then your checklist: vacuity, specification drift, assumption laundering, test theatre, negative tests failing for the wrong reason, unproved surface, on-chain budgets, solver honesty. Name every file, obligation and suite you reviewed in surface. UNPROVEN when no checker could run; never SOUND on a static read.",
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
  ]
}
```

## Verification map
- `design`: schema-only, with the angle key attached before dead seats are
  filtered. `choose`: three independent JudgmentV1 seats, each restricted to
  surviving keys plus `none`. Two of the three seats must name the same
  candidate; a dead seat casts no vote, and no majority halts. The builder
  receives the winning candidate's raw plan, files and risks as `{{winner}}`,
  alongside the generated DecisionV1 record as `{{prev}}`.
- `build`: mutates, so it is worktree-isolated and may not certify itself.
- `gate`: the only node whose exit code the campaign trusts. It never
  wrote the code. `haltWhen: exit != 0` stops the campaign before the
  reviewers burn tokens on a red branch.
- `proof-audit`: `VERDICT: WEAKENED` is harness-red, and `haltWhen` makes
  that structural, the same way it does for red-team. The halt is on
  WEAKENED alone, on purpose: a diff with no proof surface returns
  NOT-APPLICABLE and a repo with no installed prover returns UNPROVEN, and
  neither is a reason to halt a feature campaign — `record-run.py` files
  UNPROVEN as INCOMPLETE and a SOUND over an empty surface as vacuous, so
  the Evidence row still says which of the three it was. The node runs its
  ratchets in a worktree of its own because the compiled graph brackets
  every verdict with a tree sentinel.
- `red-team-lenses` runs the five boundary surfaces plus composition in
  parallel. `red-team-distinct` deduplicates findings, `red-team-refute`
  sends each claim to three independent refuters, and the separate
  `red-team` gate consumes surviving/unverified claims and the compiler's
  raw lens coverage/blocker context. Missing work cannot become SHIP just
  because findings were empty or refuted. Every SHIP carries exact ranges,
  diff counts, completed attacker/lens counts and sampled status.
- Terminal: merge happens outside the graph, per WORK.md Merge policy,
  with the harness as a required CI check.

## Failure policy
- Any single node returning nothing halts (`onRed: halt`) — an unverified
  gate never passes by default.
- Dead council seats drop and log; the campaign proceeds if at least one
  design survives.
- Budget: up to 100 agent calls, verification floor 50k tokens, and a
  cost ceiling of 1.35M estimated tokens over the compiler's estimate
  (~222k under the declared 1-hour prompt-cache TTL, prompt text included;
  `{{prev}}`, `{{decisions.*}}` and the launch `{{A.goal}}` are named as
  unpriced; the one effort transition into a key the run has not warmed,
  design -> choose, is a cold prefill the estimate charges for).
- Halt conditions a human can name: independent harness exit != 0, a
  proof-audit verdict of WEAKENED, and a red-team verdict of BLOCK.

## Decisions
Appended automatically by `scripts/record-run.py` — do not hand-edit.
A decision that overturned the prior is the one a fresh context will
silently re-decide the other way.

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|

## Evidence
Appended automatically by `scripts/record-run.py` — do not hand-edit. The
Proof cell carries the harness exit, the proof-audit verdict and the
red-team verdict together.

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|

## Notes for the next run
<current state, dead nodes, targeted repairs planned, resume point>
