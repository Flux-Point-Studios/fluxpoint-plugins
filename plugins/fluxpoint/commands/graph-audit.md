---
description: Adversarial semantic pass over the WORK.md IR — context scoping, tier-vs-stakes, prompt quality, hidden coupling — ending VERDICT SOUND or REWIRE. Structure is the compiler's job.
argument-hint: [path to a WORK.md; defaults to the repo root one] [--quick]
---

Audit the graph before anything runs. The compiler already enforces
structure; this pass judges what it cannot. By default the pass is a
multi-lens Workflow, and invoking this command is the explicit
authorization the Workflow tool requires for it, as `/fluxpoint:graph-run`
is for a graph run.

1. Resolve targets: the graph "$ARGUMENTS" names, otherwise `WORK.md` plus
   any sibling `GRAPH.*.md` campaigns. `--quick` in "$ARGUMENTS" selects
   the cheap mode in step 4 and is not a path. Audit each target on its own.
2. Run the structural check first and require it clean —
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" compile-graph.py <graph> --check`.
   If it fails, report those findings and stop: there is no point judging
   semantics of a graph that cannot compile. Keep its summary line, with
   the estimate and cache TTL, for step 4.
3. Load the settled list from the graph's audit log, not from memory. The
   log is `.claude/fluxpoint/audits/<graph path from the repo root, each
   / replaced by __>.json`, so `WORK.md.json` for the root `WORK.md`. It is
   local state: `.claude/fluxpoint/` is gitignored. Its shape:
   ```json
   {"graph": "WORK.md", "rounds": [
     {"round": 1, "mode": "multi", "verdict": "REWIRE",
      "counts": {"raw": 41, "distinct": 22, "settledMatched": 0,
                 "confirmed": 13, "refuted": 9, "unverified": 0},
      "confirmed": ["<title>"], "unverified": [], "refuted": ["<title>"],
      "fixed": ["<title>"]}
   ]}
   ```
   The settled list is every round's `fixed` titles plus every round's
   `refuted` titles, without duplicates. No log means a first round and an
   empty list.
4. Run the audit. What it judges that the compiler cannot:
   - contracts that are structurally valid but semantically vacuous for
     this campaign (a schema no useful answer would fail either).
   - verification tiers that do not match the stakes — `schema-only` on a
     claim that will cost someone a day, `panel:5` on a rename.
   - context packets that paste transcripts or whole files where spans
     and prior contracts suffice, or prompts that leak the desired
     answer to a verifier.
   - nodes whose prompts assume state no `after` edge delivers.
   - budget ceilings set so high they are not really ceilings, and an
     estimate (`--check` prints it) that no `maxEstimatedTokens` bounds.
   - effort transitions that buy nothing: a mechanical schema-only node
     bumped above its neighbours, or an inline effort that equals its
     role's — each real transition is a cold prefill, and a graph that
     fans out or parks without `cacheTtl: "1h"` is paying full prefill on
     every hop.

   **Default, the multi-lens workflow.** Invoke the Workflow tool (Claude
   Code) with
   ```
   {scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/graph-audit.js",
    args: {graph: "<graph>", pluginRoot: "${CLAUDE_PLUGIN_ROOT}",
           check: "<the --check summary line>", settled: ["<title>", ...]}}
   ```
   with args as a real JSON object. A round runs six lenses in parallel,
   each a `graph-auditor` with the same scope and evidence rule:
   `commands`, `data-flow`, `verdicts`, `runtime`, `merge-deploy`, and
   `walkthrough`, which walks the executor and every node in order. A
   reduce merges duplicates across lenses by failure path without judging
   them, keeps every source lens and the highest severity, and flags a
   finding that restates a settled title rather than dropping it. Each
   distinct finding then goes to verifiers told to refute it and to
   default to refuted when uncertain. HIGH and above get three, one per
   lens (`reproduce`, `reachability`, `scope`), and are confirmed only
   when at least 2 of 3 fail to refute; below HIGH, one verifier asks all
   three questions. A verifier that returns nothing leaves its finding
   UNVERIFIED, which is neither confirmed nor refuted.
   Tell the user the cost before launching. The rounds that shaped this
   audit, on a ten-node graph, ran 23 to 33 agents and about 2.8M to 3.1M
   subagent tokens each, where the single auditor was one agent;
   refutation roughly halved what reached the author. To audit other
   ground, pass `lenses`: ids from `LENSES` in the script, or
   `{id, focus}` objects.

   **`--quick`, one auditor.** Add `mode: "single"` to the same args. One
   `graph-auditor` runs, with no reduce and no refutation, so every
   finding comes back UNVERIFIED. It is for checks between repairs: its
   SOUND means one reader ran out of path, and it does not make a graph
   READY.

   **Without the Workflow tool** (Codex, or a Claude Code build that lacks
   it), run the same round by hand. Take the lenses and the refuter
   wording from `LENSES` and `REFUTERS` in
   `${CLAUDE_PLUGIN_ROOT}/workflows/graph-audit.js`. Run each lens as a
   subagent given `${CLAUDE_PLUGIN_ROOT}/agents/graph-auditor.md` as its
   instructions plus its lens and the settled list, or as a separate
   inline pass when no subagent can be spawned. Merge duplicates by
   failure path without judging them, then refute every distinct finding
   under the rules above: three verifiers and 2 of 3 at HIGH and above,
   one below, refuted when uncertain, UNVERIFIED when a verifier returns
   nothing. Report the same counts and name the executor; inline passes
   share one context, which makes them weaker evidence than independent
   subagents.
5. Append the round to the audit log from step 3: its mode, verdict and
   counts, the titles in the result's `confirmed`, `unverified` and
   `refuted` lists, and an empty `fixed` list.
6. Report the per-stage counts first, from the result's `counts`:
   ```
   lenses 6/6 · raw 41 → distinct 22 (settledMatched 2) → confirmed 13 · refuted 9 · unverified 0
   bySeverity (confirmed/distinct): CRITICAL 0/0 · HIGH 2/3 · MEDIUM 7/12 · LOW 4/7
   ```
   then the confirmed and unverified findings, highest severity first, in
   the auditor's table with the lenses that raised each:

   | Severity | Finding | Failure path | Minimal rewire | Lenses |

   then the refuted titles, and end with exactly one line:
   `VERDICT: SOUND` or `VERDICT: REWIRE — <the result's why>`. SOUND
   requires every lens to have returned, zero confirmed and zero
   unverified findings.
7. Treat confirmed and unverified findings as the work list, highest
   severity first. Fix the IR, recompile, and add each repaired title to
   that round's `fixed` list so the next round treats it as settled.
   Re-audit until SOUND. Never weaken a contract, drop a verifier, or
   raise a ceiling to reach SOUND — that is the graph equivalent of
   deleting tests. The settled list is the same kind of lever: a title
   goes in `fixed` only once its repair is in the IR, and an UNVERIFIED
   finding is never recorded as refuted.
8. Record the final verdict in WORK.md Notes, with the mode and the counts
   line. A graph runs only from `STATUS: READY`, and READY requires both a
   clean compile and a SOUND from the multi-lens audit, the workflow or its
   fallback, on the current IR. A `--quick` SOUND does not qualify.
