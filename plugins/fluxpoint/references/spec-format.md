# Requirement packet format

The canonical files are `.fluxpoint-spec.json` and `.fluxpoint-spec-lock.json`
in the consuming repository. `specification.py --lock` creates the lock;
never invent its digest.

WORK.md declares the runner with `SPEC: .fluxpoint-spec.json` on one line.
ASCII spaces and tabs after the colon or at the line end are optional; LF
and CRLF are supported. The filename is literal. A line break between the
colon and filename is not a declaration.

A graph file (`WORK.md` or a sibling `GRAPH.<name>.md`) may name a packet
of its own with `SPEC: <path>`, relative to the repository root and ending
in `.json`. Its lock is named after it: `.fluxpoint-spec.rollout.json`
locks into `.fluxpoint-spec.rollout-lock.json`
(`specification.py --lock --spec .fluxpoint-spec.rollout.json`). The
compiler, `record-run.py --graph` and `specification.py --graph <file>` all
read the header, so two campaigns on one branch keep separate packets
instead of overwriting one. A path outside the repository, a non-JSON path,
or two different `SPEC:` lines in one file is an error. The scaffolded
harness runs the packet `WORK.md` (else `LOOP.md`) declares, the default
one when it declares none; wire a sibling campaign's packet `--run` into
the harness explicitly. `spec-guard.py` treats a campaign as locked when
the lock of any packet `WORK.md`, `LOOP.md` or a `GRAPH.*.md` declares
exists, so no locked packet leaves the Decisions-row waiver open. Lines
inside fenced blocks never count as headers.

A graph file may also declare `CONTRACTS: <dir>`, a repository-local
directory of `*.schema.json` contracts. The compiler overlays it on the
contracts the plugin ships, so the directory holds only additions and
stricter copies, and `release.py --graph <file>` validates a parked node's
proof against the same set. `compile-graph.py --check` names the contract
set and the packet it used on its summary line. An explicit `--contracts`
flag still replaces the set wholesale.

The JSON packet has these fields:

| Field | Required content |
|---|---|
| `version` | Integer `1` |
| `goal`, `asset` | Observable outcome and existing asset improved |
| `scope`, `outOfScope` | Nonempty lists of specific boundaries |
| `decisions` | Decisions with unique ids and resolved dependencies |
| `requirements` | Checkable requirements linked to decisions and checks |
| `checks` | Commands run without a shell from the repository root |
| `challenges` | Material objections and their resolutions |
| `harnessInputs` | Optional list of additional repository files or directories executed checks depend on; directories expand to tracked files |

A decision contains `id`, `status` (`defaulted`, `user-confirmed`, or
`blocked` while drafting), `dependsOn` (decision ids), `record` (the existing
[DecisionV1 contract](../contracts/DecisionV1.schema.json)), and `costs`.
Read that schema for every required `record` field. Each option needs exactly one cost object:
`option`, `cost`, `basis` (`measured`, `estimate`, `unknown`), and a nonempty
`evidence` list explaining the measurement, estimate or unavailable fact.
The DecisionV1 `evidence` list must also be nonempty. Use actual instructions
as evidence for `user-confirmed`; a default has no approval authority.

A requirement has `id`, `statement`, `decisions`, `checks` and
`counterexample`. Reference ids must exist. Checks have unique ids and every
check must be used by a requirement. Do not turn nonfunctional requirements,
failure modes or interfaces into unlinked prose.

```json
{
  "id": "reject-stale-spec",
  "statement": "Editing the requirement packet invalidates its lock before implementation",
  "decisions": ["freeze-spec"],
  "checks": ["spec-tests"],
  "counterexample": "Change a requirement while keeping the old lock and compile a mutating graph"
}
```

A check contains `id`, `kind`, `argv`, `scope`, `assumptions` and
`timeoutSeconds` (integer 1–3600). Kinds are `test`, `property`, `model`,
`proof` and `runtime`. `{python}` is replaced by the interpreter running the
gate; other arguments are literal. Pipes and shell expansion are not implicit.
Use a repository-owned adapter when a command needs multiple operations.

