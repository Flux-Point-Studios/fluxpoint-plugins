# Role effort bench

Measures what effort and model each campaign role needs (issue #67). The
feature template's role settings used to be asserted. This bench grades
role-shaped work on fixed cases, with each setting passed to a real
headless `claude -p` call as `--model` and `--effort`. Each call's
`usage` and `total_cost_usd` come from the meter, not from the cost model.

| Role | Cases | Score |
|---|---|---|
| builder | `b-*`: implement a spec in one module. `bx-*`: a change across several files of a small codebase | Fraction of hidden acceptance tests passing. The visible tests are a subset |
| red-team | `r-*`: a module with two or three seeded defects and one or two plausible decoys | F1 of findings against the seeds. A finding on a decoy always counts against precision; a low-severity finding elsewhere does not |
| architect | `a-*`: a small repository and a change request | 0.6 × F1 of the existing source files named against those the change needs, plus 0.4 × the fraction of required risks the plan names |

The train/test split is `sweep.py`'s: random by seeded hash, stratified by
role, never by score. A cheaper setting is picked on the train split only,
then read once on the test split.

```
python3 plugins/fluxpoint/bench/effort/run.py --selfcheck
python3 plugins/fluxpoint/bench/effort/run.py --split
python3 plugins/fluxpoint/bench/effort/run.py --run OUT --reps 3 --jobs 10 --max-usd 80
python3 plugins/fluxpoint/bench/effort/run.py --report OUT
```

`--selfcheck` spends nothing. It confirms that every grader gives the
case's reference answer 1.0 and a stub less. A builder reference lives in
`reference/`; red-team seeds and decoys are `# @seed:<id>` and
`# @decoy:<id>` markers, stripped before the model sees the fixture. `--run`
needs an authenticated Claude Code on `PATH`, resumes from
`OUT/results.jsonl`, and stops starting runs at `--max-usd`. It grants no
permission bypass: reviewers and architects get read-only tools, and a
builder may edit its scratch fixture and run `python3`. `--report` prints
the grid and each role's paired difference against the shipped setting, with
case-clustered bootstrap intervals.

The recorded sweep and its reading are in
[`results/2026-10-29/`](results/2026-10-29/) and
[DESIGN-NOTES](../../DESIGN-NOTES.md#effort-is-asserted-never-measured).

What this does not measure: a whole campaign. Each case is one role's call
in isolation, with no council, judges, repair rounds or harness gate around
it. The cases are small enough that one call finishes them.
