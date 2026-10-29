# Native Compact verification

Compact has four source hatch categories: `compact.todo`,
`compact.assert_constant`, `compact.no_assert_circuit` and
`compact.unjustified_disclose`. The last accepts a real nearby
`// disclose: <reason>` comment of at least twenty characters, or a
`.fluxpoint-compact-disclosures.json` map from `file:line` to such a reason.
Quoted prose does not justify a disclosure. Exported circuit signatures and
assertion expressions also get `compact:<file>:<circuit>` statement identities;
literal values, including meaningful whitespace inside strings, bind the hash.

These scans are floors. Helper calls, arbitrary control flow and a disclosure
whose justification is wrong still need review. Native verification handles the
compiled relation and the actual proof boundary.

## Supported native profile

The initial tested profile is compactc **0.31.1**, its `ledger-8.0.2` compiler
target, compact-runtime **0.16.0**, ledger-v8 **8.1.3** and zkir-v2 **2.1.1**.
The native verifier must enable the real ledger's `proof-verifying` feature and
exclude `mock-verify`. The JavaScript ledger WASM build cannot supply that native
proof check. Compiler 0.34+ / ledger-9 drift is refused by this profile.

`.fluxpoint-compact.json` declares the compiler, native verifier and inspector as
`{argv, files}` tools. Executables and launcher file operands use absolute paths;
`files` maps each to its reviewed SHA-256. The shipped inspector also takes
`--modules <node_modules>`; every file in that tree must be pinned, and symlinks
cannot escape it. Keep a stable mounted tool-cache path across CI workers. The
manifest also pins each absolute proving-parameter path, exact compiler/runtime
versions, and a nonempty `projects` list. Each project has:

- `source`: a repository-local `.compact` file;
- `circuits`: the exact names the compiler reports as proof circuits;
- `driver`: argv with `{artifacts}` and optional `{root}` placeholders;
- `cases`: control id → expected boolean;
- `timeoutSeconds`: a bound from 1 to 7,200 seconds.

The source-controlled project driver constructs the initial state from freshly
generated code, generates synthetic proof requests and writes its native registry
under `{artifacts}`. It returns one JSON object with exactly `version: 1`,
`complete: true`, `registry`, `controls` and `witnesses`. Controls are
`{id, circuit, request}`; a domain-invalid preimage rejected during actual circuit
evaluation may instead be `{id, circuit, proverRefused: true}`. `witnesses` maps
every compiler-declared witness name to the base64 marker values used by this
test. These are ephemeral inputs, never printed in a result or baseline.

The host runs every request through the pinned native verifier and its separately
pinned inspector. Every compiled circuit needs both a genuine `verified` result
and a corrupted `invalid-proof` result. Configuration failures, unknown circuits,
empty/duplicate controls, missing circuits and incomplete runs refuse. The native
registry must use the exact freshly compiled verifier key and one pinned initial
state template per project. The driver cannot replace compiler artifacts: their
hashes are frozen before it starts and rechecked around native execution.

The inspector decodes the *same verified transaction* to measure proof bytes and
declared transcript gas, and reads the exact compiled JSON IR through zkir to get
`k`. The adapter independently measures verifier-key bytes. Requests for an SRS
level missing from the pinned parameter set refuse. Measurements are maxima over
the declared positive cases; they cover those states and inputs, not every future
ledger state. Add representative state scenarios and the Compact attack taxonomy
properties before claiming a deployment-wide gas budget.

Marker checks reject raw or field-packed sixteen-byte windows of declared witness
bytes found in verified transactions. This is an empirical privacy floor. It
does not establish noninterference, detect every encoding, or prove that a
driver supplied every sensitive value. Short or low-entropy witnesses and the
meaning of a permitted disclosure remain part of the source and property audit.
The driver, inspector and native verifier are reviewed programs, not agent prose.
The choice of meaningful inputs, constructor state, witness marker completeness
and values, and each `proverRefused: true` semantic-bad-input claim are trusted
driver assertions. The shipped inspector independently verifies the same
serialized transaction bytes; it does not independently establish application
policy, the correctness of the driver's refusal claim, or confidentiality.
Content pins bind program identity, not implementation correctness.
Driver pins cover its executable and direct file arguments. Arbitrary driver
imports are not automatically discovered: review them and include their files
in a tool's `files` map. The inspector's `--modules` tree is pinned in full.

## Arming and checking

Use the executable fixture below as a complete manifest/driver example, then adapt
the project entries to the application's circuits and hostile preimages:

```sh
python plugins/fluxpoint/scripts/compact-guard.py --baseline
python plugins/fluxpoint/scripts/compact-guard.py --check --require-armed
```

`--baseline` writes `.fluxpoint-compact-baseline.json`. Commit it with the
manifest. An existing baseline can change only with
`--baseline --accept --reason '<at least twenty characters>'`; the record includes
the reason and prior digest. `--check` compiles and verifies again. It rejects a
changed configuration, driver, generated code/key/state pin or normalized IR,
any lost measured circuit/dimension, and any rise in `k`, proof/key bytes or gas.
Only JSON whitespace and key ordering are normalized in the relation; no unknown
IR field is dropped. This is exact compiler-relation identity, not a theorem that
different IRs are semantically equivalent. `--tighten` first runs the real full
harness on a clean HEAD and permits only lower ceilings with unchanged pins.

