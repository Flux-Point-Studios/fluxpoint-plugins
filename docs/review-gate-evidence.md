# Suite evidence in independent reviews

Review lenses receive compact pointers to completed upstream prove/cited-step
results. A pointer is a candidate, not a worker-authored proof. Use the compiled
pinned witness command with `--show <attestId> --nonce <launch>` to read the actual
row without running its gate. Check the row's green exit, reviewed SHA, launch
nonce, log digest and producing witness. A suite already covered at that state
is evidence for every lens; its setup and execution need not be repeated.

Keep attack, valid-control, composition and skeptic work independent. Suite
success does not replace that work. Rerun a covered suite only for a named claim,
and record the claim as the reason. A red, missing, stale or mismatched source
row remains a blocker. Unknown interpreter/runtime identity stays unknown.

`RedTeamV1.coverage` supports three typed fields:

- `suites`: the gate names whose suite coverage the review claims.
- `citedGates`: reused upstream rows, each with `gate`, `attestId`, integer
  `exit: 0`, exact `sha`, `logSha256` and `nonce`.
- `executedGates`: the reviewer's own new source-witnessed runs, with the same
  fields and a `reason` naming the claim under test.

An optional `runtime` copies the source row's measured `host`, or its reference
image when no host measurement exists. Do not infer an interpreter from a shell
wrapper. The source witness and recorder retain the measured identity once; a
review can cite that same measurement without another runtime lane.

The emitted coherence guard refuses a suite claim with no witness, malformed or
red citations, another head/nonce, unoffered upstream IDs, and own reruns without
a claim. `record-run.py` then reads each cited row and enforces the existing
witness checks, including stale/dirty state, log identity, producer, retained
reds and deployed-runtime pins. A bad review citation cannot be recorded as a
clean completion. Artifacts preserve `reviewGateEvidence` and the typed coverage.

One execution can support several review lenses. The existing rule that two
independent prove nodes need independent executions remains intact. Historical
reviews without typed suite claims retain their historical behavior; the new
fields make suite claims explicit rather than interpreting prose as evidence.
