#!/usr/bin/env python3
"""The scaffolded harness holds a whole Rust workspace, not its root package.

`cargo test` at a manifest that is both a package and a workspace tests the
root package and exits 0 over members it never built. `grep -r kani::proof
src` finds a harness in the root crate and none in `crates/*/src`. Both
read green while part of the tree went unchecked, which is the defect this
harness exists to refuse. A recording stand-in for `cargo` shows what the
template actually asks the toolchain for; nothing here compiles Rust, so
the suite runs where no toolchain is installed.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
TEMPLATE = PLUGIN / 'templates' / 'harness.sh'
FILTER = PLUGIN / 'scripts' / 'hygiene-filter.py'

CARGO = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >>"$CARGO_LOG"
case "$1" in
  fmt) exit 0 ;;
  clippy) exit 0 ;;
  test) [ -n "${CARGO_TEST_OUT:-}" ] && cat "$CARGO_TEST_OUT"; exit "${CARGO_TEST_RC:-0}" ;;
  kani) printf 'Checking harness proofs::holds...\nVERIFICATION:- SUCCESSFUL\n\nComplete - 1 successfully verified harnesses, 0 failures, 1 total.\n'; exit 0 ;;
  *) echo "cargo stand-in: unexpected $*" >&2; exit 2 ;;
esac
'''

# Captured from a real run, 2026-11-11: cargo 1.97.0, proptest 1.12.0,
# `PROPTEST_RNG_SEED=1 cargo test --workspace --quiet fee_never` with
# RUST_BACKTRACE=0, exit 101, in the crate cex-test.sh carries. The crate
# path is edited to /work/ledger and nothing else.
PROPTEST_FAILURE = r'''
running 1 test
tests::fee_never_exceeds_amount --- FAILED

failures:

---- tests::fee_never_exceeds_amount stdout ----
proptest: Saving this and future failures in /work/ledger/proptest-regressions/lib.txt
proptest: If this test was run on a CI system, you may wish to add the following line to your copy of the file. (You may need to create it.)
cc 08067541c85fbf2ba45ce2176edd5cad70d0002a0f968a48580b00ab57d16287

thread 'tests::fee_never_exceeds_amount' (28961) panicked at src/lib.rs:31:5:
Test failed: assertion failed: fee(amount, bps) <= amount at src/lib.rs:34.
minimal failing input: amount = 12, bps = 9750
	successes: 4
	local rejects: 0
	global rejects: 0

note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace


failures:
    tests::fee_never_exceeds_amount

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 4 filtered out; finished in 0.00s

error: test failed, to rerun pass `--lib`
'''


def write(path, text):
    # LF on every platform: bash reads a CRLF shebang as part of the path.
    # (`Path.write_text` only grew `newline` in Python 3.10.)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def bash():
    for candidate in (os.environ.get('SHELL'), shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe', '/bin/bash'):
        if candidate and 'bash' in os.path.basename(candidate).lower() and os.path.exists(candidate):
            return candidate
    return 'bash'


class Scaffold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'
        (self.root / 'scripts').mkdir(parents=True)
        shutil.copy(TEMPLATE, self.root / 'scripts' / 'harness.sh')
        self.bin = Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / 'cargo.log'
        for name, body in (('cargo', CARGO), ('cargo-kani', '#!/usr/bin/env bash\nexit 0\n')):
            write(self.bin / name, body)
            (self.bin / name).chmod(0o755)
        self.git('init', '-q', '-b', 'main')
        self.put('Cargo.toml', '[package]\nname = "root"\nversion = "0.1.0"\nedition = "2021"\n\n[workspace]\nmembers = ["crates/inner"]\n')
        self.put('src/lib.rs', 'pub fn fee(amount: u64, bps: u64) -> u64 { amount * bps / 10_000 }\n')
        self.put('crates/inner/Cargo.toml', '[package]\nname = "inner"\nversion = "0.1.0"\nedition = "2021"\n')
        self.put('crates/inner/src/lib.rs', 'pub fn id(x: u8) -> u8 { x }\n')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.email=t@t', '-c', 'user.name=t', *args], check=True, capture_output=True)

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        write(path, text)
        self.git('add', rel)

    def full(self, **env):
        if self.log.exists():
            self.log.unlink()
        self.git('commit', '-qm', 'state', '--allow-empty')
        environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                           CARGO_LOG=str(self.log), FPL_PLUGIN_ROOT=str(PLUGIN), **env)
        for name in ('CLAUDE_PLUGIN_ROOT', 'PLUGIN_ROOT'):
            environment.pop(name, None)
        result = subprocess.run([bash(), 'scripts/harness.sh', '--full'], cwd=self.root, env=environment,
                                capture_output=True, text=True, errors='replace')
        calls = self.log.read_text(encoding='utf-8').splitlines() if self.log.exists() else []
        return result, calls

    def ledger(self):
        path = self.root / '.fluxpoint-cex.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []

    def test_every_workspace_member_is_linted_and_tested(self):
        result, calls = self.full()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        clippy = [call for call in calls if call.startswith('clippy ') and '--version' not in call]
        self.assertEqual(len(clippy), 1, calls)
        self.assertIn('--workspace', clippy[0])
        self.assertIn('--all-targets', clippy[0])
        self.assertIn('-D warnings', clippy[0])
        tests = [call for call in calls if call.startswith('test')]
        self.assertEqual(len(tests), 1, calls)
        self.assertIn('--workspace', tests[0])

    def test_a_kani_harness_in_a_member_crate_is_found(self):
        _, calls = self.full()
        self.assertFalse(any(call.startswith('kani') for call in calls), 'no harness is declared yet')
        self.put('crates/inner/src/lib.rs', 'pub fn id(x: u8) -> u8 { x }\n#[cfg(kani)]\nmod proofs {\n    #[kani::proof]\n    fn holds() { assert_eq!(super::id(kani::any()), 0); }\n}\n')
        result, calls = self.full()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith('kani') for call in calls), calls)

    def test_prose_about_kani_does_not_demand_the_prover(self):
        self.put('src/lib.rs', '//! Proofs live elsewhere; see `#[kani::proof]` in the docs.\npub fn f() {}\n')
        result, calls = self.full()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(call.startswith('kani') for call in calls), calls)

    def test_a_failing_property_is_recorded_and_the_test_exit_still_decides(self):
        captured = Path(self.tmp.name) / 'test.out'
        captured.write_text(PROPTEST_FAILURE, encoding='utf-8')
        result, _ = self.full(CARGO_TEST_OUT=str(captured), CARGO_TEST_RC='101')
        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assertIn('minimal failing input: amount = 12, bps = 9750', result.stdout + result.stderr)
        rows = self.ledger()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['tool'], 'proptest')
        self.assertEqual(rows[0]['title'], 'tests::fee_never_exceeds_amount')
        self.assertEqual(rows[0]['input'], 'amount = 12, bps = 9750')
        # The gate pins the seed, so the same bug shrinks to the same value.
        self.assertEqual(rows[0]['seed'], 1)

    def test_the_seed_is_the_repositorys_to_choose(self):
        captured = Path(self.tmp.name) / 'test.out'
        captured.write_text(PROPTEST_FAILURE, encoding='utf-8')
        self.full(CARGO_TEST_OUT=str(captured), CARGO_TEST_RC='101', FPL_PROPTEST_SEED='7')
        self.assertEqual(self.ledger()[0]['seed'], 7)

    def test_a_green_test_run_records_nothing(self):
        result, _ = self.full()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ledger(), [])

    def test_the_recorder_never_gets_a_vote(self):
        # Output the parser cannot read must not change a green test run.
        captured = Path(self.tmp.name) / 'test.out'
        captured.write_text('\x00\x01 not a test report \xff\n', encoding='latin-1')
        result, _ = self.full(CARGO_TEST_OUT=str(captured))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class Hygiene(unittest.TestCase):
    """A skipped test is flagged whether or not the attribute says why."""

    def flagged(self, line, path='src/lib.rs'):
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run([sys.executable, str(FILTER), path], input=line + '\n', cwd=empty,
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return bool(result.stdout.strip())

    def test_ignored_tests(self):
        self.assertTrue(self.flagged('    #[ignore]'))
        self.assertTrue(self.flagged('    #[ignore = "slow, run nightly"]'))
        self.assertTrue(self.flagged('#[ignore="flaky"]'))
        self.assertFalse(self.flagged('    #[ignore_malformed]'))
        self.assertFalse(self.flagged('    let ignore = true;'))

    def test_typed_holes(self):
        self.assertTrue(self.flagged('    todo!()'))
        self.assertTrue(self.flagged('    unimplemented!("later")'))
        self.assertFalse(self.flagged('    let pending = unimplemented_features.len();'))


if __name__ == '__main__':
    unittest.main()
