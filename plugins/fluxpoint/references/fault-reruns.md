# Recorded crash reruns

A failed attested gate remains red at its commit and launch nonce. Running it
again until a green appears does not settle the failed attempt: `prove:` cites
that green as `SUPERSEDED-RED`. A resume retains its original launch stamp and
nonce, so it retains the failed attempts too.

A repository can declare a narrow exception for a measured interpreter crash:

```json
{
  "version": 1,
  "gates": {"offline": "python -m pytest tests/offline"},
  "reference": {
    "image": "registry.example/ci@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "run": "docker run --rm -v {tree}:/w -w /w {image} {command}"
  },
  "faults": [{
    "id": "native311-unraisable-crash",
    "gates": ["offline"],
    "host": "3.11.0 Windows",
    "exit": [3221225477],
    "logMust": ["Windows fatal exception:", "collect_unraisable"],
    "logMustNot": []
  }]
}
```

Replace the example digest and signature with a real pinned runtime and measured
crash. The manifest must be tracked. Signatures have exact fields, gate names,
interpreter versions, platforms and nonzero crash exit codes. Unknown fields,
mutable image tags, invalid regexes and timeout/cancellation codes are refused.
The reference constructor is literal argv, with exactly one `{tree}` operand and
whole `{image}` and `{command}` arguments. `{command}` expands to `sh -c` plus
the declared command as one literal argument inside the reference runtime; the
image must provide `sh`. No host shell evaluates substituted command text.
The constructor itself is trusted repository policy, pinned by the launch.

`attest.py --stamp` retains the original stamp and SHA-256 of the manifest bytes.
The wrapper records that hash, the interpreter it probes and the platform before
running the gate. A direct Python gate uses that interpreter on its gate PATH/cwd;
deployed lanes use their declared interpreter. A shell script, pytest launcher
or other wrapper can choose another interpreter, so its host is unknown and its
failures cannot be classified. Declare a direct Python invocation or a deployed
lane to pin that interpreter. Classification is deterministic and uses
bounded regex workers. A stored row's `fault` field contains the signature and
matched lines, not an agent ruling.

Plugin-fixed exclusions always apply: the retained log must include a
faulthandler fatal marker and the current-thread stack. An assertion failure,
pytest FAILED/ERROR line (except an xdist crashed-worker diagnostic), or any
current-thread frame in the gate repository leaves the gate red. No stack on a
hang, a cancellation, a timeout, or a signature added after launch can qualify.

Run and collect a declared gate through the wrapper:

```bash
bash "$ROOT/scripts/py.sh" attest.py --run offline --root "$PROJECT" --detach --nonce "$NONCE"
bash "$ROOT/scripts/py.sh" attest.py --await <token> --root "$PROJECT"
```

If its wrapper row carries a qualifying `fault`, settle that one attempt with:

```bash
bash "$ROOT/scripts/py.sh" attest.py --rerun-fault <attestId> --root "$PROJECT" --nonce "$NONCE" --detach
bash "$ROOT/scripts/py.sh" attest.py --await <pair-token> --root "$PROJECT"
```

The gate tree must still have the same HEAD and have no staged changes, working
copy changes or untracked files. Ignore `.claude/` as repository state so the
runner's own evidence is not an untracked source change. The runner atomically
consumes the crash once, runs the native gate once, then runs the same declared
gate in the reference against a clean Git archive. It never overlaps those two
legs. `--await` and `--cancel` use the shared process-tree supervisor; cancellation
cannot mint a green pair or reopen the consumed attempt.

Both legs must pass, including deployed dependency pin probes inside each
runtime. A second native crash or any reference failure leaves the pair red,
with no further pair. Cite the resulting `faultPair` attestId, not a rerun leg.
The original log, native log and reference log remain in `attest-bg`, with their
tokens and raw SHA-256 hashes in the pair. Verification rereads all three logs,
checks their wrapper completion records and launch/manifest/commit bindings,
reclassifies the original crash and checks the reference digest and deployed
resolution. All four attestation ids are reserved to that one verifying node.
A later ordinary green can pass only after every red in scope has such a valid
green pair. The run artifact and Evidence row retain the verified crash count.
Before filtering the writable log by gate, nonce, tree or launch time, the reader
cross-checks known red wrapper completions in `attest-bg`. A missing row or altered
identity cannot make that retained failure disappear. These are two retained
local views, not a cryptographic boundary against rewriting all local evidence.
An interrupted local loser can yield to CI only after its cancellation completion
and log are authenticated and the covering forge green is re-read successfully;
a completed assertion or crash stays red.

This is a gate-execution exception, not a node retry setting. Commands run outside
attestation remain unwitnessed. Compiler warnings identify schema-only exit
gates whose prompt repeats a declared gate command verbatim; use `prove:<gate>`
to enforce this history.
