#!/usr/bin/env python3
"""Recall maintenance regressions with real corpora and hook entry points."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import recall

class Maintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='recall-maint-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'app'
        self.root.mkdir()
        self.cfg = Path(self.tmp.name) / 'config'
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.cfg),
                             'FPL_RECALL_CONFIG': str(Path(self.tmp.name) / 'missing.json'),
                             'FPL_EMBEDDER': 'none'}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)

    def primitive(self):
        (self.root / 'substrate.json').write_text(json.dumps({'primitives': [
            {'id': 'report-job', 'desc': 'nightly report export ordering', 'status': 'live'}]}))

    def note(self, base=None, filename='report.md', body=None):
        base = base or self.root
        folder = self.cfg / 'projects' / __import__('re').sub('[^A-Za-z0-9]', '-', str(base)) / 'memory'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / filename).write_text(body or '---\nname: report-order\ndescription: nightly report ordering\n---\n\nNightly reports wait for the csv export completion.\n', encoding='utf-8')
        return folder

    def test_non_git_session_hook_reports_recall(self):
        self.primitive()
        p = subprocess.run(['bash', str(SCRIPTS / 'inject-state.sh')], input=json.dumps({'cwd': str(self.root), 'session_id': 's1'}),
             text=True, capture_output=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root), FPL_RECALL_REFRESH='0'))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('FluxPoint recall', p.stdout)

    def test_query_never_backfills_documents(self):
        self.primitive()
        with patch.object(recall, '_sync_vectors', return_value={'pending': 0, 'provider': 'none'}) as sync:
            recall.search(str(self.root), query='nightly report', offline=False)
        self.assertFalse(sync.call_args.args[2])

    def test_independent_words_floor(self):
        lex = {'N': 6, 'df': {'dataloader': 1, 'data': 1, 'loader': 1, 'which': 1, 'login': 1},
               'docs': {'x': {'tf': {'dataloader': 1, 'data': 1, 'loader': 1, 'which': 1, 'login': 1}}}}
        self.assertEqual(recall._informative_overlap(lex, 'DataLoader', 'x'), 1)
        self.assertEqual(recall._informative_overlap(lex, 'which login', 'x'), 1)

    def test_ancestor_notes_survive_empty_nearest_folder(self):
        self.note(self.root.parent)
        near = self.note(); (near / 'report.md').unlink()
        recall.build(str(self.root))
        rows, _ = recall.search(str(self.root), query='nightly csv export', offline=True)
        self.assertEqual(rows[0]['kind'], 'note')
        self.assertIn('csv export', rows[0]['text'])
        (near / 'second.md').write_text('A separate corpus note about csv export ordering.')
        self.assertTrue(recall.is_stale(str(self.root)))

    def test_peer_roots_namespace_and_file_cap(self):
        local = self.note()
        peer = Path(self.tmp.name) / 'peer'; peer.mkdir()
        (peer / 'report.md').write_text('Peer nightly report export ordering.\n\nPeer report export completion.')
        cfg = Path(self.tmp.name) / 'recall.json'
        cfg.write_text(json.dumps({'corpora': [{'path': str(peer), 'origin': 'host-b', 'format': 'claude-automemory'}]}))
        with patch.dict(os.environ, {'FPL_RECALL_CONFIG': str(cfg)}):
            recall.build(str(self.root))
            rows, _ = recall.search(str(self.root), query='nightly report export', offline=True, k=10)
        notes = [r for r in rows if r['kind'] == 'note']
        self.assertEqual(len(notes), 2)
        self.assertEqual(len({r['id'] for r in notes}), 2)
        self.assertIn('host-b', {r['origin'] for r in notes})

    def test_note_metadata_supersession_and_render(self):
        self.note(filename='old.md', body='---\nname: old\nmetadata:\n  modified: 2026-01-05\n  supersededBy: current\n---\n\nStaging deploy target old port 8080.\n')
        self.note(filename='new.md', body='---\nname: current\nmetadata:\n  modified: 2026-03-02\n  originSessionId: session-7\n---\n\nStaging deploy target new port 9090.\n')
        recall.build(str(self.root))
        rows, _ = recall.search(str(self.root), query='staging deploy target', offline=True)
        self.assertEqual(len(rows), 1)
        self.assertIn('9090', rows[0]['text'])
        line = recall.render_lines(rows, [])
        self.assertIn('2026-03-02', line); self.assertIn('session-7', line)

    def test_query_environment_and_brief(self):
        with patch.dict(os.environ, {'FPL_RECALL_QUERY': 'fix report export ordering'}):
            self.assertEqual(recall._work_query(str(self.root)), 'fix report export ordering')
        brief = self.root / 'brief.md'; brief.write_text('---\nsecret: omitted\n---\n\nRepair nightly report export.')
        with patch.dict(os.environ, {'FPL_RECALL_QUERY_FILE': str(brief)}):
            self.assertEqual(recall._work_query(str(self.root)), 'Repair nightly report export.')

    def test_detached_refresh_builds_once_and_releases_lock(self):
        self.primitive()
        self.assertTrue(recall.request_refresh(str(self.root)))
        for _ in range(100):
            if not recall.is_stale(str(self.root)): break
            time.sleep(.05)
        self.assertFalse(recall.is_stale(str(self.root)))
        self.assertFalse((self.root / recall.INDEX_DIR / 'refresh.lock').exists())
        self.assertFalse(recall.request_refresh(str(self.root)))

    def test_empty_root_does_not_spawn_or_create_index(self):
        self.assertFalse(recall.request_refresh(str(self.root)))
        self.assertFalse((self.root / recall.INDEX_DIR).exists())

    def test_generation_mismatch_never_ranks_mixed_files(self):
        self.primitive(); recall.build(str(self.root))
        idx = self.root / recall.INDEX_DIR
        lex = json.loads((idx / recall.LEX_F).read_text()); lex['generation'] = 'wrong'
        (idx / recall.LEX_F).write_text(json.dumps(lex))
        rows, diagnostics = recall.search(str(self.root), query='report export', offline=True, allow_rebuild=False)
        self.assertFalse(rows)
        self.assertTrue(any('generation' in d for d in diagnostics))

    def test_notes_need_explicit_embedding_consent(self):
        self.note()
        spec = {'provider': 'openai', 'model': 'fixture', 'dims': 2}
        with patch.object(recall.embedder, 'resolve', return_value=spec), patch.object(recall.embedder, 'embed', return_value=[[1., 0.]]) as embed:
            recall.build(str(self.root), embed_backfill=True)
            embed.assert_not_called()
        with patch.dict(os.environ, {'FPL_EMBEDDER': 'openai'}), patch.object(recall.embedder, 'resolve', return_value=spec), patch.object(recall.embedder, 'embed', side_effect=lambda texts, *a, **k: [[1., 0.] for text in texts]) as embed:
            recall.build(str(self.root), embed_backfill=True)
            self.assertTrue(embed.called)

    def test_refresh_checks_presence_without_reading_note_bodies(self):
        self.note()
        with patch.object(recall.recall_sources, 'notes', side_effect=AssertionError('foreground read')):
            self.assertTrue(recall.request_refresh(str(self.root)))
        for child in recall._REFRESH_CHILDREN:
            child.wait(timeout=15)

    def test_legacy_source_edit_invalidates_shared_store_index(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        legacy = self.root / '.claude' / 'fluxpoint' / 'memory.jsonl'
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({'tag': 'x', 'dedupeKey': 'report', 'claim': 'report export ordering', 'establishedWhen': '2026-01-01T00:00:00Z'}) + '\n')
        recall.build(str(self.root))
        legacy.write_text(legacy.read_text().replace('ordering', 'completion'))
        self.assertTrue(recall.is_stale(str(self.root)))

    def test_concurrent_builds_serialize_complete_generations(self):
        self.primitive()
        release = Path(self.tmp.name) / 'release'
        driver = """import os, sys, time
