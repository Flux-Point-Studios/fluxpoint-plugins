---
description: Onboard a multi-repo workspace onto the substrate registry — scaffold substrate.config.json, seed a starter substrate.json in a named repo, generate SUBSTRATE.md, and install the sweep doctrine.
argument-hint: [repo dirname to seed first]
---

Onboard this workspace onto the substrate registry. Work through every
step; do not stop at scaffolding files.

1. Establish the workspace root: the directory that contains the repos as
   immediate children (discovery is exactly one level deep). Default to the
   current project directory; if the repos live elsewhere, ask once and use
   `--root <dir>` in every command below.
2. If `substrate.config.json` is absent at the root, create it:

   ```json
   {
     "graceHours": 6,
     "excludeDirs": [],
     "excludeRepos": [],
     "nonGitRepos": {}
   }
   ```

   Explain each key while writing it: `graceHours` is the window in which a
   manifest edit and its code changes count as one shipment; `excludeDirs`
   adds to the built-in excludes (`node_modules`, `.git`, `dist`,
   `build`, `out`, `coverage`, `__pycache__`) for top-level manifest and
   ledger discovery and for directory walks at every depth. `excludeRepos`
   excludes top-level directories from manifest and ledger discovery only;
   it accepts exact names and `*` / `?` patterns, such as `wt-*`, without
   pruning matching source directories inside retained repos. Git worktrees
   and clones sharing an origin are grouped automatically, with a note
   naming the checkout used and the copies ignored. `nonGitRepos` maps a non-git
   repo's dirname to the relative code paths its staleness check may walk —
   undeclared non-git repos are skipped from staleness entirely.
3. Pick the first repo to seed: "$ARGUMENTS" if provided, otherwise ask.
   Read that repo enough to propose real primitives — the engines, APIs,
   pipelines, toolkits, agents, or patterns another repo could consume.
   Write `<repo>/substrate.json`:

   ```json
   {
     "repo": "example-repo",
     "primitives": [
       {
         "id": "example-engine",
         "name": "Example engine",
         "desc": "One line on what it computes and for whom.",
         "kind": "engine",
         "paths": ["src/engine"],
         "consumes": [],
         "status": "live"
       }
     ]
   }
   ```

   Ids are kebab-case and globally unique across the workspace; `consumes`
   lists the ids of primitives this one builds on; `paths` are repo-relative.
   Demos and pitch artifacts get no manifest — that absence is deliberate
   and marks them as outside the substrate.
4. If the seeded repo is not a git repo, add its dirname and code paths to
   `nonGitRepos` in the config, or its staleness will be skipped.
5. Resolve `$ROOT` from `CLAUDE_PLUGIN_ROOT`, then `PLUGIN_ROOT`, or the
   parent of this loaded command's `commands/` directory. Use that installation
   throughout; the first recursive cache match may be an older version.
   Run `node "$ROOT/scripts/substrate-graph.mjs" --emit`.
   Fix any `problem:` lines it prints and re-run until it exits 0.
6. Read `$ROOT/templates/DOCTRINE.snippet.md` and offer to
   append it to the workspace's instructions file — `CLAUDE.md` under Claude
   Code, `AGENTS.md` under Codex — creating the file if absent.
   Summarize the doctrine either way: the sweep comes first — before any
   new build, sweep SUBSTRATE.md for the half-built thing that already
   covers part of it and name the overlap; the second occurrence of the
   same manual step or workaround is a build proposal; shipping a primitive
   means updating that repo's substrate.json in the same commit.
7. Finish with a short report: config written, repos with manifests, the
   graph counts from the emit summary, and which repo to manifest next.
