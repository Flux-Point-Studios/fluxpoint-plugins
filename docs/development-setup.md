# Development setup

Use this guide to develop and test this repository. To install the plugins in
another project, use [the installation instructions](../README.md#install) and
`/fluxpoint:init` or `$fluxpoint-init` instead.

## Prerequisites

- Bash, Git and Python 3.9 or newer (`python3` or `python`). On Windows,
  Bash scripts need Git Bash; native Python suites also run in PowerShell CI.
- Node.js 18 or newer for native Claude hooks, Substrate and generated JavaScript checks. Linux
  CI uses Python 3.11 and Node.js 22; Windows CI uses Python 3.9 and Node.js 22.
- Native Claude Code 2.1.312 or newer on `PATH` (or `FPL_CLAUDE_BINARY`): the full harness calls `claude plugin validate .`
  even when development takes place in Codex. Install it using the
  [official instructions](https://code.claude.com/docs/en/setup).
- `jq` is optional locally; hook scripts have a Python fallback. Linux CI
  installs it so the preferred parser path is exercised too.

There is no root package installation, application server or database to start.
Python helpers use the standard library, and Substrate uses Node built-ins.
An installed agent runtime is needed for interactive plugin use; the local
regression harness uses fixtures for agent calls and external provers.

## Prepare the checkout

```bash
git clone https://github.com/Flux-Point-Studios/fluxpoint-plugins.git
cd fluxpoint-plugins
if [ "$(git rev-parse --is-shallow-repository)" = true ]; then git fetch --unshallow; fi
python3 --version     # use python on Windows if appropriate
node --version
claude --version
```

Use an existing checkout when one is provided by a cloud task. A separate Git
worktree is unnecessary for environment setup. Full history is needed because
the graph replay regression reads the baseline compiler at commit `c848f07`.
Confirm it is available with `git cat-file -e c848f07^{commit}`.

Run from the repository root:

```bash
claude plugin validate .
bash scripts/harness.sh --full
```

Exit 0 is green. The harness validates manifests and schemas, checks syntax,
executes locked requirements, compiles campaign templates, and runs the wired
regression suites for both plugins. Prompt hygiene findings are advisory; a
scanner that cannot run is a failure. Do not run multiple full harnesses in
the same machine at once: some checks share `/tmp/fpl-*` scratch paths.

For a scoped check, run `bash scripts/harness.sh --changed <file>`. This is
useful while editing; use the full harness before submitting a change.
Read [CONTRIBUTING.md](../CONTRIBUTING.md) for spec-first implementation,
versioning and review conventions.

## Optional native Blaster smoke test

The default harness runs the Blaster adapter regression suite without requiring
Lean or Z3. Native solver verification is a separate check. Install Lean
**4.24.0**, Z3 **4.15.2** and a clean Lean-blaster checkout at
`a16614e5e44a693664043552f259235f15f1c884`. Put the Lean and Z3 `bin`
directories on `PATH`, then run:

```bash
cd /path/to/Lean-blaster
lake build Blaster
cd /path/to/fluxpoint-plugins
python3 plugins/fluxpoint/tests/blaster-live.py --backend /path/to/Lean-blaster
```

The check finds a boundary bug, repairs it without weakening the property,
replays and pins the counterexample, rejects a restored bug, and detects an
imported solver axiom. [The Blaster guide](../plugins/fluxpoint/references/blaster.md)
explains consumer project configuration, receipts and trust limitations.

The `blaster-live` job in [.woodpecker.yaml](../.woodpecker.yaml) contains the
Ubuntu 24.04 download URLs, SHA-256 checksums and exact commands. Use an artifact
appropriate for your OS and verify it before extraction; the CI Z3 archive
requires glibc 2.39. Other Lean/Z3 versions are not covered by this smoke test.

## Optional native captured-prefix acceptance

With Claude Code **2.1.312** installed, run:

```bash
python3 plugins/fluxpoint/tests/captured-prefix-live.py --binary /path/to/claude --output /tmp/captured-prefix-acceptance
```

This runs the unmodified compiled script through the actual Workflow tool with
a scripted localhost provider and a dummy key. It checks three distinct native
runs: the initial workers, one changed suffix worker, then no workers on an
unchanged resume. The harmless once-only node must be dispatched exactly once.
Native journals and recursively verified run artifacts remain in the output
directory. The check uses normal permission checks, makes no remote provider
request and establishes no model quality or native cache claim. Other CLI
versions require a reviewed acceptance profile.

## CI and cloud environments

[Woodpecker](../.woodpecker.yaml) runs the full Linux harness, executable-bit
checks and native Blaster smoke independently. [Windows CI](../.github/workflows/windows-specification.yml)
runs selected specification, cleanup, health, replay, resume and repair suites
plus a read-only real machine sample and native hook pipe/forced-timeout tests.
The hook suite installs pinned Claude2.1.312 and uses `--init-only`; it makes no
model calls and needs no model credentials. Locally run
`python plugins/fluxpoint/tests/native-hook-test.py --live` for that integration. A passing local Linux harness does not
establish that the remote jobs or Windows load scenarios passed.

Cloud environment install/start configuration is saved in the environment's
settings, separately from Git. Session helper scripts and tool caches under
`/workspace` are instance-local. This guide records the portable repository
workflow; it does not depend on those temporary paths or embed credentials.
