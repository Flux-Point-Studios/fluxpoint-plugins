---
description: Regenerate SUBSTRATE.md from every repo's substrate.json manifest — exit 1 means a manifest problem that CI treats as red.
argument-hint: [none]
---

Regenerate the substrate registry.

1. Resolve `$ROOT` from `CLAUDE_PLUGIN_ROOT`, then `PLUGIN_ROOT`, or the
   parent of this loaded command's `commands/` directory. Use that installation
   throughout; the first recursive cache match may be an older version.
   Run `node "$ROOT/scripts/substrate-graph.mjs" --emit`.
   Add `--root <dir>` when the repos do not live under the current project
   directory.
2. On exit 1, every `problem:` line on stderr is a manifest defect —
   unparseable JSON, a duplicate primitive id, a `consumes` entry naming a
   primitive that does not exist. `SUBSTRATE.md` is still written so the
   damage is inspectable, but the run is red. Fix the named
   `substrate.json` files and re-run until it exits 0.
3. Report the summary line (repos, primitives, edges, orphans, hubs,
   alarms) and anything that changed in `SUBSTRATE.md` since the last
   generation.
4. `SUBSTRATE.md` is generated output — never hand-edit it. If this emit
   accompanies a shipped primitive, the `substrate.json` edit belongs in
   the same commit as the code it describes; a manifest updated in a later
   commit is exactly the drift the staleness alarm exists to catch.
