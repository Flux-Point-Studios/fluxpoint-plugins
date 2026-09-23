# Contributing

This repository gates itself with the same contract the fluxpoint plugin
scaffolds into other repos: `scripts/harness.sh --full`, exit 0 is green.
CI runs it on every push to `main` and every pull request.

## Before you open a pull request

```
bash scripts/harness.sh --full
claude plugin validate .
```

`--full` checks every manifest and contract, syntax-checks every script,
validates the plugin, checks that the Claude Code and Codex manifests agree,
compiles every campaign template and syntax-checks the generated JavaScript
and the shipped workflow scripts, then runs every suite under `plugins/fluxpoint/tests/` and
`plugins/substrate/tests/`. One step is advisory: `prompt-audit.py` scans
the prompt-bearing files for patterns that degrade a frontier model and
prints the trend without failing the run.

`scripts/harness.sh --changed <file>` runs the scoped checks for one file,
which is what the plugin's own per-edit hook calls.

For implementation goals, update the decision and requirement packet before
coding and lock it with `plugins/fluxpoint/scripts/py.sh specification.py --lock`.
The full harness runs its focused checks; never point a packet check back at
the full harness. A packet or proof-baseline revision invalidates the lock.
Read [the spec-first contract](docs/spec-first.md) for the scope and trust boundary.

Contributions are accepted under the repository's Apache-2.0 license, on
the terms in section 5 of [LICENSE](LICENSE).

## Conventions

- **Versioning.** Each plugin carries its version in three places:
  `.claude-plugin/plugin.json`, `plugin.json` and the entry in
  `.claude-plugin/marketplace.json`. Bump all three together; the harness
  fails on skew.
- **Both runtimes.** A hook script must handle both payload shapes described
  in [docs/runtimes.md](docs/runtimes.md), and a new command needs its Codex
  entry point under `skills/<plugin>-<name>/`. The harness checks the second;
  tests cover the first.
- **Executable bits.** Hooks are invoked through `bash`, so a stripped bit
  cannot disarm them, but keep the bits correct for direct runs:
  `git update-index --chmod=+x $(git ls-files '*.sh')`. CI checks this.
- **Line endings.** `.gitattributes` pins LF for everything the plugins
  execute or parse.
- **Tests execute.** A suite that greps for a string proves less than one
  that runs the code path; the existing suites run hook scripts through the
  exact command strings in `hooks.json`, and new cases should do the same.
- **Prose.** The substrate prose-smell gate runs on every Write and Edit of
  a Markdown file. `.prose-smell.json` marks this tree as in-house prose, so
  the contrastive family is advisory here; README and docs are read by
  people outside the team, so keep them plain.

## Smoke test after a release

In a scratch repository: add the marketplace, install the plugin, run
`/fluxpoint:init`, make an edit containing `FIXME`, try to stop, and watch
the gate block. Repeat on the other runtime.
