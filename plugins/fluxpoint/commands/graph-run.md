---
description: Compile WORK.md's IR to a Workflow script, execute it, record provenance and the Evidence row — targeted node repair and cached resume on partial failure, never a restart from zero.
argument-hint: [path to WORK.md, plus any campaign args as JSON]
---

Run the designed graph. This command is the explicit authorization the
Workflow tool requires.

1. Resolve `$ROOT`: `CLAUDE_PLUGIN_ROOT`, then `PLUGIN_ROOT`, or the parent
   of this loaded command's `commands/` directory. Use the same installation
   for all scripts and agents below.
   The graph file is "$ARGUMENTS" if it names one, else `WORK.md`.
2. Preflight, all deterministic — stop and report exactly what is missing
   rather than improvising around it:
   - Run `bash "$ROOT/scripts/py.sh" unchecked-repairs.py --graph <graph> --root <project-root>`.
     It lists every commit since the latest recorded BLOCK's reviewed head,
     including its final `Fluxpoint-Executor` trailer. Exit 1 stops a graph
     containing a RedTeamV1 or irreversible node; exit 2 means history or
     records could not be read and also stops launch. A repair-only graph
     may run to check this list. Its independent FixCheckV1 PASS must name
     the checked SHAs in `commits`; `record-run.py` files them as `checked`.
     Alternatively, record explicit acceptance in a DecisionV1
     `acceptedCommits` array with `decision.py --record`. A SHA mentioned in
     prose, a passing build, or an executor label does not discharge it.
   - Before **every launch and resume**, run
     `bash "$ROOT/scripts/py.sh" box-health.py` outside Workflow replay.
     It samples machine CPU/process counters for 5 seconds, checks suspected
     orphans, aged orphaned plugin hooks, loopback and dynamic TCP port pressure.
     Exit 1 stops the launch and prints operator PIDs, command lines and creation
     times. Exit 2 also stops: an unreadable or unsupported observation must be
     reported and resolved before certifying the machine healthy. It never kills processes or changes machine config.
     Thresholds are explicit flags; see `docs/box-health.md`. Repeat after any
     cleanup, including when resuming a run with cached node results.
   - `bash "$ROOT/scripts/py.sh" compile-graph.py <graph> --check` exits 0.
     Its findings are the work list; fix the IR, never the compiler.
     Its versioned `launch args` and `post-launch` lists are authoritative
     for this installed compiler, even if these command instructions were
     loaded before a plugin upgrade. Load each named producer's current
     output; do not recover from a refusal by guessing a missing field.
   - Run `bash "$ROOT/scripts/py.sh" attest.py --root <project-root> --check-ci --if-present`.
     A configured CI witness must pin an identity the harness cannot post as;
     exit 2 refuses the harness's own status creator, and exit 3 means identity
     could not be established. Either stops launch. No CI declaration is dormant.
   - If `.fluxpoint-gates.json` declares a gate named `preflight`, run
     `bash "$ROOT/scripts/py.sh" attest.py --root <project-root> --run preflight`
     and require exit 0 before launch or resume. Put the repo's fast graph,
     pin-consistency and launch checks there so they run before builder suites.
     The compiler also compares a SHA explicitly restated beside its
     `argDefaults` key (such as `packetCommit is <sha>` or `packet commit at
     <sha>`). Abbreviated prefixes agree; stale literals outside fenced blocks
     and the historical Evidence section fail with their line numbers.
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
   Repeat preflight on the exact file before invoking Workflow:
   `bash "$ROOT/scripts/py.sh" compile-graph.py <graph> --check --check-script .claude/workflows/<name>.graph.js`.
   The exact-script check also verifies the embedded plugin root, version and
   witness/helper hashes. A missing installation, changed code or missing pin
   stops launch. Keep the compiling installation unchanged for the campaign;
   node prompts name its absolute pinned launcher.
   Both checks enforce the Workflow limit of 524,288 UTF-8 bytes and print
   headroom; below 16 KiB is a warning. `--max-script-bytes` overrides the
   limit only when the selected launcher supports the new limit.
   For a campaign carrying repair notes or other non-underscore args, write
   those values to a JSON object and assemble the first launch with:
   ```
   bash "$ROOT/scripts/py.sh" resume.py --root <project-root> --graph <graph> --args initial-args.json
   ```
   This prints a Workflow object on stdout, embeds the complete note text in
   the script, checks pinned evidence, loads launch state and verifies the
   exact compiled file. Diagnostics go to stderr. The compiler archives the
   source under `.claude/fluxpoint/compiled/`; retain it with the run artifacts.
   Direct `compile-graph.py --args <file>` also embeds args, refuses underscore
   fields and inline overrides, and counts every byte toward the script limit.
   Recompiling an embedded script without `--args` refuses to drop its notes;
   `--args none` explicitly discards them for a fresh launch. See
   `references/structured-resume.md` for evidence pins and exact argument carry.
