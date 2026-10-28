# Structured launch and resume

Keep campaign context in a JSON file and let the harness assemble the Workflow
object. Complete graph-run's preflight and launch-turn authorization checks
before dispatching that object; the helper itself never launches an agent.

```sh
ROOT=/path/to/installed/fluxpoint
bash "$ROOT/scripts/py.sh" resume.py --root /checkout --graph WORK.md --args initial.json
bash "$ROOT/scripts/py.sh" resume.py --root /checkout --graph WORK.md --run wf_previous \
  --set repairRelay=@relay-note.txt --set repairPublisher=@publisher-note.txt
```

Set `ROOT` to the installed plugin directory. Its `scripts/py.sh` wrapper
selects a supported interpreter on the current platform.
stdout contains only the Workflow JSON object, including `scriptPath`, launch
`args` and, on resume, `resumeFromRunId`. The deterministic compiler plan,
compile result and exact-file size check go to stderr. Read the plan before
launching. `--out` selects another generated script path. `--projects` selects
the runtime projects directory when its journals live outside the default
`$CLAUDE_CONFIG_DIR/projects` (normally `~/.claude/projects`).

Initial JSON contains only non-underscore fields. `--set key=@file` replaces
one field with the exact UTF-8 text, including CRLF and trailing newlines.
`--set-json key=@file` replaces a structured value, for example the `rejudge`
object described by the graph compiler. The initial assembly embeds every
effective public IR default alongside the provided values. Duplicate updates, underscore keys
and malformed JSON are refusals. Every other recorded input, including an
effective default, is carried unchanged. A change in an IR default therefore
cannot silently alter a carried repair epoch. When a previously recorded default changes, assembly refuses with `RESUME-DEFAULT-DRIFT`; use `--set key=@file` to adopt it or `--keep-default key` to retain the carried value. Newly added defaults are adopted into the immutable argument set and named on stderr. `--unset key` removes a carried value after its IR default is removed. An update no node or harness reads prints `RESUME-UNUSED-SET` so a misspelled repair key is visible.

The compiler emits canonical `ARGS` and its full `ARGS_SHA256`; summary inputs
retain the full resolved text and `argsSha256` identifies the embedded set.
The immutable source archive is
`.claude/fluxpoint/compiled/<args-sha>-<source-sha>.graph.js`. Resume verifies
both hashes, parses the actual constant and cross-checks each embedded value
against the canonical recorded artifact
`.claude/fluxpoint/runs/<runId>.json`. Every recorded input must be bound to
`ARGS`; an additional summary field cannot become carried context. Direct compilation with `--args` freezes public defaults too. Historical runs that left defaults or inline inputs outside the embedded set need a fresh fully embedded launch before structured resume. Different graph versions may archive the
same argument bytes; each source must still pass its own hash. Missing,
changed or conflicting source/recorded input is a refusal. Keep these local
archives with the run records. Recording preserves immutable attempt snapshots
at `.claude/fluxpoint/runs/history/<runId>/<artifact-sha>.json` as well as the
latest artifact. The helper pins the exact source artifact bytes in
`_resumeArtifactSha256`, so subsequent attempts using the same Workflow run ID
cannot replace the attempt a replay cites. Runs compiled before embedded arguments were
introduced require an explicit fresh launch with a reviewed initial JSON;
there is no trustworthy automated recovery of absent context.

When arguments are embedded, every non-underscore inline value is refused. Recording checks the summary inputs against their immutable argument archive too. Recompiling an
embedded script without `--args` refuses; `--args none` deliberately removes
the embedded set for a fresh launch. `--check --args initial.json` prices the
real rendered note text. Without an args file, the compiler names arguments
priced at defaults as estimate blind spots.

## Pin evidence read by repair notes

Use a full SHA-256 reference in a note, quoting paths containing spaces:

```text
Read /scratch/audit.json#sha256=<64 hex characters>.
Read "C:\campaign evidence\triage.json#sha256=<64 hex characters>".
```

The helper hashes the file, copies the verified bytes into
`.claude/fluxpoint/evidence/<full-sha><extension>`, and embeds its absolute
store reference with the full hash. All other note text stays in the prompt.
Existing content is verified rather than overwritten. Source mutation during
copy, missing files, a corrupt store entry or a digest mismatch refuse the
launch. Every resume rehashes carried pins before compiling; `record-run.py`
rehashes them again before recording an artifact or Evidence row. Irreversible
effects still enter the once-only ledger before a recording refusal.

Every absolute local path in a note must have a pin or an explicitly named
exception, for example `--unpinned-ok /scratch/live-observation.txt`. The
exception means the operator accepts mutable bytes outside the replay key;
it is retained in the recorded summary and subsequent resumes. URLs are not
treated as local evidence paths. Pins and exceptions work in nested argument
values too. A dictionary key containing a local path or pin is refused; put
the reference in a value so rewriting cannot collide with another key. The content store uses each platform's native file paths; a
Windows path cannot be resolved by a Linux launcher.

## Launch identity and replay

Resume keeps the recorded `_launch` stamp intact. `_base` is read fresh from
Git; required irreversible ledgers and actor release maps are freshly loaded.
Every invocation gets a fresh `_replayLaunchNonce`. The harness rereads completed journal calls and transcripts to build `_memo` and `_prefix`, then embeds that compact replay state in the compiled source behind `REPLAY_STATE_SHA256`. The Workflow object carries neither map; compiled scripts refuse an inline replacement. Memo transport carries prompt/payload hashes, results and source identities, without repeating the resolved prompts. A local compiler input file supplies state through `--replay-state`; it is a harness input, outside public arguments. The compiler's `--plan` owns replay decisions and rejects
unknown or unreached rejudge gates. For ordinary serial graphs, the plan compares the actual rendered call prompts and options with the recorded prefix, marks calls before the first change as `prefix`, and marks later calls `live`. The plan is retained under `.claude/fluxpoint/plans/`; the launch and run summary carry its path and SHA-256, and recording verifies both the retained bytes and their argument digest. A tree-moving repair still halts
`TREE-MOVED`; start a fresh run after reviewing the changed tree.

Embedded context has the same authority as inline context. It does not extend
the relayed user request, bypass a human confirmation, discharge a red gate or
authorize dispatch by itself.

## Witness version continuity

A recorded summary names the compiling plugin root, version and witness code
digests. Resume verifies that pin before assembling another launch. Keep the
installation available and unchanged for the campaign; an auto-update that
installs a different version alongside it does not change its witness. Missing
or changed pinned files refuse resume. Legacy prove/cited runs without an
identity need a reviewed fresh compilation and launch. See
[compiled witness identity](../../../docs/witness-identity.md).

Native Workflow computed-task frames are parsed before transcript comparison; unknown frames refuse explicitly. Journal rows without timestamps use observed transcript timestamps for duration. Untargeted gates do not attach branch-replay evidence to builders. Rejudge from a legacy source without the replay call protocol refuses explicitly, because introducing its options header would invalidate every cached call. Rejudge note text reaches the named gate; its fixed-command tree sentinel receives the nonce only. Graph path aliases resolve before tree comparisons. Hashes use a defined UTF-8 representation even for lone surrogate code units.

New irreversible ledger keys use SHA-256 of the rendered prompt. The compiler retains a lookup alias for prior FNV keys during migration so an effect already recorded under the old key cannot re-arm on upgrade. Memo replay is refused on declared ancestors of an irreversible effect.
