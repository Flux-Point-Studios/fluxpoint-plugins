# Captured pinned Aiken costs

Captured 2026-10-14 from the real `aiken v1.1.23+8949565` Linux x86_64 musl
release. Download SHA256:
`b98c4ccbada5e35f15ba417a52872aeaf0c65998aed77351769ce5fd2506b570`, matching the
published `.sha256` asset.

Run `aiken check --seed 1 > check.json` with stdout redirected (TTY output is
human text). Two consecutive runs of this exact tree produced identical bytes:
SHA256 `09ca012d585e3c7126f3612027b1fd558a76f457be804cf13263f407411d677a`.
`cost.unit_twice` reports `execution_units: {mem: 200, cpu: 16100}`.
`cost.property_twice` reports `iterations: 100, counterexample: null` and no
single execution cost; the extractor explicitly excludes this property case.

The compiler, Plutus version, stdlib and fuzz dependencies are pinned by the
included `aiken.toml` and `aiken.lock`. The generator defaulted to stdlib v4,
which is unsuitable for this old compiler; this probe uses stdlib/fuzz v2.2.0.
The test expression is a tiny deterministic probe; its cost is not a claimed
transaction or production-validator benchmark.
