#!/usr/bin/env python3
"""Rust escape hatches are ratcheted in production code and nowhere else.

`cargo check` exits 0 on `todo!()` exactly as on the function it stands in
for, and `clippy -D warnings` exits 0 on the lint an `#[allow]` just
silenced. These cases hold the scanner to the two properties that make a
count worth committing: every construct that discharges an obligation is
seen, and nothing else is. The second half matters as much as the first. A
test that unwraps is asserting, so a scan that counted test code would red
on every new test and be re-baselined blind.
"""
import collections
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def tally(files):
    """{category: count} over a mapping of repo-relative path to source."""
    from rust_hatches import scan_tree
    if isinstance(files, str):
        files = {'src/lib.rs': files}
    return collections.Counter(category for category, _, _, _ in scan_tree(files))


class Lexing(unittest.TestCase):
    """Only code counts: what a comment or a literal says is data."""

    def test_every_hatch_is_seen_in_production_code(self):
        found = tally('''
pub fn f(x: Option<u8>, r: Result<u8, E>) -> u8 {
    let a = x.unwrap();
    let b = r.expect("always ok; qed");
    if a > b { panic!("boom") }
    match a { 0 => unreachable!(), _ => {} }
    unsafe { core::hint::unreachable_unchecked() }
    todo!()
}
fn g() { unimplemented!("later") }
''')
        self.assertEqual(found['rust.panic'], 4)
        self.assertEqual(found['rust.unsafe'], 1)
        self.assertEqual(found['rust.todo'], 2)

    def test_strings_comments_and_literals_are_data(self):
        self.assertEqual(tally(r'''
//! Call `.unwrap()` only in tests; never `todo!()` in a release.
/// ```
/// let x = parse("1").unwrap();
/// ```
/* a block comment: unsafe { panic!() }
   /* nested: x.unwrap() */ still a comment: todo!() */
pub fn f<'a>(s: &'a str) -> &'a str {
    let quote = '"';
    let tick = '\'';
    let msg = "do not .unwrap() here; unsafe { todo!() }";
    let raw = r#"he said "x.unwrap()" and panic!("no")"#;
    let raw2 = r##"a "# is not the end: unimplemented!()"##;
    let bytes = b"unsafe";
    let label = 'outer: loop { break 'outer; };
    s
}
'''), {})

    def test_identifiers_that_only_contain_the_words(self):
        self.assertEqual(tally('''
#![forbid(unsafe_code)]
use std::panic::catch_unwind;
fn f(x: Option<u8>, todo_list: Vec<u8>, expected: u8) -> u8 {
    let unwrapped = x.unwrap_or_default();
    let y = x.unwrap_or(expected);
    let z = x.unwrap_or_else(|| 0);
    let r#unsafe = 1;
    panic_count(todo_list.len());
    unwrapped + y + z + r#unsafe
}
'''), {})

    def test_a_tuple_field_does_not_swallow_the_call_after_it(self):
        # `0.unwrap` lexed as one number would hide every newtype's unwrap.
        self.assertEqual(tally('fn f(w: W) -> u8 { w.0.unwrap() + w.1.0.unwrap() }')['rust.panic'], 2)

    def test_expect_counts_only_with_a_message(self):
        # A parser's `self.expect(Token::Comma)?` is a method of its own, and
        # counting it would red every new grammar rule.
        found = tally('''
fn f(p: &mut P, o: Option<u8>, k: &str) -> Result<u8, E> {
    p.expect(Token::Comma)?;
    let a = o.expect("present");
    let b = o.expect(&format!("missing {k}"));
    let c = o.expect(concat!("missing ", "key"));
    Ok(a + b + c)
}
''')
        self.assertEqual(found['rust.panic'], 3)

    def test_hits_carry_the_line_and_the_source(self):
        from rust_hatches import scan_tree
        hits = list(scan_tree({'src/a.rs': 'fn f(x: Option<u8>) -> u8 {\n\n    x.unwrap()\n}\n'}))
        self.assertEqual(hits, [('rust.panic', 'src/a.rs', 3, '    x.unwrap()')])

    def test_a_macro_definition_counts_once(self):
        self.assertEqual(tally('macro_rules! must { ($e:expr) => { $e.unwrap() }; }')['rust.panic'], 1)

    def test_a_file_that_does_not_parse_is_still_read_to_its_end(self):
        # The gate runs on whatever is tracked, half-written files included.
        # A scanner that raised on one would red a harness over a file the
        # compiler was about to refuse anyway.
        for text in ('#[', '#![', '#[cfg(', '#[cfg(any(test,', '#[allow(', '#[cfg_attr(', "'", '"', 'r#"', '/*',
                     '/* /* */', 'mod', 'mod a {', 'let _ =', '_ =', 'x.expect(', 'fn f() {{{{', ')]}', 'x.0.',
                     '#[pallet::weight(', 'type WeightInfo = (', 'Pays::', '#[test]', '#[test] fn', '\x00\xff',
                     '#[cfg()] fn f() {}', '#[cfg(,)] fn f() {}', '#[path = ] mod a;'):
            with self.subTest(text=text):
                self.assertIsInstance(tally({'src/lib.rs': text, 'Cargo.toml': '[lints\nx = "'}), dict)
        self.assertEqual(tally('fn f(x: Option<u8>) -> u8 { x.unwrap() }\n#[cfg(test)]\nmod tests {')['rust.panic'], 1)

    def test_a_script_shebang_is_not_an_attribute(self):
        self.assertEqual(tally('#!/usr/bin/env run-cargo-script\nfn main() { todo!() }\n')['rust.todo'], 1)


