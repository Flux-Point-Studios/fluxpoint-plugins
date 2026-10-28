#!/usr/bin/env python3
"""Counterexamples for corpus policy, project views and warm client isolation."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import recall
import recall_sources as sources
from process_tree import bash_path

class Isolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='recall-isolation-'); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base / 'report-service'; self.root.mkdir()
        self.mem = self.base / 'codex/memories'; (self.mem / 'rollout_summaries').mkdir(parents=True)
        self.cfg = self.base / 'recall.json'; self.cfg.write_text('{}')
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.mem.parent), 'CLAUDE_CONFIG_DIR': str(self.base / 'claude'),
                  'FPL_RECALL_CONFIG': str(self.cfg), 'XDG_CACHE_HOME': str(self.base / 'cache'), 'FPL_EMBEDDER': 'none'})
        env.start(); self.addCleanup(env.stop)

    def test_aggregated_codex_metadata_is_scoped_per_section(self):
        (self.mem / 'raw_memories.md').write_text(f'# Project: private-other\ncwd: {self.base}/other\nthread_id: other\nupdated_at: 2026-01-01T00:00:00Z\n\nOTHER_PROJECT_SECRET\n# Project: report-service\ncwd: {self.root}\nthread_id: report\nupdated_at: 2026-02-02T00:00:00Z\n\nExpected report content\n')
        nodes, _ = sources.notes(self.root)
        self.assertFalse(any('OTHER_PROJECT_SECRET' in n['text'] for n in nodes.values()))
        self.assertTrue(any('Expected report' in n['text'] for n in nodes.values()))
        self.assertEqual({n['session'] for n in nodes.values()}, {'report'})
        self.assertEqual({n['when'] for n in nodes.values()}, {'2026-02-02T00:00:00Z'})

    def test_rollout_body_cannot_override_its_header_scope(self):
        (self.mem / 'rollout_summaries/other.md').write_text(f'thread_id: other\ncwd: {self.base}/private-other\n\n# Other project\nPRIVATE_OTHER\n\nQuoted command example:\ncwd: {self.root}\n')
        self.assertEqual(sources.notes(self.root)[0], {})

    def test_markdown_code_examples_cannot_reset_project_scope(self):
        (self.mem / 'raw_memories.md').write_text(f'# Project: private-other\ncwd: {self.base}/other\n\nAn example document:\n```markdown\n# Project: report-service\nPRIVATE_CODE_EXAMPLE\n```\n')
        self.assertEqual(sources.notes(self.root)[0], {})

    def test_local_cwd_link_cannot_escape_the_project_view(self):
        unrelated = self.base / 'private-other'; unrelated.mkdir()
        try: (self.root / 'linked').symlink_to(unrelated, target_is_directory=True)
        except (OSError, NotImplementedError): self.skipTest('directory symlinks unavailable')
        (self.mem / 'rollout_summaries/link.md').write_text(f'thread_id: other\ncwd: {self.root}/linked\n\n# Private other\nOTHER_PROJECT_SECRET\n')
        self.assertEqual(sources.notes(self.root)[0], {})

    def test_canonical_repository_scope_survives_sibling_worktree_name(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-qm', 'base'], check=True)
        worktree = self.base / 'task-17'
        subprocess.run(['git', '-C', str(self.root), 'worktree', 'add', '-q', '--detach', str(worktree)], check=True)
        (self.mem / 'MEMORY.md').write_text('# Project: report-service\n\nCanonical ordering knowledge\n')
        (self.mem / 'rollout_summaries/canonical.md').write_text(f'thread_id: canonical\ncwd: {self.root}/src\n\n# Ordering\nShared report evidence\n')
        canonical, _ = sources.notes(self.root); sibling, _ = sources.notes(worktree)
        self.assertEqual({n['text'] for n in sibling.values()}, {n['text'] for n in canonical.values()})
        self.assertTrue(sibling)
        self.assertEqual(recall.shared_index_dir(self.root), recall.shared_index_dir(worktree))
        unrelated = self.base / 'other'; unrelated.mkdir()
        self.assertNotEqual(recall.shared_index_dir(self.root), recall.shared_index_dir(unrelated))
        self.assertFalse(sources.notes(unrelated)[0])

    def test_reader_helper_changes_get_an_independent_schema_slot(self):
        alternate = self.base / 'alternate-scripts'; shutil.copytree(SCRIPTS, alternate)
        helper = alternate / 'recall_sources.py'
        helper.write_text(helper.read_text().replace("source = _portable_path(cwd)", "source = _portable_path(cwd)  # reader policy revision"))
        command = [sys.executable, '-c', 'import sys; sys.path.insert(0,sys.argv[1]); import recall; print(recall.INDEX_SCHEMA)']
        old = subprocess.run(command + [str(SCRIPTS)], capture_output=True, text=True, check=True).stdout.strip()
        new = subprocess.run(command + [str(alternate)], capture_output=True, text=True, check=True).stdout.strip()
        self.assertNotEqual(old, new)

    @unittest.skipUnless(shutil.which('node') and shutil.which('bash'), 'warm client requires Node and Bash')
    def test_warm_queries_do_not_cross_default_configuration_roots(self):
        common = dict(os.environ, FPL_MEM_PROMPT='1', FPL_PY=sys.executable,
                      CLAUDE_PROJECT_DIR=str(self.root), FPL_RECALL_DAEMON_DIR=str(self.base / 'daemon'), FPL_RECALL_DAEMON_IDLE='2')
        common.pop('FPL_RECALL_CONFIG', None)
        environments = []
        for label in ('A', 'B'):
            notes = self.base / ('notes-' + label); notes.mkdir()
            for name, body in {'target.md': 'beacon decoder PRIVATE_' + label,
                               'pad1.md': 'mail routing', 'pad2.md': 'payment scheduling'}.items():
                (notes / name).write_text(body)
            cfg = self.base / ('config-' + label) / 'fluxpoint'; cfg.mkdir(parents=True)
            (cfg / 'recall.json').write_text(json.dumps({'corpora': [{'path': str(notes), 'origin': 'local', 'format': 'markdown'}]}))
            env = dict(common, XDG_CONFIG_HOME=str(cfg.parent)); environments.append(env)
            result = subprocess.run([sys.executable, str(SCRIPTS / 'recall.py'), '--root', str(self.root), '--build'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        command = [bash_path(), str(SCRIPTS / 'prompt-recall.sh')]
        payload = json.dumps({'cwd': str(self.root), 'prompt': 'beacon decoder'})
        a = subprocess.run(command, input=payload, env=environments[0], capture_output=True, text=True, timeout=8)
        b = subprocess.run(command, input=payload, env=environments[1], capture_output=True, text=True, timeout=8)
        self.assertIn('PRIVATE_A', a.stdout)
        self.assertIn('PRIVATE_B', b.stdout)
        self.assertNotIn('PRIVATE_A', b.stdout)
        time.sleep(2.3)  # Let the two private idle workers remove their endpoints.

if __name__ == '__main__': unittest.main()
