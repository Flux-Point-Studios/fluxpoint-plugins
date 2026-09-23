---
description: Scaffold the Flux Point harness into the current repo — harness contract, WORK.md, outer loop runner, settings and .gitignore wiring — then tailor the harness and prove it green.
argument-hint: [one-line goal]
---

Onboard this repository onto the fluxpoint harness. Work through every
step; do not stop at copying files.

1. Locate the plugin templates. Try `${CLAUDE_PLUGIN_ROOT}/templates`
   first; if that expands empty in your shell, find them with
   `find ~/.claude/plugins ~/.codex/plugins/cache -type d -path '*fluxpoint/templates' 2>/dev/null | head -1`.
2. Copy, without overwriting anything that already exists:
   - `templates/harness.sh` → `scripts/harness.sh` (then `chmod +x`)
   - `templates/loop.sh` → `scripts/loop.sh` (then `chmod +x`)
   - `templates/WORK.md` → `WORK.md`
   - `templates/WORK_PROMPT.md` → `WORK_PROMPT.md`
   If a destination exists, show a diff and propose a merge instead. If
   the repo has a pre-1.0 `LOOP.md`, stop and run `/fluxpoint:migrate`
   instead of writing a second state file.
3. Ensure `.gitignore` contains `.claude/fluxpoint/`,
   `.claude/worktrees/`, and `__pycache__/`. The worktrees entry
   matters as soon as any campaign has a `mutates` node: those run in
   isolated trees under `.claude/worktrees/`, which must never be
   committed.
4. Wire the plugin to load in every session of this repo, for the runtime
   in use (both, when the team runs both):
   - Claude Code: merge the keys from `templates/settings.snippet.json`
     into `.claude/settings.json`, creating the file if absent and
     preserving every existing key.
   - Codex: append `templates/codex.config.snippet.toml` to
     `.codex/config.toml`, creating the file if absent. It enables
     `fluxpoint@fluxpoint` once the marketplace has been added with
     `codex plugin marketplace add Flux-Point-Studios/fluxpoint-plugins`.
