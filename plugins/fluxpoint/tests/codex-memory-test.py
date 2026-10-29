#!/usr/bin/env python3
"""Codex recall never mixes unrelated project rollouts (#164)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recall_sources as sources


class CodexMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'report-service'
        self.root.mkdir()
        self.mem = self.base / 'codex' / 'memories'
        (self.mem / 'rollout_summaries').mkdir(parents=True)
        self.cfg = self.base / 'recall.json'
        self.cfg.write_text('{}')
        patch = mock.patch.dict(os.environ, {'CODEX_HOME': str(self.mem.parent), 'CLAUDE_CONFIG_DIR': str(self.base / 'claude'), 'FPL_RECALL_CONFIG': str(self.cfg)})
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, file, text):
        (self.mem / file).write_text(text)

    def test_headings_and_rollouts_have_scope_and_provenance(self):
        self.put('MEMORY.md', f'# Project: {self.root}\n## Ordering\n### Export first\nReports wait for export.\n# Project: another\n## Secret\nOther project note.\n')
        self.put('rollout_summaries/one.md', f'thread_id: aaa\nupdated_at: 2026-03-02T10:00:00Z\ncwd: {self.root}\nrollout_path: ignored.jsonl\n\n# Fixed ordering\nWait for the export.\n')
        self.put('rollout_summaries/other.md', 'thread_id: bbb\ncwd: /other/project\n\n# Other thread\nPrivate unrelated note.\n')
        nodes, diagnostics = sources.notes(self.root)
        self.assertEqual(diagnostics, [])
        self.assertTrue(any('Reports wait' in n['text'] for n in nodes.values()))
        self.assertTrue(any('Ordering' in n['name'] and 'Export first' in n['name'] for n in nodes.values()))
        self.assertFalse(any('unrelated' in n['text'] or 'Other project' in n['text'] for n in nodes.values()))
        rollout = next(n for n in nodes.values() if n.get('session') == 'aaa')
        self.assertEqual(rollout['when'], '2026-03-02T10:00:00Z')
        self.assertIn('rollout_summaries/one.md', rollout['file'])

    def test_peer_cwd_alias_and_recursive_fingerprint(self):
        self.cfg.write_text(json.dumps({'corpora': [{'path': str(self.mem), 'format': 'codex-memories', 'origin': 'peer', 'pathAliases': {'/peer/work/report': str(self.root)}}]}))
        self.put('rollout_summaries/peer.md', 'thread_id: peer-one\ncwd: /peer/work/report/subdir\n\n# Peer report\nOrdering evidence.\n')
        before = sources.fingerprints(self.root)
        nodes, _ = sources.notes(self.root)
        self.assertTrue(any(n['session'] == 'peer-one' for n in nodes.values()))
        self.put('rollout_summaries/peer.md', 'thread_id: peer-one\ncwd: /peer/work/report/subdir\n\n# Peer report\nChanged evidence and length.\n')
        self.assertNotEqual(before, sources.fingerprints(self.root))

    def test_unscoped_global_memory_requires_explicit_opt_in(self):
        self.put('memory_summary.md', '# Preferences\nKeep commits small.\n')
        self.put('raw_memories.md', '# Unrelated\nAnother project.\n')
        self.assertEqual(sources.notes(self.root)[0], {})
        self.cfg.write_text(json.dumps({'corpora': [{'path': str(self.mem), 'format': 'codex-memories', 'origin': 'shared', 'allowUnscoped': True}]}))
        nodes, _ = sources.notes(self.root)
        self.assertEqual(len(nodes), 2)


if __name__ == '__main__':
    unittest.main()
