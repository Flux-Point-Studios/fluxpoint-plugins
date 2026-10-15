---
name: fluxpoint-graph-run
description: Compile WORK.md's IR to a Workflow script, execute it, record provenance and the Evidence row — targeted node repair and cached resume on partial failure, never a restart from zero. Codex entry point for the fluxpoint `graph-run` command.
user-invocable: false
disable-model-invocation: true
---

This skill is how Codex reaches the `graph-run` command of the fluxpoint
plugin; on Claude Code the same command is `/fluxpoint:graph-run`, and the two
Claude-only keys above keep this entry point out of that runtime's menus so
nothing is listed twice.

1. Read `../../commands/graph-run.md`, relative to this file. Under an installed
   plugin that is `${PLUGIN_ROOT}/commands/graph-run.md`; Codex also sets
   `CLAUDE_PLUGIN_ROOT` to the same directory, which is what the command's
   own shell snippets use.
2. Carry out its steps exactly as written, in order, and report as it says.
   The text of the user's request stands in for `$ARGUMENTS` ([path to WORK.md, plus any campaign args as JSON]).
3. Where the command names a subagent from `agents/`, run one with that
   file's contents as its instructions, or perform the pass inline when no
   subagent can be spawned. Where it names the Workflow tool, note that graph
   execution is Claude Code only and follow the command's own fallback.

For any commit made by a Codex session without the Workflow tool, place
`Fluxpoint-Executor: codex` in the final Git trailer block. Direct repairs
remain unchecked until an independent repair run names their SHAs in a
FixCheckV1 PASS or a recorded DecisionV1 explicitly accepts them.
