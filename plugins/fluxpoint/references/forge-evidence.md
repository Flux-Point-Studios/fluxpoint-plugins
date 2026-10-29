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
    "config": [".woodpecker/python.yml", "scripts/python-suite.sh", "pyproject.toml"],
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
policy or a pinned creator found in the active token, any stored gh account
(including inactive accounts), or a `GITHUB_TOKEN` masked by `GH_TOKEN`; exit 3
means the inventory is unavailable. A gh version supporting `auth status --json`
is required. Both failures stop the harness. The check covers gh stored accounts
and `GH_TOKEN`/`GITHUB_TOKEN` only; credential helpers, netrc files, other tool
configuration and CI secrets remain an operator boundary. Use a dedicated bot
whose token is absent from every store agents can reach. CI-server tokens with
push, restart or environment-override rights must also be inaccessible to agents,
including secrets available to pipelines the agents can edit.
These pins authenticate forge API provenance; they are not artifact signatures.

The adapter reads every page of `/commits/<sha>/statuses` and all check runs,
including creator login/app ID. Only the newest row from a pinned identity can
green a context. A failure/error from any writer at any time keeps that context
red, including an old failed attempt followed by success. Skipped/neutral checks,
unpinned success, missing contexts, malformed responses and incomplete listings
never green. A target that moves during observation is refused.

A status linking a Woodpecker pipeline also requires a bounded source read of
its pipeline API: parent must be zero, event must be push or pull_request, and
the successful pipeline must name the exact judged commit and pipeline number.
Restarts and manual runs cannot green. No GitHub or CI credentials are forwarded
to that URL. If the provider API is private or unreadable, reuse is undecided;
run the independent local lane. Other providers still rely on the credential
isolation above, since a status alone does not expose arbitrary CI environment
overrides.

Coverage is deliberately conservative. Include the pipeline YAML, every tracked
file its executable tokens reference (including dot-directories), literal Python
`-m` modules/package initializers, and static imports recursively referenced by
those files. Comments and Python identifiers do not introduce dependencies;
Python AST imports and path-bearing string literals do. Ordinary literal
`getattr`/`hasattr` attribute reads are allowed, while dynamic names and
import-performing attribute lookups are refused. All tracked `pyproject.toml`,
`setup.cfg`, `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `tox.ini`
and `conftest.py` files anywhere in the repository must be included, covering implicit
reads without guessing working directories. Every listed file must exist as a
regular blob with the same mode and object ID at the head and packet. Symlinks,
truncated trees, mismatched content/object IDs, constructed shell paths and command
substitution are refused. Dynamic pipelines or excessive coverage can use the
independent local lane; an undecided reuse is never a pass.

`ci.config` applies the same complete source pinning to `prove:ci`, including
its pipeline YAML and executable closure. A source citation with no `ci.config`
or `packetSha` cannot green. `--ci --sha` remains a diagnostic observation and
cannot back a source citation. A deployed gate still requires its wrapper
witness and checked dependency resolution; forge/reuse rows cannot replace it.

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
