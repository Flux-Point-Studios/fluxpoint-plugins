---
description: Migrate a pre-1.0 repo onto the unified WORK.md contract, via the tested migrate script — plan, apply, verify, then finalize.
argument-hint: [none]
---

Migrate this repository from the split fluxpoint-loop / fluxpoint-graph
layout to the unified `fluxpoint` plugin.

The mechanical work is `scripts/migrate.py`, which is tested against real
pre-1.0 fixtures (`tests/migrate-test.sh`). Do not hand-edit the files it
manages or reimplement its steps — deleting a repo's work-state files is
not something to drive from prose.

Resolve the plugin root first: `CLAUDE_PLUGIN_ROOT`, then `PLUGIN_ROOT`, or the parent of this loaded
command's `commands/` directory. Call it `$ROOT`; use that same installation
for all scripts and agents below.

1. **Plan.** `bash "$ROOT/scripts/py.sh" migrate.py --plan`
   Show the user its output verbatim. It touches nothing. If it reports
   nothing to migrate, say so and stop.
2. **Apply.** `bash "$ROOT/scripts/py.sh" migrate.py --apply`
   This writes `WORK.md`, renames `LOOP_PROMPT.md`, moves local state under
   `.claude/fluxpoint/`, rewires `.gitignore` and `enabledPlugins`, and
   deletes compiled `.graph.js` build output. It deliberately leaves
   `LOOP.md` and `GRAPH.md` in place so the result can be reviewed.
3. **Convert the campaign, if flagged.** This is the one judgment call the
   script refuses to make: a pre-0.2 `GRAPH.md` described its campaign in
   prose, and `--apply` leaves a `MIGRATION:` marker with that prose
   preserved under `## Campaign`. Convert it to a ```json graph-ir block
   per the graph-engineering skill. Do not invent nodes the old spec did
   not describe; if the campaign is obsolete, delete the section and set
   `MODE: loop`.
4. **Verify before deleting anything:**
   - `git diff --stat` and read `WORK.md` — confirm the Definition of Done,
     Plan, Constraints, and Merge policy came across intact.
   - `scripts/harness.sh --full` still exits 0. The contract did not
     change, so a break here means the migration touched something it
     should not have.
   - If `WORK.md` has a Campaign section:
     `bash "$ROOT/scripts/py.sh" compile-graph.py WORK.md --check`
5. **Finalize.** `bash "$ROOT/scripts/py.sh" migrate.py --finalize`
   This removes `LOOP.md` and `GRAPH.md`, and refuses if `WORK.md` carries
   fewer Evidence rows than the sources did. If it refuses, the migration
   lost history — fix `WORK.md`, do not force it.
6. **If MODE is graph or both, design before running.** A migrated graph
   carries `STATUS: DESIGN`, and `/fluxpoint:graph-run` accepts only `READY`
   or `RUNNING`. Run `/fluxpoint:graph-design` to review the IR and flip it
   to `READY`; the campaign is not runnable until it does.

7. Commit as one change naming what merged, then report: MODE, Evidence
   rows carried, harness verdict, compile check, and anything that needed
   a judgment call so the user can review it.
