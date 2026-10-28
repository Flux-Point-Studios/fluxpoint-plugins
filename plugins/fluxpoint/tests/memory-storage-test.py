#!/usr/bin/env python3
"""Exercise durable cross-worktree and concurrent memory storage."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import memory


def lesson(key='same', claim='The parser bound is inclusive, so this report is false.'):
    return {'tag': 'audit', 'dedupeKey': key, 'claim': claim,
            'status': 'killed', 'node': 'refute'}


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.old_home = os.environ.pop('FPL_MEMORY_HOME', None)
        self.addCleanup(self.restore_home)

    def restore_home(self):
        if self.old_home is None:
            os.environ.pop('FPL_MEMORY_HOME', None)
        else:
            os.environ['FPL_MEMORY_HOME'] = self.old_home

    def git(self, *args, root=None):
        return subprocess.check_output(['git', '-C', str(root or self.root), *args], text=True).strip()

    def init(self):
        self.git('init', '-q')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                 'commit', '--allow-empty', '-qm', 'init')

    def record(self, run, root=None):
        directory = (root or self.root) / '.claude/fluxpoint/runs'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (run + '.json')).write_text(json.dumps({'runId': run, 'summary': {}}))
        return directory

    def test_parallel_append_is_one_transaction(self):
        code = """import sys,json;sys.path.insert(0,sys.argv[1]);import memory
root=sys.argv[2];who=sys.argv[3]
for i in range(10):
 rid=who+'-'+str(i)
 rows=[dict(tag='audit',dedupeKey='same-'+str(j),claim='concurrent finding '+who+' detail '*100,status='surviving',node='find') for j in range(30)]
 memory.append_from_summary(root,{'memory':rows},rid)
