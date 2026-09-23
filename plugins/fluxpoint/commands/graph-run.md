---
description: Compile WORK.md's IR to a Workflow script, execute it, record provenance and the Evidence row — targeted node repair and cached resume on partial failure, never a restart from zero.
argument-hint: [path to WORK.md, plus any campaign args as JSON]
---

Run the designed graph. This command is the explicit authorization the
Workflow tool requires.

1. Resolve the plugin root: try `${CLAUDE_PLUGIN_ROOT}`, else
   `find ~/.claude/plugins ~/.codex/plugins/cache -type d -name fluxpoint 2>/dev/null | head -1`.
   The graph file is "$ARGUMENTS" if it names one, else `WORK.md`.
2. Preflight, all deterministic — stop and report exactly what is missing
   rather than improvising around it:
   - `bash "$ROOT/scripts/py.sh" compile-graph.py <graph> --check` exits 0.
     Its findings are the work list; fix the IR, never the compiler.
   - Mutating and irreversible graphs require the locked spec packet.
     The compiler loads it from the path the graph file's `SPEC:` header
     names (default `.fluxpoint-spec.json` at the repository root), and a
     `CONTRACTS:` header overlays a repo-local contract directory on the
     shipped contracts; the `--check` line names both, so confirm they are
     the ones this campaign was written against. The compiler embeds the
     packet in node context. Verify the packet covers this goal, its check commands are
     wired into the repo harness, and it and its lock are committed for
     isolated nodes. On Codex's fallback, pass the embedded packet to each
     node and preserve its identity in the summary; recording rejects a
     stale or omitted identity. Do not re-lock a changed spec to repair a
     failing implementation; review the requirement change first.
   - `STATUS:` reads `READY` (or `RUNNING` with a resume point in Notes).
   - The `--check` line names the estimated tokens and the prompt-cache
     TTL the graph declares (`budget.cacheTtl`). A graph written for `1h`
     is priced for a session whose prompt cache lives an hour; run it in
     one, or expect every hop to cost a cold prefill the estimate did not
     charge for. The runtime sets the TTL per session, never per node.
   - Every command the verification map names exists (`scripts/harness.sh`
     if referenced) and every `agentType` resolves — `red-team-reviewer`,
     `proof-auditor` and `prover` ship with this plugin.
3. Compile:
   `bash "$ROOT/scripts/py.sh" compile-graph.py <graph> -o .claude/workflows/<name>.graph.js`
   The output is generated code. Never hand-edit it; edit the IR and
   recompile, or the spec and the executor start lying to each other.
