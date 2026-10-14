#!/usr/bin/env python3
"""Exercise real hooks against the reported commit, workspace and Stop cases."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'


class GateRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'workspace' / 'repo'
        self.repo.mkdir(parents=True)
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith('FPL_') or key in ('CLAUDE_PROJECT_DIR', 'CLAUDE_PLUGIN_ROOT'):
                self.env.pop(key)
        self.env.update(FPL_PY=sys.executable, FPL_MEMORY_INDEX='0', FPL_RECALL_INJECT='0')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 't@t')
        (self.repo / 'src').mkdir()
        (self.repo / 'scripts').mkdir()
        self.write('src/app.py', 'x = 1\n')
        self.write('.gitignore', '.claude/fluxpoint/\n')
        self.write('WORK.md', '# Work\n\n## Evidence\n\n'
                   '| When (UTC) | Source | Outcome | Claim | Proof |\n'
                   '|---|---|---|---|---|\n')
        self.runs = self.root / 'runs'
        self.args = self.root / 'args'
        self.write('scripts/harness.sh', '#!/usr/bin/env bash\n'
                   f'echo run >>"{self.runs}"\nprintf "%s\\n" "$*" >"{self.args}"\n'
                   'if grep -q BROKEN src/*.py; then exit 1; fi\nexit 0\n')
        self.commit('base')
        self.hook('inject-state.sh')

    def write(self, path, body):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding='utf-8')

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo,
                                       stderr=subprocess.STDOUT).decode().strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-qm', message)

    def hook(self, script='dod-gate.sh', **env):
        run = subprocess.run(['bash', str(SCRIPTS / script)], cwd=self.repo,
                             env=dict(self.env, **env), capture_output=True, text=True,
                             input=json.dumps(dict(session_id='s', cwd=str(self.repo))),
                             timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def count(self):
        return len(self.runs.read_text().splitlines()) if self.runs.exists() else 0

    def yielded(self, commit=False):
        self.write('src/app.py', 'BROKEN = 1\n')
        if commit:
            self.commit('red slice')
        self.hook(FPL_MAX_BLOCKS='1')
        self.hook(FPL_MAX_BLOCKS='1')
        self.assertEqual(self.count(), 2)
        self.assertIn('already yielded', self.hook(FPL_MAX_BLOCKS='1'))
        self.assertEqual(self.count(), 2)

    def test_yield_reruns_new_clean_commit(self):
        self.yielded(commit=True)
        self.write('src/other.py', 'y = 1\n')
        self.commit('next slice')
        self.hook(FPL_MAX_BLOCKS='1')
        self.assertEqual(self.count(), 3)

    def test_yield_reruns_rewritten_untracked_file(self):
        self.write('src/new.py', 'x = 1\n')
        self.yielded()
        self.write('src/new.py', 'x = 2\n')
        self.hook(FPL_MAX_BLOCKS='1')
        self.assertEqual(self.count(), 3)

    def test_yield_reruns_new_file_inside_untracked_directory(self):
        self.write('src/pkg/a.py', 'x = 1\n')
        self.yielded()
        self.write('src/pkg/b.py', 'y = 2\n')
        self.hook(FPL_MAX_BLOCKS='1')
        self.assertEqual(self.count(), 3)

    def test_committed_fix_passes_and_resets_blocks(self):
        self.yielded(commit=True)
        self.write('src/app.py', 'x = 2\n')
        self.commit('fix')
        self.hook(FPL_MAX_BLOCKS='1')
        self.assertEqual(self.count(), 3)
        self.assertFalse((self.repo / '.claude/fluxpoint/s.blocks').exists())
        self.assertIn('"outcome": "PASS"',
                      (self.repo / '.claude/fluxpoint/evidence.jsonl').read_text())
        self.write('src/other.py', 'BROKEN = 2\n')
        self.assertEqual(json.loads(self.hook(FPL_MAX_BLOCKS='1'))['decision'], 'block')

    def test_pass_cache_reuses_uncommitted_tree_but_not_new_args_or_source(self):
        self.write('src/app.py', 'x = 2\n')
        self.hook()
        self.hook()
        self.hook()
        self.assertEqual(self.count(), 1)
        self.hook(FPL_HARNESS_ARGS='--gate')
        self.assertEqual(self.count(), 2)
        self.write('src/new.py', 'y = 3\n')
        self.hook(FPL_HARNESS_ARGS='--gate')
        self.assertEqual(self.count(), 3)

    def test_red_run_is_retried_without_tree_changes(self):
        self.write('src/app.py', 'BROKEN = 1\n')
        self.hook()
        self.hook()
        self.assertEqual(self.count(), 2)

    def test_workspace_repo_settings_precedence_and_truthful_context(self):
        self.write('.claude/settings.json', json.dumps({'env': {'FPL_HARNESS_ARGS': '--shared'}}))
        self.write('.claude/settings.local.json', json.dumps({'env': {'FPL_HARNESS_ARGS': '--local'}}))
        self.write('src/app.py', 'x = 2\n')
        workspace = str(self.repo.parent)
        self.assertIn("runs '--local'", self.hook('inject-state.sh', CLAUDE_PROJECT_DIR=workspace))
        notice = self.hook(CLAUDE_PROJECT_DIR=workspace)
        self.assertEqual(self.args.read_text().strip(), '--local')
        self.assertIn('.claude/settings.json', notice)
        self.assertIn('.claude/settings.local.json', notice)
        self.hook(CLAUDE_PROJECT_DIR=workspace, FPL_HARNESS_ARGS='--explicit')
        self.assertEqual(self.args.read_text().strip(), '--explicit')
        (self.repo / '.claude/settings.local.json').unlink()
        self.hook(CLAUDE_PROJECT_DIR=workspace)
        self.assertEqual(self.args.read_text().strip(), '--shared')

    def test_packet_named_harness_changes_extend_trust_base(self):
        self.write('scripts/custom-check.sh', 'exit 0\n')
        self.write('.fluxpoint-spec-lock.json', json.dumps({
            'harness': {'scripts/custom-check.sh': 'old-digest'}}))
        self.commit('named check')
        self.hook('inject-state.sh')
        self.write('scripts/custom-check.sh', 'echo changed; exit 0\n')
        notice = self.hook()
        self.assertIn('scripts/custom-check.sh', notice)
        self.git('add', '-A')
        self.git('commit', '-qm', 'changed check')
        (self.repo / 'scripts/custom-check.sh').unlink()
        self.assertIn('scripts/custom-check.sh', self.hook())

    def test_timeout_advises_contract_and_clamps_to_safe_ceiling(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        fake = bindir / 'timeout'
        fake.write_text('#!/usr/bin/env bash\nprintf "%s" "$1" >"' + str(self.root / 'timeout') + '"\nexit 124\n')
        fake.chmod(0o755)
        self.write('src/app.py', 'x = 2\n')
        out = self.hook(FPL_GATE_TIMEOUT='999', PATH=str(bindir) + os.pathsep + self.env['PATH'])
        self.assertEqual((self.root / 'timeout').read_text(), '570')
        self.assertIn('clamped to 570', out)
        self.assertIn('FPL_HARNESS_ARGS', out)
        self.assertIn('keep --full in CI', out)

    def test_hygiene_hedge_comments_and_tracked_allow_policy(self):
        self.write('src/app.py', 'for now, alarmed in []:\n    pass\n'
                   'nows = [now for now in []]\nlabel = "for now"\n')
        self.assertNotIn('hygiene scan RED', self.hook())
        self.write('src/app.py', '# keep the fallback for now\n')
        self.assertIn('hygiene scan RED', self.hook())
        self.write('src/app.py', 'pytest.skip(f"UNVERIFIED: compiler absent")\n')
        self.assertIn('hygiene scan RED', self.hook())
        self.write('.fluxpoint-hygiene-allow', '# justified optional external compiler\nskip\\(f?"UNVERIFIED:\r\n')
        notice = self.hook()
        self.assertNotIn('hygiene scan RED', notice)
        self.assertIn('.fluxpoint-hygiene-allow', notice)
        self.write('src/app.py', 'pytest.skip("disabled")\n')
        self.assertIn('hygiene scan RED', self.hook())

    def test_checkout_forward_detach_inherits_prior_round_code(self):
        # Create an integration commit before the session begins, but launch
        # the session on its ancestor, as graph launching commonly does.
        self.write('src/app.py', 'x = 1  # TODO inherited\n')
        self.commit('previous round')
        integration = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', 'HEAD~1')
        shutil.rmtree(self.repo / '.claude/fluxpoint')
        self.hook('inject-state.sh')
        self.git('checkout', '-q', '--detach', integration)
        self.hook()
        self.assertEqual(self.count(), 0)
        self.write('src/own.py', 'x = 2  # TODO this session\n')
        self.commit('own slice after checkout')
        out = self.hook()
        self.assertIn('src/own.py', out)
        self.assertNotIn('TODO inherited', out)

    def test_local_evidence_keeps_committed_pass_and_checkpoint_clean(self):
        self.write('src/app.py', 'x = 2\n')
        self.commit('green slice')
        notice = self.hook()
        self.assertIn('evidence.jsonl', notice)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertIn('Recorded by the gate (local:', self.hook('inject-state.sh'))
        self.write('src/app.py', 'BROKEN = 1\n')
        self.commit('red slice')
        self.hook(FPL_MAX_BLOCKS='1')
        self.hook(FPL_MAX_BLOCKS='1')
        self.assertEqual(self.git('status', '--porcelain'), '')
        rows = (self.repo / '.claude/fluxpoint/evidence.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(r)['outcome'] for r in rows], ['PASS', 'FAIL'])

    def test_work_mode_announces_write_and_off_mode_skips_evidence(self):
        self.write('src/app.py', 'x = 2\n')
        out = self.hook(FPL_GATE_EVIDENCE='work')
        self.assertIn('uncommitted gate row', out)
        self.assertIn('| gate | PASS |', (self.repo / 'WORK.md').read_text())
        self.write('src/app.py', 'x = 3\n')
        before = (self.repo / 'WORK.md').read_text()
        self.hook(FPL_GATE_EVIDENCE='off')
        self.assertEqual((self.repo / 'WORK.md').read_text(), before)
        self.assertFalse((self.repo / '.claude/fluxpoint/evidence.jsonl').exists())

    def test_concurrent_graph_and_gate_writers_retain_every_row(self):
        def writer(i):
            if i % 2:
                cmd = [sys.executable, str(SCRIPTS / 'evidence.py'), '--record', '--outcome', 'PASS',
                       '--claim', f'claim {i}', '--proof', f'proof {i}']
                body = ''
            else:
                cmd = [sys.executable, str(SCRIPTS / 'record-run.py'), '--run-id', f'wf_{i}']
                body = json.dumps({'campaign': 'c', 'outcome': 'COMPLETE', 'results': {}, 'provenance': []})
            p = subprocess.run(cmd, input=body, text=True, capture_output=True, cwd=self.repo, env=self.env)
            self.assertEqual(p.returncode, 0, p.stderr)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(writer, range(20)))
        text = (self.repo / 'WORK.md').read_text()
        self.assertEqual(sum('| gate | PASS |' in r or '| wf_' in r for r in text.splitlines()), 20)


class PythonLaunchRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.launches = self.root / 'launches'
        self.env = {k: v for k, v in os.environ.items() if k != 'FPL_PY'}
        self.env.update(XDG_CACHE_HOME=str(self.root / 'cache'),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        self.bash = shutil.which('bash')

    def wrapper(self, directory, name, valid=True):
        directory.mkdir(exist_ok=True)
        target = directory / name
        target.write_text('#!' + self.bash + '\n'
                          f'echo "{name}:$*" >>"{self.launches}"\n' +
                          (f'exec "{sys.executable}" "$@"\n' if valid else 'exit 49\n'))
        target.chmod(0o755)

    def launch(self):
        run = subprocess.run([self.bash, str(SCRIPTS / 'py.sh'), 'secret-guard.py', '--hook'],
                             input='{"tool_input":{"command":"echo hello"}}', text=True,
                             capture_output=True, env=self.env, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_warm_launch_starts_only_target_interpreter(self):
        self.wrapper(self.bin, 'python3')
        self.launch()
        self.assertEqual(len(self.launches.read_text().splitlines()), 2)
        self.launch()
        self.assertEqual(len(self.launches.read_text().splitlines()), 3)
        # Changing PATH invalidates the saved probe.
        self.env['PATH'] += os.pathsep + str(self.root)
        self.launch()
        self.assertEqual(len(self.launches.read_text().splitlines()), 5)

    def test_alias_failure_falls_back_once_and_is_cached(self):
        self.wrapper(self.bin, 'python3', valid=False)
        self.wrapper(self.bin, 'python')
        self.launch()
        self.launch()
        lines = self.launches.read_text().splitlines()
        self.assertEqual(sum(s.startswith('python3:') for s in lines), 1)
        self.assertEqual(len(lines), 4)

    def test_windowsapps_alias_is_never_started(self):
        aliases = self.root / 'WindowsApps'
        self.wrapper(aliases, 'python3', valid=False)
        self.wrapper(self.bin, 'python')
        self.env['PATH'] = str(aliases) + os.pathsep + self.env['PATH']
        self.launch()
        self.assertNotIn('python3:', self.launches.read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
