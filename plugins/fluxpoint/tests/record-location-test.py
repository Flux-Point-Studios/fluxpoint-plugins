#!/usr/bin/env python3
"""Exercise recorder discovery and distinguish an artifact from an Evidence row."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/record-run.py'
HEADER = '| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n'


class RecordLocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.graph = self.root / 'WORK.md'
        self.graph.write_text(HEADER)
        self.sub = self.root / 'nested'
        self.sub.mkdir()

    def record(self, *flags):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root),
                               '--graph', str(self.graph), '--run-id', 'test', *flags],
                              input=json.dumps({'outcome': 'COMPLETE', 'results': {}}),
                              cwd=self.sub, capture_output=True, text=True,
                              env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)

    def artifact(self):
        return self.root / '.claude/fluxpoint/runs/test.json'

    def test_default_is_root_relative_from_subdirectory(self):
        p = self.record()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(self.artifact().exists())
        self.assertFalse((self.sub / '.claude').exists())
        self.assertIn('| test | COMPLETE |', self.graph.read_text())

    def test_rejects_other_directory_before_writing_any_state(self):
        p = self.record('--state-dir', str(self.root / 'elsewhere'))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertFalse((self.root / '.claude').exists())
        self.assertFalse((self.root / 'elsewhere').exists())

    def test_explicit_canonical_path_is_accepted(self):
        p = self.record('--state-dir', str(self.artifact().parent))
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_rejects_run_id_path_escape(self):
        p = self.record('--run-id', '../escape')
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.root / '.claude').exists())

    def test_missing_header_separator_and_evidence_file_are_incomplete(self):
        for text in ('# Work\n', HEADER.splitlines()[0] + '\n'):
            with self.subTest(text=text):
                self.graph.write_text(text)
                p = self.record()
                self.assertEqual(p.returncode, 3, p.stderr)
                self.assertTrue(self.artifact().exists())
                self.assertIn('row NOT appended', p.stderr)
                self.assertIn('--evidence WORK.md', p.stderr)
                self.assertNotIn('| test | COMPLETE |', p.stdout)
        p = self.record('--evidence', str(self.root / 'absent.md'))
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn('does not exist', p.stderr)
        self.assertTrue(self.artifact().exists())

    def test_windows_drive_ads_and_unsafe_components_are_rejected(self):
        for run_id in ('D:escape', 'run:stream', 'trailing.', 'has space', '.', '..', 'a' * 129, 'CON', 'nul.log', 'COM1.state'):
            with self.subTest(run_id=run_id):
                p = self.record('--run-id', run_id)
                self.assertEqual(p.returncode, 2, p.stderr)
        self.assertFalse((self.root / '.claude').exists())

    def test_fenced_examples_and_outside_tables_cannot_fake_evidence(self):
        examples = [
            '```markdown\n' + HEADER + '```\n',
            '~~~markdown\n' + HEADER + '~~~\n',
            '## Evidence\n| What | How | Result |\n|---|---|---|\n## Notes\n' + HEADER,
            '## Evidence\n| What | How | Result |\n|---|---|---|\n## Notes\n```markdown\n' + HEADER + '```\n',
        ]
        for content in examples:
            with self.subTest(content=content):
                self.graph.write_text(content)
                p = self.record()
                self.assertEqual(p.returncode, 3, p.stderr)
                self.assertEqual(self.graph.read_text(), content)
                self.assertIn('row NOT appended', p.stderr)

    def test_real_evidence_section_and_aligned_separator_are_used(self):
        example = '```markdown\n' + HEADER + '```\n'
        actual = HEADER.replace('|---|---|---|---|---|', '|:---|---:|:---:|---|---|')
        self.graph.write_text(example + '\n## Evidence\n' + actual + '\n## Notes\n')
        p = self.record()
        self.assertEqual(p.returncode, 0, p.stderr)
        content = self.graph.read_text()
        self.assertTrue(content.startswith(example))
        self.assertIn('| test | COMPLETE |', content.split('## Evidence', 1)[1])

    def test_compiler_uses_the_same_real_evidence_table_rule(self):
        compiler = SCRIPT.with_name('compile-graph.py')
        ir = {'version': 1, 'campaign': 'evidence preflight', 'treeGuard': False,
              'budget': {'maxNodes': 2},
              'nodes': [{'id': 'read', 'prompt': 'read only', 'contract': 'DesignV1'}]}
        self.graph.write_text('```json graph-ir\n' + json.dumps(ir) + '\n```\n'
                              '## Evidence\n| Wrong | Header |\n|---|---|\n'
                              '## Example\n```markdown\n' + HEADER + '```\n')
        p = subprocess.run([sys.executable, str(compiler), str(self.graph), '--check',
                            '--gates-root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('no recognized Evidence table', p.stderr)

    def test_unknown_executor_is_refused(self):
        p = self.record('--executor', 'imagined-runtime')
        self.assertEqual(p.returncode, 2)


if __name__ == '__main__':
    unittest.main()
