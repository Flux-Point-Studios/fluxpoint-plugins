# Rust and FRAME

Rust reaches four parts of the harness: the escape-hatch ratchet, the attack
taxonomy, the counterexample ledger and the scaffolded `--full`. This guide
covers production Rust (services, off-chain code, nodes) and FRAME pallets
and runtimes on the Polkadot SDK, which is what a Cardano partner chain is
built from. Kani, Verus, Prusti and cargo-mutants were already supported
and are described in the [plugin README](../README.md#verified-work).

## Arming

```
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" proof-guard.py --scan
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" proof-guard.py --baseline
```

Read the scan before recording it. A codebase with two hundred existing
`unwrap`s baselines at two hundred and ratchets from there. The count that
matters is the next one.

A repository that armed before these categories existed is not turned red
by them. `--check` names each one as new, with its count, and leaves it
unratcheted until the next `--baseline`.

## What is counted

`cargo check` exits 0 on `todo!()` exactly as on the function it stands in
for, and `cargo clippy -- -D warnings` exits 0 on the lint an `#[allow]`
just silenced. Each category is a construct that overrules a checker which
then reports success.

| Category | Counts | The checker it overrules |
|---|---|---|
| `rust.unsafe` | the `unsafe` keyword: blocks, impls, fns, `#[unsafe(...)]` | the borrow checker |
| `rust.todo` | `todo!`, `unimplemented!` | the type checker: a hole that type-checks at any type |
| `rust.panic` | `.unwrap()`, `.unwrap_err()`, `.expect("...")`, `panic!`, `unreachable!` | the `None`, the `Err` or the branch the types said to handle |
| `rust.lint_allow` | each lint an `allow(...)` attribute names, and each lint a Cargo manifest's `[lints]` table sets to `"allow"` | rustc and clippy lints |
| `rust.let_underscore` | `let _ = expr;` and `_ = expr;` | `#[must_use]`, usually an unhandled `Result` |
| `flags.lints_off` | `-A warnings`, `-A <tool>::<lint>` and `--cap-lints allow` or `warn` in a tracked script, workflow or Cargo config | the lint gate as a whole |

`.unwrap()`, `.expect()`, `panic!` and `unreachable!` share one category on
purpose. They are the same act in different spellings, and an `unwrap`
rewritten as an `expect` with a reason should not read as a rise. Lints are
counted by name, not by attribute, so adding a lint to an existing
`#[allow(...)]` list is a rise.

FRAME states its safety obligations as attributes, and its opt-outs the
same way.

| Category | Counts | What FRAME stops requiring |
|---|---|---|
| `frame.unbenchmarked_weight` | a `#[pallet::weight(...)]` or `#[pallet::weight_of_authorize(...)]` that names no `WeightInfo`; `type WeightInfo = ();` in a Config impl; `#[pallet::whitelist_storage]` | a measured weight |
| `frame.unbounded_storage` | `#[pallet::without_storage_info]`, `#[pallet::unbounded]` | `MaxEncodedLen`, which bounds storage and proof size |
| `frame.dev_mode` | `#[frame_support::pallet(dev_mode)]` | weights, call indices, storage bounds and explicit hashers at once |
| `frame.feeless` | `Pays::No`, `#[pallet::feeless_if(...)]` | the transaction fee |
| `frame.unsigned` | `#[pallet::validate_unsigned]`, `#[pallet::authorize(...)]`, `ensure_none(origin)` | a signature, a nonce and a fee |

A weight counts on a syntactic fact, never on a judgment of the number: the
attribute names no `WeightInfo`, so the value was written by hand. That
covers `Weight::from_parts(10_000, 0)` and it covers a careful sum over
`T::DbWeight`. The first is a guess and the second is an argument, and a
token scan cannot tell them apart. The baseline absorbs the hand-written
weights a pallet already has. The next one is a diff someone reads.

## What is test code

Only production code counts. A test that unwraps is asserting, so a scan
that counted test code would red on every new test and the baseline would
be re-recorded without being read.

Test code is recognised by the evidence the compiler uses, never by a file
name:

- an item under a `cfg` that requires `test`, `kani`, `fuzzing`, `doctest`,
  or FRAME's `runtime-benchmarks` or `try-runtime` feature. The predicate
  is evaluated, so `all(test, feature = "std")` is test code and
  `any(test, feature = "std")` is not;
- a function under `#[test]` or a sibling attribute (`#[tokio::test]`,
  `#[rstest]`, `#[test_case]`, `#[quickcheck]`, `#[bench]`), including the
  functions a `proptest!` block declares, and a `#[kani::proof]` harness;
- a file every declaration of which is such a module: `#[cfg(test)] mod
  tests;` makes `tests.rs` test code, and whatever `tests.rs` declares in
  turn. `#[path = "..."]` is followed, and so is `include!("file.rs")`;
- a file that opens with `#![cfg(test)]`;
- Cargo's own `tests/` and `benches/` targets beside a tracked `Cargo.toml`,
  and the `build.rs` beside it. A build script runs at build time, and when
  it panics the build fails, which is the opposite of a hatch that passes
  in silence.

A module merely named `tests` and declared without the cfg ships in
production and is counted. A file ships when anything that ships compiles
it, so two more rules keep code in the count:

- One production `mod` or `include!` line keeps a file counted wherever it
  sits. A module moved under `tests/` and reached from `src/lib.rs` with
  `#[path]` is still production.
- A crate root is never test code by declaration. Cargo builds
  `src/lib.rs`, `src/main.rs`, a target under `src/bin/` or `examples/`,
  and the `path` a manifest gives a `[lib]`, `[[bin]]` or `[[example]]`,
  each on its own. A test that pulls the binary in with
  `#[path = "../src/main.rs"]` declares it and does not take the program
  out of the count.

A declaration the scan cannot follow falls on the counted side: the file
it names stays production, which costs a false rise and hides nothing.

## What is deliberately not counted

A ratchet that cries wolf is re-baselined blind, so three things are left
out and one is handed to a tool with better information.

- `#[expect(...)]` warns once the lint it names stops firing, which the
  scaffolded `-D warnings` turns into a failure. It cannot rot in place the
  way `#[allow]` can. Write `#[expect(lint, reason = "...")]` where a
  suppression is justified.
- `.expect(` counts only with a message: a string literal, `format!` or
  `concat!`. A parser's own `self.expect(Token::Comma)?` is another method.
- `let _ = f()?;` propagated the error and dropped only the `Ok` payload.
- Numeric `as` casts, indexing and unchecked arithmetic lose something for
  some types and nothing for others, and a token scan has no types. Clippy
  does. Turn its lints on in the manifest and the `allow` that silences
  them is what this ratchet counts:

```toml
[workspace.lints.clippy]
cast_possible_truncation = "deny"
cast_sign_loss = "deny"
cast_possible_wrap = "deny"
arithmetic_side_effects = "deny"
indexing_slicing = "deny"
```

Tracked `vendor/` trees are counted like any tracked source. A marker file
that took a crate out of the count would be a file anyone could add. The
baseline absorbs vendored code, and a dependency bump that moves a count is
re-recorded in the diff that made it.

Known misses: code a macro generates (a macro definition is counted once,
where it is written), an `include!` whose path is computed, a target
`path` a manifest spreads over several lines of an inline table, a crate
root in a tree that tracks no `Cargo.toml` (nothing there tells it from a
module, so one that only test code declares is left out),
`Option::unwrap(x)` written as a path, a message passed to `.expect` in a
variable, `.ok();` used to discard a result, `if cfg!(test)` branches, a
`WeightInfo` whose own implementation was written by hand, and the opt-outs
of the benchmarks themselves (`#[benchmark(skip_meta)]`, `pov_mode =
Ignored`), which sit in code the scan leaves out as benchmark-only.
`/fluxpoint:proof-audit` is the pass for what a count cannot see.

## Attack classes

`templates/attack-taxonomy.json` carries two taxonomies decided from Rust
tests. Each gates only a repository that carries its surface.

| Taxonomy | Active when | Classes |
|---|---|---|
| `rust` | a Rust test function is tracked | panic on hostile input, unbounded allocation, amount overflow, decode/encode divergence, swallowed error, blind signing, secret in output, request amplification, blocked executor, shared resource exhaustion |
| `frame` | a tracked Rust file declares a pallet or assembles a runtime | weight underestimate, unbounded iteration, reachable panic, unsafe math, storage exhaustion, missing origin check, unsigned replay, unchecked inherent, discarded result, insecure randomness, account reference counts, runtime upgrade state |

A class is specified by a Rust test function whose name carries the class
id: a function under `#[test]` or a sibling attribute, including the ones a
`proptest!` block declares.

```rust
proptest! {
    #[test]
    fn attack_panic_on_hostile_input_receipt_decoder(bytes in proptest::collection::vec(any::<u8>(), 0..4096)) {
        let _decoded: Result<Receipt, DecodeError> = Receipt::decode(&bytes);
    }
}
```

A helper no test build collects, a comment and a string do not count. A
match in a file that uses proptest, quickcheck, bolero, arbtest or Kani is
a property; any other match is noted as an example over the cases it lists.
A class that cannot apply is waived in the manifest with a reason of at
least twenty characters, scoped to its own taxonomy.

The `frame` classes follow the vulnerability classes Parity publishes as
the [Polkadot Top Ten](https://security.parity.io/top), with two that a
partner chain adds: data a block author supplies as an inherent, and state
across a runtime upgrade.

A crate that builds Cardano transactions owes the builder classes too. The
manifest is the repository's own declaration, so copy the classes from the
`typescript` taxonomy into the `rust` taxonomy's `classes` list. They are
then held to Rust tests.

A test named for a class shows the class was declared. It does not show
that the test runs or that it bites. The project harness has to execute it,
and review has to read its generators and assertions.

## Counterexamples

The scaffolded harness captures `cargo test` and hands it to the ledger. A
proptest failure carries the shrunk input that broke the property:

```
thread 'tests::fee_never_exceeds_amount' (19632) panicked at src/lib.rs:31:5:
Test failed: assertion failed: fee(amount, bps) <= amount at src/lib.rs:34.
minimal failing input: amount = 180, bps = 9050
	successes: 13
```

That becomes one row in `.fluxpoint-cex.jsonl` with the input folded onto
one line and its string literals left exactly as printed. A plain assertion
failure carries no generated input and is not recorded. cargo's exit code
stays the gate. The recorder never gets a vote.

The gate pins `PROPTEST_RNG_SEED` to `FPL_PROPTEST_SEED` (default 1), for
the reason it pins Aiken's: proptest draws a new seed per run otherwise, so
the same bug shrinks to a different value each time and files a new row per
run. The exploration that gives up is bought back off-session:

```json
{"version": 1, "tool": "proptest", "seeds": 25, "maxSuccess": 1000}
```

in `.fluxpoint-fuzz.json`, then `cex.py --sweep`. Each sweep sets
`PROPTEST_RNG_SEED` to the next seeds in turn and `PROPTEST_CASES` to
`maxSuccess`, and starts where the last one stopped.

A pin is a tracked `#[test]` function carrying the recorded literals in
order, named for the counterexample id:

```rust
#[test]
fn cex_ad48137045a3() {
    assert!(fee(180, 9050) <= 180);
}
```

`#[ignore]`, with or without a reason, and `#[should_panic]` are refused,
as is a body that asserts nothing. Commit `proptest-regressions/` as well:
proptest replays the seeds saved there before any new case. A saved seed
replays a path through the generator and stops meaning the same input once
the strategy changes, which is why the pin is a test.

The parser reads the message proptest has printed since 1.2 under the panic
header rustc has printed since 1.73. A `minimal failing input:` line in any
other frame files INGEST-FAILED and records nothing. A `TestRunner` driven
by hand and unwrapped prints its error with `Debug`, which is a different
shape, so it is not read.
The fixtures are captures of real runs under cargo 1.97.0 and proptest
1.12.0.

## The scaffolded harness

With a `Cargo.toml` at the repository root, `--full` runs:

```
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --quiet -- -D warnings
PROPTEST_RNG_SEED=1 cargo test --workspace --quiet
cargo kani            # when a tracked crate declares #[kani::proof]
```

`--workspace` matters at a manifest that is both a package and a workspace.
There cargo lints and tests the root package alone and exits 0 over members
it never built. Kani harnesses are looked for in every tracked crate.

The detection is a floor. A workspace in a subdirectory, which is how a
partner chain usually sits beside its tools, needs the block pointed at it
when `/fluxpoint:init` tailors the harness:

```sh
( cd partnerchain && cargo clippy --workspace --all-targets --quiet -- -D warnings )
```

`mutation-guard.py` wraps cargo-mutants; see the
[plugin README](../README.md#verified-work).

## How the scanner was checked

The scanner reads tokens and never runs rustc, so it was compared with
tools that do parse Rust. Its lexer was run beside `proc-macro2` over
13,292 Rust files (tokio, serde, syn, rustls, the FRAME crates, pallas and
their dependencies, plus a partner chain workspace) and the per-file
counts of eight token patterns agreed on every file. Its judgment of where
a test-only item ends was run beside `syn` over the same corpus and agreed
at the exact character on all 30,809 items.

`tests/rust-hatches-live.py` repeats both comparisons. It needs a Rust
toolchain and a corpus, so it runs by hand and not in the harness:

```
bash plugins/fluxpoint/scripts/py.sh ../tests/rust-hatches-live.py ~/.cargo/registry/src
```

## Not covered

ink!, Solana, CosmWasm and other Rust contract platforms have no
categories or taxonomy here.

Neither do the newer Cardano contract languages, as of November 2026.
Nash, whose compiler is written in Rust and which its README calls the
successor to Aiken, and Pebble are each a language of their own that
compiles to UPLC. Neither is Rust, and no production-ready way to write a
validator in Rust itself was found. Both are pre-1.0 and neither emits a
CIP-57 blueprint today, so the blueprint gate has nothing to hold an
off-chain builder to. Until an adapter is verified against each compiler,
`spec-guard.py` names tracked `.nash` and `.pebble` files as NOT
COVERED on every run, so a repository that moves a validator to one of
them cannot read a green as the coverage it had under Aiken.
