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
harness still runs the default packet; wire any other packet's `--run`
into the harness explicitly. Lines inside fenced blocks never count as
headers.

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

The lock binds named obligation statements and hashes the packet and optional proof baseline with Git's LF line
endings; CRLF conversion between platforms preserves the lock.
Keep both tracked before isolated graph execution. A justified revision changes
the packet/baseline and re-runs `--lock`; review the diff. A worktree whose
packet differs from the graph's embedded packet must not certify that graph.