4. Load whatever state the graph refuses to start without.
   `resume.py` supplies these readings and a fresh 32-byte
   `_replayLaunchNonce` automatically. Treat `_memo`, `_prefix`,
   `_resumeFromRunId` and `_replayLaunchNonce` as script-produced provenance;
   never manufacture a memo from model-written arguments or old verdict text.
   Record the launch reading, fresh at every launch including a resume:
   ```
   args._base = {"sha": "$(git rev-parse HEAD)", "branch": "$(git branch --show-current)",
                 "porcelain": "$(git status --porcelain)",
                 "root": "$(git rev-parse --show-toplevel)"}
   ```
   A graph with any `isolation` node — or `mutates: true`, which implies a
   worktree — refuses to start without it. Every graph that guards its tree
   (the default) compares it against the run's first sentinel reading: the
   sentinel is a cached `agent()` call on resume, so this reading is the
   only one that sees the tree the resumed run is really on. Never reuse an
   earlier launch's `_base`.
   A supplied `_base` without `porcelain` is refused, including when clean:
   use the empty string. `sha` must be a nonempty string, `branch` a string
   when supplied (empty for detached HEAD), and optional `root` a nonempty
   string. Numeric or boolean values are refused before agents start.
   The sentinel reads its own checkout root and
   compares it before HEAD or dirt. `WRONG-ROOT` means the session and launch
   reading name different checkouts; move the session to the launch checkout
   with `/cd` and launch fresh. Repeating a fresh launch in the same wrong
   directory cannot fix it. Root normalization preserves the leading `//`
   of a network checkout so it cannot match a different local root.
   Isolated nodes are handed this base in their prompts and told to assert
   it before trusting the tree they were given: a worktree's base is the
   runtime's choice, and it has been observed cut from the default branch
   rather than the campaign's, where a file an earlier node committed is
   simply absent and nothing inside says so.
   If any node declares `verify: prove:<gate>`, `cites: [<gate>, ...]`, or a repair `repeat.until`,
   stamp the launch; the
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
   tool call runs as `attest.py --run <gate> --detach --nonce <nonce>` and
   is collected with foreground `attest.py --await <token>`; a
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
   the launch-turn rule: Workflow gives every node the launching turn's
   latest user message, unchanged for the whole run; a compiled prompt
   cannot extend it. Launch only when that message is this run command or
   the operator's go for this graph. If a narrower message arrived after
   it, ask the operator to re-issue `/fluxpoint:graph-run <graph>` before
   invoking Workflow. A recorded ruling may be bound with IR
   `authorizedBy: <decision-id>` and the same id in `imports`; its chosen
   text, recordId and date reach every mutating node as context and cannot
   override the relayed user message. Invoke with
   `{scriptPath: ".claude/workflows/<name>.graph.js", args: {...}}`.
   If Workflow refuses `scriptPath` because the compiled script is outside
   the session's working directory, move the session to that campaign
   checkout with `/cd` before launching. Do not copy the script into another
   checkout: the nodes would then run there while the launch readings still
   described the campaign checkout.
   Pass args as a real JSON object, not a stringified one. Watch with
   `/workflows`; never poll with sleep. As soon as the tool returns its run id,
   start the observer with the Bash tool's `run_in_background: true`:
   ```
   bash "$ROOT/scripts/py.sh" watchdog.py --run <runId> --root <project-root> --base <args._base.sha>
   ```
   Do this for every launch and resume, so a stall or failure wakes the
   orchestrator through the background task's completion. Exit 1 names the
   affected agents or machine findings and adds inbox items (`box-unhealthy`
   for machine findings); a killed agent also gets a resume
   note with its checkout state and commits since launch. Exit 2 means the
   process/network coverage or the internal journal/transcript format could
   not be read or is unsupported: inspect the named observation and
   report the missing observation. Exit 0 means no agents remain live and
   the journal has been quiet for 15 minutes. Hand-written Workflow scripts
   use this same command. `--stall-min` defaults to 40; `--once` performs one
   inspection (exit 3 while the run is still pending). Every poll runs a fresh
   5-second box-health sample, even while agents remain active, so CPU load or
   orphan hooks do not have to wait for a transcript stall. The observer never
   kills a process. `--health-sample-seconds`, `--health-cpu-threshold`,
   `--health-orphan-cpu-threshold`, `--health-port-threshold` and
   `--health-loopback-ms` configure those observations explicitly.
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
   Run artifacts always go to `<root>/.claude/fluxpoint/runs`, even when
   recording from a subdirectory; pass `--root <project-root>` explicitly.
   `--state-dir` may only name that canonical directory. Exit 3 retains the
   artifact but means the Evidence row was NOT appended: add the canonical
   five-column header and separator, or pass `--evidence WORK.md`, then
   record into the intended Evidence table before calling the run filed.
   That also appends any irreversible effect to the ledger, so the next
   run replays it instead of repeating it. Before diagnosing an empty or
   surprising result, read the run's `journal.jsonl` — it records what
   each node actually returned, including cached ones.
