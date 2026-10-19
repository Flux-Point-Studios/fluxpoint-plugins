# Long-command lifecycle

Run a suite that may outlive the 600 second tool cap with the shared runner.
`longrun.py` accepts arbitrary argv and attests nothing. Declared gates use
`attest.py --run <gate> --detach`, which retains the gate-name-only rule and
mints a wrapper attestation from the command's actual exit.

```bash
TOKEN="$(bash "$ROOT/scripts/py.sh" longrun.py start --log .claude/fluxpoint/api.log -- \
  python -m pytest tests/api)"
bash "$ROOT/scripts/py.sh" longrun.py await "$TOKEN" --timeout 540
```

Start returns immediately with the token. Save it and the log path. Await in a
**foreground** tool call, then repeat the foreground call on exit 3. Never end
an agent node's turn while its command runs: the runtime records turn end as
the node's answer. Every compiled agent node gets this guidance, including
builders, checkers, repair rounds, and verification nodes.

The command runs in the start call's working directory with inherited
environment and no stdin. Arguments are passed directly, without shell
interpretation; use `bash -c '...'` explicitly when a shell is needed. Logs are
UTF-8 decoded with replacement when displayed. Relative log paths resolve from
the start call's directory. Records and absolute paths survive a new await
process or a resumed session under `.claude/fluxpoint/longrun/`; pass the same
`--root <project>` **before** the subcommand when resuming from another cwd.
Keep distinct logs for concurrent runs.

```bash
bash "$ROOT/scripts/py.sh" longrun.py await "$TOKEN" --until '^server ready$' --timeout 120
bash "$ROOT/scripts/py.sh" longrun.py await "$TOKEN" --timeout 540
bash "$ROOT/scripts/py.sh" longrun.py cancel "$TOKEN"
```

`--until` is a Python regular expression matched against complete log lines.
It can return as soon as readiness appears. A match can return exit 0 while the
record still says RUNNING; continue awaiting completion before returning a
node result. Invalid regexes fail explicitly. Lines over 1 MiB are refused by
regex waits; use an ordinary await for such logs. Each await defaults to 540
seconds and clamps the requested timeout to 570 seconds, below the tool cap.
It prints status, the actual command exit when known, and the last 20 log lines.

| Await exit | Meaning |
| --- | --- |
| 0 | Completed (including a nonzero command exit or cancellation), or the requested log line matched. Inspect status and the command's exit. |
| 2 | Unknown/unreadable token or invalid request, such as a bad regex. |
| 3 | Still running. Call await again in the foreground. |
| 4 | Runner died, launch failed, or no usable completion record remains. No passing result can be inferred. |

A launch error has status ERROR and its reason, never a fabricated exit 0.
Cancellation before command launch has no command exit and creates no
attestation. Cancellation after launch records the actual termination exit
when the runner can collect it; that exit cannot certify a passing gate.

For a gate, use the same lifecycle:

```bash
bash "$ROOT/scripts/py.sh" attest.py --run harness --detach --nonce <launch-nonce>
bash "$ROOT/scripts/py.sh" attest.py --await <token> --until '^ready$' --timeout 120
bash "$ROOT/scripts/py.sh" attest.py --await <token> --timeout 540
bash "$ROOT/scripts/py.sh" attest.py --cancel <token>
```

`attest.py --await` and `--cancel` also accept a gate name for its newest run.

Classified interpreter crashes can use the recorded, one-shot
[`--rerun-fault` native/reference pair](fault-reruns.md). Its pair token uses
the same await/cancel path; an ordinary later green does not erase a red.
Attestations retain their existing record directory and citable IDs. A log match
never mints an attestation; only collecting the real command exit does that.

Both callers use `scripts/background.py` and `scripts/process_tree.py` for
records, detached creation, command ownership, waiting, liveness and cancellation.
A command leader exiting does not complete the run while its owned descendants
remain active. On POSIX, commands run in a new session/process group; the owner
observes live group members, sends TERM and escalates to KILL, and verifies that
the group drained before recording completion or cancellation. Zombies do not
count as working descendants.

On Windows, the command starts suspended with `CREATE_NEW_PROCESS_GROUP`, is
assigned to a named Job Object, and only then resumes. All ordinary descendants
inherit the job, so a fast-exiting leader cannot lose them. Await checks the
job's active-process count; cancellation opens and terminates that exact job.
`KILL_ON_JOB_CLOSE` also terminates descendants if the owning runner dies. Win32
handles use pointer-sized types throughout. The low-level compatibility helper
still provides `taskkill /T /F /PID` for separately managed commands; supervised
longrun/attest trees use jobs rather than trusting a dead command PID.

Records bind runner and POSIX member PIDs to process-creation identities.
Cancellation refuses a reused or unidentifiable PID. If a POSIX owner is forcibly
killed after all recorded members exit and an unobserved grandchild survives,
its ownership cannot be proved from the last snapshot: cancellation explicitly
returns 4 rather than killing an unrelated group or claiming cleanup. A live
owner handles this normal descendant turnover directly. Treat an unproven
owner-loss state as unresolved cleanup, not a completed verdict.

Cancellation is terminal only after observed group/job drain. If cleanup cannot
be verified within the bounded cleanup wait, the record stays RUNNING with a
pending cancellation and the CLI returns 4. Regex evaluation runs in an isolated
worker under the remaining await deadline, including worker startup; pathological
backtracking times out explicitly instead of holding the awaiter past its cap.
Cancel through these commands rather than wrapping a suite in `timeout`, killing
a random PID, or leaving a custom polling/sleep process.

The portable subtree test executes real child/grandchild cancellation on its
host platform, including a grandchild that ignores TERM. Additional POSIX tests
check group membership and gate cancellation; separate mocks check Windows
API/command shapes. A Linux pass and those mocks do not establish native
Windows behavior; run the same suite in Windows CI for that evidence.
