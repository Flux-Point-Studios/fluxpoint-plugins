# Hybrid memory recall

Design notes for the recall layer in the fluxpoint plugin: what it indexes, how it ranks, and what it refuses to do. The user-facing summary is in the repository README.


`memory.jsonl` remembers what campaigns established; until v1.26 the only
way back in was an exact `tag|dedupeKey` match, so a lesson about
beacon-prefix derivation was invisible to a session working on two-way
asset beacons. `scripts/recall.py` closes that gap with the architecture
the research points at — Graphiti's read path without its write path —
while adding **no store, no writer, no dependency, and no daemon**:

- **The graph is a projection, not a store.** `recall.py --build` compiles
  a typed knowledge graph deterministically from the stores that already
  exist — lessons, run artifacts, decisions (keyed by the import id the
  compiler already resolves), counterexample ledger entries, and the repo's own
  `substrate.json` primitives (sanitized as hostile input; consumes-edges
  kept walkable across the repo boundary) — into gitignored
  `.claude/fluxpoint/index/`. Every relation is schema-native (provenance,
  supersession, kill events, path components inside dedupe keys, campaign
  membership), so there is no LLM extraction step to pay for or to
  hallucinate: the evidence (LazyGraphRAG, HippoRAG 2's ablations,
  verbatim-beats-extracted) says extraction subtracts value when the data
  is already typed. Identical sources build byte-identical indexes, and a
  malformed line in a *source* store is still a hard error while a damaged
  *index* file is deleted and rebuilt out loud.
- **Bi-temporal by derivation.** Each lesson identity carries its full
  append chain, so recall serves the current version by default,
  `--as-of <ts>` serves the version that held then, and a kill stays a
  permanently valid `KILLED_BY` edge even though the claim it killed is
  not — kills are priors, never suppressors. A killed lesson whose touched
  file changed after the kill is marked `stale: <file> changed since`
  at build time — annotated, never dropped, because a finding that comes
  back after the code moved is exactly the regression a sweep exists to
  catch.
- **Hybrid retrieval, evidence-shaped.** Up to four legs — BM25 over an
  identifier-aware tokenization of the query, BM25 over path tokens from
  files touched, cosine against API embeddings, and personalized PageRank
  from tag/file/campaign seeds with hub-resistant specificity weights —
  fused with weighted reciprocal-rank fusion (k=60), then boosted by
  re-establishment count (a lesson filed by many runs outranks a one-off)
  and gentle recency decay on lessons only.
- **The embedder is quarantined.** `scripts/embedder.py` is a closed
  provider registry — `voyage` (default `voyage-code-3` at 256 dims;
  Anthropic's documented embeddings partner, and its code-tuned models
  lead code retrieval), `openai` (`text-embedding-3-small` at 512),
  `gemini` (`gemini-embedding-001` at 768), or `none` — resolved by key
  presence (`VOYAGE_API_KEY`, then `OPENAI_API_KEY`, then
  `GEMINI_API_KEY`) or forced with `FPL_EMBEDDER`; an unknown value is a
  hard error, never a silent fallback, and
  `FPL_EMBED_MODEL`/`FPL_EMBED_DIMS` tune the model.
  Vectors are cached by content hash (rebuilds re-embed only what
  changed), stored unit-normalized one file per model, and compared by
  brute-force dot product — at this corpus size a vector database would
  be a dependency, not a speedup. The API is the **single sanctioned
  non-deterministic input** in the memory layer, and it can only ever
  reorder advisory output: nothing embedded is stored as truth, gates
  anything, or suppresses anything. Keyless is a fully supported mode —
  BM25 + graph serve, and every result names the absent leg.
- **Injection is budgeted per site.** SessionStart injects the top 5
  lessons ranked against the work file and the session's touched paths —
  offline, never rebuilding, capped at 1200 bytes, elisions named,
  `FPL_RECALL_INJECT=0` to demote. `/fluxpoint:graph-run` seeds sweeps
  through `recall.py --format seedmap`, which emits exactly the shape
  `memory.py --load` prints with keys relevance-ordered — the compiled
  graph's advisory-seed contract is untouched. A node declaring
  `memory: {priors: true}` additionally hands its refuters the seed tag's
  killed claims with the objections that killed them (5 items, 400 chars
  each), framed as priors the panel may overturn — the panel stops paying
  to rediscover arguments the store already holds, and the finder's
  prompt stays clean. `/fluxpoint:recall` serves humans. A per-prompt
  UserPromptSubmit hook exists but ships **dark** behind
  `FPL_MEM_PROMPT=1`: offline, 3 items, 1200 bytes, and a precision floor
  — two independent retrieval legs, or a lexical match on at least two
  informative query tokens; the graph leg never corroborates, because its
  seeds come from the lexical top ranks — off by default because the
  strongest external result says wrongly retrieved memories cost more
  than none.
