# Runtimes: Claude Code and Codex

Both plugins are one tree that two runtimes load. This page records what
each runtime provides, what the adapter does with the differences, and what
stays runtime-specific. The payload shapes below are the adapter contract
covered by repository fixtures. Actual hook delivery also depends on the
installed runtime version and plugin activation; use the release smoke test
in [CONTRIBUTING.md](../CONTRIBUTING.md) to check a live installation.

## What the runtimes share

| Surface | Claude Code | Codex |
|---|---|---|
| Plugin manifest | `.claude-plugin/plugin.json` | `plugin.json` at the plugin root, on the Agent Plugins schema |
| Marketplace | `.claude-plugin/marketplace.json` | `.agents/plugins/marketplace.json` |
| Hooks | `hooks/hooks.json` | the same file, same schema |
| Skills | `skills/<name>/SKILL.md` | the same directories |
| Plugin root in hook commands | `CLAUDE_PLUGIN_ROOT` | `PLUGIN_ROOT`, and `CLAUDE_PLUGIN_ROOT` for compatibility |
| Hook payload | JSON on stdin with `session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`, `tool_response` | the same fields |
| Blocking a stop or an edit | exit 2 with the reason on stderr, or `{"decision":"block"}` | the same |
| Context from `SessionStart` | plain stdout | plain stdout |

Each runtime reads only its own manifest and ignores the other's, so the
extra files cost nothing on the runtime that does not use them.
`scripts/harness.sh --full` checks that the two manifest sets of each plugin
agree on name, version and description, and that every command has its
Codex entry point.

## Where they differ, and what the adapter does

**File edits.** Claude Code's `Write`, `Edit` and `MultiEdit` payloads carry
`tool_input.file_path`. Codex edits through `apply_patch`, whose payload
carries the whole patch in `tool_input.command` and no path. Codex fires the
`Write|Edit|MultiEdit` matcher for `apply_patch` through matcher aliases, so
the hook wiring is unchanged; `fpl_edit_paths` in `scripts/lib.sh` reads the
paths out of the patch headers (`*** Add File`, `*** Update File`,
`*** Move to`, `*** Delete File`) and re-bases a relative path from the
payload's `cwd`. The per-edit verify hook and the prose-smell hook both go
through it. A deletion arms the Stop gate and checks nothing.

**Shell results.** Claude Code's `PostToolUse` for `Bash` fires on success
and hands the hook an object with `stdout` and `stderr`; a failure arrives as
a string beginning `Error: Exit code N`. Codex fires for every completed
command and hands the hook one string whose header carries
`Process exited with code N` (the classic shell tool writes `Exit code: N`).
`attest.py` reads the exit from that header and only from the header, so a
command that prints a header-shaped line cannot mint its own exit. A
response with no readable exit is recorded as nothing. The practical
difference: Codex attests red gate runs as well as green ones.

**Project directory.** Claude Code sets `CLAUDE_PROJECT_DIR` for hooks; Codex
does not. `fpl_cd_project` in `scripts/lib.sh` already resolves the project
from the payload's `cwd` and the git toplevel when the variable is absent,
so Flux Point hooks do not require it. The Substrate graph CLI instead uses
`--root`, then `CLAUDE_PROJECT_DIR`, then the process working directory. Run
its commands from the workspace root or pass `--root` explicitly; it does
not derive a workspace from the hook payload or the Git toplevel.

**Commands.** Claude Code loads `commands/<name>.md` as `/plugin:<name>`.
Codex has no command directory and deprecated its custom prompts in favour
of skills, so each command has a thin skill under
`skills/<plugin>-<name>/SKILL.md` that reads the command file and carries out
its steps. Those skills carry `user-invocable: false` and
`disable-model-invocation: true`, which Claude Code honours and Codex
ignores, so they stay out of Claude Code's menus and nothing is listed
twice. Each one has an `agents/openai.yaml` with its display name; the
commands that write files or fire effects (`init`, `migrate`, `release`,
`graph-design`, `graph-run`, `emit`) are explicit-invocation only.

The new `grill-me` entry point is also available for implicit Codex selection
because it is a planning prerequisite. It shares `commands/grill-me.md` with
Claude's `/fluxpoint:grill-me`. Both populate the same requirement packet;
optional user answers can revise model defaults and never supply implicit
authorization. The compiler, packet runner and lock checks are shared Python.

