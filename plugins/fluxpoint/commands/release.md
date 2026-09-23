---
description: Release a node parked on a human or a third party — record the proof that the work actually happened, so the campaign can resume on evidence rather than recollection.
argument-hint: [node id, or nothing to list what is blocked]
---

Lift a park. A campaign with `actor: human` or `actor: third-party` nodes
does not halt on them — it records what would unblock each one and keeps
working the branches that can proceed. This is how the block is cleared.

Resolve the plugin root: `${CLAUDE_PLUGIN_ROOT}`, else
`find ~/.claude/plugins ~/.codex/plugins/cache -type d -name fluxpoint 2>/dev/null | head -1`. Call it `$ROOT`.

1. **Show what is actually waiting**, never a guess:
   `bash "$ROOT/scripts/py.sh" inbox.py --list`. With no argument, stop here —
   the operator picks. If `$ARGUMENTS` names a node, continue with it.
2. **Read the block's own instructions** out of the campaign's provenance
   (`.claude/fluxpoint/runs/<runId>.json`, the BLOCKED entry for that node)
   and show them verbatim. That text was written when the graph was
   designed, by someone who knew what the step required; do not paraphrase
   it into something friendlier.
3. **Ask for the proof the node's `release.proofContract` names.** A
   transaction hash for a signature. An exit code and output for a check.
   Whatever the contract requires. Do not accept — or write on the
   operator's behalf — "done", "confirmed", "it worked", or any other
   adjective. You did not watch them do it.
4. Record it:
   ```
   bash "$ROOT/scripts/py.sh" release.py --record \
     --campaign "<the IR's campaign line>" --node <id> \
     --contract <the node's release.proofContract> --graph <the campaign's graph file> \
     --by "<who>" < proof.json
   ```
   `--graph` lets a proof contract from the graph's `CONTRACTS:` directory
   resolve. The script validates the document against the contract and **refuses**
   anything that does not satisfy it. A refusal is the tool working: the
   graph resumes on the strength of this file, and a run that resumes on a
   half-remembered "yeah that's done" will eventually resume on a mistake.
   Fix the proof, never the check.
5. Close the inbox item:
   `bash "$ROOT/scripts/py.sh" inbox.py --resolve blocked:<campaign>:<node>`.
6. Resume the campaign with `/fluxpoint:graph-run`, passing
   `resumeFromRunId` from the parked run so the unchanged prefix replays
   from cache instead of re-running. The released node now reads its proof
   from the file and its dependents unblock.

**Never invent a release.** Not to unstick a campaign, not because the
work obviously happened, not because a log line suggests it did, and not
because the run has been parked a long time. A fabricated release is
strictly worse than a stalled campaign: the stall is visible and the
fabrication is not.
