# Read-only machine health

Run before every Workflow launch and resume:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/py.sh" box-health.py
# Machine-readable report, with the same exit status:
bash "$CLAUDE_PLUGIN_ROOT/scripts/py.sh" box-health.py --json
```

Exit **0** means the sampled checks were healthy. Exit **1** means a health
finding needs operator inspection. Exit **2** means an observation was
unreadable or unsupported; the report names which one. Missing process or
network data never becomes zero usage or a green result. Findings include
PID, parent PID, command line and UTC creation time. The checker never kills
a process, changes its priority or edits machine configuration.

The default sample is five seconds. CPU percentages come from differences
in current counters; a process's accumulated lifetime CPU time is not its
current load. Aggregate CPU at 90% or more reports the five busiest sampled
processes. A process with a missing or reused parent consuming at least 90%
of one core is reported separately. An orphaned plugin Python process older
than the longest timeout in the installed `hooks/hooks.json` is also reported.
Deliberately detached `attest.py --run` and `--ci` jobs are not classified as
expired hooks solely because their parent ended.

Linux uses procfs and stable PID/start-time identities. Unix reparenting loses
the original parent PID: ordinary long-standing init children are therefore
not presumed orphaned. An observed adoption during the sample, an init-adopted
literal `while True: pass` / `while 1: pass` command, or an init-adopted bounded
`secret-guard.py --hook` / `attest.py --record` process supplies stronger
suspicion. These are operator findings, not authorization to terminate work.
Aggregate CPU still reports saturation when a busy process's original parent
cannot be reconstructed.

Windows reads `Win32_Process` twice in one `powershell.exe -NoProfile`
invocation and obtains aggregate CPU counters through `GetSystemTimes`.
Creation timestamps prevent reused PIDs from inheriting old CPU measurements.
TCP range and connection queries are read-only. Access failures and missing
commands are named. Fixture tests cover the Windows command output and parser.
The [Windows CI job](../.github/workflows/windows-specification.yml) also runs a
real read-only sample and accepts a loaded runner's health findings, while
failing unreadable collection. This is not a sustained Windows/Git Bash load
or forced-timeout test, and Linux fixture results alone do not verify Windows.

The loopback probe opens a temporary listener on `127.0.0.1` and measures five
request/response round trips after warming the connection. Its default limit
is a 50 ms median. Every socket closes afterwards. TCP pressure counts distinct
local ports within the configured dynamic TCP range, including listeners and
TIME_WAIT. The threshold is 80%; this is a pressure estimate, not a guarantee
about available address/port tuples. Windows reports IPv4 and IPv6 ranges
separately. Linux observations apply to the process and network namespaces
visible to the checker. Other operating systems are explicitly unsupported.

Thresholds can be set directly:

```bash
bash "$CLAUDE_PLUGIN_ROOT/scripts/py.sh" box-health.py \
  --sample-seconds 5 --cpu-threshold 90 --orphan-cpu-threshold 90 \
  --port-threshold 80 --loopback-ms 50
```

`--plugin-root` selects the hook configuration used to obtain the longest
hook timeout. `--plugin-timeout <seconds>` sets that age threshold explicitly.
Samples must be positive and at most 60 seconds; aggregate CPU and port
thresholds must be positive and at most 100%.

The watchdog runs a fresh health sample on **every poll**, outside replayed
Workflow sentinels. New findings add `box-unhealthy` inbox items with operator
evidence and exit 1, waking the orchestrator while agents are still active.
Unreadable or unsupported observations exit 2. Repeating a finding does not
append another copy of the same open inbox item. Cleanup becomes visible on
the next observation; the operator resolves the durable inbox item explicitly.
The matching watchdog flags are `--health-sample-seconds`,
`--health-cpu-threshold`, `--health-orphan-cpu-threshold`,
`--health-port-threshold`, `--health-loopback-ms` and
`--health-plugin-timeout`. Hand-written Workflows use the same watchdog.
