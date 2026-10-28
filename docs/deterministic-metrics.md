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
before writing. The HEAD checked by the full harness must stay unchanged, even
if a command commits its own edits and leaves a clean tree. Tightening refuses
partial mutation reports with unknown statuses. It only lowers recorded count ceilings, raises guard floors,
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
pass its report with `mutation-guard.py --tighten --from <report.json>
--from-head <producer-commit>`. The producer commit must be the current clean
HEAD. The same binding is required by `--measure --from`; incomplete reports
never update a baseline.
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

For Aiken, `scripts/py.sh aiken-metrics.py --baseline` from the installed plugin
runs pinned **v1.1.23** twice with seed 1, compares the real unit-test mem/CPU
counts, and adds one metric ceiling per test and unit. Commit both manifests.
The full harness remeasures and enforces these ceilings; the shared command
runs twice regardless of how many tests select its fields. Every current unit
test emitted by each Aiken measurement command needs both mem and CPU metrics;
adding a test or dropping one of its metrics fails until explicitly baselined.
Malformed or incomplete test summaries and failed properties cannot certify
unit-test costs. Other compiler
versions are refused until their output and cost behavior are independently
verified. Rebaseline explicitly when intentionally changing the compiler,
Plutus version or cost model.

The machine format is `modules[].tests[].execution_units.{mem,cpu}` on non-TTY
stdout. Tests carrying `iterations` are properties, are listed as excluded,
and never get parsed as one execution budget. The exact source, dependency
pins, raw output, release checksum and two-run output hash from the real
v1.1.23 validation live in `plugins/fluxpoint/tests/fixtures/aiken-v1.1.23/`.
The scaffolded Aiken harness passes its current check output to
`plutus-budget.py --aiken-output` when a metrics manifest is present. That gate
can compare measured unit scenarios to protocol limits instead of typed-in
`exUnits`; a unit scenario is not a complete transaction or a sum of all tests.
