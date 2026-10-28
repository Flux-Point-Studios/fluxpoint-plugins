# Blaster verification adapter

SPEC: .fluxpoint-spec.blaster.json

## Goal

Add optional Blaster verification to the existing gate and counterexample
infrastructure. Preserve explicit solver trust, reject incomplete outcomes,
and replay concrete scalar counterexamples before accepting regression pins.

## Definition of Done

- [x] Typed backend results, domain witnesses and trust policy are enforced.
- [x] Receipts detect project, dependency, tool and evidence drift.
- [x] Concrete counterexamples enter the ledger and require replayed pins.
- [x] Lean tactic use, command obligations and named Prop bodies are scanned.
- [x] Real Lean 4.24.0 / Z3 4.15.2 discovery, repair and replay passes.
- [x] Focused regressions and the full repository harness pass.

## Decisions

The packet's `blaster-evidence` decision selects an optional structured adapter
over build-exit interpretation and a proof-reconstruction fork. It retains the
optimizer/translator/solver trust boundary. General model reification and the
later compiled-Cardano/ledger integration remain outside this slice.

## Evidence

Initial `blaster-test.py`: 11 tests ran RED before implementation. The requirement
packet was locked before production changes and then moved to the supported
goal-specific packet to preserve the existing shared-template token ceilings. The native fixture subsequently
found `n = 4`, repaired only the implementation, replayed and pinned that input,
and rejected restored implementation drift. Final verification: 180 full-harness steps passed with zero failed steps;
15 focused Blaster tests passed. The real Lean 4.24.0 / Z3 4.15.2 test passed
with the final adapter, including precondition-preserving replay and visibility
of the imported solver axiom with warnings suppressed. Six existing prompt
audit findings remain advisory. The new native CI job is configured; its
remote execution has not been observed in this task.
