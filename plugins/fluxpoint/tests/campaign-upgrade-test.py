#!/usr/bin/env python3
"""Schema and packet migrations state their actual historical coverage (#181)."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
import specification

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

c = module('upgrade_compiler', PLUGIN / 'scripts/compile-graph.py')
fixtures = module('upgrade_fixtures', Path(__file__).with_name('specification-test.py'))


class CampaignUpgrade(unittest.TestCase):
    def test_guard_fields_must_exist_in_effective_contracts(self):
        for contract, field in [('SliceV1', 'packetConflict'), ('RedTeamV1', 'coverage'), ('TreeCheckV1', 'root')]:
            with self.subTest(contract=contract):
                schemas = c.load_contracts(str(PLUGIN / 'contracts'))
                schemas[contract]['properties'].pop(field)
                ir = fixtures.graph()
                ir['nodes'].append({'id': 'review', 'after': 'gate', 'prompt': 'Review {{prev}}', 'contract': 'RedTeamV1', 'independent': True})
                findings = c.validate(ir, schemas)
                self.assertTrue(any(contract + '.' + field in finding for finding in findings), findings)

    def fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        doc = fixtures.packet()
        doc['checks'][0]['scope'] = 'archive.json#/scope'
        (root / 'archive.json').write_text('{"scope":"unchanged checked behavior"}')
        (root / '.fluxpoint-spec.json').write_text(json.dumps(doc))
        (root / 'WORK.md').write_text('SPEC: .fluxpoint-spec.json\n\n## Evidence\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        _, identity = specification.load(root, locked=False)
        old = {k: v for k, v in identity.items() if k != 'references'}
        (root / '.fluxpoint-spec-lock.json').write_text(json.dumps(old))
        return root, identity, old

    def test_old_lock_names_format_gap_instead_of_changed_archive(self):
        root, _, _ = self.fixture()
        with self.assertRaisesRegex(ValueError, 'lock has no references map') as error:
            specification.load(root)
        self.assertNotIn('file the packet points into changed', str(error.exception))

    def test_new_lock_records_format_and_unchanged_packet_loads(self):
        root, _, _ = self.fixture()
        result = subprocess.run([sys.executable, str(PLUGIN / 'scripts/specification.py'), '--root', str(root), '--lock'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lock = json.loads((root / '.fluxpoint-spec-lock.json').read_text())
        self.assertEqual(lock['lockFormat'], 2)
        specification.load(root)

    def test_overlay_reports_omitted_shipped_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'contracts'
            local.mkdir()
            (local / 'TreeCheckV1.schema.json').write_text(json.dumps({'$id': 'TreeCheckV1', 'type': 'object', 'properties': {}}))
            _, description = c.resolve_contracts(str(PLUGIN / 'contracts'), header='contracts', root=tmp)
            self.assertIn('TreeCheckV1.root', description)

    def record(self, root, old, *flags):
        summary = {'campaign': 'old run', 'outcome': 'COMPLETE', 'results': {}, 'provenance': [],
                   'contracts': {}, 'inputs': {'target': '/srv/notes/old-plan.txt'},
                   'specification': old, 'specificationPath': '.fluxpoint-spec.json'}
        return subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'), '--root', str(root),
                               '--graph', str(root / 'WORK.md'), '--run-id', 'legacy', *flags],
                              input=json.dumps(summary), capture_output=True, text=True,
                              env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)

    def test_legacy_record_retains_missing_reference_coverage_with_explicit_pin_allowance(self):
        for modern_lock in (False, True):
            with self.subTest(modern_lock=modern_lock):
                root, identity, old = self.fixture()
                if modern_lock:
                    (root / '.fluxpoint-spec-lock.json').write_text(json.dumps(identity))
                rejected = self.record(root, old)
                self.assertNotEqual(rejected.returncode, 0)
                result = self.record(root, old, '--unpinned-ok', '/srv/notes/old-plan.txt')
                self.assertEqual(result.returncode, 0, result.stderr)
                artifact = json.loads((root / '.claude/fluxpoint/runs/legacy.json').read_text())
                self.assertEqual(artifact['specificationCoverage']['references'], 'uncovered-legacy')
                self.assertIn('LEGACY reference files not covered', (root / 'WORK.md').read_text())

    def test_legacy_claim_cannot_downgrade_a_changed_modern_reference_lock(self):
        root, identity, old = self.fixture()
        (root / '.fluxpoint-spec-lock.json').write_text(json.dumps(identity))
        (root / 'archive.json').write_text('{"scope":"changed after locking"}')
        with self.assertRaisesRegex(ValueError, 'reference.*changed since lock'):
            specification.recording_identity(root, old)
        result = self.record(root, old, '--unpinned-ok', '/srv/notes/old-plan.txt')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / '.claude/fluxpoint/runs/legacy.json').exists())

    def test_legacy_compatibility_cannot_hide_packet_change(self):
        root, _, old = self.fixture()
        doc = json.loads((root / '.fluxpoint-spec.json').read_text())
        doc['goal'] += ' changed'
        (root / '.fluxpoint-spec.json').write_text(json.dumps(doc))
        result = self.record(root, old, '--unpinned-ok', '/srv/notes/old-plan.txt')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / '.claude/fluxpoint/runs/legacy.json').exists())


if __name__ == '__main__':
    unittest.main()
