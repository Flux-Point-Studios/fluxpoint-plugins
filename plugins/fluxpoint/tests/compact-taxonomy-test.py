#!/usr/bin/env python3
"""Midnight class declarations gate contracts and builders (#186)."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('spec_guard', PLUGIN / 'scripts/spec-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class CompactTaxonomy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        template = json.loads((PLUGIN / 'templates/attack-taxonomy.json').read_text())
        self.tax = next((tax for tax in template['taxonomies'] if tax['language'] == 'compact'), None)
        self.assertIsNotNone(self.tax, 'shipped Midnight taxonomy is missing')
        self.ids = [cls['id'] for cls in self.tax['classes']]
        self.assertEqual(len(self.ids), 9)
        self.put('.fluxpoint-attacks.json', json.dumps({'taxonomies': [self.tax]}))

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        subprocess.run(['git', '-C', str(self.root), 'add', name], check=True)

    def findings(self):
        doc, problem = guard.load_attacks(str(self.root))
        self.assertIsNone(problem)
        return guard.class_findings(str(self.root), doc, guard.taxonomy_context(str(self.root), {}))

    def properties(self, generator=True):
        self.put('tests/midnight.test.ts', ('import fc from "fast-check";\n' if generator else '') + '\n'.join(f'test("{cid}", () => {{}});' for cid in self.ids))

    def test_contract_missing_examples_properties_and_waivers(self):
        self.put('contract.compact', 'export circuit post(): [] { assert true; }\n')
        self.assertEqual(len(self.findings()[0]), 9)
        self.properties(False)
        self.assertEqual(len(self.findings()[0]), 9, 'examples cannot satisfy the new property obligation')
        self.properties(True)
        self.assertEqual(self.findings()[0], [])
        self.put('tests/midnight.test.ts', '')
        self.tax['waived'] = {cid: 'This deployment does not expose this operation; reviewed explicitly.' for cid in self.ids}
        self.put('.fluxpoint-attacks.json', json.dumps({'taxonomies': [self.tax]}))
        self.assertEqual(self.findings()[0], [])
        self.tax['waived'][self.ids[0]] = 'no'
        self.put('.fluxpoint-attacks.json', json.dumps({'taxonomies': [self.tax]}))
        self.assertEqual(len(self.findings()[0]), 1)

    def test_midnight_builder_without_compact_source_is_active(self):
        self.put('src/post.ts', 'import { submitTx } from "@midnight-ntwrk/midnight-js-contracts";\n')
        self.assertEqual(len(self.findings()[0]), 9)
        self.properties(True)
        self.assertEqual(self.findings()[0], [])

    def test_unrelated_repo_stays_dormant_and_cli_checks_classes(self):
        self.put('src/app.ts', 'export const app = 1;\n')
        self.assertEqual(self.findings()[0], [])
        self.put('contract.compact', 'export circuit post(): [] {}\n')
        command = [sys.executable, str(PLUGIN / 'scripts/spec-guard.py'), '--root', str(self.root), '--check']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.ids[0], result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