A prose value may consist entirely of `<repo-relative path>#<RFC 6901 pointer>`,
for example `docs/archive.json#/checks/settlement/scope`. The file must contain
JSON and the pointer must resolve to nonempty text. Object keys use `~1` for
`/` and `~0` for `~`; array indices are supported. Absolute paths, traversal,
symlinks outside the repository, missing targets, non-string targets and cycles
are errors. A target that is another complete reference (optionally prefixed
`See: `) resolves recursively. A reference mentioned inside a prose sentence
remains that sentence; write a complete reference to bind archived text.

References are supported in goals, assets, scope/exclusion lists, requirement
statements/counterexamples, check scope/assumptions, challenge claims/resolutions,
decision questions/rationales/evidence/objections and cost descriptions/evidence.
Command arguments and identifier lists are literal. Compiled node context
contains the resolved text and preserves a scalar's original reference beside
it (`scopeRef` for a check scope). A bare check-scope path emits a warning.

```json
{
  "id": "spec-tests",
  "kind": "test",
  "argv": ["{python}", "tests/test_spec.py"],
  "scope": "Absent and stale packet rejection at the public CLI",
  "assumptions": ["The harness and tests are reviewed code"],
  "timeoutSeconds": 60
}
```

`model` and `proof` also require `obligations`, a nonempty list of tracked ids
from `spec-guard.py --scan` (for example `dafny:proof.dfy:Positive`). Each must
resolve; the lock records its statement hash even without a proof baseline.
Other check kinds may bind obligations too. Unsupported proof surfaces need
a scanner adapter before they can use these assurance kinds. This binding
detects changes to the scanner's statement surface, not every transitive
definition or assumption; review the scanner's coverage and use proof-auditor.

They also require `witness`, a command argument list that
checks reachability or satisfiability of the domain. It runs before `argv`
and must pass. State model bounds, fairness, trusted axioms and any link to
production behavior in `scope` and `assumptions`; include a separate
implementation conformance check where the proof is about a model.
Formal runs also invoke `proof-guard.py --check --require-armed`. Review and
arm its baseline before execution, then re-lock. Missing counts or newly
active unratcheted categories fail. Locking a draft may precede that step;
running a declared formal check may not pass without it.

Each challenge is `{"claim": "...", "resolution": "..."}`. A critical
challenge that is unresolved must also be a `blocked` decision. The validator
checks the packet's structure and references. It cannot establish that prose
captures intent, that an asserted measurement was made, or that a command
actually proves the claim. The grill pass and independent verification own
those judgments; raw check execution is never promoted to universal proof.

The lock binds named obligation statements and hashes the packet, optional proof
baseline and every referenced JSON file (`references: {path: sha256}`) with Git's
LF line endings; CRLF conversion between platforms preserves the lock. Changing
an archive, including a transitively referenced archive, invalidates the lock.
Keep both tracked before isolated graph execution. A justified revision changes
the packet/baseline and re-runs `--lock`; review the diff. A worktree whose
packet differs from the graph's embedded packet must not certify that graph.