**Agents.** Claude Code runs `agents/*.md` as subagents by name. Codex has
subagents but does not read those files, so the commands that name an
agent tell a Codex session to hand the agent file to a subagent as its
instructions, or to perform the pass inline. The verdict format and the
contracts are the same either way.

**Outer loop.** `templates/loop.sh` drives one iteration through
`claude -p` by default and through `codex exec` with `AGENT_CLI=codex`. Both
read `WORK_PROMPT.md`; Codex takes its approval and sandbox settings as
config overrides in `CODEX_ARGS`, whose default permits edits inside the
workspace without prompting. The bypass flag, like Claude Code's, belongs in
a sandboxed container only.

**Plugin lookup outside a hook.** The scaffolded harness and the outer loop
locate the installed plugin when no runtime variable is set. They search
Claude Code's cache under `~/.claude/plugins` and Codex's under
`~/.codex/plugins/cache` (`CODEX_HOME` when set), preferring a locally
installed marketplace copy and then the highest cached version.
The harness first accepts `FPL_PLUGIN_ROOT`, `CLAUDE_PLUGIN_ROOT`, then
`PLUGIN_ROOT`. For command instructions, use the injected runtime root or the
parent of the loaded command's `commands/` directory. A recursive `find` with
`head -1` can select a stale cache copy and should not determine the version.

**State directory.** Repo-local state lives under `.claude/fluxpoint/` on
both runtimes. The path predates Codex support and is kept because every
onboarded repo ignores it and every script reads it; it is a state directory
name, and nothing in it is specific to Claude Code.

## What stays runtime-specific

- **Graph execution.** `/fluxpoint:graph-run` compiles the IR into a Claude
  Code Workflow script and invokes the Workflow tool. Codex has no
  native Workflow executor. Without that tool, the command documents running
  the nodes as parallel subagent calls with the same contracts and recording
  `--executor degraded-subagents`. That fallback has explicit executor
  provenance; it does not establish Workflow journal replay guarantees.
  `graph-design`, the compile check and `graph-audit` run on either runtime.
- **In-session drivers.** `/goal`, `/loop` and cloud Routines are Claude
  Code features. On Codex, the outer loop and scheduled `codex exec` runs
  cover unattended work.
- **Memory lint.** The substrate memory lint reads Claude Code's per-project
  memory directory under `~/.claude/projects`. Codex keeps no such
  directory, so the lint is silent there.
- **Attestation of failures.** Recorded on Codex, and on Claude Code only
  once a hook runs on the runtime's failure event; today the log binds
  passes there and is silent about reds.

## Verifying a change against both runtimes

The suites under `plugins/fluxpoint/tests/` and `plugins/substrate/tests/`
drive the hook scripts with payloads in both shapes: `hooks-test.sh` and
`prose-smell.test.mjs` cover `apply_patch` payloads, `attest-test.sh` covers
the string-shaped shell response, and `loop-runner-test.sh` runs the outer
loop against a fake `claude` and a fake `codex`. `scripts/harness.sh --full`
runs those suites. Native Blaster and the selected Windows jobs are separate;
see [development setup](development-setup.md#ci-and-cloud-environments).


### Windows hook startup under load

Python launchers cache a probed absolute interpreter against `PATH` in the
user's cache (`$XDG_CACHE_HOME/fluxpoint/python`, or
`$HOME/.cache/fluxpoint/python`). WindowsApps execution aliases are skipped.
A cache hit starts only the target Python process; the secret guard avoids
`dirname` and `cygpath` subprocesses and has a 60-second startup allowance.
Setting `FPL_PY` explicitly bypasses auto-detection. Remove the cache file to
force a new probe if an interpreter installation changes at the same path.

These changes reduce exposure to the MSYS2 suspended-child startup race
reported in #124. They do not guarantee that a runtime killing Bash also
terminates every native child. That guarantee requires Windows runtime
process-tree termination. The Windows CI job exercises selected cleanup
paths and an actual health observation. Sustained Git Bash load and runtime
forced-timeout behavior still require native verification; Linux regression
tests establish cache and launch-count behavior only.