Newly scaffolded harnesses run `compact-guard.py --check`, adding
`--require-armed` when a native manifest or baseline exists; existing copied
harnesses must add that call. Tracked Compact source containing real `ledger` or
`witness` tokens refuses without a manifest, and every such source must appear
as a manifest project. Comments and strings do not arm the adapter; pure helper
modules remain dormant. A project whose stateful/witness modules are imported
libraries must expose a native-test project for each module before this profile
can arm. Explicit native proof commands must use `--require-armed`, including
projects without state or witnesses. Missing manifests and empty proof/control
sets then refuse. The manifest, native baseline and disclosure policy join the
trust-base warning paths. Initialization must also arm source/statement ratchets.
The scaffold also detects state/witness tokens when the plugin is missing, so
that missing installation refuses unless the existing explicit
`FPL_ALLOW_MISSING_GATES=1` opt-out is chosen.
Legacy directories outside Git have no tracked inputs and remain dormant;
inventory errors inside a repository still refuse.

## Native mutation score

Declare `{"version":1,"tool":"compact","failWhenStale":true}` in
`.fluxpoint-mutation.json`, then use the existing `mutation-guard.py --measure` /
`--check` interface. `args: ["--config", "other-compact.json"]` selects another
manifest. Imported mutation reports are refused: native controls run live.

Before scoring, each circuit also needs a separate semantic bad-input case that
the baseline prover refuses. Dropping an assertion can then construct a hostile
proof: if the real native verifier accepts it where the case expects false, the
mutant is killed. Corrupted-proof checks alone cannot establish mutation coverage.
Mutant compiler/artifact pins are not compared with the ordinary baseline to
manufacture kills.

The adapter copies tracked files to isolated snapshots and generates assert
deletions, comparison flips, disclosure removal and disclosure around witness
calls. Strict `<`/`>` flips require surrounding whitespace, so generic type
brackets are not mistaken for comparisons. Ordinary frontend rejections are
unviable and unscored; compiler crashes, missing parameter levels, backend failures,
timeouts and incomplete proof controls abort measurement. Native semantic failures
count as killed; completed unnoticed mutants survive. Source input and HEAD checks
and the existing exact mutation-score/survivor/surface ratchets remain active.
An exact match of generated relation, code, keys, constructor state and frozen
driver identity is reported separately as `equivalent` and excluded. The live
fixture's disclosure annotation around a private witness is erased by this pinned
compiler; it is not called a native kill. A run with no remaining scored mutants
refuses to publish a green measurement.

## Real native fixture

The small source and driver in `tests/fixtures/compact-native/` use synthetic
witnesses. No binary, key or proving parameter is committed. Prepare external
tools using the pinned public Orynq fixture revision:

```sh
git clone https://github.com/Flux-Point-Studios/orynq-sdk native-fixture
git -C native-fixture checkout d1e1548682d33a430c0f42ac53f88f01a652fefe
bash native-fixture/tools/compactc/install.sh /path/to/tools/compactc /path/to/tools/params
npm install --prefix /path/to/tools/js --ignore-scripts --no-audit --no-fund \
  @midnight-ntwrk/compact-runtime@0.16.0 \
  @midnight-ntwrk/ledger-v8@8.1.3 @midnight-ntwrk/zkir-v2@2.1.1
CARGO_TARGET_DIR=/path/to/tools/native-target cargo +1.99.0 test --release --locked \
  --manifest-path native-fixture/packages/midnight-prover/native/Cargo.toml
CARGO_TARGET_DIR=/path/to/tools/native-target cargo +1.99.0 build --release --locked \
  --manifest-path native-fixture/packages/midnight-prover/native/Cargo.toml
python plugins/fluxpoint/tests/compact-live.py \
  --compiler-dir /path/to/tools/compactc --params /path/to/tools/params \
  --node-modules /path/to/tools/js/node_modules \
  --native-verifier /path/to/tools/native-target/release/orynq-midnight-verifier \
  --native-source native-fixture --fetch-params
```

The live smoke currently prepares Linux x86_64 release pins. `--fetch-params`
explicitly downloads missing public SRS files and verifies their pins from native
ledger revision `b5d3e965a18e4cac17fac4f38446fc55c524439a`; omit it for a fully
offline run after preparation. It repeats fresh compilation/native measurements,
then runs all four native source mutants. The recipe checks genuine and corrupted
proofs, a hostile preimage and deterministic artifacts/costs. Assertion deletion
and comparison inversion are semantic kills; removing disclosure is a frontend
refusal. Adding disclosure around the private witness is compiler-identical in
this fixture and is explicitly excluded, rather than counted as a privacy kill.
It provides a reproducible native smoke, not network finality or a model of every
Compact application.