`--lock` also records `harness: {path: sha256}` beside the specification identity.
It includes repository files named in `argv`/`witness`, including `file::node`
selectors and `--option=file`, plus every `harnessInputs` file. A directory
expands to its Git-tracked files; an empty or untracked-only directory refuses
to freeze. Explicit inputs must exist inside the repository. Package cwd
flags (`--prefix`, `--dir`, `--cwd`, and a directory-valued `-C` or `--project`)
resolve package-relative paths. A `--project` file is pinned as configuration;
its parent guides local module resolution. Non-directory `-C` values and Jest
`--testPathPattern(s)` regexes do not become input paths. A path without that
context may resolve against a unique repository package root. Ambiguous package paths refuse rather than choosing one.
Deleting a frozen input does not alter the specification identity; its next run
record names the missing file with a null hash. Both lock operations compute
the same additive support closure: Python conftest ancestry, package
`__init__.py`, pytest config tables and local imports in test packages,
including function-local imports. Literal pytest `pythonpath` entries also
guide local module resolution. JavaScript test directories include ancestor
package manifests, Vitest/Vite configs and literal relative test-support imports. Repository code
is parsed, never imported or executed. A Python support parse failure refuses
freezing. Production imports guide reach but do not freeze production code.
Keep `harnessInputs` for opaque commands, dynamic imports, resources outside a
test package, generated loaders and dependencies no static walk can see.
Known Python dynamic loaders and computed or template-literal JS imports emit
warnings in lock and preflight output; a known target explicitly covered by
`harnessInputs` discharges its warning. Static closure still cannot enumerate
arbitrary resource paths computed by repository programs.
Every path in a previous harness map is retained as a closure root, even after
an argv or import is removed. A missing retained file refuses re-locking; no
path can silently leave the locked set.
The harness map stays outside the compared identity, so editing check code
changes neither the graph's IR nor its compiled script and does not stale its
specification. It remains reviewed code, with its own recorded identity.
`--check --strict-harness` and `--run --strict-harness` refuse changed, missing
or newly discovered harness inputs in the exact checkout being judged, naming
them before running checks and checking again afterwards. Newly scaffolded
harnesses use strict mode, including when executed in builder worktrees.
Existing copied harnesses need that flag added explicitly. Review intended
harness edits and re-lock them before allowing that tree to certify a packet;
plain `--check` remains an identity diagnostic for migration and review.

After reviewing and committing a harness fix, run its declared gate through
`attest.py --run <name>`, then run
`specification.py --lock-harness --gate <name>` (with `--spec` or `--graph` for a
sibling packet). This operation requires an attested exit 0 for the current
gate command at HEAD. It rewrites only the harness map and prints every changed
path. A changed packet, archive or proof baseline still requires a full spec
review and `--lock`. Attestation is evidence of a local execution at a commit,
not a sandbox or a guarantee that a dirty tree did not change after that run.

`record-run.py` hashes the harness in the tree it records from and writes its
aggregate SHA-256, per-file hashes and every path differing from the lock into
the run artifact; its Evidence row names the digest and changed paths. Missing
locked files are recorded with a null hash. These describe the tree at recording
time; record from the executed tree before further edits. The Stop gate also
names edited check files from declared packets' harness maps as trust-base edits.
It reads frozen maps directly and only walks legacy locks lacking a map, so
recording a green Stop does not repeat the live closure walk.

Before a repair, `specification.py --inputs --closure` reports JSON paths,
hashes, holding check ids and reasons, including packet/lock, proof baseline,
gates, references, argv/witness, explicit inputs and computed support. Plain
`--inputs` includes the persisted locked map and direct inputs; `--closure`
adds computed paths even on a legacy lock with no harness map. `frozen: false`
identifies an inferred dependency not yet in that lock; the report never writes
or re-locks anything. Use `--spec` or `--graph` for a sibling packet.

`--touches <path>...` always includes closure and exits 4 if a planned file or
directory overlaps locked inputs, naming the checks that hold them. Exit 0
only describes the declared list, never grants edit permission or proves the
live tree unchanged. Planned ambient files such as a new `conftest.py`, package
initializer or test-runner configuration also conflict when locked inputs lie
under their directory, even before the ambient file exists. `--reach <production-path-or-module>...` reports holding
check commands, reached test files and statically discovered pytest node ids.
Unresolved targets refuse; dynamic imports may be missed. Module/package-level
imports conservatively reach every test in the file, so this is an early probe
plan, never authority to omit any packet check at a gate.

A BLOCKING repair note declares JSON arrays `touches` and
`changesBehaviourOf`. Run touches before dispatch (exit 4 requires a packet
revision first), append reach results as "locked checks that must stay green
untouched", and obtain a recorded operator ruling for a contradiction. The
builder's first work is a minimal scratch change and those reached checks. A
red probe returns immediately as optional `SliceV1.packetConflict` containing
nonempty `checks`, failing test `nodes` and a concrete `counterexample`, together
with proposed `packetEdits`. Conflict evidence only blocks for a ruling; it
never permits a carve-out, automatic revision, re-lock or skipped gate.

