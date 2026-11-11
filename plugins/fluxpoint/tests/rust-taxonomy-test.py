#!/usr/bin/env python3
"""Rust tests specify attack classes for services and FRAME pallets.

The taxonomy gate decides "specified" from a test's name. For Rust that
means a function a test build actually collects: one under `#[test]` or a
sibling attribute, including the functions a `proptest!` block declares. A
class id in a comment, in a string, or as the name of a helper nothing runs
is not evidence, which is the rule the other languages already hold.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
spec = importlib.util.spec_from_file_location('spec_guard', PLUGIN / 'scripts/spec-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

PALLET = '''#[frame_support::pallet]
pub mod pallet {
    #[pallet::pallet]
    pub struct Pallet<T>(_);
    #[pallet::call]
    impl<T: Config> Pallet<T> {
        #[pallet::call_index(0)]
        #[pallet::weight(T::WeightInfo::submit())]
        pub fn submit(origin: OriginFor<T>) -> DispatchResult { Ok(()) }
    }
}
'''
WAIVER = 'This repository exposes no such surface; reviewed explicitly.'


class RustTaxonomies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        template = json.loads((PLUGIN / 'templates/attack-taxonomy.json').read_text(encoding='utf-8'))
        self.shipped = {tax['language']: tax for tax in template['taxonomies']}
        for language in ('rust', 'frame'):
            self.assertIn(language, self.shipped, f'the shipped {language} taxonomy is missing')

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', name], check=True)

    def manifest(self, *languages, **extra):
        doc = dict({'version': 1, 'taxonomies': [self.shipped[name] for name in languages]}, **extra)
        self.put('.fluxpoint-attacks.json', json.dumps(doc))

    def ids(self, language):
        return [cls['id'] for cls in self.shipped[language]['classes']]

    def findings(self):
        doc, problem = guard.load_attacks(str(self.root))
        self.assertIsNone(problem)
        context = guard.taxonomy_context(str(self.root), {})
        found, notes = guard.class_findings(str(self.root), doc, context)
        for language, missing, text in guard.language_gaps(doc, context):
            (found if missing and doc['requireAllLanguages'] else notes).append((language, text))
        return found, notes

    def functions_for(self, ids, generator=False):
        body = '\n'.join(f'    #[test]\n    fn {cid}_holds() {{ assert!(run()); }}' for cid in ids)
        if generator:
            body = 'use proptest::prelude::*;\nproptest! {\n' + '\n'.join(
                f'    #[test]\n    fn {cid}_holds(n in 0u32..9) {{ prop_assert!(run(n)); }}' for cid in ids) + '\n}\n'
            return body
        return '#[cfg(test)]\nmod tests {\n' + body + '\n}\n'

    def cli(self, *args):
        return subprocess.run([sys.executable, str(PLUGIN / 'scripts/spec-guard.py'), '--root', str(self.root), *args],
                              capture_output=True, text=True)

    # ---- shape of what ships -------------------------------------------
    def test_the_shipped_taxonomies_are_complete_objects(self):
        for language in ('rust', 'frame'):
            classes = self.shipped[language]['classes']
            self.assertGreaterEqual(len(classes), 6)
            for cls in classes:
                with self.subTest(language=language, cls=cls.get('id')):
                    self.assertRegex(cls['id'], r'^attack_[a-z0-9_]+$')
                    self.assertTrue(cls['applies'])
                    for field in ('property', 'fuzz'):
                        self.assertGreater(len(cls[field]), 40)
            self.assertEqual(self.shipped[language]['waived'], {})
        self.assertEqual(set(self.ids('rust')) & set(self.ids('frame')), set())

    # ---- rust: services and off-chain code -------------------------------
    def test_a_repository_without_rust_tests_is_dormant(self):
        self.manifest('rust')
        self.put('src/lib.rs', 'pub fn f() {}\n')
        found, notes = self.findings()
        self.assertEqual(found, [])
        self.assertTrue(any('gates nothing' in str(note) for note in notes), notes)

    def test_each_class_needs_a_collected_rust_test(self):
        ids = self.ids('rust')
        self.manifest('rust')
        self.put('src/lib.rs', 'pub fn f() {}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn smoke() {}\n}\n')
        self.assertEqual(len(self.findings()[0]), len(ids))
        # Named in prose, in a string, and by a helper no test build collects.
        decoys = ''.join(f'// {cid}\nconst S_{n}: &str = "{cid}";\nfn {cid}() {{}}\n' for n, cid in enumerate(ids))
        self.put('src/decoys.rs', decoys)
        self.assertEqual(len(self.findings()[0]), len(ids))
        self.put('src/lib.rs', 'pub fn f() {}\n' + self.functions_for(ids))
        found, notes = self.findings()
        self.assertEqual(found, [])
        self.assertTrue(any('example' in str(note) for note in notes), 'an example is noted as weaker than a property')

    def test_a_property_over_a_generator_is_not_noted_as_an_example(self):
        ids = self.ids('rust')
        self.manifest('rust')
        self.put('Cargo.toml', '[package]\nname = "svc"\n')
        self.put('tests/attacks.rs', self.functions_for(ids, generator=True))
        found, notes = self.findings()
        self.assertEqual(found, [])
        self.assertFalse(any('example' in str(note) for note in notes), notes)

    def test_async_and_parameterised_test_attributes_count(self):
        ids = self.ids('rust')
        self.manifest('rust')
        body = ''.join(f'#[tokio::test]\nasync fn {cid}_a() {{}}\n' for cid in ids[:2])
        body += ''.join(f'#[rstest]\n#[case(1)]\nfn {cid}_b(#[case] n: u8) {{}}\n' for cid in ids[2:])
        self.put('src/lib.rs', body)
        self.assertEqual(self.findings()[0], [])

    def test_waivers_are_scoped_and_need_a_reason(self):
        ids = self.ids('rust')
        self.put('src/lib.rs', '#[test]\nfn smoke() {}\n')
        rust = dict(self.shipped['rust'], waived={cid: WAIVER for cid in ids})
        self.put('.fluxpoint-attacks.json', json.dumps({'taxonomies': [rust]}))
        self.assertEqual(self.findings()[0], [])
        rust['waived'][ids[0]] = 'n/a'
        self.put('.fluxpoint-attacks.json', json.dumps({'taxonomies': [rust]}))
        self.assertEqual(len(self.findings()[0]), 1)

    def test_a_builder_written_in_rust_owes_the_builder_classes_it_declares(self):
        # The manifest is the repository's own declaration: classes copied in
        # from the TypeScript builder taxonomy are held to Rust tests.
        borrowed = self.shipped['typescript']['classes'][:3]
        rust = dict(self.shipped['rust'], classes=borrowed)
        self.put('.fluxpoint-attacks.json', json.dumps({'taxonomies': [rust]}))
        self.put('src/lib.rs', '#[test]\nfn smoke() {}\n')
        self.assertEqual(len(self.findings()[0]), 3)
        self.put('src/lib.rs', self.functions_for([cls['id'] for cls in borrowed]))
        self.assertEqual(self.findings()[0], [])

    # ---- frame: pallets and runtimes -------------------------------------
    def test_a_rust_service_is_silent_on_pallet_classes(self):
        self.manifest('frame')
        self.put('src/lib.rs', '#[test]\nfn smoke() {}\n')
        found, notes = self.findings()
        self.assertEqual(found, [])
        self.assertTrue(any('gates nothing' in str(note) for note in notes), notes)

    def test_a_pallet_activates_every_frame_class(self):
        ids = self.ids('frame')
        self.manifest('frame')
        self.put('pallets/thing/src/lib.rs', PALLET)
        self.assertEqual(len(self.findings()[0]), len(ids))
        self.put('pallets/thing/src/lib.rs', PALLET + '#[cfg(test)]\nmod tests;\n')
        self.put('pallets/thing/src/tests.rs', '\n'.join(f'#[test]\nfn {cid}() {{ new_test_ext().execute_with(|| {{}}); }}' for cid in ids))
        self.assertEqual(self.findings()[0], [])

    def test_a_runtime_without_a_pallet_of_its_own_is_still_frame(self):
        self.manifest('frame')
        self.put('runtime/src/lib.rs', 'construct_runtime!(\n    pub enum Runtime { System: frame_system }\n);\n')
        self.assertEqual(len(self.findings()[0]), len(self.ids('frame')))

    def test_frame_named_only_in_a_comment_is_not_frame(self):
        self.manifest('frame')
        self.put('src/lib.rs', '// uses #[frame_support::pallet] upstream\nconst DOC: &str = "construct_runtime!";\n')
        self.assertEqual(self.findings()[0], [])

    # ---- the manifest against the repository ------------------------------
    def test_a_tracked_language_without_a_taxonomy_is_named(self):
        self.manifest('aiken')
        self.put('pallets/thing/src/lib.rs', PALLET + '#[test]\nfn smoke() {}\n')
        found, notes = self.findings()
        self.assertEqual(found, [])
        text = ' '.join(str(note) for note in notes)
        self.assertIn('rust', text)
        self.assertIn('frame', text)
        self.manifest('aiken', requireAllLanguages=True)
        self.assertEqual(sorted(language for language, _ in self.findings()[0]), ['frame', 'rust'])
        self.manifest('aiken', requireAllLanguages=True, languages=['aiken', 'frame'])
        self.assertEqual([language for language, _ in self.findings()[0]], ['frame'])

    def test_the_cli_reports_and_gates_both(self):
        self.manifest('rust', 'frame')
        self.put('pallets/thing/src/lib.rs', PALLET + '#[test]\nfn smoke() {}\n')
        result = self.cli('--check')
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn(self.ids('rust')[0], output)
        self.assertIn(self.ids('frame')[0], output)
        scan = self.cli('--scan').stdout
        self.assertIn(f"attack class rust:{self.ids('rust')[0]}: UNSPECIFIED", scan)
        self.assertIn(f"attack class frame:{self.ids('frame')[0]}: UNSPECIFIED", scan)
        self.put('pallets/thing/src/lib.rs', PALLET + self.functions_for(self.ids('rust') + self.ids('frame')))
        result = self.cli('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
