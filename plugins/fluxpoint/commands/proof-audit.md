---
description: Check that verification got stronger, not just greener — run the proof-strength ratchet, then the proof-auditor over the diff.
argument-hint: [path or diff spec; defaults to the working diff]
---

Audit the verification work in this repo. A green checker is the input to
this command, never its conclusion.

In a campaign this whole sequence is the `proof-audit` node of
`templates/WORK.feature.md` and `templates/WORK.verified.md`: the
`proof-auditor` agent bound by `agentType`, contracted to `ProofV1`
(`verdict` SOUND / WEAKENED / UNPROVEN / NOT-APPLICABLE, the `surface` it
reviewed, the findings table), with `haltWhen: "verdict == 'WEAKENED'"`
making WEAKENED harness-red structurally and `record-run.py` filing the
verdict into the Evidence row. This command is the same audit run by hand.

Resolve the plugin root: `${CLAUDE_PLUGIN_ROOT}`, else
`find ~/.claude/plugins ~/.codex/plugins/cache -type d -name fluxpoint 2>/dev/null | head -1`. Call it `$ROOT`.

1. **Mechanical pass — both ratchets.**
   `bash "$ROOT/scripts/py.sh" proof-guard.py --scan` to see the current escape
   hatches, then `--check` to compare against
   `.fluxpoint-proof-baseline.json`. If no baseline exists and the repo has
   proof files, say so and run `--baseline` to arm it — an unarmed ratchet
   protects nothing. If `--check` is red, that is the work list: each rise
   is an obligation someone made disappear rather than discharged.
   Then the statement ratchet:
   `bash "$ROOT/scripts/py.sh" spec-guard.py --scan`, then `--check`. Where
   proof-guard asks whether the proof got weaker, this asks whether the
   *claim* did — a dropped `ensures` conjunct, a widened `requires`, a
   deleted or renamed property test, a `fail` test flipped positive, a
   narrowed fuzzer. None of those move a hatch count, and every checker
   still exits 0. It covers Aiken, Dafny, Lean, Coq, Isabelle, TLA+ (with
   the invariants a TLC `.cfg` names) and Kani; read the `NOT COVERED`
   lines for anything else, and the `NO TAXONOMY` and `excluded by
   declaration` lines for a tracked language whose attack classes no
   taxonomy gates, because that surface is the auditor's alone.
   Read the rest of its output as ground already covered: a DoD line
   whose `— proof:` obligation is missing, an attack class in
   `.fluxpoint-attacks.json` with no test, and an `AXIOM AUDIT` line —
   `NOT RUN` means a headline theorem's assumptions were not checked in
   this run, which is a gap to name, never a pass.
1a. **Can the tests fail?** If `.fluxpoint-mutation.json` exists, run
   `bash "$ROOT/scripts/py.sh" mutation-guard.py --report`. A ratchet on
   proof strength says nothing about a suite that executes every line and
   asserts nothing; the mutation score is the only number here that cannot
   be faked by running code. Read the survivors: each one is a change to the
   implementation that no test noticed. If the measurement is stale or
   absent, say so as a gap rather than treating green as measured.
2. **Confirm the checker actually ran.** Read `scripts/harness.sh` and
   verify that `--full` invokes the prover, not only the per-file
   `--changed` path. A Definition-of-Done gate that never calls the prover
   is the failure this whole command exists to catch. Report it as
   CRITICAL if so. For Aiken, check for `aiken build` as well as `aiken
   check`: `check` proves the logic, `build` is what produces the compiled
   script, so a `--full` that only checks can never measure script size or
   execution units — the budgets that decide whether a correct validator is
   submittable at all. Same shape elsewhere: confirm the artifact the
   downstream gate needs is actually produced, not just that the prover
   exited 0.
3. **On-chain budgets (Cardano).** If `plutus.json` exists, run
   `bash "$ROOT/scripts/py.sh" plutus-budget.py --report`. A validator over
   `maxTxSize` cannot be submitted no matter how well it is proved, and the
   prover never mentions it. Report an unmeasured `exUnits` as a gap rather
   than a pass — it is the budget most often assumed met.
4. **Semantic pass.** Launch the `proof-auditor` agent — Claude Code's
   subagent of that name; under Codex a subagent, or an inline pass, given
   `${CLAUDE_PLUGIN_ROOT}/agents/proof-auditor.md` as its instructions — over
   "$ARGUMENTS" (or the working diff). It judges vacuity, specification
   drift, assumption laundering, test theatre, unproved surface, on-chain
   budgets, and solver honesty — everything a count cannot see. It ends
   `VERDICT: SOUND` or `VERDICT: WEAKENED`.
5. **Treat WEAKENED as harness-red.** Fix the findings; never lower a
   specification, add an assumption, or re-record either baseline to reach
   SOUND. Re-recording is legitimate only when a change is deliberate and
   justified in review — and then the justification belongs in the commit
   message or a Decisions row naming the obligation id, not in a silent
   baseline bump.
6. Record the outcome in WORK.md's Evidence table: the ratchet result, the
   auditor verdict, and what you ran to establish each. A verification
   claim without a row is treated as false, exactly like any other.
