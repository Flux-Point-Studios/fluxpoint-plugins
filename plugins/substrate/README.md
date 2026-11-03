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
  prose-smell gate (both below). A `PreToolUse` hook on `Bash|PowerShell`
  runs the opt-in mail-sender check described below.
- `scripts/send-chokepoint.mjs` — detects recognized mail-send paths outside
  the repository's declared sender. Dormant without `.send-chokepoint.json`.
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
codex plugin add substrate@fluxpoint
```

Onboard with `/substrate:init <repo>`
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

A repo without a manifest is silently ignored by the graph: by doctrine,
that absence marks demos and pitch artifacts as outside the substrate.
A `deliverables.json` in that directory is still checked.

Discovery groups Git worktrees by their real common Git directory and uses
one checkout: the main worktree when present, otherwise the first directory
by name. Separate clones are also grouped when they share a normalized
`remote.origin.url`, or a local clone's origin points at another discovered
checkout. For a local clone, the source checkout takes precedence. All passes
use that checkout's files, including the manifest, imports, staleness, and
ledger; a note names the ignored directories in `--check`, `--json`, and
`SUBSTRATE.md`. No fetch or other network access is needed.

Directories with the same manifest `repo` label but no shared Git identity
produce one problem naming their `substrate.json` files. Exclude unwanted
copies with `excludeRepos`. The graph keeps the first directory's version of
any colliding id and still validates the other manifests' consumes targets;
the emit gate stays red until the directory conflict is resolved. Duplicate
ids across different repo labels, or inside one manifest, remain problems
naming the exact files.

## Config reference

Optional `substrate.config.json` at the workspace root:

| Key | Default | Meaning |
|---|---|---|
| `graceHours` | `6` | A manifest edit within this many hours of the code change counts as the same shipment and never alarms. |
| `excludeDirs` | `[]` | Extra exact directory names excluded from top-level manifest and ledger discovery and pruned at every depth of import and non-git staleness walks, on top of the built-ins: `node_modules`, `.git`, `dist`, `build`, `out`, `coverage`, `__pycache__`. |
| `excludeRepos` | `[]` | Top-level directory names or patterns (`*` and `?`) excluded from manifest and ledger discovery only, e.g. `["alpha-copy", "wt-*"]`. Matching names inside a retained repo are still scanned. |
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
it can prove: a missing cited path within a repository at the root or along
the cited path (marked by `.git`, including a worktree gitfile, or
`substrate.json`), a `MEMORY.md` index entry pointing at a missing memory file,
and an "unsent" claim contradicted by a `deliverables.json` `sentAt`. Detection is
mechanical and precision-first — URLs, API-endpoint fragments, command lines
with flags, and citations without a workspace anchor stay silent, because a
false alarm teaches the reader to ignore the channel. A plain directory such
as a home directory's `bin/` does not anchor a citation. For spans such as
`tools/run.py check` and `tools/run.py:12 SYMBOL`, only the leading path is
checked; later slash-bearing words do not create citations. Repeated references
to the same normalized path in one memory count as one finding, including
references with different line numbers or slash styles. The lint never rewrites
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

## Mail-sender gate

Opt in with `.send-chokepoint.json` in the project or a parent directory:

```json
{
  "sender": "tools/outbound_mail.py",
  "exempt": ["services/mailer"],
  "providers": ["graph.microsoft.com"]
}
```

The `Bash|PowerShell` pre-tool hook checks inline commands, readable referenced
scripts and their supported local imports for both a watched provider host and
a recognized send verb. A detected send outside the declared paths exits 2
before execution. `sender` is an exact path; each `exempt` entry also permits
its descendants. Paths resolve against the configuration's directory, so a
same-named file elsewhere does not inherit permission. Leading `cd` commands
and Windows paths are covered by regression fixtures.

A `Bash` command made only of read-only tools is not judged, so the files the
gate protects can still be read, searched, hashed and compared. The exemption
reads bash grammar, so it covers the `Bash` tool alone: PowerShell takes no
backslash as an escape, and a separator bash reads as text can end a
PowerShell statement, so every `PowerShell` line is judged on its full text.
The tools are
`cat`, `head`, `tail`, `wc`, `grep`, `egrep`, `fgrep`, `sha1sum`,
`sha256sum`, `sha512sum`, `md5sum`, `b2sum`, `cksum`, `diff`, `cmp`, `comm`,
`file`, `stat`, `ls`, `readlink`, `realpath`, `basename`, `dirname`, `du`,
`od`, `xxd`, `hexdump`, `strings`, `uniq`, `cut`, `nl`, `echo`, `printf` and
`true`, each named bare, so `./cat` is judged. They may be joined by pipes,
`;`, `&&`, `||`, `&` and newlines, nested in `$(...)`, and run under
`timeout` or `nice` with their documented options or under `env` with no
option. Quoting and line continuations are read as bash reads them, so
`grep -E "send|reply" sender.py` is exempt while `cat sender.py | python3`
and `echo $(python3 sender.py)` are judged. The whole line is judged when any
of these holds:

- It runs any other program. That includes `git`, whose every subcommand
  honours repository configuration an agent can write, and `rg`, `ugrep`,
  `sort`, `less`, `more`, `sed` and `awk`, which each have an option or
  command that runs a program.
- A read-only tool carries an option that runs a program or writes a file,
  in any abbreviation: `grep`, `egrep` or `fgrep` with `--filter`,
  `--pager`, `--view`, `--config`, `---FILE`, `-Q`/`--query` or
  `--save-config` (in the Claude Code shell `grep` runs ugrep, where these
  name a command, load a file that can, start a TUI that runs a pager on a
  key press, or write a file), `diff` with `-l` or `--paginate` (it runs
  `pr`), or `file` with `-z`, `-Z` or `--uncompress` (it runs decompressors
  found on `PATH`). One of these tools' arguments being an expansion or a
  glob judges the line too, since it could produce such an option.
- `uniq` or `xxd` has an argument that is not an ordinary file, as defined
  for redirections below: each writes its second operand.
- It assigns a variable: `NAME=value`, `env NAME=value`, `printf -v NAME`, or
  a `{NAME}>` redirection.
- `env` carries an option (`-S` splits its value into a new command line), or
  `timeout` or `nice` carries one they do not document.
- A redirection opens anything except an ordinary file or `/dev/null`,
  `/dev/stdin`, `/dev/stdout` and `/dev/stderr`: `/dev/tcp/HOST/PORT` and
  `/dev/udp/HOST/PORT`, which bash opens as sockets, any other path under
  `/dev`, `/proc` or `/sys`, or a target built from an expansion. `<&` and
  `>&` may also duplicate descriptor 0, 1 or 2 or close one with `-`; any
  other descriptor was inherited from whatever started the shell. bash
  expands a `>&` target that is not a descriptor a second time, so a file
  named there must be plain text: letters, digits and `_ . / , : = % + @ -`.
- The reader cannot follow it: backquotes, arithmetic, subshells, process
  substitution, `${...}` operators, a backslash-newline right after `$`, `&`
  or a redirection operator (bash joins it to what follows), an unterminated
  quote or heredoc, or nesting deeper than the reader's stack.

A heredoc body is data. A line whose commands are all read-only is not judged
when it writes a heredoc, so `cat > probe.py <<EOF` may write a script that
names a sender. bash expands an unquoted body, so one holding a command
substitution, arithmetic, a `${...}` operator or a trailing backslash leaves
the line judged. A body is quoted, and always data, when a quote or an
escaping backslash appears in its delimiter (`<<'EOF'`, `<<"EOF"`,
`<<\EOF`); a backslash-newline there quotes nothing. Any other line is
judged on its full text, bodies included, so `python3 <<EOF` and a
write-then-run in one command are still caught.

The full judgment reads every line the exemption does not cover, and every
`PowerShell` line, with the same reader. Here it follows lists, pipelines,
subshells, `{ }` groups, functions (`NAME ()` and `function NAME`, before
any compound command), `if`/`while`/`for`/`case` (with newlines before
`in`), `time` and `time -p`, `$(...)`, backquotes, arithmetic, process
substitution, `${...}` operators, bash 5.3's `${ list; }` and heredocs,
whose delimiter is the word with quotes removed and nothing expanded, so
`<<$D` ends at a line `$D` (a delimiter holding a command substitution
is not followed). A script is a name ending in `.py`,
`.mjs`, `.js`, `.ts`, `.ps1`, `.sh` or `.rb`. Script names are taken from
the raw text and from every word as bash builds it:

- quotes, escapes, line continuations and `$'...'` joined, so
  `bl''ast.py`, `"bl"ast.py`, `bl\ast.py` and `$'bl\x61st.py'` are all
  `blast.py`;
- `~`, `~+` and `~-` expanded to each value HOME, PWD and OLDPWD may
  take: HOME from the hook's environment, PWD and OLDPWD as every
  directory the line may be in, and each of them as the line assigns it;
- a variable given its value in the hook's environment (`$PWD` is the
  command's own directory), and also each value the line assigns it: in
  a command made only of assignments, before a command's name (`env`'s
  included), in `export`, `declare`, `typeset`, `local` or `readonly`,
  and as a `for` loop's words; `$PWD` and `$OLDPWD` also take every
  directory the line may be in;
- brace lists and sequences expanded as bash expands them: only braces
  it reads unquoted, found by bash's own matching rules, in the word as
  written and before its variables, so `$x{a,b}` is `$xa` and `$xb` and
  a quoted `'{a,b}'` stays text;
- an unquoted glob matched against the filesystem from the command's
  directories (a `case` pattern and the operands of `[[` are not globs).
  Extglob groups (`@(...)`, `?(...)`, `*(...)`, `+(...)`, `!(...)`) are
  read into their word and matched as patterns always, since bash refuses
  a line holding one while extglob is off; `!(...)` matches as `*` does.
  `nocaseglob`, `globstar` and `dotglob` count as on for the whole line
  when any word it reads names one of them (as `shopt -s` or a shell's
  `-O` would), `GLOBIGNORE` turns `dotglob` on the same way, and all
  three count as on when `shopt`, `-O` or `BASHOPTS=` takes a value
  built from an expansion. With `globstar`, `**` walks every directory
  below, as bash does, without following symlinks;
- each `cd` or `pushd` target (none means HOME, `-` means OLDPWD) and
  each `env -C` or `--chdir` directory, built the same way, globs
  included, and resolved against every directory the line may be in:
  directly and, for a name not starting with `/`, `.` or `..`, through
  each CDPATH entry the environment or the line gives, both as `cd`
  joins it and as the kernel resolves it. This
  repeats until no new directory appears, so neither the order of the
  line's commands nor a loop hides one, and only a directory that exists
  counts;
- for `find -execdir` or `-okdir`, the script names in its command
  resolved in every directory under find's starting points, walked as
  find walks them.

A relative name is joined to each directory the line may be in without
removing `..` first, as the kernel joins it, so `link/..` is the parent
of where `link` points, and every copy found is judged. A name without a
slash is also looked for in each directory of PATH, as the hook's
environment and the line set it (`~` expanded, an empty entry meaning
the directory itself), since bash finds a command, a `source` or `.`
operand and a script a shell does not find in its directory there, and
so do the programs that run others. Each existing
script found is judged by its text and its local Python imports, as
above. The sender and the exempt directories are compared with it after
the kernel resolves symlinks, so a path that reads as the sender but
leads elsewhere is judged. The inline send check reads the built words
as well as the raw text.

Shell code inside the line is read the same way, to any depth: the `-c`
string of `sh`, `bash`, `dash`, `zsh`, `ksh`, `mksh`, `ash`, `yash` or
`posh` named anywhere in a command (so under `sudo`, `xargs` or
`find -exec` too); `env`'s `-S` or `--split-string` string, split as GNU
env splits it, after which env's options are read again; `eval`'s
arguments; a `trap` action; an alias value, set by `alias NAME=VALUE`
or `BASH_ALIASES[NAME]=VALUE` (the Claude Code shell expands aliases,
reading the value in place of the name); and the commands a shell at the
head of a command reads from a heredoc or here-string. A shell is at the head of a
command when only assignments, reserved words, unquoted expansions that
come out empty, and `sudo`, `doas`, `env`, `exec`, `time`, `nice`,
`timeout`, `nohup`, `setsid`, `stdbuf`, `xargs`, `command` or `builtin`
with their options come before it. A word names a shell through its text, its value
in the hook's environment or a value the line assigns it. Every other word,
every heredoc body and the inside of `((...))`, which POSIX shells read as
two subshells, is also read as a command line for the script names in it,
since it may run as code: written to a file and run, handed to `su`,
`watch` or `ssh`, or kept in an arithmetic subscript. That
reading refuses nothing and stops eight strings deep, and its braces,
globs and variable values are expanded from a second budget the size of
the one below, which blocks nothing when it runs out.

The line is blocked, whatever it names, when it runs shell code the gate
cannot read:

- a `-c` or `-S` string, `eval`'s arguments, a `trap` action or an
  alias value that is built from an expansion, missing from the line, or
  that the reader cannot follow; an alias value that ends in a
  backslash, which joins it to the line after it; and a write to
  `BASH_ALIASES` other than `BASH_ALIASES[NAME]=VALUE` written out;
- an expansion among the options of a shell or `env` at the head of a
  command, where it may be `-c` or `-S`, or a command named by an
  expansion and given `-c`;
- a shell at the head of a command that reads its commands from standard
  input (it has no script operand, or `-s`, or `/dev/stdin` or one of its
  aliases as its script), unless a heredoc or here-string written out in
  the line or an ordinary file supplies that input: a pipe, a descriptor,
  a process substitution, a path under `/dev`, `/proc` or `/sys`, the input
  it inherits, or a heredoc or here-string built from expansions;
- a script operand of a shell at the head of a command, `source` or `.`
  that is a process substitution, another path under `/dev`, `/proc` or
  `/sys`, or a word built from an expansion while the command reads a pipe
  or a redirection;
- a `Bash` line the reader cannot follow to its end, its call stack
  included, since bash goes on past whatever stopped the reader; a line
  from another tool, read with bash's grammar only for the names in it,
  only when it also names a shell, `env`, `eval`, `trap` or `source`;
- braces, globs and variable values in the words bash reads that
  together spend more than 4096 units: one for each word a brace or a
  variable value makes, plus one for each further 1024 characters in
  it, one for each directory a glob reads and each path it visits, one
  for each directory a `find -execdir` walk visits, and one for each
  1024 unquoted `{`, `,`, `}` and `.` looked at while finding a pair;
- a line that may run in more than 64 directories.

Shell code is read with bash's grammar, whichever of those shells runs it.
Not resolved: a value computed while the line runs (command output,
arithmetic, other `${...}` operators, `read`, `printf -v`, arrays,
positional parameters from `set --` or a function's arguments), a name
another program supplies (`find -exec ... {}`, the input of `xargs`), a
value or an alias an earlier command set that the hook does not see,
`~user`,
a directory another program changes into (`git -C`, `make -C`, `sudo -D`),
a script name without one of those extensions, code another interpreter
assembles (`python3 -c`), syntax only `zsh` or another shell gives a
meaning, a script operand built from an expansion in a loop body that reads
a pipe, and the scripts a script file runs.

Omit `providers` to use the built-in Microsoft Graph, Gmail, SendGrid, Mailgun,
Postmark and Resend hosts. This is a static routing check: it does not send
mail, grant authorization, verify recipients or prove that the approved
sender enforces its own checks. Dynamic URLs, the values listed as not
resolved above and unsupported imports can escape detection. Missing
configuration, malformed payloads and unreadable files allow execution;
configuration problems are reported when observed. Claude Code runs a
command whose hook fails, runs out of memory or outlives the 15 seconds
`hooks.json` gives it, so these are blocked: a line the judgment itself
fails on, for instance by running out of call stack; a line longer than
262 144 characters (256 KiB), without being read, since reading costs
memory in proportion to its words; and a line not judged within 10
seconds, a limit `SEND_CHOKEPOINT_DEADLINE_MS` in the hook's environment
changes. The scans of the line's text and of a script's text each run
in one pass, and the work beyond them is bounded by the budgets above.
Do not treat it as an egress sandbox. Its tests use local fixture files and
never contact mail providers.

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