- **Curation is a campaign, not a daemon.** `templates/WORK.consolidate.md`
  reads the store, proposes merges and restatements as findings, has a
  skeptic attack each one with the killed priors in hand, and lets
  `record-run.py` file the survivors through the same single-writer path —
  supersession by append, never deletion. Run it by hand or from a
  scheduled Routine.

What it refuses, on purpose: LLM extraction or reranking anywhere in the
write or rank path; any new dependency (no numpy, faiss, sqlite-vec, or
local models); graph databases, bundled MCP servers, daemons; new writers
to `memory.jsonl`; and retrieval-driven suppression of any kind — ranking
decides what reaches a prompt first, never what gets judged.


## Corpus configuration and safe refresh

Recall discovers every Claude Code auto-memory folder on the path from the
repository root to the filesystem root, under `CLAUDE_CONFIG_DIR` (default
`~/.claude`). An empty child folder never hides an ancestor's notes. It reads
Markdown topic files with optional `name`, `description` and `metadata` front
matter, and indexes passages of at most 1,200 characters. `MEMORY.md`, dotfiles,
symlinks and backups are skipped. Unreadable, invalid UTF-8 and files over 1 MiB
are skipped with a named diagnostic. Results include at most one passage per
origin and file.

Settings survive plugin upgrades in
`$XDG_CONFIG_HOME/fluxpoint/recall.json` (default
`~/.config/fluxpoint/recall.json`), or the file named by `FPL_RECALL_CONFIG`:

```json
{
  "refresh": true,
  "corpora": [
    {
      "path": "/srv/mirror/host-b/memory",
      "origin": "host-b",
      "format": "claude-automemory",
      "weight": 0.8
    }
  ]
}
```

`markdown` is also accepted for ordinary topic folders. Corpus paths are read
only; recall does not synchronize them. Each origin must be unique and use
letters, numbers, dots, underscores or dashes. IDs include the origin, so files
with the same basename in two corpora remain distinct. `weight` defaults to 1
and must be greater than zero and at most 2.

Repository stores build a small index under
`.claude/fluxpoint/index/<schema>/`. External note corpora build once under
`$XDG_CACHE_HOME/fluxpoint/recall/<schema>/<corpus-hash>/`, falling back to the
user's platform cache directory. Checkouts that resolve to the same corpus
roots and options share that slot; note bodies and vectors are not written into
their repository indexes. Codex corpora also include the project scope in the
key, because different projects must not reuse each other's filtered passages.
Search fuses each slot's lexical ranks before combining its retrieval legs.

The schema hash binds the kind registry, tokenizer and source fingerprint
implementation. A different schema gets a different slot, so plugin versions
can read their own caches without rewriting another version's files. `--stats`
prints both slots, their schema and the plugin version that last built each.
Legacy unversioned caches are ignored and remain disposable derived files;
source memory and corpus folders are never rewritten by this migration.

Codex stores are discovered through `$CODEX_HOME/memories` (default
`~/.codex/memories`) in either runtime. The `codex-memories` format reads
`MEMORY.md`, `memory_summary.md`, `raw_memories.md` and `rollout_summaries/*.md`.
Aggregate files split at headings and preserve the heading path on each card.
Rollouts preserve `thread_id` and `updated_at`; only a `cwd` inside the current
project is eligible. A configured corpus can map peer paths with
`"pathAliases": {"/peer/work/report-service": "/local/report-service"}`.
An explicit corpus entry overrides automatic discovery of that folder.

Aggregate passages without `cwd` need an ancestor heading with an exact
absolute checkout path, for example `Project: /work/report-service` (or `Repo:` /
`Repository:`). A bare name such as `Project: frontend` is excluded by default:
unrelated repositories can share that basename. To use a display name, map it
explicitly in the corpus configuration:

