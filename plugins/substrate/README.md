# substrate

A registry for multi-repo workspaces that makes reuse checkable instead of
remembered. Each repo declares its reusable primitives — engines, APIs,
pipelines, toolkits, agents, patterns — in a `substrate.json` manifest;
`substrate-graph.mjs` compiles every manifest into one generated
`SUBSTRATE.md` graph of nodes, consumes-edges, orphans (dormant value) and
hubs (harden first); and a `SessionStart` hook injects the compact summary
plus staleness alarms into every session, including post-compaction, so the
sweep-before-build doctrine has something deterministic to sweep.

## Layout

- `scripts/lifecycle.mjs` — zero-dependency validators the checker leans on:
  strict ISO-8601 timestamp parsing, duplicate-JSON-key detection, and
  per-entry deliverable indexing.
- `scripts/substrate-graph.mjs` — the whole engine: zero dependencies,
  Node ≥ 18, cross-platform. `--emit` writes `SUBSTRATE.md` and exits 1 on
  manifest problems (the CI half); `--check` prints the summary and always
  exits 0 (the session half); `--json` prints the same graph as
  machine-readable JSON — deterministic, sanitized, exit 1 on manifest
  problems — so other tools (fluxpoint's recall layer among them) consume
  a projection instead of parsing prose or raw manifests. Root
  resolution: `--root <dir>`, else `CLAUDE_PROJECT_DIR`, else the current
  directory.
- `hooks/hooks.json` — one hook set for Claude Code and Codex.
  `SessionStart` with no matcher, so startup, resume, clear, and
  post-compaction all get the `--check` output; the output is factual
  statements only, and a stale manifest is reported, never commanded. A
  second `SessionStart` hook runs `memory-lint.mjs` over the workspace's
  memory files, and a `PostToolUse` hook on file writes runs the
  prose-smell gate (both below).
- `commands/` — `/substrate:init` (onboard a workspace), `/substrate:status`
  (run and interpret `--check`), `/substrate:emit` (regenerate, and keep the
  manifest edit in the same commit as the primitive it describes),
  `/substrate:smell` (scan prose before it ships). On Codex each is reached
  through the matching `skills/substrate-<name>/` skill.
- `templates/` — `DOCTRINE.snippet.md`, the sweep doctrine, ready to append
  to a workspace `CLAUDE.md` or `AGENTS.md`; `WRITING.snippet.md`, the
  outward-prose rules a regex cannot check.
- `tests/` — `node:test` suites over temp-dir fixture workspaces; wired into
  this repo's `scripts/harness.sh --full`.

## Install

Claude Code:

```
/plugin marketplace add Flux-Point-Studios/fluxpoint-plugins
/plugin install substrate@fluxpoint
```

Codex:

```
codex plugin marketplace add Flux-Point-Studios/fluxpoint-plugins
```

then enable `substrate@fluxpoint`. Onboard with `/substrate:init <repo>`
(Claude Code) or `$substrate-init <repo>` (Codex) in the workspace that
contains your repos.

## Manifest schema

`<workspace-root>/<repo>/substrate.json`, discovered exactly one level under
the root:

| Key | Type | Meaning |
|---|---|---|
| `repo` | string | Display and sort name for the repo (usually the dirname). |
| `version` | number, optional | Schema version; `1` is tolerated and currently the only value. |
| `primitives` | array | The repo's reusable primitives. Empty is valid — the repo still appears in the graph. |
| `primitives[].id` | string | Kebab-case, globally unique across the workspace. Duplicates are a manifest problem. |
| `primitives[].name` | string | Short human title. |
| `primitives[].desc` | string | One line on what it computes and for whom. |
| `primitives[].kind` | string | e.g. `engine`, `api`, `pipeline`, `toolkit`, `agent`, `pattern`. |
| `primitives[].paths` | string[] | Repo-relative files or directories that are the primitive. |
| `primitives[].consumes` | string[] | Ids of primitives this one builds on; `[]` for none. Unknown ids are a manifest problem. |
| `primitives[].status` | string | e.g. `live`, `core-only`. |
| `primitives[].terminal` | boolean, optional | A delivered surface a human uses directly. Terminal primitives are listed in their own section and excluded from the orphan list, because zero in-edges is their correct end state rather than dormant value. |

A repo without a manifest is silently ignored: by doctrine, that absence
marks demos and pitch artifacts as outside the substrate.

## Config reference

Optional `substrate.config.json` at the workspace root:

| Key | Default | Meaning |
|---|---|---|
| `graceHours` | `6` | A manifest edit within this many hours of the code change counts as the same shipment and never alarms. |
| `excludeDirs` | `[]` | Extra directory names pruned from non-git staleness walks, on top of the built-ins: `node_modules`, `.git`, `dist`, `build`, `out`, `coverage`, `__pycache__`. |
| `nonGitRepos` | `{}` | Map of non-git repo dirname → relative code paths its staleness check may walk. Undeclared non-git repos are skipped from staleness with a one-line note, never walked blind. |

## Staleness semantics

- **Git repos** (detected via `git rev-parse`, so worktrees and gitfile
  checkouts count): the alarm fires when the last commit is more than
  `graceHours` newer than the manifest file's mtime **and** at least one
  committed file other than `substrate.json` changed in that window. A
  re-commit touching only the manifest is not drift.
- **Declared non-git repos**: file mtimes under the declared paths stand in
  for commit times. Walks are capped at depth 6 and 5000 entries; a capped
  walk says so, and its counts are a lower bound.
- **git missing from PATH**: one note, staleness skipped for git repos —
  never per-repo error spam. `--check` exits 0 regardless of what it finds.

## Deliverables ledger

Task boards and scratchpads that hold "built but not yet sent" state do not
survive an assistant's context compaction; a file in the repo does. Any repo
under the root — with or without a `substrate.json` — may carry a
`deliverables.json`:

```json
{
  "deliverables": [
    {
      "id": "vendor-bundle-round-3",
      "recipient": "Avery",
      "artifact": "vendor-bundle-2026-08-10.zip",
      "builtAt": "2026-08-10T22:00:00Z",
      "sentAt": null
    }
  ]
}
```

Every entry with a `builtAt` and no recorded ending is an open obligation:
`--check` prints `ALARM: UNSENT deliverable: <repo>/<id> for <recipient> —
built <age> ago (<artifact>)` at session start, every session, until someone
records how it ended. Create the entry when the build **starts**, not when it
finishes — the half-built state is exactly what a compaction orphans.
Malformed ledgers and undated entries surface as problems, never alarms and
never crashes: the ledger is read as a regular non-symlink file, capped at
1 MiB and 5,000 entries before it is parsed, and a single malformed or
duplicate-id entry reports only itself — the register keeps alarming every
other open obligation rather than going dark on one typo. `builtAt` must be a
full ISO-8601 instant. All printed fields are sanitized and hard-capped before
they reach the injected session context.

### How an obligation ends

`sentAt` means one thing: an email left. An obligation that ended any other
way closes on three fields together — `closedAt`, a `closedReason` of
`superseded` / `withdrawn` / `answered-elsewhere`, and a `closedBecause`
sentence a future reader can audit:

```json
{
  "id": "billing-preview",
  "recipient": "Jordan",
  "builtAt": "2026-08-15T15:06:19Z",
  "sentAt": null,
  "closedAt": "2026-08-20T00:00:00Z",
  "closedReason": "superseded",
  "closedBecause": "reshaped 8/20 into a self-serve preview the recipient tests directly; the emailed preview no longer exists"
}
```

Without this, the only way to quiet a superseded or withdrawn item was to
write a send that never happened — a register that had to lie to stay quiet,
whose alarms then read as noise and got skimmed. A closure missing its reason
or its explanation keeps alarming and says what it needs, because going quiet
on a typo is the failure the field exists to end; an entry carrying both
`sentAt` and `closedAt` is a problem, since a send and a non-send closure
cannot both be true.

When the evidence proves only the calendar date rather than an instant, use
the day-precision fields `sentOn` and `closedOn` with `YYYY-MM-DD`; never put
a date-only value in an `*At` field. The `*At` and `*On` forms are mutually
exclusive per obligation, and day-precision chronology is exclusive of its
bounds. One seam to know: `memory-lint` still recognises `sentAt` only, so an
obligation closed with `sentOn`/`closedOn` reads as unsent to that lint until
it is taught the day-precision fields.

An entry may also carry a `warning`. It rides the alarm line as
`⚠ DO NOT SEND COLD: <warning>` — for the artifact that is built and still
owed, but whose claims have gone stale underneath it.

## The import-scan lint

A graph assembled from hand-written JSON that nobody validates is a map that
lies, and the sweep-before-build doctrine makes that map the mandatory first
step. So the emit verifies itself: for every primitive, it walks the declared
paths, reads each `.js` / `.mjs` / `.cjs` / `.ts` / `.py` file with comment and
docstring bodies blanked out, resolves the relative and intra-repo imports it
finds (`import`, `import()`, `require()`, `export … from`, and Python's
relative, sibling and dotted-package forms), maps each resolved file back to
its owning primitive, and reports a problem when a real import crosses a
primitive boundary that no `consumes` edge declares:

```
problem: import lint: tool -> core undeclared (js/src/tool.mjs:1 imports ./core.mjs)
```

`--emit` exits 1 on those, so a manifest edit and its emit ride the same
commit and a stale graph blocks instead of regenerating quietly. One line per
`from -> to` pair, naming the first site, so closing the list is mechanical.

Ownership rules: a declared directory owns the code files under it, the most
specific declaration wins (a primitive that names one file keeps it even when
a sibling declares the whole directory), and an import into a file no
primitive declares is repo-internal and never a miss.

What the lint cannot see — and what therefore stays hand-declared: HTTP seams
between processes, Apex and other non-scanned languages, cross-repo imports,
and dependencies expressed as file handoffs. A green lint means no import
contradicts the manifest, never that the manifest is complete.

## The memory lint

Memory files rot the same way manifests do: a memory cites a module that was
since renamed, the index points at a deleted entry, a "still unsent" claim
outlives the send it describes. `memory-lint.mjs` runs at session start over
`~/.claude/projects/<key>/memory/` and alarms on the three staleness classes
it can prove: a cited path whose anchor directory exists but whose file is
gone, a `MEMORY.md` index entry pointing at a missing memory file, and an
"unsent" claim contradicted by a `deliverables.json` `sentAt`. Detection is
mechanical and precision-first — URLs, API-endpoint fragments, command lines
with flags, and citations without a workspace anchor stay silent, because a
false alarm teaches the reader to ignore the channel. The lint never rewrites
a memory: the agent reading the alarm owns the repair, with the truth in
front of it. Echoed content is control-character-stripped and capped, and the
exit code is 0 unconditionally — a lint that can wedge a session start is
worse than the staleness it reports.

## The prose-smell gate

`prose-smell.mjs` rides every `Write`/`Edit` of a prose file (`.md`,
`.txt`, `.html`, `.eml`) as a PostToolUse hook and fails the write (exit 2,
findings on stderr) when the text carries a frame that marks generated
prose. It is a regex gate and says so: it catches spellings, and the
WRITING doctrine snippet covers what a regex cannot.

| Rule | Severity | What it matches |
|---|---|---|
| `negation-contrast`, `more-than-just` | HIGH | the canonical contrastive frame: `isn't just about X — it's about Y`, `is more than just` |
| `contrast-comma`, `contrast-slogan` | HIGH on one hit | the slogan spellings of the same frame: `a computation, not a decision`; `The product is not the gap.` A comparand that keeps going (`, not the number the vendor quoted in …`) is a sentence and is left alone, as is a qualifier (`counsel, not full-time`, `, not yet`, `, not because …`). |
| `rather-than`, `instead-of`, `contrast-never`, `no-and-no`, `would-rather-than` | by density | the connective spellings, counted together per 500 words with the denominator floored at 500: two or three is MEDIUM, four or more is HIGH. One `rather than` in a page is English. |
| `whether-your`, `assistant-artifact`, `stock-vocab` | HIGH | inclusive triples, leftover assistant phrasing, stock LLM vocabulary |
| `from-to-coverage`, `concluder`, `em-dash-density`, `aphoristic-restatement` | MEDIUM | advice: a coverage opener, a summary opener, heavy em dashes, a slogan of eight words or fewer with a copula right after a sentence of twenty-five or more |

Fenced code blocks and inline `` `code` `` spans are blanked before
scanning, with offsets preserved, so a bug report that quotes the tell it
is about is not flagged for quoting it and every reported line number is
real.

**In-house prose.** Dense engineering documentation uses `X, not Y` as a
precision device several times a page, and a gate that failed every edit
to such a tree would be routed around. A repo therefore declares its own
docs in-house with a `.prose-smell.json` at its root:

```json
{ "contrastive": "advise" }
```

The file is found by walking up from the scanned file's directory. Under
it, the contrastive family is reported at MEDIUM (every finding still
prints, tagged `in-house: advised`) and the exit code stays 0; every other
rule keeps its severity. An artifact drafted outside such a tree gets the
default, which fails. A malformed config, an unknown key, or an unknown
mode is reported on stderr and ignored — the default applies — because a
config that could silently switch the gate off would be the inverse of
what a config is for.

## Honest limitations

- Discovery is exactly one level under the root: `root/repo/substrate.json`.
  Nested workspaces and deeper monorepo packages are not scanned.
- Staleness is a heuristic. It compares the manifest file's
  mtime against commit times (git) or file mtimes (declared non-git), so a
  fresh clone or copy — which resets mtimes — can defer a legitimate alarm
  until the next real commit. It answers "did code move after the manifest,"
  never "is the manifest's content correct."
- Non-git mtime semantics count file entries newer than the manifest, which
  is not identical to git's distinct-files-changed count.
- The graph is only as true as the manifests. The doctrine snippet
  (`templates/DOCTRINE.snippet.md`) is the enforcement mechanism: manifest
  updates ride the same commit as the primitive they describe.