sys.path.insert(0, sys.argv[1]); import recall
original = recall._atomic_json
first = True
def publish(path, value):
 global first
 original(path, value)
 if first:
  first = False
  print('publishing', flush=True)
  deadline = time.monotonic() + 10
  while not os.path.exists(sys.argv[3]):
   if time.monotonic() > deadline: raise RuntimeError('release missing')
   time.sleep(.02)
recall._atomic_json = publish
recall.build(sys.argv[2])
"""
        first = subprocess.Popen([sys.executable, '-c', driver, str(SCRIPTS), str(self.root), str(release)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: first.kill() if first.poll() is None else None)
        self.assertEqual(first.stdout.readline().strip(), 'publishing')
        (self.root / 'substrate.json').write_text(json.dumps({'primitives': [{'id': 'fresh', 'desc': 'newest report export'}]}))
        second = subprocess.Popen([sys.executable, str(SCRIPTS / 'recall.py'), '--root', str(self.root), '--build'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: second.kill() if second.poll() is None else None)
        with self.assertRaises(subprocess.TimeoutExpired): second.wait(timeout=.2)
        release.touch()
        self.assertEqual(first.wait(timeout=10), 0, first.stderr.read())
        self.assertEqual(second.wait(timeout=10), 0, second.stderr.read())
        self.assertFalse(recall.is_stale(str(self.root)))
        graph, lex, _ = recall.ensure_index(str(self.root), allow_rebuild=False)
        self.assertIn('prim:fresh', graph['nodes'])
        self.assertEqual(graph['generation'], lex['generation'])
        first.stdout.close(); first.stderr.close(); second.stdout.close(); second.stderr.close()

if __name__ == '__main__': unittest.main()