5. Perform `/fluxpoint:grill-me` (Codex: `$fluxpoint-grill-me`) for the goal.
   Draft and challenge the packet, fill routine decisions with model defaults,
   and lock `.fluxpoint-spec.json`. The new WORK.md declares its mandatory
   spec runner. On an existing WORK.md add `SPEC: .fluxpoint-spec.json` and
   merge that runner from the template into the existing harness.
   Tailor `scripts/harness.sh`: inspect the repo's real stack and replace
   the auto-detection floor with the exact commands the Definition of Done
   requires (build, unit and property tests, lint, typecheck, formal
   checks, preview-net exercises). Then run `scripts/harness.sh --full` and
   iterate until it exits 0, or report precisely what is red and why.

   **Then prove it once in a worktree.** A graph node marked `mutates: true`
   runs worktree-isolated, and green in the primary checkout is not green
   there: `git worktree add` checks out tracked files only, so a gitignored
   build artifact or an installed `node_modules` is absent, and a test that
   pins an absolute path is false in a worktree by construction. Under
   `set -euo pipefail` the first such failure aborts the rest of `--full`,
   including gates a later campaign node was told to read.

   ```sh
   git worktree add --detach ../.fpl-probe
   (cd ../.fpl-probe && bash scripts/harness.sh --full); probe=$?
   git worktree remove --force ../.fpl-probe
   [ "$probe" -eq 0 ] || echo "worktree probe FAILED (rc=$probe)" >&2
   ( exit "$probe" )
   ```

   The status is captured before cleanup and re-raised after it. Written as
   one `&&` chain ending in `git worktree remove`, the whole thing exits with
   the REMOVE's status — so a red harness reports success, and this is a step
   an agent runs and reads the exit code of.

   Fix what that finds now. A harness only ever proven in the primary
   checkout is not proven for the isolation a campaign imposes, and the
   failure surfaces later as a verification node blaming the implementer —
   wrong, and pointed at an innocent node.

   While you are reading the stack, ask the one question the harness cannot
   answer for itself: **which artifacts have to agree with each other?** An
   on-chain predicate and the off-chain builder that constructs
   transactions for it; a migration and the schema it assumes; a wire
   format and both ends of it; a deploy gate that re-reads a config the
   keeper also parses. Each side has its own tests and passes them; the
   pair is what breaks. Start with the mirrors nobody declared:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" pair-guard.py --scan
   ```
   prints suggested manifest entries (the same error message thrown from two
   modules, overlapping thrown-message sets, a docstring claiming to mirror
   or delegate) with the evidence in each `why`; it is a suggestion list,
   never a gate. Then write the pairs into `.fluxpoint-pairs.json` with a
   `parity` command wherever one can be written, and a `differential`
   wherever both sides can be run over generated inputs — a co-change rule
   only proves somebody touched both files, never that they agree. Name the
   funds-moving side with `authority`, and give every parity a `bite`, a
   mutation of the reader that `pair-guard.py --verify` must turn RED:
   ```json
   [{"id": "config-gate",
     "source": "keeper/config.py", "mirror": ["deploy/gate.py"],
     "authority": "source",
     "differential": {"generator": "python gen_configs.py",
                      "sourceRun": "python -m keeper.config --check",
                      "mirrorRun": "python -m deploy.gate --check",
                      "cases": 500, "seed": 1},
     "bite": {"file": "deploy/gate.py",
              "find": "if doc.fee < 0:", "replace": "if False:",
              "compile": "npx tsc --noEmit"}}]
   ```
   The generator reads `FPL_PAIR_SEED` and `FPL_PAIR_CASES` and prints one
   input per line; both runs read one input on stdin, and any difference in
   exit code, stdout or stderr is a divergence, bytes compared, no
   normalisation. `--check` runs parity and differential on every `--full`;
   `--verify` (mutate the reader, require RED, restore, every step read back)
   belongs off-session on a Routine, like `guard-guard.py --verify`. A parity
   nobody has shown to fail is named as such by `--check` and `--list` and
   fails only under `--verify`. The manifest is worthless if it is not written
   at onboarding, because nobody adds a pair after the incident it would have
   caught — and nobody declares a mirror they do not know they wrote, which
   is what `--scan` is for.
6. Fill in the WORK.md goal line. Use "$ARGUMENTS" if provided; otherwise
   ask for the goal before writing.
7. Set `MODE`. Default to `loop` and delete the Campaign section — most
   repos start there. Choose `graph` or `both` only when the work already
   meets the escalation rule in the graph-engineering skill (independent
   subtasks, a work-list to fan out over, claims needing adversarial
   verification, cross-zone ownership). If the Campaign section stays,
   verify it compiles:
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" compile-graph.py WORK.md --check`
8. Arm the ratchets that share `.fluxpoint-proof-baseline.json`. In ANY
   repo with tests — proof languages or not — arm the seam ratchet:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" seam-guard.py --baseline
   ```
   If the repo tracks proof-language files (`.ak`, `.dfy`, `.lean`, `.v`,
   `.thy`, `.tla`, or verified Rust) **or TypeScript**, arm the hatch
   ratchet; add the statement ratchet where a prover is involved:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" proof-guard.py --baseline
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" spec-guard.py --baseline
   ```
   TypeScript is in the first list because a type checker is a prover with
   a weak logic and `as any` is its `sorry`: the obligation is discharged
   without being met and `tsc` exits 0 either way. On an off-chain repo
   that is the surface an autonomous caller actually reaches, so run
   `proof-guard.py --scan` first and read what it found. A codebase with a
   hundred existing `any`s baselines at a hundred and ratchets from there;
   arguing about the number is the wrong fight, and stopping the hundred
   and first is the right one.
   Each preserves the others' sections of the shared file. Commit it — it
   belongs in review, because a rise in the seam counts is someone walling
   a module off behind a mock, a rise in the hatch counts is someone
   making a proof obligation disappear, and a change in the statements is
   someone making a theorem claim less. Read
   `spec-guard.py --scan` before arming: it lists exactly what is being
   treated as an obligation, and names any tracked proof language it does
   not parse yet, so an unarmed corner never reads as a covered one.
   Confirm `harness.sh --full` actually invokes the prover; per-file
   checking on edit is not a Definition-of-Done gate.
   After onboarding has finished arming or updating these baselines, review
   them and re-run `specification.py --lock`; the packet lock covers the
   proof baseline too. Re-run the full harness with the final lock.
   Where the repo has headline theorems — the results everything else
   rests on — record what the prover says they depend on, so an
   assumption that later sneaks in through a helper lemma is a red gate
   rather than a flat hatch count:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" spec-guard.py --baseline --axioms \
     --headline lean:Vault/Safety.lean:no_double_spend --headline dafny:*
   ```
   Lean (`#print axioms`), Coq (`Print Assumptions`, resolved through
   `_CoqProject`) and Dafny (`dafny audit`) are read; the ids come from
   `spec-guard.py --scan`, and `dafny:*` audits every tracked `.dfy`.
   Copy `templates/attack-taxonomy.json` to `.fluxpoint-attacks.json` and
   commit it. It carries one taxonomy per language, and each gates only a
   repo that tracks that language, so a validator-only repo is silent on
   the builder classes and a builder-only repo is silent on the validator
   ones.
   The **aiken** taxonomy names the eUTxO classes every validator has to
   rule out: double satisfaction, datum hijack, token-name confusion,
   unbounded value, staking-credential substitution, foreign UTxOs,
   unbounded validity ranges, arbitrary mints. A class is specified by a
   property test of that exact name over `aiken/fuzz`
   (`test attack_double_satisfaction(n: Int via bounded_int(1, 99)) { … }`).
   The **typescript** taxonomy names what the transaction builder answers
   for, which is the surface an autonomous caller actually reaches:
   unvalidated change address, datum round-trip loss, stale protocol
   parameters, a replayable signed transaction, unbounded UTxO selection,
   a missing script data hash, unbounded collateral. A class is specified
   by a test whose title carries the class id, and the scan notes when
   that test uses no fast-check, because an example is weaker than a
   property over a generator.
   `spec-guard.py --check` is red for each unspecified class until the
   test exists or the taxonomy's own `waived` object gives it a reason of
   at least twenty characters. A waiver is scoped to its taxonomy, so an
   Aiken waiver never excuses a builder class. Write those tests now,
   against this repo's real validators and its real builder, and then arm
   the ratchets so their signatures are hashed. A class that genuinely
   cannot apply is waived in the committed file, where review sees it; it
   is never left unspecified.
   Keep both taxonomies unless the repo has a reason to drop one. When the
   repo tracks a language whose taxonomy is missing from the manifest,
   `spec-guard.py` prints a `NO TAXONOMY` line naming the language, how
   many classes the template holds for it and the first few ids. That line
   is a note by default. Two optional top-level fields in
   `.fluxpoint-attacks.json` change it:
   ```json
   {"version": 1, "requireAllLanguages": true, "languages": ["typescript"],
    "taxonomies": [ … ]}
   ```
   `"requireAllLanguages": true` turns each `NO TAXONOMY` line into a
   failure; set it once the manifest carries every half the repo needs.
   `"languages"` lists the halves the repo gates on purpose. A tracked
   language left off the list prints one `excluded by declaration` line
   and never fails, even under `requireAllLanguages`. A language on the
   list with no taxonomy is still reported as `NO TAXONOMY`, and a
   taxonomy in the manifest keeps gating whether the list names it or not.
   Both fields are validated: a `requireAllLanguages` that is not `true`
   or `false`, a `languages` value that is not a non-empty list, or a name
   in it that is neither a language spec-guard knows nor the language of
   a taxonomy in the manifest makes the manifest unreadable, which is red.
   When a Definition-of-Done line rests on a proof, say which one:
   `- [x] withdraw never overdraws — proof: dafny:src/vault.dfy:Withdraw`.
   `spec-guard.py --check` refuses a checked box whose obligation does not
   exist or no longer says what was recorded.
   For Aiken repos the scaffolded harness also captures `aiken check`'s
   JSON and records any counterexample it finds to `.fluxpoint-cex.jsonl`;
   for Dafny repos it captures `dafny verify` the same way, and
   `FPL_DAFNY_ARGS="--extract-counterexample"` in the environment makes
   the prover print the model the ledger records. A Rust crate with
   `#[kani::proof]` harnesses and `cargo-kani` installed is captured the
   same way, with `FPL_KANI_ARGS="-Z concrete-playback
   --concrete-playback=print"` so the ledger gets the interpreted values
   and not only the failed check (declare `cfg(kani)` under
   `[lints.rust] unexpected_cfgs` in `Cargo.toml`, or the clippy step that
   runs first fails on the harness module's `#[cfg(kani)]`); an Apalache
   spec runs only when
   `FPL_APALACHE_ARGS` names it (`--inv=Inv Spec.tla`), and the ITF trace
   it writes is what gets recorded. A TypeScript repo's `test` script is
   captured the same way: a fast-check failure carries a shrunk
   counterexample with the `seed` and `path` that replay it, and only the
   vitest reporter is parsed.
   **Then buy back the exploration the gate gives up.** The gate pins
   `--seed 1` so a shrink is reproducible and the ledger can dedupe, which
   means every run explores the same cases at the tool's default iteration
   count. Declare a wide sweep and run it off-session, on a Routine, the
   way `mutation-guard.py --measure` already runs:
   ```json
   {"version": 1, "tool": "aiken", "seeds": 25, "maxSuccess": 1000}
   ```
   in `.fluxpoint-fuzz.json`, then
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" cex.py --sweep`. Each sweep
   starts where the last one stopped, so the ground covered grows instead
   of repeating, and anything it finds lands in the same ledger under the
   same pin discipline. `--check` then reports what the last sweep covered
   and how stale it is; set `failWhenStale` once the cadence is established. That file and
   `.fluxpoint-cex/` are committed artifacts like the baselines — a
   ratchet only anyone else can see is one that lives in the tree, so do
   not add them to `.gitignore`.
8b. If this repo builds Aiken validators, hold the off-chain builder to the
   blueprint they declare. Everything in step 8 judges the on-chain
   predicate; a validator proved correct still signs whatever the
   transaction builder constructs, and the builder is the caller an
   autonomous agent actually reaches. Start by reading what the blueprint
   declares:
   ```
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" blueprint-guard.py --scan
   ```
   It lists every validator's datum and redeemer schema and names the ones
   that are opaque `Data` — a validator taking untyped data has no
   specification to hold anyone to, so say that in review rather than
   reading a green as coverage. Then declare a corpus in
   `.fluxpoint-blueprint.json`: a command that prints one PlutusData hex
   per line, exactly as the builder encodes it.
   ```json
   {"version": 1,
    "corpora": [{"validator": "vault.vault.spend", "purpose": "datum",
                 "produce": "node offchain/scripts/sample-datums.mjs"}],
    "waived": {"vault.vault.else": "the else handler declares no schema of its own"}}
   ```
   `--check` decodes each line and asks the blueprint whether the validator
   would recognise it, then reds on a value the schema refuses, a manifest
   naming a validator the blueprint lacks, or a `$ref` that does not
   resolve. The producer should be the builder's own encoder over its own
   fixtures; a corpus written by hand tests the hand that wrote it. Set
   `requireTyped` once every typed schema has a corpus, and the gate starts
   refusing to leave one uncovered.
9. If this repo has guards — the specific lines that stop money moving
   wrongly, an auth check, a spend limit, a signature verification — name
   them in `.fluxpoint-guards.json` so the guard ratchet can hold each one
   down. An entry names the guard and the test that fails without it:
   ```json
   {"guards": [{
     "id": "spend-cap",
     "protects": "no single tx may exceed the treasury cap",
     "guard":    {"file": "api/spend.py",        "contains": "if amount > CAP:"},
     "proof":    {"file": "tests/test_spend.py", "test": "test_over_cap_refused"},
     "mutation": {"find": "if amount > CAP:", "replace": "if False:"},
     "expect":   "exceeds the treasury cap",
     "run":      "{py} -m pytest -q {file}::{test}"
   }]}
   ```
   `protects` is prose for the next reader; `expect` is a substring the
   failing proof must print, so a proof that fails for the wrong reason
   does not count as the guard biting.
   The scaffolded harness runs the cheap structural `--check` on every
   `--full` once the manifest exists; `guard-guard.py --verify` is the
   expensive mode — it runs the proof intact, then disables the guard and
   requires the same proof to break — and belongs off-session, on a
   Routine, not in the gate. Commit the manifest. A repo with nothing
   worth naming here should say so in review rather than leave the file's
   absence read as coverage.
10. Declare this repo's gates so their exit codes stop being self-reported.
   Write `.fluxpoint-gates.json` naming each command whose verdict decides
   something — at minimum the harness — exactly as it is invoked:
   ```json
   {"version": 1, "gates": {"harness": "scripts/harness.sh --full"}}
   ```
   A PostToolUse hook then records the runtime's own exit code for every one
   of those runs to `.claude/fluxpoint/attest.jsonl`, and `record-run.py`
   cross-checks any campaign node that claims a gate exit against it. Match
   the declared string to how the command is actually run: a gate invoked
   with a pipe, a redirect, or a trailing `|| true` reports a different exit
   and is deliberately not attested, so it shows up as UNATTESTED rather
   than being credited to the gate. Commit the manifest; it is part of the
   trust base. A gate that outlives one tool call (600 s) runs through
   `attest.py --run <gate>` in the background and is collected with
   `attest.py --await <token>`, which attests the exit itself (from a worktree,
   `--root <this project>` keeps it in this project's log; graph nodes are
   handed it). A gate declared with a leading `cd <dir> &&` is matched only
   when run in that directory. A gate may not be
   named `ci`: that name is the forge's. If merges
   rest on CI, add a `ci` section so `prove:ci` can cite the forge's own
   commit statuses (`attest.py --ci --pr <n>`, GitHub via `gh`; the forge
   names the commit):
   ```json
   {"version": 1, "gates": {"harness": "scripts/harness.sh --full"},
    "ci": {"forge": "github", "contexts": ["harness"]}}
   ```
   Without `contexts`, every context the forge reports must pass and at
   least one must exist.
11. Fill in the Merge policy block, asking the user once: may green + SHIP
   PRs merge autonomously in this repo, and does merging trigger a deploy?
   If auto-merge is on, verify `gh` is authenticated and record the
   required CI check names the merge will wait on.
12. Finish with a short report: files created, harness verdict, MODE, merge
   policy, and the one command that starts an outer loop
   (`scripts/loop.sh`) or a campaign (`/fluxpoint:graph-design`, which flips
   the work file to READY, then `/fluxpoint:graph-run`).
