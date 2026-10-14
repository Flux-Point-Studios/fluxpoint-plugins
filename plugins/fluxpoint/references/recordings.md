# Provider recordings and test doubles

A provider double can agree with the fix because both were written from the
same mistaken belief. Capture the provider's actual response, preserve its
provenance, and check the fixture against it before using a green test as evidence.

Declare captures in `.fluxpoint-recordings.json`:

```json
{
  "providerModules": ["koios", "httpx", "requests", "fetch"],
  "recordings": [
    {
      "id": "koios-address-utxos",
      "provider": "Koios",
      "endpoint": "/address_utxos",
      "capturedAt": "2026-10-12T18:01:49Z",
      "body": "tests/captures/koios-address-utxos.json",
      "doubles": ["tests/fixtures/address-utxos.json"]
    }
  ]
}
```

`body` names a JSON file containing the captured response body; each `doubles`
entry names a separate JSON fixture file. Paths are relative to the repository.
Missing files, traversal, symlinks outside the repository, duplicate JSON keys,
invalid JSON and incomplete provenance fail the gate. IDs must be unique;
`capturedAt` must include its timezone. Keep the capture and fixture separate so
an accidental self-comparison cannot pass. Commit captures and review changes to
them as evidence. Remove credentials and personal data before committing;
preserve response keys and JSON types when sanitizing.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" seam-guard.py --check
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" seam-guard.py --conform \
  tests/fixtures/address-utxos.json tests/captures/koios-address-utxos.json
```

Every declared double is compared recursively. A missing or invented key, a
changed JSON type (including `null` against a string or a boolean against a
number), or an array length change fails and names the JSON path. Arrays compare
positionally, including every row; use separate captures for different response
cardinalities. Numbers are one JSON type, so integer and fractional values have
the same shape. Scalar values may differ. Shape conformance does **not** prove a
transaction id or hash correct: compare derived values with the captured value,
independently of the computation under test. Provenance fields document the
capture; this local check cannot prove that its author contacted production.

The scaffolded `harness.sh --full` checks the manifest even without a seam
baseline or test files. Declaring the manifest also makes a missing
`seam-guard.py` fail instead of silently skipping this gate.

## Counting doubles

`--scan` and `--baseline` include JS/TS `*.test.*` / `*.spec.*` and Python
`test_*.py` / `*_test.py`, including untracked files. The scanner recognizes:

- `vi.mock`, `vi.doMock`, `jest.mock` and `jest.doMock`; a partial module mock
  that imports the real implementation remains exempt.
- Python `mock.patch`, imported `patch` (including import aliases), `patch.object`,
  `mocker.patch` and `monkeypatch.setattr`.
- Fetch stubs through `stubGlobal`, `spyOn` and global fetch assignment; `respx`
  routes/mock setups (target `httpx`); `responses` routes/mock setups (target
  `requests`).

Python source is parsed as Python, so comments and string literals containing
example patches do not count. A Python test that cannot be parsed fails with its
path and line. Python dotted targets resolving to a local module under the repo
or `src/` count as first party; other module targets count as third party.
Dynamic/custom mock wrappers may need reviewer inspection; the scanner is not a
proof that every possible double was found.

Targets listed in `providerModules` (including their dotted/subpath members)
count in `provider`, taking precedence over first/third party. To add a provider
double, name the recording its fixture was checked against in the test:

```python
# fluxpoint-recording: koios-address-utxos
with mock.patch("koios.Client.address_utxos", return_value=fixture):
    ...
```

Use `// fluxpoint-recording: koios-address-utxos` in JS/TS. The ID must exactly
match a declared recording. Review that the named fixture is the one the test
uses. All manifest pairs must conform before the marker can justify growth.

The baseline records both `provider` and `provider_unrecorded`: all provider
doubles and the subset without a recording marker. The latter may never rise;
this prevents an existing recorded double from buying room for a new unrecorded
one. Recorded provider doubles may grow. First/third-party increases still fail.
An older baseline treats missing provider counters as zero. Review and re-arm
with `--baseline` when adopting this scanner in an existing Python/HTTP suite;
the guard preserves other sections of the shared proof baseline. `--tighten`
continues to lower existing counters only after a clean, green full harness.