```json
{
  "corpora": [{
    "path": "/home/user/.codex/memories",
    "origin": "local-codex",
    "format": "codex-memories",
    "projectScopes": {"frontend": "/work/customer-a/frontend"}
  }]
}
```

`projectScopes` values must be absolute local project root paths. The mapping
admits that repository and its known sibling worktrees; an unrelated repository
called `frontend` does not inherit it. Full-path headings and `cwd` can also use
configured `pathAliases`. Unbound passages are skipped to keep the user-wide
Codex store from mixing unrelated projects.
Set `"allowUnscoped": true` on an explicit `codex-memories` corpus only when
its global preferences and cross-project notes are intended for this index.
This opt-in never disables the rollout `cwd` filter. Files stay read only;
symlinked files and rollout directories are skipped.

A note's `metadata.modified` supplies its date (file mtime is the fallback), and
`metadata.originSessionId` preserves the session. `metadata.supersededBy` or
`status: superseded` hides an obsolete note unless `--include-superseded` is
requested. Among notes with the same `name`, the newest timestamp wins across
origins. Dates with offsets are compared as instants. Recall prints the source
origin, date and session and applies the same recency decay used for lessons.
Lesson histories are ordered by their establishment time and run ID, so merging
JSONL files does not let a later file position replace newer knowledge.

SessionStart now runs recall in non-git roots too. It requests a detached,
keyless refresh when sources exist and the index is absent or stale. The hook
returns immediately; the next session can use the completed index. One refresh
lock per root expires after five minutes if a worker dies. Set
`FPL_RECALL_REFRESH=0`, or `"refresh": false` in the configuration, to disable
this launch. Plain `recall.py --for-session` only reads; pass `--refresh` to
request the same background behavior explicitly. Concurrent builds serialize;
cache files are replaced atomically, metadata is published last, and a reader
refuses mixed generations.

An ordinary query may rebuild the lexical index, but never backfills document
embeddings. Document backfill requires `--build --embed`; external notes also
require an explicit provider in `FPL_EMBEDDER`. A provider key exported for some
other tool is insufficient consent to embed external notes. Detached refresh
and session/prompt hooks use no embedding calls.

Workers can rank context against a supplied brief: set `FPL_RECALL_QUERY` to
query text, or `FPL_RECALL_QUERY_FILE` to a UTF-8 brief path. Text takes priority;
the file has front matter removed and is capped at 512 UTF-8 bytes. Without
either setting, recall uses WORK.md or LOOP.md. The section names its query
source. The opt-in prompt hook now counts distinct meaningful input words,
excluding English stopwords, so a single `DataLoader` cannot clear its
two-word precision floor through tokenizer pieces.

## Durable lessons across worktrees

Lessons and copies of their run artifacts live under
`<git-common-dir>/fluxpoint/{memory.jsonl,runs/}`. Every worktree of a clone reads
that store, and deleting the originating worktree preserves its lessons and
decisions. Non-git roots retain `.claude/fluxpoint/`. `FPL_MEMORY_HOME` overrides
the store location for an explicitly shared or externally synchronized home.
Appends hold a cross-process lock across history inspection and writing; readers
use the same lock.

The current checkout's old `.claude/fluxpoint/memory.jsonl` and run artifacts
remain readable. Before removing an older worktree, import its legacy store:

```
python plugins/fluxpoint/scripts/memory.py --root /path/to/checkout --adopt /path/to/old-worktree
```

Adoption is idempotent and leaves the original files in place. Recall unions
shared and local source artifacts and invalidates its cache when either changes.


### Scoped reader and warm-process identity

Codex aggregate sections inherit metadata only from their own heading ancestors.
A later section's `cwd`, timestamp or thread ID cannot relabel earlier sections,
and metadata quoted in a rollout body cannot override its leading header. Fenced
Markdown examples do not create scope boundaries. Local cwd paths resolve links
before comparison, so a link into another project cannot widen a project view.

Git sibling worktrees use their common repository identity and known checkout
paths when scoping Codex memory. They can read the canonical repository's
headings and rollouts and share one corpus cache when their corpus configuration
agrees. Unrelated repositories retain distinct scoped slots. Reader-policy
changes use a new schema namespace, including helper and configuration code.

Warm prompt workers also distinguish default configuration, cache and home path
environments. Relative path selectors are resolved for the process key while
retaining their original spelling, so a fixed daemon state directory cannot
make a new configuration inherit an older worker's corpus environment.
