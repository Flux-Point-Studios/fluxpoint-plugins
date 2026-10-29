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
stdout contains only the Workflow JSON object, including `scriptPath` and launch
`args`. Receipt-enabled graphs start a fresh native Workflow on resume;
source run and immutable attempt identity remain internal provenance fields.
Historical ordinary serial graphs without that protocol retain native
`resumeFromRunId`. The deterministic compiler plan,
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
Every invocation gets a fresh `_replayLaunchNonce`. The harness rereads completed
journal calls and transcripts to build a versioned captured prefix, per-node
memos and ledger history. The compiled `REPLAY_STATE` holds exact JSON result
values in a deduplicated pool, prompt/options fingerprints and immutable source
identities. Ordered result encoding preserves downstream `JSON.stringify`
prompts, including object property order. The whole capsule is limited to
128 KiB; a result is never partly copied. The compiler also checks the full
generated script size. Missing legacy receipts, incomplete calls, null results,
foreign-artifact content or a capacity boundary end the available prefix.

The Workflow object carries no replay maps, and compiled scripts refuse inline
replacements. A local compiler input file supplies state through
`--replay-state`; compilation rebuilds and verifies it from its source before
embedding it behind `REPLAY_STATE_SHA256`. Unchanged prefix calls return locally
from these verified values. The first changed, fresh, rejudged or forked call
closes the prefix and runs through the normal native agent API. A new outer
Workflow invocation prevents that live suffix from consulting an old native
cache position. Native agent counts and tools describe newly executed work;
plugin receipts describe local reuse. Recording recursively verifies both.

A once-only ledger hit never calls its effect agent. It consumes the next
captured slot only when the node, original prompt, full options and result match,
retaining a logical call receipt for subsequent resumes. Otherwise the prefix
closes. Old ledger-only sources, or effects whose native prompts carry fresh
nonce or gate decorations, can restore the recorded effect while the suffix
runs live; summaries explicitly mark their missing replay proof. A known effect
whose ledger receipt is lost, whose result differs or whose current prompt key
changes halts `LEDGER-CONFLICT`, even with carried confirmation. Use a reviewed
new campaign or node identity for different intent.

`NativeReadingsV1` preflights always run live: their fixed command evidence must
bind to the current native worker transcript. They close the captured prefix
and cannot use a memo. A completed irreversible native call whose outer guard
rejects the result still leaves refusal history, even without an accepted
ledger write; a resume cannot use carried confirmation to repeat it.

The compiler's `--plan` owns replay decisions and rejects unknown or unreached
rejudge gates. It compares rendered prompts and options with the verified
prefix, marks calls before the first change as `prefix`, and marks later calls
`live`. The plan is retained under `.claude/fluxpoint/plans/`; the launch and run
summary carry its path and SHA-256, and recording verifies both the retained
bytes and their argument digest. A tree-moving repair still halts
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

A targeted tree sentinel before the first lane fork can retain its exact recorded prompt and nonce only when both prompt and option fingerprints match the next verified prefix slot. If its gate leaves that prefix, the compiler takes a fresh nonce reading before consulting any memo. Builders compare a fresh branch tip with the source run’s last corroborated tip for that branch; gates still bind to the exact tip they judged, so an intermediate gate can legitimately run again after later builders have pushed. The plan uses the last source branch reading, and shared-branch graphs print the possible extra cost. Different concurrent lanes do not trigger a serial-adjacency warning. SSH aliases are resolved through local `ssh -G` configuration; the known forges’ `www` aliases use the same identity as their bare host.

Every executed declared builder now has fixed-command before/after
`git ls-remote --heads origin` receipts. Net creation, deletion or movement of
any undeclared head halts, including movement by an unrelated concurrent
actor. An unchanged declared head remains a `PUSH-NO-CHANGE` finding. A live
builder cannot reuse a cached before-read, and a successful record requires
corroborated endpoints and execution receipts. These interval observations
do not prove causal attribution, inspect tags or detect a push reverted
before the after-read.

The plan also removes lane, push, replay, fresh and branch-target opt-ins
from the same current graph and renders it with prior and requested inputs.
This serial counterfactual keeps recorded node outputs fixed. Rows name the
serial prefix/live status; totals count reached live nodes and known source
recorded-node minutes, with unknown costs explicit. A warning names extra
opt-in work. This is a conditional cost estimate, unavailable as a forecast
of new outputs, remote state, changed prior graph source or future wall time.
