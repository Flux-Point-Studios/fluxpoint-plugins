# Deterministic metrics and automatic tightening

Declare exact integer counts in `.fluxpoint-metrics.json`. Commands execute as
trusted repository code from the repo root; a command string uses the shell,
while an argv array runs directly. Defaults are two repetitions and a 120 second
timeout for each run. Every repetition must agree, including during checks.

```json
{"version": 1, "metrics": [
  {"name": "bundle.bytes", "command": "wc -c < dist/app.js", "direction": "down"},
  {"name": "coverage.lines", "command": ["node", "coverage-count.js"],
   "extract": {"pointer": "/covered"}, "direction": "up"}
]}
```

Run `scripts/py.sh metric-guard.py --baseline` from the installed plugin to arm
all declared metrics. This writes `.fluxpoint-metrics-baseline.json`, which must
be committed. A noisy or failed command leaves the entire baseline unchanged.
Extraction accepts plain integer stdout, one regex capture with exactly one
match, or a JSON pointer selecting an integer. Floats and booleans are refused.
A changed command, extraction, direction or repetition policy needs deliberate
rebaselining; a stale definition cannot pass as an armed metric.

`--check` is wired into the scaffolded `scripts/harness.sh --full`; missing,
changed, noisy and worse metrics fail it. Without a manifest the guard is
dormant. `--report` (also used by `/fluxpoint:status`) reports current values
against ceilings or floors; `--json` supplies the same records to other tools.
These are task-specific graded measurements; the full harness remains the
independent correctness requirement.

`proof-guard.py`, `seam-guard.py`, `guard-guard.py`, `mutation-guard.py` and
`metric-guard.py` accept `--tighten`. It requires a clean Git tree, runs the
actual `scripts/harness.sh --full`, refuses red, then checks cleanliness again
before writing. It only lowers recorded count ceilings, raises guard floors,
raises mutation score floors while lowering survivor ceilings, and improves
metric limits in their declared direction. It preserves sibling sections and
prints each change. It never arms a new category or silently weakens one.
Statement text and counterexample ledgers have no numeric safe direction and
remain explicit review operations.

Copy `plugins/fluxpoint/templates/tighten-baselines.yml` into a consuming repo's
GitHub workflows, provide the same pinned tools as its full harness, and set
`FPL_PLUGIN_ROOT` to the installed plugin. The scheduled job starts at green
`main`, makes local commits between ratchets to preserve the clean-tree
requirement, and opens one review PR. It never pushes directly to `main`.
Mutation tightening re-runs the configured tool; to reuse a CI measurement,
pass its report with `mutation-guard.py --tighten --from <report.json>`.
Generated reports and build output must already be ignored by the repo; any
unexpected dirty output causes tightening to refuse.

The scaffolded `loop.sh` records the first declared metric before starting and
after each unfinished iteration. Set `PLATEAU_METRIC` to choose another declared
name. It stops with inbox kind `plateau` when best-so-far gain over
`PLATEAU_WINDOW` completed iterations (default 3) is less than `PLATEAU_MIN`
(default 1 integer unit). Both ceiling and floor directions work. A worse
sample cannot erase an earlier gain. A failed or nondeterministic measurement
is reported and cannot trigger plateau; no manifest preserves the original
iteration-budget behavior. `FPL_PLUGIN_ROOT` (or the runtime plugin root) must
locate the helper when metrics are declared.