class TestCode(unittest.TestCase):
    """Code that only a test build compiles discharges nothing in production."""

    def test_an_inline_test_module_and_test_functions_are_skipped(self):
        found = tally('''
pub fn real(x: Option<u8>) -> u8 { x.unwrap() }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn it_works() { assert_eq!(real(Some(1)), Some(1).unwrap()); }
    fn helper() -> u8 { None.expect("never") }
}

#[test]
fn loose_test() { real(None::<u8>.or(Some(1))).checked_add(1).unwrap(); }

#[tokio::test]
async fn async_test() { run().await.unwrap(); }

#[cfg(test)]
fn only_for_tests() { panic!("fine here") }

fn after() -> u8 { todo!() }
''')
        self.assertEqual(found, {'rust.panic': 1, 'rust.todo': 1})

    def test_a_module_named_tests_without_cfg_test_is_production(self):
        # The cfg is the evidence. A name is not, or naming a file `tests.rs`
        # would be a way to take code out of the count while it still ships.
        found = tally({'src/lib.rs': 'mod tests;\n', 'src/tests.rs': 'pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n'})
        self.assertEqual(found['rust.panic'], 1)

    def test_out_of_line_test_modules_are_resolved_through_their_declaration(self):
        files = {
            'pallets/p/src/lib.rs': '#[cfg(test)]\nmod tests;\n#[cfg(test)]\nmod mock;\npub mod real;\n',
            'pallets/p/src/tests.rs': 'mod helpers;\nfn t() { a().unwrap(); b().expect("x"); }\n',
            'pallets/p/src/tests/helpers.rs': 'pub fn h() { c().unwrap() }\n',
            'pallets/p/src/mock.rs': 'pub fn new_test_ext() { build().unwrap() }\n',
            'pallets/p/src/real.rs': 'pub fn r(x: Option<u8>) -> u8 { x.unwrap() }\n',
            'runtime/src/lib.rs': '#[cfg(test)]\nmod tests;\n',
            'runtime/src/tests/mod.rs': 'mod fees;\n',
            'runtime/src/tests/fees.rs': 'fn t() { todo!() }\n',
        }
        self.assertEqual(tally(files), {'rust.panic': 1})

    def test_a_file_one_production_module_also_declares_stays_counted(self):
        files = {
            'src/lib.rs': '#[cfg(test)]\nmod shared;\nmod also;\n',
            'src/also.rs': '#[path = "shared.rs"]\nmod shared;\n',
            'src/shared.rs': 'pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n',
        }
        self.assertEqual(tally(files)['rust.panic'], 1)

    def test_cargo_test_and_bench_targets_are_test_code(self):
        files = {
            'Cargo.toml': '[package]\nname = "x"\n',
            'src/lib.rs': 'pub fn f() {}\n',
            'tests/it.rs': 'mod common;\n#[test]\nfn t() { x().unwrap(); }\nfn helper() { y().unwrap(); }\n',
            'tests/common/mod.rs': 'pub fn setup() { z().unwrap(); }\n',
            'benches/b.rs': 'fn main() { w().unwrap(); }\n',
            'examples/demo.rs': 'fn main() { v().unwrap(); }\n',
        }
        # An example is a program a reader runs, so it stays in the count.
        self.assertEqual(tally(files), {'rust.panic': 1})

    def test_a_build_script_fails_the_build_and_ships_nowhere(self):
        files = {
            'Cargo.toml': '[package]\nname = "x"\nbuild = "build.rs"\n',
            'build.rs': 'fn main() { let out = std::env::var("OUT_DIR").expect("cargo sets it"); probe(&out).unwrap(); }\n',
            'src/lib.rs': 'mod build;\n',
            'src/build.rs': 'pub fn plan(x: Option<u8>) -> u8 { x.unwrap() }\n',
            'tools/gen/build.rs': 'fn main() { run().unwrap(); }\n',
        }
        # Only the script beside a manifest: a module that happens to be
        # called `build`, or a build.rs no manifest sits beside, is counted.
        self.assertEqual(tally(files), {'rust.panic': 2})

    def test_a_test_module_nested_under_a_tests_directory(self):
        # `src/tests/fees.rs` sits where a Cargo test target would under a
        # manifest, and is an ordinary module here: its own children live in
        # `src/tests/fees/`, and test-only reaches them through it.
        files = {
            'src/lib.rs': '#[cfg(test)]\nmod tests;\npub fn real(x: Option<u8>) -> u8 { x.unwrap() }\n',
            'src/tests/mod.rs': 'mod fees;\n',
            'src/tests/fees.rs': 'mod cases;\nfn t() { a().unwrap(); }\n',
            'src/tests/fees/cases.rs': 'pub fn all() { b().unwrap(); c().expect("x"); }\n',
        }
        self.assertEqual(tally(files), {'rust.panic': 1})

    def test_a_tests_directory_beside_no_manifest_is_not_a_cargo_target(self):
        files = {'src/main.rs': 'mod tests;\n', 'src/tests/mod.rs': 'pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n'}
        self.assertEqual(tally(files)['rust.panic'], 1)

    def test_a_file_that_declares_itself_test_only(self):
        self.assertEqual(tally('#![cfg(test)]\nfn t() { x().unwrap(); }\n'), {})

    def test_cfg_predicates_are_evaluated_not_matched(self):
        def one(attr):
            return tally(attr + '\nfn f(x: Option<u8>) -> u8 { x.unwrap() }\n')['rust.panic']
        for attr in ('#[cfg(test)]', '#[cfg(all(test, feature = "std"))]', '#[cfg(any(test))]',
                     '#[cfg(any(test, feature = "runtime-benchmarks"))]',
                     '#[cfg(feature = "runtime-benchmarks")]', '#[cfg(feature = "try-runtime")]',
                     '#[cfg(kani)]', '#[cfg(fuzzing)]', '#[kani::proof]'):
            with self.subTest(skipped=attr):
                self.assertEqual(one(attr), 0)
        for attr in ('#[cfg(not(test))]', '#[cfg(any(test, feature = "std"))]', '#[cfg(feature = "std")]',
                     '#[cfg(all(not(test), feature = "std"))]', '#[cfg_attr(test, derive(Debug))]',
                     '#[inline]', '#[cfg(any())]'):
            with self.subTest(counted=attr):
                self.assertEqual(one(attr), 1)

    def test_a_test_only_item_ends_where_the_item_ends(self):
        found = tally('''
#[cfg(test)]
use std::collections::{BTreeMap, BTreeSet};
#[cfg(test)]
const FIXTURE: Point = Point { x: 1, y: 2 };
#[cfg(test)]
impl Thing { fn probe(&self) -> u8 { self.0.unwrap() } }
struct S {
    #[cfg(test)]
    seen: Vec<u8>,
    live: Option<u8>,
}
fn real(s: S) -> u8 {
    #[cfg(test)]
    let _trace = s.seen.first().unwrap();
    match s.live {
        #[cfg(test)]
        None => panic!("test-only arm"),
        _ => s.live.unwrap(),
    }
}
''')
        self.assertEqual(found, {'rust.panic': 1})

    def test_test_functions_inside_a_property_macro(self):
        found = tally('''
proptest! {
    #[test]
    fn roundtrips(x in any::<u64>()) { prop_assert_eq!(decode(&encode(x)).unwrap(), x); }
}
pub fn decode(b: &[u8]) -> Option<u64> { Some(read(b).unwrap()) }
''')
        self.assertEqual(found, {'rust.panic': 1})

    def test_no_marker_file_takes_tracked_code_out_of_the_count(self):
        # A `cargo vendor` checksum beside a crate is a file anyone can add,
        # so honouring it would be an off switch that costs nothing.
        files = {'vendor/dep/src/lib.rs': 'pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n',
                 'vendor/dep/.cargo-checksum.json': '{"files": {}, "package": null}\n',
                 'src/lib.rs': 'pub fn f(x: Option<u8>) -> u8 { x.unwrap() }\n',
                 'src/.cargo-checksum.json': '{"files": {}, "package": null}\n'}
        self.assertEqual(tally(files), {'rust.panic': 2})


