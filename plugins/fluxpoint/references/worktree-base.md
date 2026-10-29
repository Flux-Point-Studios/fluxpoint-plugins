# Exact campaign bases in worktrees

An isolated node must examine the campaign's exact commit. A clean worktree
behind that commit can fast-forward its own branch; ancestry alone also accepts
an ahead tree that the campaign never judged.

The compiler requires the launch `_base.root` and a full forty-character Git
SHA for worktree nodes. Its emitted preamble runs plain commands in the worker's
own directory: read HEAD and root, refuse the shared checkout, require empty
porcelain and an attached branch, then run `git merge --ff-only <base>` only
when HEAD differs. A final HEAD reading must equal the base exactly. Shared,
dirty, detached, divergent, missing-object and ahead states stop without reset
or detach. Measurement snapshots retain their separate existing preamble.

Every schema carrying that preamble requires the full `baseSeen` observed
before the requested work starts. A worker that later creates commits keeps
that original base observation; its produced commit is a separate result.
The script checks live and restored results before their findings count. A
missing, malformed or different value files `WRONG-BASE`, excludes the result
and prevents a complete run. The summary retains the expected base and checked
observations. This validates the reported hash; fixed command nodes can also
bind actual Bash observations through [native captures](native-binding.md).

## Choosing the runtime's initial base

To ask Claude Code to start worktrees at the launching HEAD, use this project
local setting:

```json
{"worktree":{"baseRef":"head"}}
```

Write it to `.claude/settings.local.json`. The default `fresh` may start at
`origin/<default-branch>`, behind campaign commits. Both settings retain the
in-node checks. Uncommitted code in the launching checkout reaches neither
kind of worktree.

`compile-graph.py --check` reports the project/local setting when worktree nodes
exist. `worktree-base.py --root <project-root> --json` reports it separately:

| State | Meaning within inspected project/local files |
|---|---|
| `head` | The highest inspected setting says `head` |
| `fresh/absent` | It says `fresh`, or neither file names a base |
| `unknown` | A file cannot be read/parsed or names an unsupported value |

The reading is advisory and always exits zero. It is not effective runtime
policy: user/managed settings, CLI `--settings` and alternate
`CLAUDE_CONFIG_DIR` profiles can override it. The worker's actual base check
remains authoritative.

## Actual runtime acceptance

The portable native fixture uses installed Claude Code **2.1.312**, the actual
Workflow tool and `agent(..., {isolation: 'worktree'})`. It submits the unmodified
compiled script to a localhost Anthropic-compatible provider returning scripted
tool calls. It uses a dummy test key, isolated configuration, default permission
mode and an explicit generic Git allow rule. The outside-write negative must be
refused specifically by the runtime worktree-isolation guard; generic approval
denial and permission bypass cannot establish acceptance.

```sh
python plugins/fluxpoint/tests/worktree-base-live.py \
  --binary /path/to/claude --output /new/private-capture-directory
```

`FPL_CLAUDE_BINARY` also selects the executable, including the native Windows
`claude.exe`. The output directory must be new or empty; native journals,
metadata, tool transcripts, API request/response records and the binding
certificate remain there for review. The fixture checks a clean behind start,
actual outside shared-ref write refusal, successful own-directory ff-only,
exact final base and unchanged attached branch. The Linux control was run in
a fresh-home container with external networking disabled; Windows CI runs the
same fixture against its installed native executable before acceptance.

Scripted replies test runtime permissions and tool transport. They establish
neither model quality nor provider cost. No provider credential or paid model
is required. Do not enable permission-bypass modes to make this fixture pass.
