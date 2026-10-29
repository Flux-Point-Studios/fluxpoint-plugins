#!/usr/bin/env python3
"""External corpus caches are shared and schema-isolated (#162)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import recall


class SharedIndexes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.roots = [self.base / 'one', self.base / 'two']
        for root in self.roots:
            root.mkdir()
            (root / 'substrate.json').write_text(json.dumps({'primitives': [{'id': root.name, 'desc': 'local report export primitive', 'status': 'live'}]}))
        self.notes = self.base / 'notes'; self.notes.mkdir()
        (self.notes / 'report.md').write_text('Private nightly export ordering: report waits for csv completion.')
        self.cfg = self.base / 'recall.json'
        self.cfg.write_text(json.dumps({'corpora': [{'path': str(self.notes), 'origin': 'shared', 'format': 'markdown'}]}))
        env = patch.dict(os.environ, {'FPL_RECALL_CONFIG': str(self.cfg), 'XDG_CACHE_HOME': str(self.base / 'cache'), 'CLAUDE_CONFIG_DIR': str(self.base / 'claude'), 'CODEX_HOME': str(self.base / 'codex'), 'FPL_EMBEDDER': 'none'})
        env.start(); self.addCleanup(env.stop)

    def test_shared_slot_is_reused_and_checkout_contains_no_note_text(self):
        one, two = map(str, self.roots)
        recall.build(one)
        slot = Path(recall.shared_index_dir(one))
        self.assertEqual(slot, Path(recall.shared_index_dir(two)))
        before = (slot / recall.GRAPH_F).stat().st_mtime_ns
        with patch.object(recall.recall_sources, 'notes', side_effect=AssertionError('already built corpus was read again')):
            recall.build(two)
        self.assertEqual((slot / recall.GRAPH_F).stat().st_mtime_ns, before)
        for root in self.roots:
            self.assertFalse(any(node['kind'] == 'note' for node in recall._load_json(str(root), recall.GRAPH_F)['nodes'].values()))
            self.assertFalse(any(nid.startswith('note:') for nid in recall._load_json(str(root), recall.LEX_F)['docs']))
            for file in (root / recall.INDEX_DIR).rglob('*'):
                if file.is_file():
                    self.assertNotIn('Private nightly', file.read_text())
            rows, _ = recall.search(str(root), query='report export ordering', offline=True)
            self.assertEqual({r['kind'] for r in rows}, {'primitive', 'note'})

    def test_corpus_changes_refresh_once_and_do_not_stale_repo_slot(self):
        one, two = map(str, self.roots)
        recall.build(one); recall.build(two)
        repo_generation = recall._load_json(one, recall.META_F)['generation']
        (self.notes / 'report.md').write_text('Private nightly export ordering changed to generation two.')
        self.assertTrue(recall.is_stale(one)); self.assertTrue(recall.is_stale(two))
        recall.search(one, query='ordering changed generation', offline=True)
        self.assertFalse(recall.is_stale(two))
        self.assertEqual(recall._load_json(one, recall.META_F)['generation'], repo_generation)

    def test_schema_slots_coexist_and_stats_name_writer(self):
        one = str(self.roots[0])
        recall.build(one)
        local, shared = Path(recall.index_dir(one)), Path(recall.shared_index_dir(one))
        first = (shared / recall.GRAPH_F).read_bytes()
        with patch.object(recall, 'INDEX_SCHEMA', 'another-schema'):
            recall.build(one)
            self.assertNotEqual(Path(recall.index_dir(one)), local)
            self.assertNotEqual(Path(recall.shared_index_dir(one)), shared)
        self.assertEqual((shared / recall.GRAPH_F).read_bytes(), first)
        result = subprocess.run([sys.executable, str(SCRIPTS / 'recall.py'), '--root', one, '--stats'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(shared), result.stdout)
        self.assertIn('writer', result.stdout)

    def test_shared_generation_mismatch_never_ranks_mixed_cache(self):
        one = str(self.roots[0]); recall.build(one)
        path = Path(recall.shared_index_dir(one)) / recall.LEX_F
        lex = json.loads(path.read_text()); lex['generation'] = 'wrong'; path.write_text(json.dumps(lex))
        rows, diagnostics = recall.search(one, query='nightly export', offline=True, allow_rebuild=False)
        self.assertFalse(rows)
        self.assertTrue(any('generation' in d for d in diagnostics))

    def test_codex_project_views_do_not_share_private_passages(self):
        rollouts = self.notes / 'rollout_summaries'; rollouts.mkdir()
        for root in self.roots:
            (rollouts / (root.name + '.md')).write_text(f'thread_id: {root.name}\ncwd: {root}\n\n# Deployment\nPrivate {root.name} deployment instructions.')
        self.cfg.write_text(json.dumps({'corpora': [{'path': str(self.notes), 'origin': 'codex', 'format': 'codex-memories'}]}))
        one, two = map(str, self.roots)
        recall.build(one); recall.build(two)
        self.assertNotEqual(recall.shared_index_dir(one), recall.shared_index_dir(two))
        for root in self.roots:
            graph, _, _ = recall.ensure_index(str(root), allow_rebuild=False)
            sessions = {node.get('session') for node in graph['nodes'].values() if node['kind'] == 'note'}
            self.assertEqual(sessions, {root.name})


if __name__ == '__main__':
    unittest.main()
