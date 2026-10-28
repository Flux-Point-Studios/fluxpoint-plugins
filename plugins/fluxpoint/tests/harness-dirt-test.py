#!/usr/bin/env python3
"""Attested harness state is distinct from edited program content."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import attest
import evidence_table

BASE = '''# Campaign
STATUS: READY

```json graph-ir
{"nodes": []}
```

## Evidence

| When (UTC) | Source | Outcome | Claim | Proof |
|---|---|---|---|---|
| old | run | COMPLETE | original | log |

## Decisions

| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |
|---|---|---|---|---|---|
| old | first | keep | no | none | original |
'''

class HarnessDirt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.write('WORK.md', BASE)
        self.write('src.py', 'print(1)\n')
        self.write('.gitignore', '.claude/fluxpoint/\n')
        self.write('.claude/settings.json', '{}\n')
        self.write('.fluxpoint-gates.json', json.dumps({'version': 1, 'gates': {'test': '"' + sys.executable.replace('\\', '/') + '" -c "print(7)"'}}))
        self.git('add', '.')
        self.git('commit', '-qm', 'base')

    def git(self, *argv):
        return subprocess.run(['git', '-C', str(self.root), *argv], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def test_harness_status_and_inserted_rows_are_clean(self):
        changes = [BASE.replace('STATUS: READY', 'STATUS: RUNNING'),
                   evidence_table.append(BASE, {evidence_table.ROW_HDR: '| now | run | COMPLETE | checked | log |'})[0],
                   BASE.replace('| old | first', '| now | second | keep | no | none | new |\n| old | first')]
        for current in changes:
            with self.subTest(current=current[:35]):
                self.write('WORK.md', current)
                self.assertFalse(attest.dirty(str(self.root)))
                self.assertEqual([r['kind'] for r in attest.dirty_paths(str(self.root))], ['harness'])

    def test_content_and_existing_row_edits_stay_dirty(self):
        for current in [BASE.replace('"nodes": []', '"nodes": [1]'), BASE.replace('# Campaign', '# Changed'),
                        BASE.replace('| original | log |', '| rewritten | log |'),
                        BASE.replace('original |', 'changed |'), BASE.replace('STATUS: READY', 'STATUS: RUNNING\nnew prose')]:
            self.write('WORK.md', current)
            self.assertTrue(attest.dirty(str(self.root)), current)
        self.write('WORK.md', BASE)
        for path in ['src.py', '.claude/settings.json', 'deps.lock', '.claude/unknown/data.json']:
            with self.subTest(path=path):
                target = self.write(path, 'changed')
                self.assertTrue(attest.dirty(str(self.root)))
                if path in ['src.py', '.claude/settings.json']:
                    self.git('checkout', '--', path)
                else:
                    target.unlink()

    def test_only_untracked_workflows_are_harness_metadata(self):
        compiled = self.write('.claude/workflows/run.js', 'one')
        self.assertFalse(attest.dirty(str(self.root)))
        self.git('add', str(compiled))
        self.git('commit', '-qm', 'tracked compiler artifact')
        compiled.write_text('changed')
        self.assertTrue(attest.dirty(str(self.root)))

    def test_worktree_gets_no_harness_exemption(self):
        tree = self.root / '.claude/fluxpoint/tree'
        self.git('worktree', 'add', '--detach', str(tree), 'HEAD')
        (tree / 'WORK.md').write_bytes(BASE.replace('STATUS: READY', 'STATUS: RUNNING').encode())
        self.assertTrue(any(p['kind'] == 'content' for p in attest.dirty_paths(str(tree), harness=False)))

    def test_wrapper_after_status_edit_is_attested_and_ir_edit_names_path(self):
        launch = attest.stamp(str(self.root))
        for changed, expected in [(BASE.replace('STATUS: READY', 'STATUS: RUNNING'), 'ATTESTED'),
                                  (BASE.replace('"nodes": []', '"nodes": [1]'), 'STALE')]:
            self.write('WORK.md', changed)
            run = subprocess.run([sys.executable, str(SCRIPTS / 'attest.py'), '--root', str(self.root), '--run', 'test', '--nonce', launch['nonce']], text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            row = json.loads(Path(attest.path_for(str(self.root))).read_text().splitlines()[-1])
            self.assertIn('dirtyPaths', row)
            claim = dict(attestId=row['attestId'], exit=0, sha=self.git('rev-parse', 'HEAD'))
            result = attest._check_cited([row], 'gate', 'test', claim, launch, self.git('rev-parse', 'HEAD'), {})
            self.assertEqual(result['status'], expected, result)
            if expected == 'STALE':
                self.assertIn('WORK.md', result['detail'])

if __name__ == '__main__': unittest.main()
