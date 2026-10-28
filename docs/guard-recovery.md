# Interrupted guard verification

`guard-guard.py --verify` saves the guard's exact original bytes and writes
`.fluxpoint-guards-restoring` before replacing the guard with its mutation.
Normal exit and handled signals restore it immediately. Native Windows process
termination and abrupt exit may skip those handlers; the next guard CLI
invocation recovers the journal before reading the guard manifest.

The journal records a backup within the repository, the target path, byte
digests and the verifier's process birth identity. Recovery refuses a verifier
that is still running, an unsafe path, a damaged backup or a file changed since
the interrupted verification. It preserves the journal and backup and reports
the reason. Preserve any new edits before manually restoring the original
bytes from the named backup. Legacy two-line sentinels contain no backup path
and require manual recovery.

Recovery completes when the restored bytes match the original digest and both
owned artifacts are removed. It does not establish that the interrupted proof
passed; run `--verify` again to measure the guard.
