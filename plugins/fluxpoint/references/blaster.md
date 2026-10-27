# Optional Blaster verification

Blaster checks named Lean propositions with an SMT backend and retains the
result, query, model, input hashes and dependency revisions. Flux Point requires
an explicit expected result and domain witness for every check. Unknown results,
failed witnesses, missing outputs, compiler failures and timeouts fail the gate.

The integration was exercised with Lean 4.24.0, Z3 4.15.2 and Lean-blaster commit
`a16614e5e44a693664043552f259235f15f1c884`. It uses the backend API through
`templates/blaster/Bridge.lean`. The adapter does not install tools or fetch
dependencies in consumer repositories.

For a contributor checkout and the native smoke test, use
[development setup](../../../docs/development-setup.md#optional-native-blaster-smoke-test).
The instructions below configure verification in a consuming Lean project.

## Trust and scope

Every check declares `trust: "solver"`. Blaster's current tactic uses the
`Blaster.Tactic.blasterProven` axiom on success; proof reconstruction is absent.
The bridge queries the API without declaring a theorem, and records axiom
dependencies of the registered proposition and its witness. It still trusts
Blaster's normalization, SMT translation and Z3. A `kernel` policy refuses.

`ProofV1` remains a review assessment. The local receipt is a machine result;
execution authority still comes from the existing declared gate and its
independent attestation. An agent able to rewrite the gate and its artifacts can
rewrite local evidence. Review and CI remain the external boundary.

The witness must establish satisfiability or a successful reachable case in the
same domain as the safety property. A constant `True` supplies no useful domain
coverage. The adapter executes the witness but cannot establish that its author
selected a meaningful one. Review the witness and the model's connection to the
implementation. Bounded CEK execution additionally needs explicit fuel and
completion evidence; this adapter does not supply a Cardano ledger model or
establish an evaluator's conformance.

## Configure a project

Install Lean **4.24.0** and Z3 **4.15.2** with TLS and artifact verification,
and make `lake`, `lean` and `z3` available on `PATH`. The CI download URLs and
checksums are in [.woodpecker.yaml](../../../.woodpecker.yaml); its Z3 binary
targets Ubuntu 24.04/glibc 2.39. Select a verified artifact for your platform.
Set the project's `lean-toolchain` to `leanprover/lean4:v4.24.0` and add this
dependency to `lakefile.toml`:

```toml
[[require]]
name = "Blaster"
git = "https://github.com/input-output-hk/Lean-blaster"
rev = "a16614e5e44a693664043552f259235f15f1c884"
```

Resolve dependencies with Lake and retain `lake-manifest.json`. Every dependency
must be a Git package at its locked full SHA, present and locally clean. Path
packages are unsupported. The installed Blaster commit must also match the
manifest's toolchain declaration.

Copy `templates/blaster/manifest.json` to `.fluxpoint-blaster.json` and
`templates/blaster/Model.lean` to `Verification/Model.lean`. Track the model,
manifest and Lake configuration. Adapt the property, witness, scope and
assumptions to the actual project; the starter is only an arithmetic example.
Declare `lean_lib Verification` in `lakefile.lean`, or add
`[[lean_lib]]` with `name = "Verification"` in `lakefile.toml`. Alternatively,
put the model under an existing library and adjust the manifest's module path.
The adapter builds Blaster explicitly as well as the selected model modules.

The registered module and declarations use ordinary qualified Lean identifiers.
Propositions must be closed declarations with no free universe parameters.
`expected` is `valid` or `falsified`; an expected falsification is useful for a
deliberate negative test and does not become an open bug. The witness must always
return `valid`. Each check has a fixed seed and explicit scope/assumptions.

Run from the project root, with `ROOT` set to the installed fluxpoint plugin:

```sh
bash "$ROOT/scripts/py.sh" blaster.py --run
bash "$ROOT/scripts/py.sh" blaster.py --check
bash "$ROOT/scripts/py.sh" cex.py --check
```

`--run` builds the selected modules, executes the structured bridge, and writes
`.claude/fluxpoint/blaster/runs/<id>/receipt.json`, the generated probe, and build
and query logs. `latest.json` points by content to the latest receipt. A passing
receipt has `schema: "BlasterVerificationV1"`, explicit solver trust, observed
query and witness results, exact replay outcomes, a complete project fingerprint
and tool/dependency identities. It is not an SMT proof certificate.

`--check` runs no solver query. It verifies that the passing receipt still covers
the current configuration, tracked input set, HEAD, tools, dependencies and
retained logs. All tracked project files are conservatively hashed, together with
the Lake lock and toolchain files even if ignored. The counterexample ledger is
excluded because it is output from this run. Required subject files must be
tracked; generated subject data must be made a reviewed tracked input before
claiming coverage. Untracked imports or arbitrary external files are outside the
snapshot contract. Build the subject through the project's normal checked path.

The bridge/adapter implementations and Lake/Z3 launcher binaries are also hashed.
This is freshness checking over the declared local environment, not process
isolation or a hermetic build. Backend packages are rechecked for local changes.

Freshly copied harnesses run the adapter when `.fluxpoint-blaster.json` exists,
then run the counterexample guard. Older copied harnesses need the explicit
adapter invocation added to `--full`; updating an installed plugin cannot rewrite
them. Keep the normal `lake build` check. No Blaster manifest leaves the added
gate dormant and requires no Lean/Z3 installation in unrelated repositories.

The full run has a configurable build timeout and an outer process deadline
covering the selected query count, plus a Z3 timeout per query. Descendants are
terminated on timeout and early parent exit through the shared process runner.
There is no separate memory cap in this first integration. Put expensive models
in supervised CI/off-session gates and use a freshness check when appropriate;
`longrun.py` alone does not attest a gate.

## Record and replay a counterexample

Unexpected falsification of a safety check with a passing witness is ingested
into the existing `.fluxpoint-cex.jsonl`. Its raw model and originating receipt
are retained in the normal evidence sidecar. Expected refutations and failed
witnesses do not manufacture bug rows. Open Blaster counterexamples make
`cex.py --check` fail until pinned or explicitly retired through the existing
reason/Decisions discipline.

Concrete replay supports complete scalar assignments of `Nat`, `Int`, `Bool`
and simple `String` values. The decoder never evaluates solver text. Missing,
duplicate, ambiguous, opaque or unsupported assignments remain open and cannot
be pinned through this adapter. SMT escaped strings outside the supported
literal spelling remain opaque. No general model shrinking or state-trace
reification is claimed.

Generate a draft entry:

```sh
bash "$ROOT/scripts/py.sh" cex.py --pin cex_0123456789ab
```

Add the draft to the manifest's `regressions` list, preserving the exact recorded
values. For example:

```json
{
  "cexId": "cex_0123456789ab",
  "check": "safety",
  "bindings": [{"name": "n", "type": "Nat", "value": 4}]
}
```

Repair the implementation while preserving the safety statement, then run the
adapter again. It specializes the original registered proposition with those
exact typed values; the Lean bridge checks variable names, order, types and
completeness. Both the safety check and concrete replay must succeed before:

```sh
bash "$ROOT/scripts/py.sh" cex.py --pin cex_0123456789ab \
  --file .fluxpoint-blaster.json --test-name cex_0123456789ab
bash "$ROOT/scripts/py.sh" cex.py --check
```

A pin cannot be satisfied by including literals in a comment or printing a
model. It requires the tracked manifest, the exact original check/declaration
and bindings, and a fresh receipt of successful specialization. Future full
runs re-execute every declared regression. Editing or deleting a regression,
changing inputs, modifying evidence, or restoring the bug invalidates the pin.
As with every regression, weakening the specification can defeat its intent;
keep the statement ratchet and semantic review armed.

## Statement and assumption guards

`proof-guard.py` counts imported `blaster` tactic use and explicit
`blasterProven` references as `lean.blaster`. Existing baselines report the new
category until deliberately reviewed and re-armed under the normal ratchet
policy. Source matching cannot discover arbitrary macro aliases; the prover's
headline assumption audit remains the stronger dependency check.

`spec-guard.py` also scans closed `def/abbrev name : Prop := ...` definitions.
Standalone `#blaster`, `#bmc` and `#kind` commands have per-kind ordinal ids,
such as `lean:Model.lean:#blaster:1`, bound conservatively to their whole source
file. A command has no Lean proof term to audit with `#print axioms`. The command
scan does not itself execute it or give a bounded result an unbounded meaning.

## Development validation

The normal repository harness runs `tests/blaster-test.py`, including CLI
protocol failures, unknowns, failed witnesses, trust policy, source/dependency
drift, supervised cleanup and exact pin/replay enforcement.

With the pinned tools on PATH and a checkout at the pinned backend SHA, run the
real integration fixture:

```sh
bash "$ROOT/scripts/py.sh" ../tests/blaster-live.py --backend /path/to/Lean-blaster
```

It finds the concrete `n = 4` boundary failure in a Lean implementation, repairs
the implementation without changing its quantified property, checks the exact
old input, pins the regression, and confirms that reintroducing the bug refuses.
It uses a temporary project and can reuse valid build outputs from that exact
backend checkout. It does not run or verify a compiled Cardano contract.
