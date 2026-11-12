---
description: Turn a campaign goal into a validated WORK.md IR — nodes with named contracts, verification tiers chosen by stakes, a budget ceiling — compiling clean and audited SOUND before it may run.
argument-hint: [campaign goal]
---

Design the graph. The IR is the deliverable; nothing executes from this
command.

1. Read `WORK.md`; if absent, run `/fluxpoint:init` first.
   Set the `campaign` field from "$ARGUMENTS" if provided, otherwise ask
   before writing.
2. Apply the loop-or-graph rule from the graph-engineering skill. If the
   goal fits one loop slice — one zone, one contract, serial
   evidence — say exactly that, recommend `/goal` or WORK.md, and stop.
   Never build a graph as ceremony.
3. Before designing implementation nodes, perform `/fluxpoint:grill-me`
   (Codex: `$fluxpoint-grill-me`). Reuse the current packet when it covers
   the goal; revise changed decisions and their dependents. Require
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" specification.py --check`
   to pass. A defaulted model decision is sufficient for routine authorized
   work, but a blocked authority decision is not. Read-only discovery can
   establish facts for the packet before it is locked.
   Interrogate the repo until every planned node is grounded: which files
   each prompt names, which commands each verifier runs, which
   `agents/*.md` roles already exist to reference by `agentType`.
4. Write the `graph-ir` block:
   - one node per responsibility, each with a `contract` from
     `contracts/` (add a new `*.schema.json` there if none fits — make it
     one a lazy output cannot satisfy: `required`, `minLength`, enums).
   - `verify` tier chosen by stakes, not habit: `schema-only` for cheap
     mechanical output, `skeptic:1` for low-severity claims, `panel:3` for
     findings that cost someone real time, `panel:5` only for CRITICAL.
     Panels are odd so majority is defined; every panel needs `verifyOver`.
     To gate on the harness, do not reach for a tier — mark the producer
     `mutates: true` and add an `independent` node that runs it.
   - any node that writes to the tree gets `mutates: true`, and a later
     node with `independent: true` and `verifies: "<id>"` must re-derive
     the verdict. The compiler enforces this; do not try to talk it out
     of the rule.
   - any node that builds, compiles, or measures the tree without meaning
     to keep the result gets `isolation: 'measure'` — a frozen
     `git archive HEAD` snapshot the node cuts itself, so a fan-out's
     numbers stop being a function of what the siblings are doing and the
     base is the campaign's by construction. The compiler warns on a
     measuring-looking fan-out that declares nothing.
   - `budget.maxNodes` set to the real worst-case ceiling (the compiler
     prices discovery rounds into it), `nodeFloorTokens` where work
     fan-out should stop before exhaustion, `foreach` lists named,
     `onRed` policy per node, `haltWhen` where a numeric result should
     stop the campaign.
   - `budget.maxEstimatedTokens` set from the estimate `--check` prints —
     calls are the wrong unit for the bill, and this is the ceiling on the
     right one. It is checked against the plan when the graph compiles
     and enforced on the run by the compiled script, which declines a
     call once the estimated spend would pass it. Leave headroom over the
     estimate for what the plan cannot see: more findings than
     `expectItems`, another discovery round. Set `budget.cacheTtl: "1h"`
     whenever a node fans out or
     parks, because the default five-minute prompt cache expires while
     the parent blocks. Read the compiler's transition warnings: an effort
     bump between consecutive nodes is a cold prefill, so bump only where
     the task shape needs it and place the change where the cache is cold
     anyway. Effort is a cost lever with no measurement behind it yet;
     prefer the role default unless the node's job is to keep looking for
     evidence (verification, red-team).
   - where the size of the work is unknown rather than given, a `repeat`
     block instead of a fixed fan-out: `untilDryRounds`, `maxRounds`, and
     a `dedupeBy` key that is neither so coarse it collapses distinct
     items nor so fine that a reworded restatement reads as new. Put
     `{{seen}}` in the prompt so each round works new ground. See
     `templates/WORK.discovery.md`.
   - compress before you reason: between a fan-out and the node that
     judges its output, put a `reduce` node (`from`, `over` where the
     source yields whole contracts, then `dedupeBy` / `sortBy` +
     `order` / `topK`) so the judge reads a shortlist, not a landfill —
     and where a consumer needs one field of its predecessor, project it
     with `{{prev.<field>}}` instead of pasting the contract.
   - between red-team lenses and their refuter panel, the `reduce` is
     also the claim key: the panel judges one claim per `dedupeBy` key
     and shows the refuters every lens's wording of it. Key on
     `target` and `class`. A key on `finding` or `exploit_path` merges
     nothing two lenses word differently, and every lens that sees a
     defect then costs its own panel out of `budget.maxNodes`.
   - a review that must run once per run declares `"resume": "never"` on
     its fan-out. A resume replays the calls before the first dead one
     and runs the rest again, so without the field a resumed run rolls
     the review twice. With it, a run that started the node is refused
     at resume (`RESUME-PAST-NO-RESUME-NODE`) and launches fresh. Prose
     in a prompt that forbids the resume is not enforced by anything.
5. Compile-check until clean:
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" compile-graph.py WORK.md --check`
   Its findings are structural. Fix the IR — never weaken a contract,
   drop a verifier, or raise the budget just to silence it.
6. Run `/fluxpoint:graph-audit` for the semantic pass the compiler
   cannot do: are the prompts scoped to what the node may believe, do the
   tiers match the stakes, does any edge exist only because it looked
   tidy. Apply every REWIRE finding and re-audit until `VERDICT: SOUND`.
7. Set `STATUS: READY` and report: node count, planned agent calls and
   the estimated tokens from `--check` (with the ceiling and the TTL the
   graph declares), the verification tiers in one line, the audit
   verdict, and the run command (`/fluxpoint:graph-run`).
