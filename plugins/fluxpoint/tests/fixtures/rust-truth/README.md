# rust-truth

What two real Rust front ends say about a source file, printed in a form
`tests/rust-hatches-live.py` can compare with `scripts/rust_hatches.py`.

- `rust-truth tokens FILE...` lexes each file with `proc-macro2`, Rust's own
  tokenizer outside the compiler, and counts eight token patterns over the
  whole file.
- `rust-truth extents FILE...` parses each file with `syn` and prints where
  every item, block, field, variant, arm and parameter under a test-only
  attribute ends, as `line:column:kind`.

Neither mode shares code with the scanner. The cfg predicate and the list
of test attributes are written out again here on purpose, so a mistake in
one has to be made twice to go unnoticed.

It is a fixture, built on demand into a temporary directory. It is not
part of any consuming repository's build and the harness does not need a
Rust toolchain.
