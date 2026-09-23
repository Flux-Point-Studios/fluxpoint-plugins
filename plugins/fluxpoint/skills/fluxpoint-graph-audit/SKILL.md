---
name: fluxpoint-graph-audit
description: Adversarial semantic pass over the WORK.md IR — context scoping, tier-vs-stakes, prompt quality, hidden coupling — ending VERDICT SOUND or REWIRE. Structure is the compiler's job. Codex entry point for the fluxpoint `graph-audit` command.
user-invocable: false
disable-model-invocation: true
---

This skill is how Codex reaches the `graph-audit` command of the fluxpoint
plugin; on Claude Code the same command is `/fluxpoint:graph-audit`, and the two
Claude-only keys above keep this entry point out of that runtime's menus so
nothing is listed twice.

1. Read `../../commands/graph-audit.md`, relative to this file. Under an installed
   plugin that is `${PLUGIN_ROOT}/commands/graph-audit.md`; Codex also sets
   `CLAUDE_PLUGIN_ROOT` to the same directory, which is what the command's
   own shell snippets use.
2. Carry out its steps exactly as written, in order, and report as it says.
   The text of the user's request stands in for `$ARGUMENTS` ([path to a WORK.md; defaults to the repo root one] [--quick]).
3. Where the command names a subagent from `agents/`, run one with that
   file's contents as its instructions, or perform the pass inline when no
   subagent can be spawned. Where it names the Workflow tool, note that the
   tool is Claude Code only and follow the command's own fallback: the same
   lenses, reduce and refutation, run as subagents or inline passes, with
   the same counts in the report.