`SliceV1.packetEdits` carries proposed specification revisions as data. Each item
has exactly `file`, `anchor`, `op`, `text` and `reason`. `file` is repository
relative; `anchor` is a nonempty literal in the original LF-normalized file,
including serialized JSON quoting/escapes when applicable. `replace` replaces
the anchor, `insertAfter` inserts immediately after it, and `append` appends at
EOF with the unique anchor as a precondition. `text` may be empty for a deletion;
`reason` explains the revision. An empty array explicitly proposes no revision.

```json
[
  {
    "file": ".fluxpoint-spec.json",
    "anchor": "\"scope\": \"Old checked boundary\"",
    "op": "replace",
    "text": "\"scope\": \"New independently verified boundary\"",
    "reason": "The check now covers both provider paths; retain the measured bounds."
  }
]
```

Measure the consolidated array before landing or re-locking:

```sh
python compile-graph.py GRAPH.build.md --check --packet-edits packet-edits.json
```

This reads the current packet without treating it as locked, applies proposals
only to in-memory snapshots, resolves archived binding text from those snapshots,
and recomputes the complete projected identity. It prints the exact emitted
script's UTF-8 size and headroom, rejects overflow, and warns below the 16 KiB
margin. An unlocked or stale source is labelled explicitly. The projection
cannot emit a launch script, certify `--check-script`, or write any packet/lock.
Normal compilation still requires the real locked packet. Review and use the
normal specification-revision process before launching the resulting change.

Anchors must occur exactly once, counting overlapping matches. Every anchor
refers to the original snapshot, never text another edit introduced. Overlapping
anchor spans and colliding insertions fail independently of proposal order;
symlink aliases cannot hide a conflict. Targets are the packet, its original or
projected referenced JSON files, and graph files declaring that same packet.
Unrelated files, locks, paths outside the repository and graph packet redirects
are refused. A newly referenced archive must already exist in the tree. Invalid
projected JSON or packet structure fails rather than receiving a borrowed lock
identity. Inline context keeps archived text fully resolved. Reference context
keeps only the packet path, its exact identity and a catalogue of IDs in the
compiled script, so archived prose does not inflate every prompt or the script.

In a repair round, integrate every passing slice's proposals, including earlier
passing rounds, into one immutable `packet-edits.json`. The integration checker
compares the artifact with the returned `packetEdits` and projects it against
every graph naming that packet. Conflicts, dropped proposals and overflow fail
the round; low headroom raises a finding while revision is still reviewable.


## Packet context by reference

Set `"specificationContext": "reference"` in graph IR to pass the packet by
reference. Shipped WORK templates use this setting. Existing graphs without it
retain `"inline"` behavior. The compiler still fully validates and resolves the
packet, but emits only its repo-relative path, the full lock identity (including
archive digests), and requirement/check/decision IDs. Each node first runs the
quoted, pinned `specification.py --check --check-identity <digest>` command from
its working-tree root, then reads applicable requirements, binding check scopes
and rulings from the verified files. A reviewer challenging packet completeness
must read the whole packet. Reference mode changes transport, not applicability.

`--check-identity` compares the canonical current identity with the compiled
expectation, so relocking a changed packet or archive cannot make an old graph
trust new bytes. A mismatch stops the worker. Run summaries retain the complete
identity and record their context mode. `--check` reports packet input tokens per
call, and each profile row records `specificationInputTokens`; prices include
reference metadata and validation instructions. Tool output from targeted reads
is workload-dependent and does not become a claim of measured latency savings.

Reference text uses UTF-8 serialization instead of ASCII escapes in both modes.
The starter, consolidation, discovery, feature, verified and repair template
ceilings are now 200,000, 350,000, 2,500,000, 1,450,000, 1,300,000 and 9,500,000
estimated tokens. Recompute against each campaign's packet and runtime context.