"""
        for who in ('a', 'b', 'c'):
            for i in range(10):
                self.record(who + '-' + str(i))
        procs = [subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), str(self.root), who])
                 for who in ('a', 'b', 'c')]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=40), 0)
        rows = memory.read(str(self.root))
        self.assertEqual(len(rows), 900)
        for key in {r['dedupeKey'] for r in rows}:
            self.assertEqual(sorted(r['arrivals'] for r in rows if r['dedupeKey'] == key), list(range(1, 31)))

    def test_worktree_lesson_and_run_survive_worktree_deletion(self):
        self.init()
        wt = self.root.parent / (self.root.name + '-wt')
        self.addCleanup(lambda: subprocess.run(['git', '-C', str(self.root), 'worktree', 'remove', '--force', str(wt)], capture_output=True))
        self.git('worktree', 'add', '-q', '--detach', str(wt))
        runs = self.record('run-1', wt)
        memory.append_from_summary(str(wt), {'memory': [lesson()]}, 'run-1', str(runs))
        self.assertEqual(memory.seed_map(str(self.root), ['audit'])['audit']['keys'], ['same'])
        self.git('worktree', 'remove', '--force', str(wt))
        self.assertEqual(len(memory.current(str(self.root))), 1)
        self.assertTrue((Path(memory.runs_path(str(self.root))) / 'run-1.json').exists())

    def test_override_and_explicit_adoption_preserve_legacy(self):
        legacy = self.root / 'old/.claude/fluxpoint'
        legacy.mkdir(parents=True)
        row = dict(lesson(), provenance={'runId': 'old'}, establishedWhen='2026-01-01T00:00:00Z')
        (legacy / 'memory.jsonl').write_text(json.dumps(row) + '\n')
        (legacy / 'runs').mkdir()
        (legacy / 'runs/old.json').write_text('{"runId":"old","summary":{}}')
        os.environ['FPL_MEMORY_HOME'] = str(self.root / 'shared')
        result = subprocess.run([sys.executable, str(SCRIPTS / 'memory.py'), '--root', str(self.root), '--adopt', str(self.root / 'old')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(memory.read(str(self.root))), 1)
        self.assertTrue((legacy / 'memory.jsonl').exists())
        self.assertTrue((Path(memory.runs_path(str(self.root))) / 'old.json').exists())
        subprocess.check_call([sys.executable, str(SCRIPTS / 'memory.py'), '--root', str(self.root), '--adopt', str(self.root / 'old')], stdout=subprocess.DEVNULL)
        self.assertEqual(len(memory.read(str(self.root))), 1)

    def test_latest_uses_timestamp_then_run_id(self):
        rows = [dict(lesson(claim='The newer claim wins regardless of merge order.'), provenance={'runId': 'new'}, establishedWhen='2026-03-02T00:00:00Z'),
                dict(lesson(claim='The older claim came last in the merged file.'), provenance={'runId': 'old'}, establishedWhen='2026-01-01T00:00:00Z')]
        path = Path(memory.path_for(str(self.root)))
        path.parent.mkdir(parents=True)
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        self.assertEqual(memory.current(str(self.root))[0]['provenance']['runId'], 'new')

    def test_recurrence_includes_legacy_and_shared_rows(self):
        self.init()
        legacy = self.root / '.claude/fluxpoint/memory.jsonl'
        legacy.parent.mkdir(parents=True)
        older = dict(lesson(), status='surviving', provenance={'runId': 'old'}, establishedWhen='2026-01-01T00:00:00Z')
        legacy.write_text(json.dumps(older) + '\n')
        runs = self.record('new')
        memory.append_from_summary(str(self.root), {'memory': [dict(lesson(), status='surviving')]}, 'new', str(runs))
        module = importlib.util.spec_from_file_location('recurrence_guard', SCRIPTS / 'recurrence-guard.py')
        guard = importlib.util.module_from_spec(module)
        module.loader.exec_module(guard)
        rows, bad, manifest = guard.load(str(self.root))
        self.assertEqual(len(rows), 2)
        self.assertEqual(bad, 0)
        self.assertEqual(len(guard.promoted(rows)[0]['runs']), 2)

    def test_metrics_includes_shared_run_and_lessons(self):
        self.init()
        run = {'runId': 'shared', 'when': '2026-01-01 00:00', 'outcome': 'COMPLETE', 'summary': {'campaign': 'audit', 'provenance': []}}
        shared = Path(memory.runs_path(str(self.root)))
        shared.mkdir(parents=True)
        (shared / 'shared.json').write_text(json.dumps(run))
        row = dict(lesson(), provenance={'runId': 'shared'}, establishedWhen='2026-01-01T00:00:00Z')
        Path(memory.path_for(str(self.root))).write_text(json.dumps(row) + '\n')
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'metrics.py'), '--root', str(self.root), '--json'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data['campaigns']['audit']['runs'], 1)
        self.assertEqual(data['campaigns']['audit']['lessons']['filed'], 1)

    def test_decision_lookup_reads_shared_and_legacy_runs_once(self):
        self.init()
        import decision
        local = self.record('legacy')
        record = {'chosen': 'retain the verified result'}
        row = {'runId': 'legacy', 'when': '2026-01-01 00:00', 'summary': {'decisions': {'storage': record}}}
        (local / 'legacy.json').write_text(json.dumps(row))
        memory.retain_run(str(self.root), 'legacy', local / 'legacy.json')
        shared = Path(memory.runs_path(str(self.root)))
        (shared / 'sibling.json').write_text(json.dumps({'runId': 'sibling', 'when': '2026-03-02 00:00', 'summary': {'decisions': {'storage': record}}}))
        found = decision.find(str(self.root), 'storage')
        self.assertEqual([hit[1] for hit in found], ['legacy', 'sibling'])

    def test_corrupt_memory_does_not_drop_evidence(self):
        path = Path(memory.path_for(str(self.root)))
        path.parent.mkdir(parents=True)
        path.write_text('not JSON\n')
        graph = self.root / 'WORK.md'
        graph.write_text('# w\n\n## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        summary = {'memory': [lesson()], 'provenance': [{'node': 'find', 'status': 'OK', 'detail': ''}], 'outcome': 'COMPLETE'}
        env = dict(os.environ, FPL_MEMORY_INDEX='0')
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'), '--root', str(self.root), '--graph', str(graph), '--run-id', 'corrupt-store'], input=json.dumps(summary), text=True, capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('lesson filing failed', proc.stderr)
        self.assertIn('corrupt-store', graph.read_text())
        with self.assertRaises(SystemExit):
            memory.read(str(self.root))


if __name__ == '__main__':
    unittest.main()
