---
description: Run the substrate registry check and interpret it — graph size, orphans as dormant value, hubs to harden, staleness alarms and coverage notes — then recommend the single next action.
argument-hint: [none]
---

Report the state of the substrate registry, concisely. Read-only; change
nothing.

1. Resolve `$ROOT` from `CLAUDE_PLUGIN_ROOT`, then `PLUGIN_ROOT`, or the
   parent of this loaded command's `commands/` directory. Use that installation
   throughout; the first recursive cache match may be an older version.
   Run `node "$ROOT/scripts/substrate-graph.mjs" --check`.
   Add `--root <dir>` when the repos do not live under the current project
   directory.
2. Interpret each line rather than pasting the raw output:
   - The summary line is the graph's size: repos with manifests, primitives,
     consumes-edges, orphan count, and the hubs with their in-degrees.
   - `PROBLEM:` lines are manifest defects — unparseable JSON, duplicate
     primitive ids, `consumes` pointing at ids that do not exist. These fail
     `--emit` (and CI, where it is wired in); name the exact file to fix.
   - `ALARM: manifest may be stale: <repo> ...` means code shipped in that
     repo after its `substrate.json` was last updated. The fix is to refresh
     that manifest and regenerate — a re-commit touching only the manifest
     clears it without ever alarming.
   - `NOTE:` lines are coverage facts, not failures: git missing from PATH,
     a non-git repo with no `nonGitRepos` declaration, or a walk that hit
     its depth/entry cap. Say what each one means for coverage.
   - Orphans have no consumers: dormant-value candidates. The highest-value
     move in the registry is usually wiring an existing orphan into a
     consumer rather than building something new.
   - Hubs (in-degree ≥ 2) are load-bearing: harden, test, and document
     these first, because every consumer inherits their failures.
3. Read `SUBSTRATE.md` at the workspace root for the full graph when the
   summary alone does not answer the question at hand.
4. Close with the single most useful next action: fix a named problem,
   refresh a named stale manifest and run `/substrate:emit`, declare a
   skipped repo in `substrate.config.json`, or activate a named orphan.
