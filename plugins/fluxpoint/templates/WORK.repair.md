# GRAPH: repair a BLOCK through independent fix/check rounds

STATUS: DESIGN

Copy this graph beside the blocked campaign. Replace `slices` with findings
partitioned by files and invariant, and lock the requirement packet before
running. Every slice starts from the launch commit. Keep a slice's patch
complete against that base, so a checker can apply it on a fresh snapshot.
This graph produces reviewed patches and an integrated branch; merging uses
the repository's normal merge policy after the final gate passes.

```json graph-ir
{
  "version": 1,
  "name": "repair-campaign",
  "campaign": "Repair a blocked review with independent fresh-tree checks and per-round integration",
  "budget": {
    "maxNodes": 18,
    "cacheTtl": "1h",
    "maxEstimatedTokens": 800000
  },
  "requiredArgs": [
    "findings"
  ],
  "roles": {
    "fixer": {
      "effort": "high"
    },
    "checker": {
      "agentType": "fix-checker",
      "effort": "medium"
    }
  },
  "lists": {
    "slices": [
      {
        "key": "boundary",
        "brief": "authority and input boundary findings"
      },
      {
        "key": "composition",
        "brief": "state, value and composition findings"
      }
    ]
  },
  "nodes": [
    {
      "id": "fix",
      "phase": "Repair",
      "role": "fixer",
      "foreach": "slices",
      "mutates": true,
      "prompt": "Repair only {{item.brief}} from this BLOCK: {{A.findings}}. Previous independent check: {{lastCheck}} (null in round one). Assert the campaign base, then work in your isolated worktree. Write scratch and a complete patch against the launch base only in {{roundDir}}. Create this directory and never overwrite an existing artifact: write a temporary file, validate it, then rename to patch.diff. Do not execute scripts from another slice or old round. Add regression coverage for the original attack, run affected suites and scripts/harness.sh --full. Include patch.diff's absolute path and top-level definitions added in evidence; return SliceV1. Do not weaken requirements, tests or baselines.",
      "contract": "SliceV1",
      "onRed": "halt"
    },
    {
      "id": "check",
      "phase": "Repair",
      "role": "checker",
      "foreach": "slices",
      "after": "fix",
      "independent": true,
      "verifies": "fix",
      "isolation": "measure",
      "prompt": "Independently attack this repair: {{prev}}. In the fresh frozen git-archive snapshot, apply {{roundDir}}patch.diff. Reproduce the original {{item.brief}} attacks from {{A.findings}}, run the affected suites and full harness yourself, inspect fix-induced defects and composition, and delegate skeptical reproduction. Record exact snapshot and command evidence. Return FixCheckV1: PASS only if the original attack is defended and no unresolved claim remains; otherwise FAIL or INCOMPLETE. Each finding names its stable defectClass, authoritative invariant, classRewire and measurement. Validate any required class reframe with your own command/result measurement. Findings feed the next fixer round.",
      "contract": "FixCheckV1",
      "onRed": "halt",
      "repeat": {
        "until": "verdict == 'PASS'",
        "maxRounds": 3,
        "integrate": [
          "integrate",
          "integration-check"
        ],
        "classBy": [
          "defectClass"
        ],
        "reframeAfter": 2
      },
      "verifyOver": "findings"
    },
    {
      "id": "integrate",
      "phase": "Integrate",
      "after": "check",
      "mutates": true,
      "prompt": "Integrate all passing slices in {{prev}} onto one isolated tree from the launch base. The records name each versioned patch and round directory; apply exactly those patches, never an old canonical path. Previous integration feedback: {{lastCheck}}. List the top-level definitions each patch adds and flag duplicate primitives across slices. Resolve cross-slice conflicts with regression tests. Write a complete integrated patch to {{roundDir}}patch.diff and commit the integrated branch. Return SliceV1 with the branch, patch path, definitions and full harness evidence.",
      "contract": "SliceV1",
      "onRed": "halt"
    },
    {
      "id": "integration-check",
      "phase": "Integrate",
      "role": "checker",
      "after": "integrate",
      "independent": true,
      "verifies": "integrate",
      "isolation": "measure",
      "prompt": "Check the merged repair {{prev}} independently in the fresh frozen snapshot by applying {{roundDir}}patch.diff. Run full harness, original attacks and composition attacks across every passing slice. Check duplicate top-level definitions, multiplied resource bounds and cross-slice regressions. Delegate skeptical reproduction. Return FixCheckV1 with stable defectClass, invariant, classRewire and measurement for each finding; FAIL or INCOMPLETE reopens the passing slices with this feedback.",
      "contract": "FixCheckV1",
      "haltWhen": "verdict != 'PASS'",
      "onRed": "halt"
    }
  ]
}
```

The checker owns `repeat.until`; its paired fixer is consumed into the same
loop. Passing items leave the pending list. Every later fix receives that
item's last check as `{{lastCheck}}`. The cap records pending item keys as
INCOMPLETE. Integration runs each round over all passing patches; a failed
merged-tree review reopens those slices with its findings.

`{{roundDir}}` is an absolute path under
`.claude/fluxpoint/rounds/<launch nonce>/<item.key>/r<round>/`; integration
uses `_integration` as its item key. Never reuse or overwrite old outputs.
`graph-run` supplies a stamped launch identity and the graph retains it in
the run summary. Tree sentinels, the runtime node ceiling and run recording
apply just as they do to every compiled campaign. Use `/fluxpoint:graph-run`
so the run is recorded and the Evidence row cannot lose the repair.

The checker classifies each failure by mechanism in FixCheckV1. Two
consecutive failed rounds on the same `defectClass` make the next round a
mandatory reframe. The fixer must state the authoritative invariant and
adopt/refute the class rewire with a command and observed measurement, or
remove the mechanism. The independent checker reruns that measurement and
validates the reframe. A third narrow patch, missing evidence, or a claimed
PASS without independent reframe validation returns REFRAME-REQUIRED before
integration. Recurring classes and their rounds remain in the run record.
