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
        "brief": "authority and input boundary findings",
        "targetClasses": [
          "authority",
          "input"
        ]
      },
      {
        "key": "composition",
        "brief": "state, value and composition findings",
        "targetClasses": [
          "state",
          "value",
          "composition"
        ]
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
      "prompt": "Repair only {{item.brief}} from this BLOCK: {{A.findings}}. Previous independent check: {{lastCheck}} (null in round one). Assert the campaign base, then work in your isolated worktree. Write scratch and a complete patch against the launch base only in {{roundDir}}. Create this directory and never overwrite an existing artifact: write a temporary file, validate it, then rename to patch.diff. Do not execute scripts from another slice or old round. Add regression coverage for the original attack, run affected suites and scripts/harness.sh --full. Include patch.diff's absolute path and top-level definitions added in evidence; return SliceV1. Do not weaken requirements, tests or baselines. Propose necessary packet or graph text revisions only in SliceV1.packetEdits: file, unique original-file anchor, replace/append/insertAfter op, text and reason. Never apply or re-lock these proposals. Return sharedResources as an explicit array of {resource,writers[],readers[]} for every shared pool, executor, cache, lock or single-writer store to which this patch adds a user; return [] only when none. Include existing writers reached by new readers.",
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
      "prompt": "Independently attack this repair: {{prev}}. In the fresh frozen git-archive snapshot, apply {{roundDir}}patch.diff. Reproduce the original {{item.brief}} attacks from {{A.findings}}, run the affected suites and full harness yourself, inspect fix-induced defects and composition, and delegate skeptical reproduction. Record exact snapshot and command evidence. Return FixCheckV1: PASS only if the original attack is defended and no unresolved claim remains; otherwise FAIL or INCOMPLETE. Each finding names its stable defectClass, authoritative invariant, classRewire and measurement. Validate any required class reframe with your own command/result measurement. Findings feed the next fixer round. Use the supplied ORIGIN REPLAY CONTEXT and scripts/repair-replay.py: execute the valid control and finding reproduction on bare launch base and each actual complete round patch on separate fresh base archives. Return required origin and structured originEvidence; fix-introduced also requires the first reproducing introducedIn round. Attack every listed writer/reader, including shared stores reached through executors; inspect the diff for unlisted resources and reproduce any omission as a fix-introduced claim rather than assuming a new route adds only readers. Missing replay is INCOMPLETE, never an inferred origin.",
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
      "prompt": "Integrate all passing slices in {{prev}} onto one isolated tree from the launch base. The records name each versioned patch and round directory; apply exactly those patches, never an old canonical path. Previous integration feedback: {{lastCheck}}. List the top-level definitions each patch adds and flag duplicate primitives across slices. Resolve cross-slice conflicts with regression tests. Write a complete integrated patch to {{roundDir}}patch.diff and commit the integrated branch. Return SliceV1 with the branch, patch path, definitions and full harness evidence. Collect every passing slice's packetEdits verbatim into {{roundDir}}packet-edits.json as one JSON array, including proposals from earlier passing rounds. Report conflicting anchors rather than dropping a proposal. Name the absolute proposal artifact path in evidence. Return the consolidated packetEdits in SliceV1 too; never edit or re-lock the packet to land them. Return an explicit sharedResources array covering every added writer/reader across the combined patches, including shared caches and single-writer stores; [] means no such resource.",
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
      "prompt": "Check the merged repair {{prev}} independently in the fresh frozen snapshot by applying {{roundDir}}patch.diff. Run full harness, original attacks and composition attacks across every passing slice. Check duplicate top-level definitions, multiplied resource bounds and cross-slice regressions. Delegate skeptical reproduction. Verify the consolidated packet-edits.json artifact named in {{prev}} equals {{prev.packetEdits}} without overwriting it. Enumerate every graph naming the same packet and run compile-graph.py <graph> --check --packet-edits <that artifact> for each one, preserving its exact UTF-8 size/headroom output. Anchor conflicts, unrelayed proposals or a projected script above 524288 bytes return FAIL. A projected headroom below 16384 bytes raises a finding for review. This is a measurement only; never write or re-lock the projected packet. Return FixCheckV1 with stable defectClass, invariant, classRewire and measurement for each finding; FAIL or INCOMPLETE reopens the passing slices with this feedback. Replay origin evidence on bare base, every listed component patch separately, and the integrated patch. A failure only on the integrated patch is cross-slice; do not call it fix-introduced from identifier age. Attack all shared-resource users, total concurrency and cache eviction across slices. Return origins and executed replay evidence for each finding.",
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

Packet proposals are data, independent of code patches. Every passing slice
returns `packetEdits` (an empty array when no revision is needed). Integration
retains all proposals and the checker projects them against every graph sharing
the packet on every round. Projection uses unique anchors in the original
LF-normalized files, rejects overlapping proposals, and never modifies files or
locks. `replace` replaces its anchor, `insertAfter` inserts immediately after it,
and `append` appends at EOF with its anchor as a precondition. Review the proposed
text and size before the repository's normal spec-revision and re-lock process.

Each slice declares the stable defect classes it targets in `targetClasses`; replace
the template placeholders with the actual classes from the blocked findings.
This caller-owned input is mandatory and pinned before any repair agent starts;
use an explicit `[]` for a slice with no targeted runtime classes. The checker
cannot add targets or select a different `--target-class` set to change attribution.
The checker compares the harm on the bare base and every actual round patch,
not the file or identifier that happens to contain it. A base failure in a
target class is `incomplete-fix`, a base failure outside them is `pre-existing`,
the first failing patch establishes `fix-introduced` and `introducedIn`, and a
failure absent from all component patches but present after integration is
`cross-slice`. Defects in requirement, scope, argv or graph text use `spec-text`
with a measured document check. No executable control or missing replay means
INCOMPLETE; an origin label alone is refused even if schema validation skipped.

`repair-replay.py` runs the supplied control/reproduction on fresh archives and
produces structured observations. Save the compiler's `ORIGIN REPLAY CONTEXT`
as JSON and pass it with `--context`; the attack must exit 1 for reproduced
harm and 0 for defended, while the valid control must exit 0 on every snapshot.
The helper reads `targetClasses` from that context. Optional `--target-class`
arguments only assert the same pinned set; missing context targets are INCOMPLETE.
Each full patch is applied to the bare launch base, never stacked on the prior
round. A timed-out tool, broken control or patch that cannot apply refuses
attribution. The checker still owns independently verifying the case and
class scope. Shared resource arrays are mandatory for each fixer/integrator;
name every writer and reader added or reached by the change, not just the
thread that starts the work.

Each completed check writes all five origin counts into live provenance and
`summary.fixOrigins`, including zero-finding PASS rounds. `/fluxpoint:status`
can run `metrics.py` to show the fix-introduced share per round and per run.
Run totals count finding observations across checks, including reobserved
failures, rather than claiming a count of unique defects. Optional checker
`commits` list only commits actually verified by that PASS; fixer proposals
never become checked commits on their own.

The same reproduction and valid control must run on every snapshot. Keep
portable probe code outside the patch when the regression test itself is new;
an import error or a test command absent from the base is an unavailable
experiment, not evidence that the base was defended. The replay helper
refuses exits other than 0/1 and terminates the command's process tree on
timeout or cancellation before cleaning its scratch directory.
