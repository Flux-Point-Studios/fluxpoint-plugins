#!/usr/bin/env python3
"""Exercise recorder discovery and distinguish an artifact from an Evidence row."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/record-run.py'
# Every recorded run is also kept in the clone-wide store, and a run's vetoes
# are read from there. A memory home shared by the whole machine would carry
# one case's run into the next, since the cases reuse their run ids.
os.environ.pop('FPL_MEMORY_HOME', None)
HEADER = '| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n'
COVERAGE = {'subject': [{'repository': 'repo', 'base': 'a' * 40, 'head': 'b' * 40,
                         'files': 2, 'additions': 10, 'deletions': 3}],
            'attackers': 6, 'lenses': ['authority', 'composition'], 'sampled': False}
PLAIN = {'outcome': 'COMPLETE', 'results': {}}


def verdict(value, **extra):
    """A run summary whose one red-team node returned `value` coherently."""
    result = {'verdict': value, 'findings': [], 'worstSeverity': 'NONE', 'worstSeverityRank': 0}
    if value == 'SHIP':
        result['coverage'] = COVERAGE
    summary = {'campaign': 'c', 'outcome': 'COMPLETE', 'contracts': {'rt': 'RedTeamV1'},
               'results': {'rt': result}, 'provenance': [{'node': 'rt', 'status': 'OK'}]}
    summary.update(extra)
    return summary


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


class PostRunVetoTests(unittest.TestCase):
    """A check that runs after the run reaches the record as a veto or not at all.

    `--red-team BLOCK` over a summary that holds a SHIP was dropped without a
    word, so a run its own post-run certifier refused was filed as shipped.
    The flag still never overrides a derived verdict; it now says so. The
    veto is the logged way down, and it only goes down.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.graph = self.root / 'WORK.md'
        self.graph.write_text(HEADER, encoding='utf-8')
        self.log = self.root / 'certifier.log'
        self.log.write_bytes(b'check_reruns: sibling coverage is not certified\nexit 1\n')
        self.store = self.root / '.claude/fluxpoint/runs'

    def record(self, summary, *flags, run_id='vetoed'):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root),
                               '--graph', str(self.graph), '--run-id', run_id, *flags],
                              input=json.dumps(summary), cwd=self.root, capture_output=True, text=True,
                              env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)

    def veto(self, summary, *flags, reason='certifier exit 1', evidence=None, run_id='vetoed'):
        return self.record(summary, '--veto', reason, '--veto-evidence', str(evidence or self.log),
                           *flags, run_id=run_id)

    def artifact(self, run_id='vetoed'):
        return json.loads((self.store / (run_id + '.json')).read_text(encoding='utf-8'))

    def rows(self):
        """Evidence rows, newest first: the recorder inserts below the separator."""
        return [line for line in self.graph.read_text(encoding='utf-8').splitlines()[2:] if line.strip()]

    def refused(self, p, code):
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn(code, p.stderr)
        self.assertFalse((self.root / '.claude').exists(), 'a refused veto left state behind')
        self.assertEqual(self.rows(), [])

    # ---- the flag: ignored as before, and no longer silently --------------

    def test_red_team_flag_over_a_derived_verdict_is_named_as_ignored(self):
        p = self.record(verdict('SHIP'), '--red-team', 'BLOCK')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.artifact()['redTeam'], 'SHIP')
        self.assertIn('--red-team BLOCK ignored', p.stderr)
        self.assertIn('SHIP', p.stderr.split('--red-team BLOCK ignored', 1)[1])
        self.assertIn('--veto', p.stderr)

    def test_red_team_flag_is_still_the_fallback_without_a_derived_verdict(self):
        p = self.record(PLAIN, '--red-team', 'BLOCK')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.artifact()['redTeam'], 'BLOCK')
        self.assertNotIn('ignored', p.stderr)

    def test_help_states_the_precedence_and_the_veto(self):
        p = subprocess.run([sys.executable, str(SCRIPT), '--help'], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        # argparse wraps on spaces and hyphens; undo both before matching.
        text = re.sub(r'(?<=\w)- (?=\w)', '-', ' '.join(p.stdout.split()))
        self.assertIn('--veto REASON', text)
        self.assertIn('--veto-evidence FILE', text)
        self.assertIn('ignored whenever the summary carries a red-team verdict', text)
        self.assertIn('prove: node', text)

    # ---- a veto that cannot be accounted for records nothing --------------

    def test_veto_without_evidence_is_refused_and_nothing_is_recorded(self):
        self.refused(self.record(verdict('SHIP'), '--veto', 'certifier exit 1'), 'VETO-EVIDENCE-REQUIRED')

    def test_evidence_without_a_reason_is_refused(self):
        self.refused(self.record(verdict('SHIP'), '--veto-evidence', str(self.log)), 'VETO-REASON-REQUIRED')
        self.refused(self.veto(verdict('SHIP'), reason=' \t '), 'VETO-REASON-REQUIRED')

    def test_evidence_that_is_not_a_readable_file_is_refused(self):
        self.refused(self.veto(verdict('SHIP'), evidence=self.root / 'absent.log'), 'VETO-EVIDENCE-MISSING')
        self.refused(self.veto(verdict('SHIP'), evidence=self.root), 'VETO-EVIDENCE-MISSING')

    def test_empty_evidence_is_refused(self):
        for content in (b'', b' \r\n\t\n'):
            with self.subTest(content=content):
                self.log.write_bytes(content)
                self.refused(self.veto(verdict('SHIP')), 'VETO-EVIDENCE-EMPTY')

    def test_a_refused_veto_still_ledgers_an_effect_that_already_happened(self):
        # The once-only ledger records what happened in the world, whatever
        # the record says, so it is written before a veto can be refused.
        summary = verdict('SHIP', ledger=[{'key': 'k1', 'campaign': 'c', 'node': 'mint',
                                           'result': {'exit': 0, 'txHash': 'tx_1'}}])
        p = self.record(summary, '--veto', 'certifier exit 1')
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn('VETO-EVIDENCE-REQUIRED', p.stderr)
        self.assertIn('"k1"', (self.root / '.claude/fluxpoint/irreversible.jsonl').read_text(encoding='utf-8'))
        self.assertFalse(self.store.exists())
        self.assertEqual(self.rows(), [])

    def test_a_veto_is_not_dropped_by_recording_from_another_directory(self):
        # The run store sits under --root, which defaults to the directory the
        # recorder is started in. Started somewhere else, it found no veto and
        # filed SHIP into the same Evidence table.
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        nested = self.root / 'nested'
        nested.mkdir(exist_ok=True)
        elsewhere = subprocess.run([sys.executable, str(SCRIPT), '--graph', str(self.graph), '--run-id', 'vetoed'],
                                   input=json.dumps(verdict('SHIP')), cwd=nested, capture_output=True, text=True,
                                   env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)
        self.assertEqual(elsewhere.returncode, 2, elsewhere.stdout + elsewhere.stderr)
        self.assertIn('--root', elsewhere.stderr)
        self.assertFalse((nested / '.claude').exists())
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn('| vetoed | BLOCKED-REDTEAM |', rows[0])
        other = self.root.parent / (self.root.name + '-other')
        other.mkdir()
        self.addCleanup(lambda: __import__('shutil').rmtree(other, ignore_errors=True))
        moved = subprocess.run([sys.executable, str(SCRIPT), '--root', str(other), '--graph', str(self.graph), '--run-id', 'vetoed'],
                               input=json.dumps(verdict('SHIP')), cwd=self.root, capture_output=True, text=True,
                               env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)
        self.assertEqual(moved.returncode, 2, moved.stdout + moved.stderr)
        self.assertFalse((other / '.claude').exists())
        self.assertEqual(self.rows(), rows)
        # The Evidence file is held to the same rule as the graph.
        graph = other / 'GRAPH.x.md'
        graph.write_text(HEADER, encoding='utf-8')
        split = subprocess.run([sys.executable, str(SCRIPT), '--root', str(other), '--graph', str(graph),
                                '--evidence', str(self.graph), '--run-id', 'vetoed'],
                               input=json.dumps(verdict('SHIP')), cwd=other, capture_output=True, text=True,
                               env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)
        self.assertEqual(split.returncode, 2, split.stdout + split.stderr)
        self.assertIn('--evidence', split.stderr)
        self.assertEqual(self.rows(), rows)

    def test_a_root_reached_through_a_link_still_holds_its_files(self):
        # The refusal is about where the files are, whichever way they are spelled.
        link = self.root.parent / (self.root.name + '-link')
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('this platform cannot create a directory link')
        self.addCleanup(link.unlink)
        through = subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root),
                                  '--graph', str(link / 'WORK.md'), '--run-id', 'linked'],
                                 input=json.dumps(verdict('SHIP')), cwd=self.root, capture_output=True, text=True,
                                 env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)
        self.assertEqual(through.returncode, 0, through.stderr)
        # A graph that is itself a link to a file kept elsewhere is where that
        # file is. Its rows land outside the root, in a table another root's
        # store may already have recorded into.
        kept = self.root / 'kept-elsewhere.md'
        target = self.root.parent / (self.root.name + '-kept.md')
        target.write_text(HEADER, encoding='utf-8')
        self.addCleanup(lambda: target.unlink() if target.exists() else None)
        kept.symlink_to(target)
        linked = subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root),
                                 '--graph', str(kept), '--run-id', 'kept'],
                                input=json.dumps(verdict('SHIP')), cwd=self.root, capture_output=True, text=True,
                                env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)
        self.assertEqual(linked.returncode, 2, linked.stdout + linked.stderr)
        self.assertIn(str(target.resolve()), linked.stderr)
        # The way out is to keep the file in the project. A root that holds
        # the real file would file the run away from the project's own state.
        self.assertIn('inside the project', linked.stderr)
        self.assertNotIn('pass --root', linked.stderr)
        self.assertEqual(target.read_text(encoding='utf-8'), HEADER)
        self.assertFalse((self.store / 'kept.json').exists())

    def nested(self):
        """outer/project/camp/WORK.md, and a recorder for it under any root and directory."""
        outer = self.root / 'outer'
        project = outer / 'project'
        camp = project / 'camp'
        camp.mkdir(parents=True)
        graph = camp / 'WORK.md'
        graph.write_text(HEADER, encoding='utf-8')

        def record(root, cwd, *flags, run_id='vetoed', summary=None):
            return subprocess.run([sys.executable, str(SCRIPT), *(['--root', str(root)] if root else []),
                                   '--graph', str(graph), '--run-id', run_id, *flags],
                                  input=json.dumps(summary or verdict('SHIP')), cwd=cwd, capture_output=True,
                                  text=True, env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)

        def rows():
            return [line for line in graph.read_text(encoding='utf-8').splitlines()[2:] if line.strip()]

        return outer, project, camp, record, rows

    def test_a_veto_is_found_from_a_root_above_or_below_the_project(self):
        # The graph sits under each of these roots, so each is a root a record
        # may be given, and each has a store of its own that holds nothing.
        # A run's vetoes are read from every store above its Evidence file.
        outer, project, camp, record, rows = self.nested()
        first = record(project, project, '--veto', 'certifier exit 1', '--veto-evidence', str(self.log))
        self.assertEqual(first.returncode, 0, first.stderr)
        for label, root, cwd in (('a root above', outer, project), ('the same root, spelled ..', '..', project),
                                 ('a root below', None, camp)):
            with self.subTest(label):
                again = record(root, cwd)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                self.assertIn('still applies', again.stderr)
                self.assertEqual([row for row in rows() if '| vetoed | BLOCKED-REDTEAM |' not in row], [])
        for store in (outer, camp):
            art = json.loads((store / '.claude/fluxpoint/runs/vetoed.json').read_text(encoding='utf-8'))
            self.assertEqual((art['outcome'], art['redTeam'], len(art['vetoes'])), ('BLOCKED-REDTEAM', 'BLOCK', 1))

    def test_a_veto_whose_record_was_refused_is_found_from_another_root(self):
        outer, project, camp, record, rows = self.nested()
        twice = verdict('SHIP', waits=[{'node': 'sign', 'check': 'true', 'everyMinutes': 5},
                                       {'node': 'sign', 'check': 'true', 'everyMinutes': 5}])
        refused = record(project, project, '--veto', 'certifier exit 1', '--veto-evidence', str(self.log), summary=twice)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(rows(), [])
        plain = record(outer, project)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertIn('still applies', plain.stderr)
        self.assertEqual(len(rows()), 1)
        self.assertIn('| vetoed | BLOCKED-REDTEAM |', rows()[0])

    def test_a_veto_is_found_from_a_linked_worktree_of_the_same_clone(self):
        # A campaign branch is often checked out twice. The second checkout has
        # its own store and the same WORK.md, and the row filed there is merged
        # like any other. Every recorded run is also kept clone-wide.
        project = self.root / 'clone'
        project.mkdir()

        def git(*argv, cwd=project):
            return subprocess.run(['git', *argv], cwd=cwd, check=True, capture_output=True, text=True)

        git('init', '-q')
        git('config', 'user.name', 'fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (project / '.gitignore').write_text('.claude/\n', encoding='utf-8')
        (project / 'WORK.md').write_text(HEADER, encoding='utf-8')
        git('add', '.')
        git('commit', '-qm', 'base')
        twin = self.root / 'twin'
        git('worktree', 'add', '-q', str(twin), '-b', 'twin')

        def record(tree, *flags, summary=None, run_id='vetoed'):
            return subprocess.run([sys.executable, str(SCRIPT), '--root', str(tree), '--graph', str(tree / 'WORK.md'),
                                   '--run-id', run_id, *flags], input=json.dumps(summary or verdict('SHIP')), cwd=tree,
                                  capture_output=True, text=True, env=dict(os.environ, FPL_MEMORY_INDEX='0'),
                                  timeout=30)

        first = record(project, '--veto', 'certifier exit 1', '--veto-evidence', str(self.log))
        self.assertEqual(first.returncode, 0, first.stderr)
        again = record(twin)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn('still applies', again.stderr)
        self.assertIn('| vetoed | BLOCKED-REDTEAM |', (twin / 'WORK.md').read_text(encoding='utf-8'))
        self.assertNotIn('| vetoed | COMPLETE |', (twin / 'WORK.md').read_text(encoding='utf-8'))
        # The same holds for a veto whose own record was refused.
        twice = verdict('SHIP', waits=[{'node': 'sign', 'check': 'true', 'everyMinutes': 5},
                                       {'node': 'sign', 'check': 'true', 'everyMinutes': 5}])
        refused = record(project, '--veto', 'certifier exit 1', '--veto-evidence', str(self.log), summary=twice,
                         run_id='refused')
        self.assertEqual(refused.returncode, 2, refused.stderr)
        later = record(twin, run_id='refused')
        self.assertEqual(later.returncode, 0, later.stdout + later.stderr)
        self.assertIn('| refused | BLOCKED-REDTEAM |', (twin / 'WORK.md').read_text(encoding='utf-8'))

    def test_a_root_that_does_not_hold_the_graph_is_refused_in_a_clone_as_anywhere(self):
        # Inside a clone every directory shares the clone-wide store, and that
        # store moves with FPL_MEMORY_HOME. It is no reason to accept a root
        # that is not above the graph: a subdirectory, or the main checkout
        # for a graph in a linked worktree. Nothing is written, the once-only
        # ledger included.
        project = self.root / 'clone'
        (project / 'x').mkdir(parents=True)

        def git(*argv):
            return subprocess.run(['git', *argv], cwd=project, check=True, capture_output=True, text=True)

        git('init', '-q')
        git('config', 'user.name', 'fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (project / '.gitignore').write_text('.claude/\n', encoding='utf-8')
        (project / 'WORK.md').write_text(HEADER, encoding='utf-8')
        git('add', '.')
        git('commit', '-qm', 'base')
        twin = self.root / 'twin'
        git('worktree', 'add', '-q', str(twin), '-b', 'twin')
        summary = verdict('SHIP', waits=[{'node': 'sign', 'check': 'true', 'everyMinutes': 5}])

        def record(root, graph, cwd):
            return subprocess.run([sys.executable, str(SCRIPT), *(['--root', str(root)] if root else []),
                                   '--graph', str(graph), '--run-id', 'vetoed'], input=json.dumps(summary), cwd=cwd,
                                  capture_output=True, text=True, env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)

        below = record(None, '../WORK.md', project / 'x')
        self.assertEqual(below.returncode, 2, below.stdout + below.stderr)
        self.assertIn('--root', below.stderr)
        self.assertFalse((project / 'x' / '.claude').exists())
        beside = record(project, twin / 'WORK.md', twin)
        self.assertEqual(beside.returncode, 2, beside.stdout + beside.stderr)
        self.assertFalse((project / '.claude').exists())
        self.assertFalse((project / '.git' / 'fluxpoint').exists())
        for checkout in (project, twin):
            self.assertEqual((checkout / 'WORK.md').read_text(encoding='utf-8'), HEADER)
        # The same graph under a root that holds it is recorded as ever.
        held = record(twin, twin / 'WORK.md', twin)
        self.assertEqual(held.returncode, 0, held.stdout + held.stderr)

    def test_vetoes_given_at_the_same_moment_are_all_kept(self):
        # Each recorder read the journal, added its veto and replaced the file,
        # so two at once could leave one. The records are refused on purpose:
        # the journal is then the only place their vetoes are.
        twice = verdict('SHIP', waits=[{'node': 'sign', 'check': 'true', 'everyMinutes': 5},
                                       {'node': 'sign', 'check': 'true', 'everyMinutes': 5}])
        reasons = [f'check {n} refused' for n in range(8)]
        started = []
        for n, reason in enumerate(reasons):
            log = self.root / f'check-{n}.log'
            log.write_text(reason + '\n', encoding='utf-8')
            started.append(subprocess.Popen(
                [sys.executable, str(SCRIPT), '--root', str(self.root), '--graph', str(self.graph), '--run-id', 'vetoed',
                 '--veto', reason, '--veto-evidence', str(log)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=self.root, env=dict(os.environ, FPL_MEMORY_INDEX='0')))
        for proc in started:
            proc.stdin.write(json.dumps(twice))
            proc.stdin.close()
        for proc in started:
            self.assertEqual(proc.wait(timeout=120), 2, proc.stderr.read())
            proc.stdout.close()
            proc.stderr.close()
        plain = self.record(verdict('SHIP'))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(sorted(v['reason'] for v in self.artifact()['vetoes']), reasons)

    def test_a_directory_that_only_links_to_the_graph_is_not_its_root(self):
        # An empty directory holding a link to the project's WORK.md, used as
        # the root: the row landed in the project's table from a store that
        # had never seen the run.
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        aside = self.root.parent / (self.root.name + '-aside')
        aside.mkdir()
        self.addCleanup(lambda: __import__('shutil').rmtree(aside, ignore_errors=True))
        try:
            (aside / 'WORK.md').symlink_to(self.graph)
        except (OSError, NotImplementedError):
            self.skipTest('this platform cannot create a link')
        before = self.rows()
        linked = subprocess.run([sys.executable, str(SCRIPT), '--graph', 'WORK.md', '--run-id', 'vetoed'],
                                input=json.dumps(verdict('SHIP')), cwd=aside, capture_output=True, text=True,
                                env=dict(os.environ, FPL_MEMORY_INDEX='0'), timeout=30)
        self.assertEqual(linked.returncode, 2, linked.stdout + linked.stderr)
        self.assertIn('--root', linked.stderr)
        self.assertFalse((aside / '.claude').exists())
        self.assertEqual(self.rows(), before)

    def test_a_veto_outlives_a_record_that_is_refused_for_another_reason(self):
        # Two waits on one node refuse the record after the veto was read. The
        # veto used to go with it, and the next plain record was filed SHIP.
        twice = verdict('SHIP', waits=[{'node': 'sign', 'check': 'true', 'everyMinutes': 5},
                                       {'node': 'sign', 'check': 'true', 'everyMinutes': 5}])
        refused = self.veto(twice)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertFalse((self.store / 'vetoed.json').exists())
        plain = self.record(verdict('SHIP'))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        art = json.loads((self.store / 'vetoed.json').read_text())
        self.assertEqual((art['outcome'], art['redTeam']), ('BLOCKED-REDTEAM', 'BLOCK'))
        self.assertEqual(len(art['vetoes']), 1)
        self.assertIn('still applies', plain.stderr)

    def test_evidence_is_never_filed_over_other_bytes_or_through_a_link(self):
        digest = hashlib.sha256(self.log.read_bytes()).hexdigest()
        kept = self.store / 'evidence' / digest
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b'something else under the same name')
        p = self.veto(verdict('SHIP'))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn('VETO-EVIDENCE-UNFILED', p.stderr)
        self.assertFalse((self.store / 'vetoed.json').exists())
        self.assertEqual(self.rows(), [])
        kept.unlink()
        try:
            os.symlink(str(self.log), str(kept))
        except (OSError, NotImplementedError):
            return  # this account may not create links (Windows without the privilege)
        p = self.veto(verdict('SHIP'))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn('VETO-EVIDENCE-UNFILED', p.stderr)
        self.assertFalse((self.store / 'vetoed.json').exists())

    # ---- a veto lowers, and only lowers ------------------------------------

    def test_veto_lowers_a_derived_ship_and_files_the_evidence(self):
        data = self.log.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        p = self.veto(verdict('SHIP'))
        self.assertEqual(p.returncode, 0, p.stderr)
        art = self.artifact()
        self.assertEqual((art['redTeam'], art['redTeamDerived'], art['outcome']),
                         ('BLOCK', 'SHIP', 'BLOCKED-REDTEAM'))
        (filed,) = art['vetoes']
        self.assertEqual((filed['reason'], filed['over']), ('certifier exit 1', 'SHIP'))
        self.assertEqual(filed['evidence']['sha256'], digest)
        self.assertEqual(filed['evidence']['bytes'], len(data))
        # The bytes sit in the store the recorder already keeps repair evidence in.
        self.assertEqual((self.store / 'evidence' / digest).read_bytes(), data)
        # What the run itself derived is still there to read.
        self.assertEqual(art['summary']['results']['rt']['verdict'], 'SHIP')
        self.assertEqual(art['redTeamCoverage'][0]['verdict'], 'SHIP')
        (row,) = self.rows()
        self.assertIn('| vetoed | BLOCKED-REDTEAM |', row)
        self.assertIn('red-team BLOCK;', row)
        self.assertNotIn('red-team SHIP;', row)
        self.assertIn('veto lowered red-team SHIP to BLOCK', row)
        self.assertIn('certifier exit 1', row)
        self.assertIn(digest, row)

    def test_the_legacy_row_carries_the_vetoed_verdict_too(self):
        self.graph.write_text('| When (UTC) | runId | Outcome | Nodes OK/dead | Findings | Harness | Red-team |\n'
                              '|---|---|---|---|---|---|---|\n', encoding='utf-8')
        p = self.veto(verdict('SHIP'))
        self.assertEqual(p.returncode, 0, p.stderr)
        (row,) = self.rows()
        self.assertIn('| vetoed | BLOCKED-REDTEAM |', row)
        self.assertTrue(row.rstrip().endswith('| BLOCK |'), row)

    def test_veto_over_a_derived_block_changes_no_verdict(self):
        self.assertEqual(self.record(verdict('BLOCK'), run_id='unvetoed').returncode, 0)
        p = self.veto(verdict('BLOCK'))
        self.assertEqual(p.returncode, 0, p.stderr)
        plain, art = self.artifact('unvetoed'), self.artifact()
        self.assertEqual((art['redTeam'], art['outcome']), (plain['redTeam'], plain['outcome']))
        self.assertEqual((art['redTeam'], art['redTeamDerived']), ('BLOCK', 'BLOCK'))
        self.assertEqual(art['vetoes'][0]['over'], 'BLOCK')
        self.assertIn('| vetoed |', self.rows()[0])
        self.assertNotIn('lowered', self.rows()[0])
        self.assertIn('certifier exit 1', self.rows()[0])

    def test_a_veto_never_produces_ship(self):
        unbacked = verdict('SHIP')
        del unbacked['results']['rt']['coverage']  # a SHIP the recorder refuses on its own
        cases = (('ship', verdict('SHIP')), ('block', verdict('BLOCK')), ('no-verdict', PLAIN),
                 ('unbacked-ship', unbacked), ('halted', verdict('SHIP', outcome='HALTED')))
        number = 0
        for name, summary in cases:
            for flag in (None, 'SHIP', 'BLOCK', 'n/a'):
                number += 1
                run_id = 'never-%d' % number
                with self.subTest(summary=name, flag=flag):
                    p = self.veto(summary, *(('--red-team', flag) if flag else ()), run_id=run_id)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    art = self.artifact(run_id)
                    self.assertEqual(art['redTeam'], 'BLOCK')
                    self.assertNotIn(art['outcome'], ('COMPLETE', 'UNKNOWN'))
                    row = self.rows()[0]
                    self.assertIn('| %s |' % run_id, row)
                    self.assertIn('red-team BLOCK;', row)
                    self.assertNotIn('| COMPLETE |', row)

    # ---- recording the run again -------------------------------------------

    def test_a_second_record_without_the_flag_keeps_the_filed_veto(self):
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        filed = self.artifact()['vetoes']
        p = self.record(verdict('SHIP'))
        self.assertEqual(p.returncode, 0, p.stderr)
        art = self.artifact()
        self.assertEqual((art['redTeam'], art['redTeamDerived'], art['outcome']),
                         ('BLOCK', 'SHIP', 'BLOCKED-REDTEAM'))
        self.assertEqual(art['vetoes'], filed)
        self.assertIn('still applies', p.stderr)
        self.assertIn('certifier exit 1', p.stderr)
        self.assertEqual(len(self.rows()), 2)
        for row in self.rows():
            self.assertIn('| vetoed | BLOCKED-REDTEAM |', row)
            self.assertIn('red-team BLOCK;', row)

    def test_the_veto_stays_on_the_run_id_when_the_summary_changes(self):
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        p = self.record(verdict('SHIP', campaign='the same run, resumed'), '--red-team', 'SHIP')
        self.assertEqual(p.returncode, 0, p.stderr)
        art = self.artifact()
        self.assertEqual((art['redTeam'], art['outcome']), ('BLOCK', 'BLOCKED-REDTEAM'))
        self.assertEqual(len(art['vetoes']), 1)

    def test_the_veto_is_read_back_from_a_retained_attempt(self):
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        latest = self.store / 'vetoed.json'
        # The latest pointer is removed, then replaced by a record without the
        # veto. The immutable attempt under history/ still holds it both times.
        for replace in (latest.unlink,
                        lambda: latest.write_text(json.dumps({'runId': 'vetoed', 'redTeam': 'SHIP', 'summary': {}}),
                                                  encoding='utf-8')):
            with self.subTest(replace=replace):
                replace()
                p = self.record(verdict('SHIP'))
                self.assertEqual(p.returncode, 0, p.stderr)
                art = self.artifact()
                self.assertEqual((art['redTeam'], art['outcome']), ('BLOCK', 'BLOCKED-REDTEAM'))
                self.assertEqual([v['reason'] for v in art['vetoes']], ['certifier exit 1'])

    def test_a_second_veto_is_filed_beside_the_first(self):
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        second = self.root / 'second.log'
        second.write_bytes(b'second certifier: refused\n')
        p = self.veto(verdict('SHIP'), reason='second certifier refused', evidence=second)
        self.assertEqual(p.returncode, 0, p.stderr)
        art = self.artifact()
        self.assertEqual([(v['reason'], v['over']) for v in art['vetoes']],
                         [('certifier exit 1', 'SHIP'), ('second certifier refused', 'BLOCK')])
        for v in art['vetoes']:
            self.assertTrue((self.store / 'evidence' / v['evidence']['sha256']).is_file())

    def test_repeating_the_same_veto_does_not_file_it_twice(self):
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        filed = self.artifact()['vetoes']
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        self.assertEqual(self.artifact()['vetoes'], filed)

    def test_a_veto_on_one_run_leaves_another_run_alone(self):
        self.assertEqual(self.veto(verdict('SHIP')).returncode, 0)
        p = self.record(verdict('SHIP'), run_id='other')
        self.assertEqual(p.returncode, 0, p.stderr)
        art = self.artifact('other')
        self.assertEqual((art['redTeam'], art['outcome']), ('SHIP', 'COMPLETE'))
        self.assertNotIn('vetoes', art)
        self.assertNotIn('redTeamDerived', art)
        self.assertNotIn('veto', p.stderr)


if __name__ == '__main__':
    unittest.main()
