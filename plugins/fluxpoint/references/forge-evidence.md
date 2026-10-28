# CI evidence on the judged commit

Commit and push the gate manifest before running a guarded graph. CI can replace
one independent local step only when the forge itself names the exact PR head
or branch/tag tip, an independent pinned identity passes every covering context,
and all coverage files match a packet commit pinned in this manifest:

```json
{
  "version": 1,
  "gates": {"python-suite-312": "scripts/python-suite.sh"},
  "ci": {
    "forge": "github", "repository": "your-org/your-repo",
    "contexts": ["ci/python"],
    "witness": {"statusCreators": ["dedicated-ci-bot"], "checkRunApps": []},
    "packetSha": "0123456789012345678901234567890123456789",
    "covers": {
      "python-suite-312": {
        "contexts": ["ci/python"],
        "config": [".woodpecker/python.yml", "scripts/python-suite.sh", "pyproject.toml"]
      }
    }
  }
}
```

Replace the example repository, identity, commit, context and complete file list with this
repository's reviewed values. The repository and github.com API host are explicit;
`GH_REPO`, `GH_HOST` and mutable git remotes cannot redirect the witness. `packetSha` is the authoritative base, not a node
argument or a field trusted from the attest log. `--reuse` has no base override.
For App check runs, pin numeric `checkRunApps` IDs instead of a status creator.
The harness runs `attest.py --check-ci --if-present`: exit 2 refuses malformed
policy or a status creator equal to `gh api user`; exit 3 means the identity
cannot be checked. Both stop the harness. A same-account Woodpecker installation
cannot green until it uses a dedicated bot whose token is absent from this host.
These pins authenticate forge API provenance; they are not artifact signatures.

The adapter reads every page of `/commits/<sha>/statuses` and all check runs,
including creator login/app ID. Only the newest row from a pinned identity can
green a context. A failure/error from any writer at any time keeps that context
red, including an old failed attempt followed by success. Skipped/neutral checks,
unpinned success, missing contexts, malformed responses and incomplete listings
never green. A target that moves during observation is refused.

Coverage is deliberately conservative. Include the pipeline YAML, every tracked
file its tokens reference, literal Python `-m` modules/package initializers,
and static imports recursively referenced by those files. All tracked `pyproject.toml`, `setup.cfg`, `pytest.ini`, `tox.ini` and
`conftest.py` files anywhere in the repository must be included, covering implicit
reads without guessing working directories. Every listed file must exist as a
regular blob with the same mode and object ID at the head and packet. Symlinks,
truncated trees, mismatched content/object IDs, constructed shell paths and command
substitution are refused. Dynamic pipelines or excessive coverage can use the
independent local lane; an undecided reuse is never a pass.

An independent node of any object contract can declare
`"cites": ["python-suite-312"]`. The compiler extends that node's result schema
with a full top-level judged `sha` and `citations`, exactly one `{gate, exit, attestId, sha, target}` per step.
Every step `sha` must equal the node's full top-level judged SHA; `target` is `pr:N` or `ref:branch`. Its injected
instructions start `attest.py --run GATE --detach --nonce NONCE`, poll
`attest.py --reuse GATE --pr N --wait --nonce NONCE` in bounded foreground
slices, and cancel/collect the local token only after a forge green. A completed
red from either source dominates. Never end the node with a local lane running.
Steps without CI coverage still run locally.

`record-run` re-reads every forge/reuse pointer, including memoized results on
resume. The PR/ref must still name the claimed SHA; identity pins, current
contexts and reuse blobs must still substantiate the claimed exit. A fabricated
log green cannot substitute for those facts. Local rows retain single-use,
launch nonce and `since` checks. Wrapper rows record dirt before and after the
run; hook rows record dirt when the hook observes completion. Dirty rows and
legacy rows without content binding are refused. The hook cannot detect a dirty
edit reverted before completion; use the wrapper when pre-run binding matters.
The writable local log remains an honest-executor check, not protection against
a deliberate forger of local executions.

Tests use deterministic GitHub API fixtures and actual CLI subprocesses, not a
live forge or a provisioned CI bot. Windows fixtures mock only the API transport
because a POSIX shebang `gh` stub is not a Windows executable.