7. Partial failure is a targeted repair: fix the red node (its IR prompt,
   contract or code), record the previous run, and stop it if still live.
   Assemble the resume, changing only the named notes:
   ```
   bash "$ROOT/scripts/py.sh" resume.py --root <project-root> --graph <graph> \
     --run <runId> --set repairRelay=@repair-relay.txt
   ```
   The helper recovers the executed script's embedded args from its verified
   archive, cross-checks recorded `summary.inputs`, carries all other values
   exactly and refuses drift. `--set` reads UTF-8 text without changing line
   endings; `--set-json` supplies structured values such as `rejudge`.
   It prints the compiler's deterministic per-node rerun plan to stderr before
   returning the Workflow object on stdout. Read that plan before dispatch.
   It retains the original `_launch`, refreshes `_base`, `_ledger` and
   `_releases`, and loads replay receipts from the original journal and
   transcripts. `_resumeArtifactSha256` pins the exact source attempt, including
   when later resumes keep the same Workflow run ID. Initial assembly embeds
   effective defaults too; extra unbound recorded inputs are refused. Pinned
   evidence is rehashed at assembly and recording.
   Do not hand-copy large args or override an embedded key inline. A legacy
   run without embedded args/source archives needs an explicit fresh launch;
   the helper refuses to guess its context. A repair that changed code the campaign's verdicts
   judged moves the tree, and the resume halts `TREE-MOVED` at launch: the
   cached verdicts describe the old tree, so launch fresh instead. Edits to
   the graph file, `WORK.md` and the plugin's own `.claude/fluxpoint/`,
   `.claude/workflows/` and `.claude/worktrees/` do not count; an edit to
   tracked config such as `.claude/settings.json` does. The compiler plan
   determines which calls replay under the graph's declared replay rules;
   irreversible nodes replay from the fresh ledger rather than firing twice.
   Apply step 5's launch-turn rule to every resume too. A node that stopped
   because its slice goes beyond the relayed user's request has an
   authorization-context stop, not a code defect. Resume from the operator's
   run command or go for this graph, with no invented code repair; if a
   narrower message arrived, ask for the run command to be re-issued first.
   Before recompiling or dispatching a BLOCKING repair, write a structured
   repair note with JSON arrays `touches: [<repo-relative planned paths>]`
   and `changesBehaviourOf: [<production paths or module names>]`; use `[]`
   explicitly when none. Run `specification.py --graph <graph> --touches
   <paths>` for nonempty touches (it must exit 0; exit 4 names holding checks)
   and `specification.py --graph <graph> --reach <paths-or-modules>` for
   nonempty behaviour changes. Append that JSON as "locked checks that must
   stay green untouched". An edit to a locked input or a demanded behaviour
   contradicting a reached check needs a recorded operator ruling and packet
   revision before dispatch. A declared path list is not permission or live
   change evidence. The builder first probes reached checks on a minimal
   scratch change and returns `SliceV1.packetConflict` immediately if red,
   with check ids, failing test node ids and a counterexample plus proposed
   `packetEdits`. A conflict only blocks; it never permits skipping a gate.
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
   Direct repairs made outside a graph remain unchecked until an independent
   repair run or explicit recorded acceptance discharges them. End each Codex
   commit with `Fluxpoint-Executor: codex` in the final Git trailer block,
   alongside other trailers, so preflight can identify the executor.

For any agent node running a long undeclared command, use
`longrun.py start --log <file> -- <command> <args>`. Await in the foreground,
repeat after exit 3, and never end the node turn while the command runs: that
turn end is the node's answer. A log match is readiness, not completion. Use
`longrun.py cancel <token>` or `attest.py --cancel <token>` to stop the command
process tree. See [long-command lifecycle](../references/longrun.md).
