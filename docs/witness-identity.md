# Compiled execution witness identity

A compiled graph records the plugin installation it was audited against. The
`PLUGIN` constant and `meta.plugin` name its absolute root, version, SHA-256 of
`attest.py` and `py.sh`, and an aggregate digest of the Python and shell helpers
under `scripts/`. The aggregate detects helper drift that an entry-point hash
alone would miss. Run summaries retain the same `plugin` and `compilerVersion`.

Prove and independently cited gate prompts use the absolute pinned launcher,
with `attest.py --plugin-pin '<identity JSON>'`. The wrapper verifies that
installation before running or collecting a gate. The pin is inherited by
its detached worker, so updating the files between detachment and child startup
also refuses execution. Do not resolve a newer installation through
`installed_plugins.json` inside a running campaign.

`attest.py --self-check` prints the loaded witness identity. Add `--plugin-pin`
to compare it with a compiled identity before using the witness. Every new
attest row carries its loaded `producer`, including hook and forge rows. A
compiled citation whose row lacks that producer or names another producer is
`MISMATCH`; record-run retains the discrepancy and cannot file the declared
verification as a clean completion. Historical summaries without a plugin pin
retain their historical recording behavior.

Before launch, `compile-graph.py --check --check-script <generated.js>` checks
the installation named inside the exact script. A missing pin, unavailable
installation or content mismatch refuses launch. Structured resume checks the
prior summary's pin before compiling its next launch, and refuses a helper
from another installation. Run resume.py from the pinned root to continue it. An old prove/cited run
without a pin must be reviewed and recompiled for a fresh launch; it cannot be
silently resumed under the newest witness.

Keep the old installation available and unchanged until its campaign ends.
An update that installs another version alongside it is safe: the graph still
names the old path. To change witness code deliberately, review the change,
recompile and audit the graph, then launch with its new pin. This is provenance
for honest execution and upgrade drift, not a sandbox against an actor who can
rewrite both source pins and the attest log.