class Suppressions(unittest.TestCase):
    """A silenced lint and a discarded result are the checker being overruled."""

    def test_each_allowed_lint_counts(self):
        def n(text):
            return tally(text)['rust.lint_allow']
        self.assertEqual(n('#[allow(dead_code)]\nfn f() {}'), 1)
        self.assertEqual(n('#![allow(clippy::all, unused)]\nfn f() {}'), 2)
        self.assertEqual(n('#[allow(clippy::unwrap_used, reason = "checked above")]\nfn f() {}'), 1)
        self.assertEqual(n('#[cfg_attr(feature = "std", allow(dead_code))]\nfn f() {}'), 1)
        # Allowed only where tests are compiled, which is not this ratchet's scope.
        self.assertEqual(n('#![cfg_attr(test, allow(clippy::unwrap_used))]\nfn f() {}'), 0)
        self.assertEqual(n('#[deny(missing_docs)]\n#[warn(unused)]\n#[forbid(unsafe_code)]\nfn f() {}'), 0)

    def test_a_longer_list_is_a_rise(self):
        # Counting attributes would let a lint ride in on an existing one.
        before = tally('#[allow(dead_code)]\nfn f() {}')['rust.lint_allow']
        after = tally('#[allow(dead_code, clippy::unwrap_used)]\nfn f() {}')['rust.lint_allow']
        self.assertEqual((before, after), (1, 2))

    def test_expect_is_not_counted_because_it_retires_itself(self):
        self.assertEqual(tally('#[expect(dead_code)]\nfn f() {}\n#![expect(clippy::unwrap_used)]\n'), {})

    def test_a_discarded_value(self):
        def n(body):
            return tally('fn f() -> Result<(), E> {\n' + body + '\nOk(())\n}\n')['rust.let_underscore']
        self.assertEqual(n('let _ = tx.send(v);'), 1)
        self.assertEqual(n('_ = tx.send(v);'), 1)
        self.assertEqual(n('if ready { let _ = flush(); }'), 1)
        # The error went up the stack; only the Ok payload was dropped.
        self.assertEqual(n('let _ = ensure_signed(origin)?;'), 0)
        self.assertEqual(n('let _ = client.get(url).await?;'), 0)
        self.assertEqual(n('if let _ = x {}'), 0)
        self.assertEqual(n('while let _ = next() { break; }'), 0)
        self.assertEqual(n('let _guard = lock();'), 0)
        self.assertEqual(n('let _: u8 = 1;'), 0)
        self.assertEqual(n('match x { _ => {} }'), 0)
        self.assertEqual(n('let (_, b) = pair;'), 0)

    def test_a_manifest_allows_a_lint_for_the_whole_crate(self):
        def n(text, name='Cargo.toml'):
            return tally({name: text})['rust.lint_allow']
        self.assertEqual(n('[lints.clippy]\nunwrap_used = "allow"\n'), 1)
        self.assertEqual(n('[workspace.lints.rust]\nwarnings = { level = "allow", priority = -1 }\n'), 1)
        self.assertEqual(n('[lints]\nclippy.all = "allow"\nrust.unused = \'allow\'\n'), 2)
        self.assertEqual(n('[lints.clippy]\npedantic = "warn"\nunwrap_used = "deny"\n'), 0)
        self.assertEqual(n('[lints.clippy]\n# unwrap_used = "allow"\n'), 0)
        self.assertEqual(n('[dependencies]\nallow = "1"\n[package.metadata.x]\nlevel = "allow"\n'), 0)
        self.assertEqual(n('[lints.clippy]\nunwrap_used = "allow"\n', 'crates/a/Cargo.toml'), 1)
        self.assertEqual(n('[lints.clippy]\nunwrap_used = "allow"\n', 'pyproject.toml'), 0)


