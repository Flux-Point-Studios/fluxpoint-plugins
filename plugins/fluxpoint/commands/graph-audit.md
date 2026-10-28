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
   the estimate and cache TTL, for step 4. For every node the audit probes,
   `--check` also runs the probe grammar from `workflows/graph-audit.js`
   under Node.js (step 4). It reads an `{{A.x}}` with no `argDefaults`
   value as the word `A.x`, so its verdict matches the Probe phase only once
   the audit resolves the real arguments.
3. Load the settled list from the graph's audit log, not from memory. The
   log is `.claude/fluxpoint/audits/<graph path from the repo root, each
   / replaced by __>.json`, so `WORK.md.json` for the root `WORK.md`. It is
   local state: `.claude/fluxpoint/` is gitignored. Its shape:
   ```json
   {"graph": "WORK.md", "rounds": [
     {"round": 1, "mode": "multi", "verdict": "REWIRE",
      "counts": {"raw": 41, "distinct": 22, "settledMatched": 0,
                 "confirmed": 13, "refuted": 9, "unverified": 0},
      "confirmed": [{"title": "<title>", "defectClass": "<class>", "classRewire": "<invariant-level repair>"}],
      "unverified": [], "refuted": ["<title>"],
      "fixed": ["<title>"]}
   ]}
   ```
   The settled list is every round's `fixed` titles plus every round's
   `refuted` titles, without duplicates. No log means a first round and an
   empty list.
   Preserve each confirmed finding's `defectClass`, `defectClasses` and
   `classRewire` beside its title. Read the last two multi-lens rounds
   before launching: if a class was confirmed in both, or the positive
   confirmed count did not fall, end with `VERDICT: REWIRE — plateau:
   <classes or confirmed count>`. Reframe before another audit. Old logs
   containing title strings still support the count rule.
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
           ir: <the exact parsed graph-ir JSON>, bindings: <actual campaign args, if known>,
           check: "<the --check summary line>", settled: ["<title>", ...],
           history: [<recorded rounds from step 3>]}}
   ```
   with args as a real JSON object. Before Lenses, a `Probe` phase runs one
   agent with `isolation: 'worktree'` per node declaring `isolation: true`,
   `isolation: 'worktree'`, or `mutates: true`. A `measure` node uses its
   frozen snapshot and does not get this runtime-worktree probe. Without
   `ir`, a mechanical reader discovers all nodes and `argDefaults` first;
   an unavailable plan blocks SOUND. `bindings` overrides `argDefaults`
   when resolving `{{A.x}}` and nested paths such as `{{A._launch.root}}`;
   a command left holding any other `{{...}}` leaves the probe incomplete
   unless a shape declaration types it (below).

   Each probe reads the actual node prompt and tries its prescribed commands
   only in safe, non-mutating read forms. Preserve the `git -C` target,
   `cd` target, `$(...)`, and `&&`, `;`, `|` chaining while replacing mutating
   subcommands with reads. These shapes can be refused by the runtime's
   isolation hook even when they work in the launch checkout. Never run
   fetches, ref changes, worktree creation, shared writes or deployments in
   the name of probing. If a safe equivalent cannot retain the shape, report
   it UNRESOLVED. An earlier short-circuit or a Git syntax error does not
   establish whether the isolation boundary accepted the intended command.
   Keep every original/safe command and measured response in the result.
   The workflow inventories shell fences, inline command snippets and command
   lines (including `Run git ...`) from the resolved prompt. Every inventory
   entry needs exactly one receipt with its original text; an unrelated
   `git status` cannot discharge a prescribed compound command. It checks
   operators, substitutions, `cd` operands and global Git targets itself.
   Existing read commands retain their arguments; only mutating Git
   subcommands can be replaced with approved reads. The grammar admits Git
   reads and `cd`, `pwd`, `test`, `[`, `echo`, `printf`, `cat`, `grep`,
   `head`, `tail`, `wc`, `true` and `false`. It excludes redirections,
   backticks, background processes and every other program.
   An agent that finds a command missing from the inventory reports a blocker.

   A node that runs a script says how its probe covers each such command,
   in a node-level `probe` object reviewed with the IR:
   ```json
   "probe": {
     "commands": [
       {"command": "python scripts/report.py --json", "kind": "read-only"},
       {"command": "bash \"{{A._launch.root}}/scripts/py.sh\" longrun.py await --timeout 240 <token>",
        "kind": "shape", "form": "bash \"{{A._launch.root}}/scripts/py.sh\" longrun.py await --help",
        "data": {"token": "token"}}
     ],
     "templates": [
       {"template": "git -C <dir> <subcommand> ...",
        "instances": ["git -C {{A._launch.root}} status --porcelain"]}
     ]
   }
   ```
   - `read-only` runs the exact command. Any Git part must be a read. No
     part may run a writer or network tool (`rm`, `cp`, `mv`, `touch`,
     `mkdir`, `curl`, `wget` and their kin), a launcher, an interpreter with
     an option before its script, a variable assignment, or a program named
     by a shell expansion.
   - `shape` runs `form` instead: the command's words up to some point,
     then `--help`. `-h` is refused, because `du`, `df`, `sort` and `ls`
     read it as human-readable output and `docker run` as a hostname. Every
     other part repeats exactly, with the same operators, `cd` and
     `git -C` targets and `$(...)` substitutions. The help flag may not
     follow a `--` separator or a word naming another program, where
     `longrun.py start --log L -- <argv>` would forward it to the child
     after starting work, nor an operand of a writer such as `rm`. Use the
     shortest prefix that reaches the program's own argument parser.
   - A launcher hands its trailing words to another program, container or
     host, so it never takes a shape form: `docker`, `podman`, `kubectl`,
     `ssh`, `wsl`, `sudo`, `env`, `nice`, `nohup`, `timeout`, `time`,
     `xargs`, `exec`, `eval`, `npx`, `pipx` and similar.
   - An interpreter takes no option before its script or module. Python
     (`python`, `python3`, `python3.N`, `py`) takes only `-I`, `-u`, `-B`,
     `-E`, `-s`, `-S` and `-O`, and `-m <module>` counts as its script;
     `node`, `bash`, `sh` and the other shells take none. So `-c`, `-e`,
     `-p`, `--eval` and combined forms such as `-Ic` are refused, by name.
   - `<name>` in a shape command, or a later-bound token such as
     `{{prev.sha}}` or `{{item.id}}`, is data an earlier output fills in.
     `data` gives it a type, `token`, `sha` or `integer`, keyed by the name
     or by the token text without braces (`"prev.sha": "sha"`), and the
     probe runs the form with that type's fixed dummy, never a value of its
     own. A read-only command cannot carry either. `{{A.x}}` is a launch
     binding: it resolves from `argDefaults` and `bindings` and is never
     typed as data.
   - A `template` is an illustrative command from the prompt, with `<name>`
     or `...` placeholders. Its `instances` are the concrete commands the
     node runs; each must match the template and is receipted like any
     other inventoried command.
   An undeclared command outside the grammar, a declaration that matches
   nothing in the inventory, a refused form and an unresolved binding each
   leave the probe incomplete. `--check` refuses the same, except where a
   binding known only at launch changes the command. Wording a prompt so the
   inventory misses a command is not a remedy: the node still runs it.

   The inventory takes shell fences, command lines, and inline spans whose
   first word names, by basename, a program the grammar knows (so
   `"C:/Py/python.exe" x.py` counts) or that directly follow run, execute,
   probe or invoke (so "Before approving, execute `aiken check`." counts).
   Punctuation left beside an inline command is not a command. Git must be
   spelled `git`: a path, quotes or `.exe` is refused wherever it appears,
   because the grammar judges Git by its subcommand. A program word with a
   backslash is refused too, since a shell reads the backslash as an escape
   and Windows as a separator; write such paths with forward slashes.

   A declaration remains the author's reviewed assertion. A `read-only`
   program must really be read-only, and a `shape` program must act on
   `--help` in its own parser before any effect. The grammar refuses the
   classes above mechanically and no more. The Probe phase runs the
   declarations before any lens reads the IR, so review the IR diff,
   including every `probe` object, before launching the audit.

   Each receipt in the result carries its evidence `kind`: `read-only` when
   the exact command ran, `shape-only` when a safe form ran in its place,
   including a Git read standing in for a mutating subcommand. The result's
   `probeEvidence` lists both, and the verdict line names every accepted
   shape-only receipt. Neither is semantic proof: a shape-only receipt shows
   that the isolation hook admits the executable, cwd and argv prefix, and
   what the command does stays with the graph's attested gates.

   Probe refusals join the finding reduction and get refuters with
   `isolation: 'worktree'` too. A refuter unable to reach the same runtime
   boundary returns `reproduced: false`, leaving that finding UNVERIFIED;
   uncertainty never refutes a measured isolation refusal. Missing or
   incomplete probes block SOUND. A successful probe covers prescribed
   shapes only: an agent can later invent a compound command the hook refuses.

   A round then runs six lenses in parallel,
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
   probes add one agent per isolated node, plus a plan reader when `ir` was
   omitted. The observed motivating probe ran eight commands in 16 seconds;
   treat that as one observation, not a runtime guarantee.
   refutation roughly halved what reached the author. To audit other
   ground, pass `lenses`: ids from `LENSES` in the script, or
   `{id, focus}` objects.

   **`--quick`, one auditor.** Add `mode: "single"` to the same args. One
   `graph-auditor` runs after the same isolated probes, with no reduce and no refutation, so every
   finding comes back UNVERIFIED. It is for checks between repairs: its
   SOUND means one reader ran out of path, and it does not make a graph
   READY.

   **Without the Workflow tool** (Codex, or a Claude Code build that lacks
   it), run the same round by hand. Take the lenses and the refuter
   wording from `LENSES` and `REFUTERS` in
   `${CLAUDE_PLUGIN_ROOT}/workflows/graph-audit.js`. Run each lens as a
   subagent. First run the safe command probes in actual isolated worktrees;
   a plain launch-checkout shell cannot substitute for the runtime hook.
   If equivalent runtime isolation cannot be exercised, leave those probes
   UNVERIFIED and the graph short of SOUND. Give each audit
   subagent `${CLAUDE_PLUGIN_ROOT}/agents/graph-auditor.md` as its
   instructions plus its lens and the settled list, or as a separate
   inline pass when no subagent can be spawned. Merge duplicates by
   failure path without judging them, then refute every distinct finding
   under the rules above: three verifiers and 2 of 3 at HIGH and above,
   one below, refuted when uncertain, UNVERIFIED when a verifier returns
   nothing. Report the same counts and name the executor; inline passes
   share one context, which makes them weaker evidence than independent
   subagents.
5. Append the round to the audit log from step 3: its mode, verdict and
   counts, the full findings in the result's `confirmed`, titles in `unverified` and
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
   For a plateaued class, state the invariant its system of record enforces
   (for example the ledger, validator or a code-derived census). Adopt the
   finding's `classRewire`, refute that repair with a measurement, or remove
   the mechanism. A third patch to just the last reported shape is refused.
   Once that reframe is recorded with its measurement in Notes and the audit
   log, begin a new history epoch; retain earlier rounds under `epochs` so
   resetting the active `rounds` never erases the record of the plateau.
8. Record the final verdict in WORK.md Notes, with the mode and the counts
   line. A graph runs only from `STATUS: READY`, and READY requires both a
   clean compile and a SOUND from the multi-lens audit, the workflow or its
   fallback, on the current IR. A `--quick` SOUND does not qualify.