4. Load whatever state the graph refuses to start without.
   Record the launch reading, fresh at every launch including a resume:
   ```
   args._base = {"sha": "$(git rev-parse HEAD)", "branch": "$(git branch --show-current)",
                 "porcelain": "$(git status --porcelain)"}
   ```
   A graph with any `isolation` node — or `mutates: true`, which implies a
   worktree — refuses to start without it. Every graph that guards its tree
   (the default) compares it against the run's first sentinel reading: the
   sentinel is a cached `agent()` call on resume, so this reading is the
   only one that sees the tree the resumed run is really on. Never reuse an
   earlier launch's `_base`.
   Isolated nodes are handed this base in their prompts and told to assert
   it before trusting the tree they were given: a worktree's base is the
   runtime's choice, and it has been observed cut from the default branch
   rather than the campaign's, where a file an earlier node committed is
   simply absent and nothing inside says so.
   If any node declares `verify: prove:<gate>`, stamp the launch; the
   compiled graph refuses to start without it:
   ```
   bash "$ROOT/scripts/py.sh" attest.py --stamp
   ```
   into `args._launch` (`{"since": ..., "nonce": ..., "root": ...}`; `root` is
   the project, which the prove preamble passes to `attest.py` as `--root`
   so a node working in a worktree attests into this project's log). `record-run.py`
   files a citation of any row minted before `since`, or by a run with a
   different nonce, as STALE, so neither an earlier run's execution nor an
   overlapping run's on the same commit can stand in for this one. The
   compiled graph tells each `prove:` node to run its gate as
   `FPL_ATTEST_NONCE=<nonce> <declared command>` (and cite what
   `attest.py --last <gate> --nonce <nonce>` prints); a gate that outlives one
   tool call runs as `attest.py --run <gate> --nonce <nonce>` in the
   background and is collected with `attest.py --await <token>`; a
   `prove:ci` node runs `attest.py --ci --pr <n> --wait --nonce <nonce>`
   (or `--ref <branch>`) so the forge, not the node, names the commit, and
   returns that `sha`, which whatever merges after the gate pins with
   `gh pr merge --match-head-commit <sha>`. On a resume pass the stamp of
   the run being resumed (its recorded artifact carries it as
   `summary.launch`), never a fresh one: the replayed nodes' citations
   carry the original nonce.
   If any node has `actor: human` or `actor: third-party`:
   ```
   bash "$ROOT/scripts/py.sh" release.py --load --campaign "<the IR's campaign line>"
   ```
   into `args._releases`. A node with no release parks: the run reports it
   BLOCKED with its instructions, marks itself INCOMPLETE, and works the
   branches that do not depend on it. That is the expected outcome, not a
   failure — clear it with `/fluxpoint:release <node>`, never by inventing
   a release.
   If the IR has an `imports` block, there is nothing for you to load: the
   compiler resolves each frozen decision from `.claude/fluxpoint/runs` and
   from the records `decision.py` keeps in `.claude/fluxpoint/decisions.jsonl`
   at compile time — `'latest'` is the newest of either carrying that
   decision id, otherwise the named runId or `dec_` recordId — and embeds
   the record and its source in the generated script. No hand-assembled `args._decisions`
   map exists to get wrong, and a missing or malformed record fails step 2's
   `--check` rather than the launch. If it does fail there, the campaign
   that makes the decision has to run first; never hand-write or edit a run
   artifact to get past the compiler.
   If any node declares `memory.seed`, load the lessons earlier runs filed
   under that tag, ranked against this campaign so the most relevant prior
   keys reach the prompt first:
   ```
   bash "$ROOT/scripts/py.sh" recall.py --format seedmap \
     --tag "<each seed tag>" --query "<the IR's campaign line>" \
     --files "<comma-separated paths the campaign targets, if any>"
   ```
   into `args._seen`. The output shape is exactly what `memory.py --load`
   prints — relevance-ordered instead of file-ordered — and the seeds stay
   advisory either way: they reach the `{{seen}}` prompt, never the dedup
   set. If recall.py is unavailable or errors, fall back to the unranked
   loader:
   ```
   bash "$ROOT/scripts/py.sh" memory.py --load --tag "<each seed tag>"
   ```
   Skipping both is not an error — the graph logs that it seeded nothing
   and the Evidence row says the sweep started cold — but it throws away
   the whole point of a repeat sweep, which is that the second run starts
   where the first stopped.
   If the IR contains any `irreversible` node, load the once-only ledger —
   the compiled graph refuses to start without it:
   ```
   bash "$ROOT/scripts/py.sh" ledger.py --load --campaign "<the IR's campaign line>"
   ```
   Put its output in `args._ledger`. Run `--list` and show the operator what
   has already fired before asking for anything. `confirm` is theirs to
   supply and yours never to invent: it is a comma-separated list of the
   node ids a human authorized this run, and a node not named in it refuses
   to fire. Never pass `confirm` because a previous run used it, because
   the campaign obviously intends it, or because the user said "go" — only
   when they have named the effect.
5. Set `STATUS: RUNNING`. Invoke the Workflow tool (Claude Code) with
   `{scriptPath: ".claude/workflows/<name>.graph.js", args: {...}}`.
   Pass args as a real JSON object, not a stringified one. Watch with
   `/workflows`; never poll with sleep.
6. On completion, record the run — evidence is a build artifact, not
   something you remember to write:
   ```
   echo '<the workflow return value as JSON>' | bash "$ROOT/scripts/py.sh" record-run.py \
     --run-id <runId> --graph <graph> [--evidence WORK.md] [--harness <exit>] [--red-team SHIP|BLOCK]
   ```
   `--graph` is the file the run was compiled from (its `SPEC:` is the
   packet the run is checked against); `--evidence` is the file whose
   Evidence and Decisions tables receive the rows, `--graph` by default. A
   campaign in `GRAPH.<name>.md` with a packet of its own passes both.
   That also appends any irreversible effect to the ledger, so the next
   run replays it instead of repeating it. Before diagnosing an empty or
   surprising result, read the run's `journal.jsonl` — it records what
   each node actually returned, including cached ones.
7. Partial failure is a targeted repair, not a restart: fix the one red
   node (its IR prompt, its contract, or the code it touched), recompile,
   stop the run if still live, then re-invoke with
   `{scriptPath, resumeFromRunId}` — passing `_ledger` and `_base` again,
   both freshly loaded. A repair that changed code the campaign's verdicts
   judged moves the tree, and the resume halts `TREE-MOVED` at launch: the
   cached verdicts describe the old tree, so launch fresh instead. Edits to
   the graph file, `WORK.md` and the plugin's own `.claude/fluxpoint/`,
   `.claude/workflows/` and `.claude/worktrees/` do not count; an edit to
   tracked config such as `.claude/settings.json` does. The unchanged prefix returns from cache; only the repaired
   node onward re-runs, and any irreversible node among them replays
   from the ledger rather than firing twice. Restarting a mostly-green
   graph from zero is a finding.
8. Graph green is not done. The campaign still exits through the
   loop-engineering ship pipeline — `scripts/harness.sh --full`, red-team
   `VERDICT: SHIP`, the Merge policy in WORK.md — and the Stop-hook DoD
   gate keeps final authority. Set `STATUS: DONE` only when the Evidence
   row shows the terminal gate green; otherwise back to `READY` with the
   repair plan in Notes.
9. If the Workflow tool is unavailable — Codex has no equivalent, and older
   Claude Code builds lack it — degrade per the graph-engineering skill: run the compiled script's nodes as parallel subagent calls with
   the same contracts, then record with `--executor degraded-subagents` so
   the Evidence row says which executor ran the graph.
