# Warm prompt recall

Per-prompt recall remains off by default. Set `FPL_MEM_PROMPT=1` to arm it;
`FPL_DISABLE=1` disables it. The existing precision floor and 1,200-byte context
budget apply to both warm and cold queries.

When Node.js is available, `prompt-recall.sh` starts a small Node client without
sourcing the shell library or starting Python. Its first query starts a detached
Python query process through `py.sh`; later queries reuse that process and its
parsed indexes. The process calls the existing offline prompt retrieval code.
It never rebuilds indexes or contacts an embedding provider. Session refresh
and explicit recall builds retain their separate behavior.

The query process caches at most 32 MiB of JSON input bytes and 32 files. It checks
file identity, size and modification time before reusing each file, so an atomic
index replacement is visible on the next query. Source freshness diagnostics are
cached for at most five seconds. Repository and corpus schema changes select a
new process identity; existing processes exit after five idle minutes. Set
`FPL_RECALL_DAEMON_IDLE` to an idle duration from 1 to 3,600 seconds if needed.

Endpoint metadata lives in the user's private
`~/.cache/fluxpoint/prompt-recall/` directory. Each process listens only on
`127.0.0.1`, using an ephemeral port. Nonce-bound HMACs authenticate requests and
responses, including when a dead process's port has been reused; the secret
never travels over the socket. The process removes its own endpoint on idle
exit. Query payloads are limited to 32 KiB and replies to 4 KiB; ordinary rendered
recall remains capped at 1,200 bytes. Inputs, connections, startup and cold
fallbacks all have time bounds.

The detached process inherits no hook standard handles. Project jurisdiction and
workspace containment match the other hooks: a project already inside Git stays
in that project, while a non-Git workspace can follow a contained payload cwd.
Sibling paths, symlink escapes and Git-reported roots outside the workspace are
refused. Without Node, the shell uses the existing cold query path. If a warm
endpoint is unavailable, the Node client makes one bounded cold attempt. Failure
to recall context remains advisory and cannot fail a prompt.
