# Native command reading bindings

A worker's returned text is a transcription. `bind-native.py` compares it with
completed Workflow capture files under the configured Claude projects store.
It reads the native journal, the agent JSONL transcript and its metadata; the
caller selects a run and agent, rather than supplying raw tool events.

```bash
bash "$ROOT/scripts/py.sh" bind-native.py --run-id wf_example --agent-id AGENT
# A non-default store is explicit:
bash "$ROOT/scripts/py.sh" bind-native.py --run-id wf_example --agent-id AGENT \
  --projects /path/to/claude/projects
```

The fixed worker returns the closed `NativeReadingsV1` shape:

```json
{"commands":["git status --porcelain"],"readings":[{"exit":0,"output":""}]}
```

There must be exactly one foreground Bash call for each command in that order,
from the metadata's `worktreePath`, followed by one `StructuredOutput` matching
the native completed result. Other tools, extra commands, mismatched cwd,
unfinished calls, conflicting duplicates and explicit truncation are refused.
Transport copies with the same tool-use id keep the first occurrence and its
cwd. Unrecognized capture shapes remain unbound.

Only these output normalizations are accepted: CRLF becomes LF, a native
leading `Exit code N` header supplies the reported exit, and the native empty
output sentence becomes empty text. An error without a readable exit uses
`null`, which cannot establish success. The certificate retains every tool-use
id, output hashes, whether the strict strings matched, and each normalization
applied. It prints no raw command output. An optional `--report file.json`
compares an external copy with the native returned value too.

For long output, write a relative artifact and have the exact command print
only its `sha256sum` marker: digest, two spaces, relative path, newline. Return
`{"exit":0,"artifact":{"path":"output.json","sha256":"<64 hex>"}}`.
The binder compares that marker and reads the file bytes back. Artifacts must
stay within the worktree, have no escaping symlink and fit the 64 MiB bound.
Each native capture file has the same bound. A truncated runtime result cannot
be recovered by inventing the missing text.

A scalar graph node can use `NativeReadingsV1` with `isolation: "worktree"`.
Its command list includes compiler base-check commands as well as its own
prescribed commands. The compiler supplies fixed-reading instructions;
`record-run.py` locates that node's completed native call and records a binding
certificate. Missing or mismatched source keeps the run INCOMPLETE. Fan-out,
repeat, panel, reducer and parked forms are refused for this contract. The
binding proves the listed tool observations; completeness of the intended
command inventory remains the graph/specification's separate obligation.

Graph audits expose `nativeCommandBinding: PENDING` and `ready: false` whenever
they run probes. Their probe seats use only the prescribed safe Bash commands
and StructuredOutput, and include the actual exit and complete output. After
the Workflow completes, bind its probe receipts before accepting SOUND:

```bash
bash "$ROOT/scripts/py.sh" bind-native.py --run-id wf_example \
  --audit audit-result.json > bound-audit.json
```

The postprocessor matches every probe label with its unique native agent.
When the native journal omits labels/options, a fixed initial
`Fluxpoint-Native-Seat` task marker supplies the label from the native user
prompt (after the known Workflow frame), rather than from the worker result. It
checks the relayed receipts against its returned value, then binds the actual
Bash observations. Use the resulting file in the audit log. The certificate
corroborates command evidence; it does not independently judge the audit's
findings, safe-form semantics, or verdict. Quick mode never establishes READY.

Domain-specific readers can use `native_binding.tool_receipts` to obtain the
same stable source hashes, exact Bash commands, tool ids and normalized results
while applying their own reviewed helper policy. That interface permits other
inspection tools and therefore does not certify the fixed-worker inventory.

These sources are runtime-owned during ordinary host execution, and are local
files rather than service signatures. An actor able to rewrite all native
source files can forge this boundary. The current implementation is tested
against native-shaped capture fixtures and actual CLI/recording subprocesses.
This development environment exposes no Workflow execution or transcript
connector; it supplies no live native service attestation. Configurations
without accessible native captures remain unbound.