class Frame(unittest.TestCase):
    """FRAME states its obligations as attributes, and its opt-outs too."""

    PALLET = '''
#[frame_support::pallet]
pub mod pallet {
    #[pallet::pallet]
    pub struct Pallet<T>(_);

    #[pallet::storage]
    pub type Items<T> = StorageMap<_, Blake2_128Concat, u32, BoundedVec<u8, T::Max>>;

    #[pallet::call]
    impl<T: Config> Pallet<T> {
        #[pallet::call_index(0)]
        #[pallet::weight(T::WeightInfo::add(items.len() as u32))]
        pub fn add(origin: OriginFor<T>, items: Vec<u8>) -> DispatchResult { Ok(()) }

        #[pallet::call_index(1)]
        #[pallet::weight(<T as Config>::WeightInfo::remove())]
        pub fn remove(origin: OriginFor<T>) -> DispatchResult { Ok(()) }

        #[pallet::call_index(2)]
        #[pallet::weight(T::WeightInfo::touch().saturating_add(T::DbWeight::get().reads(1)))]
        pub fn touch(origin: OriginFor<T>) -> DispatchResult { Ok(()) }
    }
}
'''

    def test_a_benchmarked_pallet_is_clean(self):
        self.assertEqual(tally(self.PALLET), {})

    def test_a_weight_that_does_not_come_from_a_weight_info(self):
        def n(weight):
            text = self.PALLET.replace('T::WeightInfo::add(items.len() as u32)', weight)
            return tally(text)['frame.unbenchmarked_weight']
        # A guess and a careful sum are both claims nobody measured, and the
        # scan does not pretend to tell them apart.
        for weight in ('0', '10_000', '{0}', 'Weight::zero()', 'Weight::from_parts(10_000, 0)',
                       'Weight::from_parts(10_000, 0).saturating_add(T::DbWeight::get().writes(1))',
                       'T::DbWeight::get().reads_writes(4, 3).saturating_add(Weight::from_parts(BASE_REF_TIME, 0))',
                       '(Weight::from_parts(10_000, 0), DispatchClass::Operational)',
                       'enact_weight::<T>(call.as_ref())'):
            with self.subTest(counted=weight):
                self.assertEqual(n(weight), 1)
        for weight in ('T::WeightInfo::add(1)', '<T as Config>::WeightInfo::add(items.len() as u32)',
                       '<T as crate::pallet::Config>::WeightInfo::add(1)',
                       'T::WeightInfo::add(1).saturating_add(T::DbWeight::get().reads(1))',
                       '(T::WeightInfo::add(1), DispatchClass::Operational)', 'T::ReceiptWeightInfo::add(1)'):
            with self.subTest(benchmarked=weight):
                self.assertEqual(n(weight), 0)

    def test_a_storage_key_left_out_of_the_benchmark(self):
        text = self.PALLET.replace('#[pallet::storage]', '#[pallet::storage]\n    #[pallet::whitelist_storage]')
        self.assertEqual(tally(text), {'frame.unbenchmarked_weight': 1})

    def test_a_call_admitted_without_a_signature(self):
        text = self.PALLET + '''
#[pallet::validate_unsigned]
impl<T: Config> ValidateUnsigned for Pallet<T> {
    type Call = Call<T>;
    fn validate_unsigned(_s: TransactionSource, _c: &Self::Call) -> TransactionValidity { todo_validity() }
}
impl<T: Config> Pallet<T> {
    pub fn heartbeat(origin: OriginFor<T>) -> DispatchResult { ensure_none(origin)?; Ok(()) }
    pub fn signed(origin: OriginFor<T>) -> DispatchResult { let who = ensure_signed(origin)?; Ok(()) }
}
'''
        self.assertEqual(tally(text), {'frame.unsigned': 2})
        text = self.PALLET.replace('#[pallet::call_index(1)]', '#[pallet::call_index(1)]\n        #[pallet::authorize(Self::may_remove)]')
        self.assertEqual(tally(text), {'frame.unsigned': 1})
        # Declaring the helper is not calling it.
        self.assertEqual(tally('pub fn ensure_none<O>(o: O) -> Result<(), BadOrigin> { check(o) }'), {})

    def test_a_runtime_wired_to_placeholder_weights(self):
        found = tally('''
impl pallet_thing::Config for Runtime {
    type RuntimeEvent = RuntimeEvent;
    type WeightInfo = ();
}
impl pallet_other::Config for Runtime {
    type WeightInfo = pallet_other::weights::SubstrateWeight<Runtime>;
    type SystemWeightInfo = ();
}
''')
        self.assertEqual(found, {'frame.unbenchmarked_weight': 1})

    def test_storage_that_opts_out_of_a_bound(self):
        text = self.PALLET.replace('#[pallet::pallet]', '#[pallet::pallet]\n    #[pallet::without_storage_info]')
        text = text.replace('#[pallet::storage]', '#[pallet::storage]\n    #[pallet::unbounded]')
        self.assertEqual(tally(text), {'frame.unbounded_storage': 2})

    def test_dev_mode_removes_every_obligation_at_once(self):
        for attr in ('#[frame_support::pallet(dev_mode)]', '#[pallet(dev_mode)]', '#[frame::pallet(dev_mode)]'):
            with self.subTest(attr=attr):
                self.assertEqual(tally(self.PALLET.replace('#[frame_support::pallet]', attr)), {'frame.dev_mode': 1})

    def test_a_call_nobody_pays_for(self):
        text = self.PALLET.replace('T::WeightInfo::add(items.len() as u32)',
                                   '(T::WeightInfo::add(1), DispatchClass::Normal, Pays::No)')
        self.assertEqual(tally(text), {'frame.feeless': 1})
        text = self.PALLET.replace('#[pallet::call_index(1)]', '#[pallet::call_index(1)]\n        #[pallet::feeless_if(|_o: &OriginFor<T>| -> bool { true })]')
        self.assertEqual(tally(text), {'frame.feeless': 1})
        self.assertEqual(tally('fn done() -> DispatchResultWithPostInfo { Ok(Pays::No.into()) }'), {'frame.feeless': 1})
        self.assertEqual(tally('fn done() -> DispatchResultWithPostInfo { Ok(Pays::Yes.into()) }'), {})

    def test_a_mock_runtime_is_test_code(self):
        files = {'src/lib.rs': self.PALLET + '\n#[cfg(test)]\nmod mock;\n',
                 'src/mock.rs': 'impl pallet_thing::Config for Test { type WeightInfo = (); }\n'}
        self.assertEqual(tally(files), {})


