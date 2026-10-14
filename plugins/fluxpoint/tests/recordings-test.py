#!/usr/bin/env python3
"""Execute provider conformance and seam growth against real temporary repos."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
GUARD = PLUGIN / 'scripts' / 'seam-guard.py'


class Recordings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.root)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value) + '\n')
        return target

    def source(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def run_guard(self, *args, code=0):
        result = subprocess.run([sys.executable, str(GUARD), '--root', str(self.root), *args],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def manifest(self, capture=None, double=None, modules=None):
        if capture is None:
            capture = {'outputs': [{'reference_script': {'bytes': None}, 'sheets': []}]}
        self.write('captures/koios.json', capture)
        self.write('fixtures/utxos.json', capture if double is None else double)
        doc = {'providerModules': modules or ['koios', 'fetch', 'httpx', 'requests'],
               'recordings': [{'id': 'koios-utxos', 'provider': 'Koios',
                               'endpoint': '/address_utxos', 'capturedAt': '2026-10-12T18:01:49Z',
                               'body': 'captures/koios.json', 'doubles': ['fixtures/utxos.json']}]}
        self.write('.fluxpoint-recordings.json', doc)
        return doc

    def test_hm2_null_against_string_is_red_without_tests_or_baseline(self):
        self.manifest(double={'outputs': [{'reference_script': {'bytes': 'deadbeef'}, 'sheets': []}]})
        out = self.run_guard('--check', code=1)
        self.assertIn('$.outputs[0].reference_script.bytes', out)
        self.assertIn('double string, recording null', out)
        self.assertIn('Koios /address_utxos, captured 2026-10-12T18:01:49Z', out)
        self.assertFalse((self.root / '.fluxpoint-proof-baseline.json').exists())

    def test_xs2_omitted_sheets_and_extra_fields_name_paths(self):
        self.manifest(double={'outputs': [{'reference_script': {'bytes': None, 'invented': True}}]})
        out = self.run_guard('--check', code=1)
        self.assertIn('$.outputs[0].sheets: key only in recording', out)
        self.assertIn('$.outputs[0].reference_script.invented: key only in double', out)

    def test_conform_reaches_deep_array_elements_and_distinguishes_json_types(self):
        actual, recorded = True, 1
        for _ in range(40):
            actual, recorded = [{'nested': actual}], [{'nested': recorded}]
        self.write('double.json', actual)
        self.write('capture.json', recorded)
        out = self.run_guard('--conform', 'double.json', 'capture.json', code=1)
        self.assertIn('$' + '[0].nested' * 40, out)
        self.assertIn('double boolean, recording number', out)

    def test_conform_checks_all_array_rows_and_cardinality(self):
        self.write('double.json', [{'x': 2}, {'x': None}])
        self.write('capture.json', [{'x': 1}, {'x': 'real'}, {'x': 3}])
        out = self.run_guard('--conform', 'double.json', 'capture.json', code=1)
        self.assertIn('array length mismatch', out)
        self.assertIn('$[1].x', out)

    def test_scalar_values_are_left_for_independent_value_review(self):
        self.manifest(capture={'txId': 'ledger-id', 'cost': 1},
                      double={'txId': 'different-id', 'cost': 1.5})
        self.assertIn('1 double fixture(s) conform', self.run_guard('--check'))

    def test_every_declared_pair_checked_and_failed_baseline_preserved(self):
        doc = self.manifest()
        self.write('fixtures/second.json', {'wrong': []})
        doc['recordings'][0]['doubles'].append('fixtures/second.json')
        self.write('.fluxpoint-recordings.json', doc)
        baseline = self.write('.fluxpoint-proof-baseline.json', {'counts': {'todo': 2}})
        before = baseline.read_bytes()
        self.assertIn('fixtures/second.json', self.run_guard('--baseline', code=1))
        self.assertEqual(baseline.read_bytes(), before)

    def test_manifest_provenance_and_schema_are_required(self):
        base = self.manifest()
        cases = [({'recordings': []}, 'nonempty list'),
                 ({'recordings': 'not a list'}, 'nonempty list'),
                 ({**base, 'providerModules': ['koios', 'koios']}, 'unique nonempty'),
                 ({**base, 'unknown': 1}, 'expected an object')]
        for doc, why in cases:
            with self.subTest(why=why):
                self.write('.fluxpoint-recordings.json', doc)
                self.assertIn(why, self.run_guard('--check', code=1))
        for key, value in [('id', 'bad id'), ('provider', ''), ('endpoint', ''),
                           ('capturedAt', '2026-10-12'), ('capturedAt', 'not a timestamp'),
                           ('doubles', []), ('body', {})]:
            with self.subTest(key=key, value=value):
                doc = json.loads(json.dumps(base))
                doc['recordings'][0][key] = value
                self.write('.fluxpoint-recordings.json', doc)
                self.run_guard('--check', code=1)
        doc = json.loads(json.dumps(base))
        doc['recordings'].append(dict(doc['recordings'][0]))
        self.write('.fluxpoint-recordings.json', doc)
        self.assertIn('unique identifier', self.run_guard('--check', code=1))

    def test_manifest_cannot_check_itself_escape_repo_or_silently_drop_missing_file(self):
        base = self.manifest()
        for value, why in [('captures/missing.json', 'does not exist'),
                           ('../outside.json', 'without traversal'),
                           ('/tmp/outside.json', 'without traversal'),
                           ('C:/outside.json', 'without traversal'),
                           ('captures/koios.json', 'different file')]:
            with self.subTest(value=value):
                doc = json.loads(json.dumps(base))
                doc['recordings'][0]['doubles'] = [value]
                self.write('.fluxpoint-recordings.json', doc)
                self.assertIn(why, self.run_guard('--check', code=1))
        outside = self.root.parent / (self.root.name + '-outside.json')
        try:
            outside.write_text('{}')
            (self.root / 'linked.json').symlink_to(outside)
            doc = json.loads(json.dumps(base))
            doc['recordings'][0]['body'] = 'linked.json'
            self.write('.fluxpoint-recordings.json', doc)
            self.assertIn('escapes the repository', self.run_guard('--check', code=1))
        finally:
            outside.unlink(missing_ok=True)

    def test_invalid_duplicate_and_non_json_numbers_fail_closed(self):
        self.manifest()
        for text in ['not JSON', '{"bytes":null,"bytes":"fake"}', '{"n":NaN}']:
            with self.subTest(text=text):
                self.source('fixtures/utxos.json', text)
                self.assertIn('not readable JSON', self.run_guard('--check', code=1))
        self.source('.fluxpoint-recordings.json', '{"recordings":[], "recordings":[]}')
        self.assertIn('duplicate JSON key', self.run_guard('--check', code=1))

    def test_python_patches_and_http_doubles_count_in_both_filename_forms(self):
        self.source('tests/test_api.py', '''from unittest import mock
from unittest.mock import patch as replace
import respx as http
import responses as reply
# mock.patch("comment_only")
with mock.patch("koios.Client.read"):
    pass
with replace.object(koios.Client, "read"):
    pass
monkeypatch.setattr("koios.Client.read", lambda: None)
http.get("https://example.test").mock(return_value=None)
reply.add(reply.GET, "https://example.test", json={})
''')
        self.source('tests/wallet_test.py', '''from unittest.mock import patch
@patch("koios.Client.read")
def test_read():
    pass
''')
        self.source('tests/helper.py', 'mock.patch("ignored")\n')
        out = self.run_guard('--scan')
        self.assertIn('third_party = 6', out)
        self.assertNotIn('comment_only', out)
        self.assertNotIn('ignored', out)
        self.run_guard('--baseline')
        self.source('tests/new_test.py', 'mock.patch("koios.new")\n')
        self.assertIn('third_party mocks rose 6 -> 7', self.run_guard('--check', code=1))

    def test_python_local_module_patches_are_first_party(self):
        self.source('src/api.py', 'def read(): pass\n')
        self.source('test_api.py', 'mock.patch("api.read")\n')
        self.assertIn('first_party = 1', self.run_guard('--scan'))

    def test_python_syntax_errors_are_reported_instead_of_silently_unscanned(self):
        self.source('test_invalid.py', 'def bad(\n')
        self.assertIn('cannot scan Python test', self.run_guard('--check', code=1))

    def test_provider_growth_needs_an_exact_declared_recording_marker(self):
        self.manifest()
        self.source('test_api.py', 'def test_x(): pass\n')
        self.run_guard('--baseline')
        self.source('test_api.py', 'mock.patch("koios.Client.read")\n')
        self.assertIn('provider_unrecorded mocks rose 0 -> 1', self.run_guard('--check', code=1))
        self.source('test_api.py', '# fluxpoint-recording: koios-utxos-invented\nmock.patch("koios.Client.read")\n')
        self.run_guard('--check', code=1)
        self.source('test_api.py', '# fluxpoint-recording: koios-utxos\nmock.patch("koios.Client.read")\n')
        self.assertIn('provider=1 provider_unrecorded=0', self.run_guard('--check'))
        # Recording-backed module doubles are still measured, never invisible.
        self.assertIn('provider = 1', self.run_guard('--scan'))
        self.source('fixtures/utxos.json', '{"outputs": []}')
        self.run_guard('--check', code=1)

    def test_old_recorded_provider_cannot_mask_new_unrecorded_double(self):
        self.manifest()
        self.source('test_old.py', '# fluxpoint-recording: koios-utxos\nmock.patch("koios.Client.read")\n')
        self.run_guard('--baseline')
        self.source('test_old.py', 'mock.patch("koios.Client.read")\n')
        self.run_guard('--check', code=1)

    def test_process_fetch_doubles_count_and_use_provider_recording_exception(self):
        self.manifest()
        self.source('api.test.ts', 'it("works", () => {});\n')
        self.run_guard('--baseline')
        self.source('api.test.ts', 'vi.stubGlobal("fetch", vi.fn());\n')
        self.run_guard('--check', code=1)
        self.source('api.test.ts', '// fluxpoint-recording: koios-utxos\nvi.stubGlobal("fetch", vi.fn());\n'
                    'vi.spyOn(globalThis, "fetch");\nglobalThis.fetch = vi.fn();\n')
        self.assertIn('provider=3 provider_unrecorded=0', self.run_guard('--check'))

    def test_scaffolded_harness_enforces_recordings_and_missing_gate(self):
        self.manifest(double={'outputs': []})
        harness = self.root / 'scripts' / 'harness.sh'
        harness.parent.mkdir()
        shutil.copyfile(PLUGIN / 'templates' / 'harness.sh', harness)
        env = {**os.environ, 'FPL_PLUGIN_ROOT': str(PLUGIN)}
        env.pop('CLAUDE_PLUGIN_ROOT', None)
        result = subprocess.run(['bash', str(harness), '--full'], cwd=self.root,
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('array length mismatch', result.stdout + result.stderr)
        # A repo declaring recordings must fail even if no plugin is installed.
        env['FPL_PLUGIN_ROOT'] = str(self.root / 'missing-plugin')
        harness.write_text(harness.read_text().replace('plugin_script() {', 'plugin_script() {\n  return 0'))
        result = subprocess.run(['bash', str(harness), '--full'], cwd=self.root,
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('seam-guard.py', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
