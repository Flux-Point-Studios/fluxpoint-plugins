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
    "maxNodes": 256,
    "cacheTtl": "1h",
    "maxEstimatedTokens": 13000000
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
      "prompt": "Repair only {{item.brief}} from this BLOCK: {{A.findings}}. Previous independent check: {{lastCheck}} (null in round one). Assert the campaign base, then work in your isolated worktree. Write scratch and a complete patch against the launch base only in {{roundDir}}. Create this directory and never overwrite an existing artifact: write a temporary file, validate it, then rename to patch.diff. Do not execute scripts from another slice or old round. Add regression coverage for the original attack, run affected suites and scripts/harness.sh --full. Include patch.diff's absolute path and top-level definitions added in evidence; return SliceV1. Do not weaken requirements, tests or baselines. Propose necessary packet or graph text revisions only in SliceV1.packetEdits: file, unique original-file anchor, replace/append/insertAfter op, text and reason. Never apply or re-lock these proposals. Return sharedResources as an explicit array of {resource,writers[],readers[]} for every shared pool, executor, cache, lock or single-writer store to which this patch adds a user; return [] only when none. Include existing writers reached by new readers. Enumerate siblings in SliceV1.siblings for each pinned target class: every output field, accepted encoding, caller/signer, network, role and path. Each row names finding (the target-class identity), invariant, axis, surface, attack/control command and result, outcome and reason. Close the class rather than only the original instance. These rows are claims; the independent checker reruns them. For each BLOCKING finding FIRST honor the repair note's structured touches and changesBehaviourOf arrays: run specification.py --touches <declared paths> and --reach <changed production paths/modules> with this packet, then probe all reached locked checks on a minimal scratch change before other work. If a reached locked check goes red, return immediately with packetConflict {checks,nodes,counterexample} and packetEdits; do not apply proposals or weaken/skip any gate. A conflict requires an operator ruling before dispatch resumes.",
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
      "prompt": "Independently attack this repair: {{prev}}. Use the fresh already-patched archive supplied by the harness; do not apply {{roundDir}}patch.diff again. Reproduce the original {{item.brief}} attacks from {{A.findings}}, run the affected suites and full harness yourself, inspect fix-induced defects and composition, and delegate skeptical reproduction. Record exact snapshot and command evidence. Return FixCheckV1: PASS only if the original attack is defended and no unresolved claim remains; otherwise FAIL or INCOMPLETE. Each finding names its stable defectClass, authoritative invariant, classRewire and measurement. Validate any required class reframe with your own command/result measurement. Findings feed the next fixer round. Use the supplied ORIGIN REPLAY CONTEXT and scripts/repair-replay.py: execute the valid control and finding reproduction on bare launch base and each actual complete round patch on separate fresh base archives. Return required origin and structured originEvidence; fix-introduced also requires the first reproducing introducedIn round. Attack every listed writer/reader, including shared stores reached through executors; inspect the diff for unlisted resources and reproduce any omission as a fix-introduced claim rather than assuming a new route adds only readers. Missing replay is INCOMPLETE, never an inferred origin. PASS must echo pinned targetClasses and independently rerun every fixer-listed sibling attack/control, then probe at least one unlisted sibling on each of field, encoding, caller, network, role and path per class. Return siblings; NOT-APPLICABLE cites inspected snapshot file:line. Cover every harness-supplied added-hunk id in diffCoverage at its exact patchSha256 with attack and control evidence. Measure added provider/timer latency against a declared deadline, declare rate-limited pools in sharedResources, and measure alarm false-positive rate against a recorded production series with absolute path and SHA-256. With no claims, delegate an independent skeptic to sample these sibling and diff rows for false negatives. The harness separately spawns the diff attackers and refuters; your own PASS cannot discharge that tier.",
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
      "verifyOver": "findings",
      "expectItems": 1
    },
    {
      "id": "integrate",
      "phase": "Integrate",
      "after": "check",
      "mutates": true,
      "prompt": "Integrate all passing slices in {{prev}} onto one isolated tree from the launch base. The records name each versioned patch and round directory; apply exactly those patches, never an old canonical path. Previous integration feedback: {{lastCheck}}. List the top-level definitions each patch adds and flag duplicate primitives across slices. Resolve cross-slice conflicts with regression tests. Write a complete integrated patch to {{roundDir}}patch.diff and commit the integrated branch. Return SliceV1 with the branch, patch path, definitions and full harness evidence. Collect every passing slice's packetEdits verbatim into {{roundDir}}packet-edits.json as one JSON array, including proposals from earlier passing rounds. Report conflicting anchors rather than dropping a proposal. Name the absolute proposal artifact path in evidence. Return the consolidated packetEdits in SliceV1 too; never edit or re-lock the packet to land them. Return an explicit sharedResources array covering every added writer/reader across the combined patches, including shared caches and single-writer stores; [] means no such resource. Preserve the component sibling claims in siblings for the independent checker to rerun.",
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
      "prompt": "Check the merged repair {{prev}} independently in the fresh already-patched archive supplied by the harness; do not apply {{roundDir}}patch.diff again. Run full harness, original attacks and composition attacks across every passing slice. Check duplicate top-level definitions, multiplied resource bounds and cross-slice regressions. Delegate skeptical reproduction. Verify the consolidated packet-edits.json artifact named in {{prev}} equals {{prev.packetEdits}} without overwriting it. Enumerate every graph naming the same packet and run compile-graph.py <graph> --check --packet-edits <that artifact> for each one, preserving its exact UTF-8 size/headroom output. Anchor conflicts, unrelayed proposals or a projected script above 524288 bytes return FAIL. A projected headroom below 16384 bytes raises a finding for review. This is a measurement only; never write or re-lock the projected packet. Return FixCheckV1 with stable defectClass, invariant, classRewire and measurement for each finding; FAIL or INCOMPLETE reopens the passing slices with this feedback. Replay origin evidence on bare base, every listed component patch separately, and the integrated patch. A failure only on the integrated patch is cross-slice; do not call it fix-introduced from identifier age. Attack all shared-resource users, total concurrency and cache eviction across slices. Return origins and executed replay evidence for each finding. PASS must echo pinned targetClasses and independently rerun every fixer-listed sibling attack/control, then probe at least one unlisted sibling on each of field, encoding, caller, network, role and path per class. Return siblings; NOT-APPLICABLE cites inspected snapshot file:line. Cover every harness-supplied added-hunk id in diffCoverage at its exact patchSha256 with attack and control evidence. Measure added provider/timer latency against a declared deadline, declare rate-limited pools in sharedResources, and measure alarm false-positive rate against a recorded production series with absolute path and SHA-256. With no claims, delegate an independent skeptic to sample these sibling and diff rows for false negatives. The harness separately spawns the diff attackers and refuters; your own PASS cannot discharge that tier.",
      "contract": "FixCheckV1",
      "haltWhen": "verdict != 'PASS'",
      "onRed": "halt",
      "expectItems": 1
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

Repair notes supply structured JSON `{"touches":["repo-relative paths"],
"changesBehaviourOf":["production path or module"]}`. Before dispatch,
`specification.py --touches` must exit 0; append the `--reach` report as
“locked checks that must stay green untouched”. A reached locked check that
contradicts a repair requires an operator ruling before dispatch resumes.

Every FixCheckV1 PASS now has three independent obligations. It proves the
repaired invariant's siblings, covers the harness's actual added hunks, and
survives the compiled diff-scoped attacker/refuter tier. A missing sibling
axis, unmapped hunk, different digest, dead attacker or unresolved refutation
cannot complete the item. Surviving findings enter `lastCheck` with origin
replay and count toward the existing class streak/reframe rule. Integration
has its own tier over the complete integrated patch. The normal full-range
red-team before an irreversible node still applies.

The Workflow sandbox has no filesystem API. A separate fixed-command sentinel
runs `scripts/py.sh repair-proof.py read` and returns the patch's raw base64
bytes and pinned base/item/round/path. Compiled code computes SHA-256 and the
hunk list itself, independently of fixer/checker summaries. This sentinel is
the same trusted read boundary as the tree sentinel; it is not a cryptographic
proof of a model's tool use. The recorder re-reads the live patch and recorded
alarm series, checks the complete diff receipt, and binds any named commits
to the exact reviewed tree. A changed or missing artifact cannot mint checked
commits. Historical PASS receipts without this tier no longer discharge them.

The checker also receives its own fresh, already-patched archive. Every
DEFENDED/NOT-APPLICABLE sibling and every hunk attack/control row must match
the execution helper's actual command/result log in that archive. Its owned
cleanup witness, distinct from every attacker/refuter snapshot, is required
for PASS and commit corroboration. Deadline rows copy the helper's elapsed
time. Alarm commands read the frozen `FPL_PRODUCTION_SERIES` copy and emit
JSON metrics; a checker cannot substitute typed counts for measured output.

Each launch/resume supplies a fresh `_replayLaunchNonce`, separate from the
stable repair `_launch.nonce`. It changes sentinel prompts so replay cannot
reuse a read on its own word. The tier always reruns: no stored diff receipt
is sufficient. Every attacker/refuter uses a separate fresh base archive
materialized with the expected digest. Run experiments through
`repair-proof.py execute`; it owns and drains command descendants. A pending
command tree prevents snapshot cleanup and completion. Cleanup receipts bind
to the actual spawned seat identities. These archives isolate repository
state, not arbitrary host access by a malicious command.

Surface selection examines changed additions and deletions. Static keyword
classification conservatively selects lenses/operational obligations; it does
not prove the absence of a new behavior. Unknown surfaces select all six
lenses. Attackers inspect the whole diff and a separate no-claims skeptic
checks for omitted surfaces and false negatives. Text/binary added hunks are
inventoried, with a 16 MiB UTF-8 patch witness limit; unsupported input remains
INCOMPLETE. `expectItems` prices claims per diff attacker; actual spawns still
obey the runtime ceiling. Reprice this template for the campaign's expected
findings instead of treating its generous worst-case ceiling as an instruction
to spend it.

`maxNodes` counts actual work seats, including every diff attacker/refuter.
Fixed read/materialization/cleanup sentinels are priced in token estimates
and bypass that work-seat ceiling, as tree sentinels do: proof collection and
cleanup still run after a work seat is declined. Receipt spawn ids are the
harness's unique Workflow call labels, which the journal exposes; this API
does not supply provider-specific opaque agent ids.

The 13M estimated-token ceiling includes the expanded locked packet and leaves
headroom above this template's declared worst-case token estimate. Run `--check`
against the consuming repository's packet, slices and expected findings before
launch; runtime substitutions listed as unpriced still need their own allowance.