class Ratchet(unittest.TestCase):
    """The counts reach proof-guard, and an upgrade does not red a repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.write('Cargo.toml', '[package]\nname = "svc"\nversion = "0.1.0"\nedition = "2021"\n')
        self.write('src/lib.rs', 'pub mod pay;\n#[cfg(test)]\nmod tests;\n')
        self.write('src/pay.rs', 'pub fn fee(x: Option<u64>) -> u64 {\n    x.unwrap()\n}\n')
        self.write('src/tests.rs', 'use super::pay::fee;\n#[test]\nfn pays() { assert_eq!(fee(Some(1)), Some(1).unwrap()); }\n')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', rel], check=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'proof-guard.py'), '--root', str(self.root), *args],
                              capture_output=True, text=True)

    def baseline(self):
        return json.loads((self.root / '.fluxpoint-proof-baseline.json').read_text())['counts']

    def test_a_rust_repo_arms_and_a_new_panic_site_is_red(self):
        self.assertIn('rust.panic', self.cli('--scan').stdout)
        self.assertEqual(self.cli('--baseline').returncode, 0)
        self.assertEqual(self.baseline()['rust.panic'], 1)
        self.assertEqual(self.cli('--check').returncode, 0)
        self.write('src/pay.rs', 'pub fn fee(x: Option<u64>) -> u64 {\n    x.unwrap()\n}\n'
                                 'pub fn tip(x: Option<u64>) -> u64 {\n    x.expect("tip is set")\n}\n')
        result = self.cli('--check')
        self.assertEqual(result.returncode, 1)
        self.assertIn('rust.panic: 1 -> 2', result.stderr)
        self.assertIn('src/pay.rs:5', result.stderr)

    def test_a_new_test_that_unwraps_is_not_a_rise(self):
        self.cli('--baseline')
        self.write('src/tests.rs', 'use super::pay::fee;\n#[test]\nfn pays() { fee(Some(1)); }\n'
                                   '#[test]\nfn more() { a().unwrap(); b().unwrap(); c().expect("x"); }\n')
        self.assertEqual(self.cli('--check').returncode, 0)

    def test_replacing_an_unwrap_with_an_error_is_progress(self):
        self.cli('--baseline')
        self.write('src/pay.rs', 'pub fn fee(x: Option<u64>) -> Result<u64, E> {\n    x.ok_or(E::NoFee)\n}\n')
        result = self.cli('--check')
        self.assertEqual(result.returncode, 0)
        self.assertIn('stronger than the baseline', result.stdout)

    def test_a_typed_hole_is_named_for_what_it_is(self):
        self.cli('--baseline')
        self.write('src/pay.rs', 'pub fn fee(x: Option<u64>) -> u64 {\n    x.unwrap()\n}\npub fn refund() -> u64 { todo!() }\n')
        result = self.cli('--check')
        self.assertEqual(result.returncode, 1)
        self.assertIn('rust.todo: 0 -> 1', result.stderr)

    def test_a_baseline_from_before_these_categories_is_not_red(self):
        # Every category this plugin adds would otherwise be a red gate for
        # each repo that armed before it, over a diff nobody wrote.
        (self.root / '.fluxpoint-proof-baseline.json').write_text(
            json.dumps({'counts': {'rust.external_body': 0, 'rust.assume': 0}}))
        result = self.cli('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NOT yet ratcheted', result.stdout + result.stderr)
        self.assertIn('rust.panic: 1 (new)', result.stdout + result.stderr)

    def test_a_lint_switched_off_from_the_command_line(self):
        self.write('.github/workflows/ci.yml', 'steps:\n  - run: cargo clippy --all-targets -- -D warnings\n')
        self.cli('--baseline')
        self.assertEqual(self.baseline()['flags.lints_off'], 0)
        for line in ('cargo clippy --all-targets -- -A warnings', 'cargo clippy -- -D warnings -A clippy::unwrap_used',
                     'RUSTFLAGS="--cap-lints allow" cargo build', 'cargo clippy -- -Awarnings'):
            with self.subTest(line=line):
                self.write('.github/workflows/ci.yml', 'steps:\n  - run: ' + line + '\n')
                result = self.cli('--check')
                self.assertEqual(result.returncode, 1)
                self.assertIn('flags.lints_off: 0 -> 1', result.stderr)
        self.write('.cargo/config.toml', '[build]\nrustflags = ["-A", "warnings"]\n')
        self.write('.github/workflows/ci.yml', 'steps:\n  - run: git add -A src && ssh -A host && cargo clippy -- -D warnings\n')
        result = self.cli('--check')
        self.assertIn('flags.lints_off: 0 -> 1', result.stderr)
        self.assertIn('.cargo/config.toml', result.stderr)


if __name__ == '__main__':
    unittest.main()
