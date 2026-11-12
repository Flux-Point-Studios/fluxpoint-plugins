#!/usr/bin/env python3
"""Locked support closure and repair preflight regressions (#171)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
import specification

module = importlib.util.spec_from_file_location('packet_fixtures', Path(__file__).with_name('specification-test.py'))
fixtures = importlib.util.module_from_spec(module)
module.loader.exec_module(fixtures)


class PacketInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.doc = fixtures.packet()
        self.doc['checks'][0]['argv'] = ['{python}', '-m', 'pytest', 'tests/test_landing.py::test_sign']
        self.write('src/prod/__init__.py', '')
        self.write('src/prod/signer.py', 'def sign(): return True\n')
        self.write('src/prod/other.py', 'OTHER = True\n')
        self.write('tests/__init__.py', '')
        self.write('tests/test_landing.py', 'from tests import helper\n\ndef test_sign():\n    assert helper.sign()\n')
        self.write('tests/helper.py', 'def sign():\n    from tests import test_engine\n    return test_engine.engine()\n')
        self.write('tests/test_engine.py', 'from prod import signer\n\ndef engine(): return signer.sign()\ndef test_engine(): assert engine()\n')
        self.write('tests/conftest.py', 'from tests import fixtures\n')
        self.write('tests/fixtures.py', 'VALUE = 1\n')
        self.write('conftest.py', '# root fixtures\n')
        self.write('pyproject.toml', '[tool.pytest.ini_options] # pytest configuration\npythonpath = ["src"]\n')
        self.write('archive.json', '{"scope":"Signer behaviour under fixture inputs"}')
        self.doc['checks'][0]['scope'] = 'archive.json#/scope'
        self.save()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def save(self):
        self.write(specification.SPEC, json.dumps(self.doc))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(PLUGIN / 'scripts/specification.py'), *args],
                              cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=20)

    def lock(self):
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.root / specification.LOCK).read_text())

    def report(self, *args):
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, capture_output=True, check=True)

    def commit(self):
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Fixture')

    def test_default_packet_header_is_deduplicated_and_frozen_trust_paths_do_not_walk(self):
        from unittest.mock import patch
        self.lock()
        self.write('WORK.md', 'SPEC: .fluxpoint-spec.json\n')
        self.assertEqual(specification.declared_packets(self.root), [None])
        with patch.object(specification.packet_inputs.Inputs, '_expand', side_effect=AssertionError('live walk')):
            paths = specification.packet_inputs.trust_paths(self.root, self.doc,
                json.loads((self.root / specification.LOCK).read_text())['harness'])
        self.assertIn('tests/helper.py', paths)

    def test_frozen_and_live_closure_are_expanded_once_per_record(self):
        from unittest.mock import patch
        self.lock()
        original = specification.packet_inputs.Inputs._expand
        calls = []
        def counted(inputs):
            calls.append(inputs)
            return original(inputs)
        with patch.object(specification.packet_inputs.Inputs, '_expand', counted):
            specification.harness_record(self.root)
        self.assertEqual(len(calls), 1)

    def test_project_files_context_counts_and_jest_patterns_remain_lockable(self):
        self.write('tsconfig.json', '{}')
        self.write('notes.txt', 'sample')
        for argv, expected in [(['npx', 'tsc', '--noEmit', '--project', 'tsconfig.json'], 'tsconfig.json'),
                               (['pyright', '--project=tsconfig.json'], 'tsconfig.json'),
                               (['grep', '-C', '2', 'sample', 'notes.txt'], 'notes.txt'),
                               (['npx', 'jest', '--testPathPattern', r'src/.*\.test\.ts$'], None),
                               (['npx', 'jest', '--testPathPatterns=landing'], None)]:
            with self.subTest(argv=argv):
                self.doc['checks'][0]['argv'] = argv
                self.save()
                frozen = self.lock()
                if expected:
                    self.assertIn(expected, frozen['harness'])

    def test_planned_new_ambient_files_conflict_before_they_exist(self):
        self.lock()
        for rel in ['tests/__init__.py', 'tests/conftest.py', 'conftest.py', 'pyproject.toml']:
            (self.root / rel).unlink()
        for rel in ['tests/conftest.py', 'conftest.py', 'tests/__init__.py', 'pytest.ini',
                    'tox.ini', 'setup.cfg', 'package.json', 'vitest.config.ts', 'vite.config.js']:
            with self.subTest(rel=rel):
                result = self.cli('--touches', rel)
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertIn('spec-tests', json.loads(result.stdout)['conflicts'][0]['checks'])
        self.assertEqual(self.cli('--touches', 'unrelated/conftest.py').returncode, 0)

    def test_dynamic_python_and_js_inputs_warn_on_lock_touches_and_reach(self):
        self.write('tests/test_landing.py', 'import importlib as il\nimport runpy\n'
                   'il.import_module("tests.dyn_helper")\n__import__("tests.dunder_helper")\n')
        self.write('tests/dyn_helper.py', 'VALUE=1')
        self.write('tests/dunder_helper.py', 'VALUE=2')
        self.save()
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('dynamic', result.stderr)
        self.assertIn('harnessInputs', result.stderr)
        touched = json.loads(self.cli('--touches', 'tests/dyn_helper.py').stdout)
        self.assertTrue(touched['warnings'])
        self.assertTrue(self.report('--reach', 'tests/dyn_helper.py')['warnings'])
        self.doc['harnessInputs'] = ['tests/dyn_helper.py', 'tests/dunder_helper.py']
        self.save()
        self.lock()
        self.assertFalse(self.report('--reach', 'tests/dyn_helper.py')['warnings'])
        self.doc['checks'][0]['argv'] = ['node', 'checks/test.js']
        self.doc.pop('harnessInputs', None)
        self.write('checks/test.js', 'await import(`./tmpl`);')
        self.write('checks/tmpl.js', 'export const n=1;')
        self.save()
        self.lock()
        self.assertTrue(self.report('--reach', 'checks/tmpl.js')['warnings'])
        self.doc['harnessInputs'] = ['checks/tmpl.js']
        self.save()
        self.lock()
        self.assertFalse(any('dynamic JS' in warning
                             for warning in self.report('--reach', 'checks/tmpl.js')['warnings']))

    def test_strict_harness_check_refuses_drift_in_the_worktree_being_judged(self):
        self.lock()
        self.commit()
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / 'builder'
            self.git('worktree', 'add', '-q', '-b', 'builder', str(worktree))
            (worktree / 'tests/helper.py').write_text('def sign(): return True\n')
            subprocess.run(['git', '-C', str(worktree), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(worktree), '-c', 'user.name=Test',
                '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Change frozen helper'], check=True)
            self.assertEqual(specification.harness_record(self.root)['changedPaths'], [])
            result = self.cli('--root', str(worktree), '--check', '--strict-harness')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('tests/helper.py', result.stderr)
            self.assertIn('locked harness inputs changed', result.stderr)

    def test_lock_persists_imports_conftests_inits_config_but_not_production(self):
        lock = self.lock()
        self.assertEqual(set(lock['harness']), {'tests/test_landing.py', 'tests/helper.py',
            'tests/test_engine.py', 'tests/__init__.py', 'tests/conftest.py', 'tests/fixtures.py',
            'conftest.py', 'pyproject.toml'})
        self.assertNotIn('src/prod/signer.py', lock['harness'])
        report = self.report('--inputs', '--closure')
        rows = {row['path']: row for row in report['inputs']}
        self.assertEqual(set(lock['harness']), {p for p, row in rows.items()
            if any(r['kind'] not in ('packet', 'lock', 'baseline', 'gates', 'references') for r in row['reasons'])})
        self.assertTrue(all(row['frozen'] for row in rows.values()))
        self.assertIn('test-import', {r['kind'] for r in rows['tests/helper.py']['reasons']})
        self.assertEqual(rows['archive.json']['reasons'][0]['kind'], 'references')
        self.assertEqual(rows[specification.BASELINE]['sha256'], None)

    def test_legacy_report_adds_derived_closure_without_mutation(self):
        lock = self.lock()
        del lock['harness']
        self.write(specification.LOCK, json.dumps(lock))
        before = (self.root / specification.LOCK).read_bytes()
        direct = {row['path'] for row in self.report('--inputs')['inputs']}
        closure = {row['path']: row for row in self.report('--inputs', '--closure')['inputs']}
        self.assertNotIn('tests/fixtures.py', direct)
        self.assertFalse(closure['tests/fixtures.py']['frozen'])
        self.assertEqual((self.root / specification.LOCK).read_bytes(), before)
        result = self.cli('--touches', 'tests/fixtures.py')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(json.loads(result.stdout)['conflicts'][0]['checks'], ['spec-tests'])
        record = specification.harness_record(self.root)
        self.assertIn('tests/fixtures.py', record['changedPaths'])

    def test_runner_directory_options_do_not_seed_the_repository(self):
        from packet_inputs import Inputs
        self.write('README.md', 'unrelated\n')
        self.commit()
        argv = self.doc['checks'][0]['argv']
        expected = Inputs(self.root, self.doc).hashes()
        for option in (['--rootdir=.'], ['--confcutdir', '.'],
                       ['--confcutdir=tests']):
            with self.subTest(option=option):
                self.doc['checks'][0]['argv'] = argv + option
                self.assertEqual(Inputs(self.root, self.doc).hashes(), expected)
        self.doc['checks'][0]['argv'] = argv + ['--path', '.']
        self.assertIn('README.md', Inputs(self.root, self.doc).hashes())
        self.doc['checks'][0]['argv'] = argv + ['.']
        self.assertIn('README.md', Inputs(self.root, self.doc).hashes())

    def test_touches_covers_directories_fixed_inputs_and_unlocked_changes(self):
        self.lock()
        for path in ('tests', '.', 'tests/helper.py', specification.SPEC, specification.LOCK,
                     specification.BASELINE, '.fluxpoint-gates.json', 'archive.json'):
            with self.subTest(path=path):
                result = self.cli('--touches', path)
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertIn('spec-tests', json.loads(result.stdout)['conflicts'][0]['checks'])
        self.assertEqual(self.cli('--touches', 'src/prod/signer.py', 'new-module.py').returncode, 0)
        for path in ('../outside.py', '/etc/passwd', 'tests/../../outside.py'):
            self.assertEqual(self.cli('--touches', path).returncode, 1)

    def test_reach_follows_function_local_import_and_names_test_nodes(self):
        self.lock()
        for target in ('prod.signer', 'src/prod/signer.py'):
            reached = self.report('--reach', target)
            self.assertEqual([row['check'] for row in reached['checks']], ['spec-tests'])
            files = {row['path']: row['nodes'] for row in reached['checks'][0]['files']}
            self.assertIn('tests/test_landing.py::test_sign', files['tests/test_landing.py'])
            self.assertIn('tests/test_engine.py::test_engine', files['tests/test_engine.py'])
            self.assertIn('tests/helper.py', files)
        # Sharing a package __init__ does not make another production module reached.
        self.assertEqual(self.report('--reach', 'prod.other')['checks'], [])
        self.assertEqual(self.cli('--reach', 'prod.missing').returncode, 1)

    def test_reach_includes_autoloaded_fixture_imports(self):
        self.write('tests/fixtures.py', 'from prod import signer\nVALUE = signer.sign()\n')
        self.lock()
        reached = self.report('--reach', 'prod.signer')
        files = {row['path'] for row in reached['checks'][0]['files']}
        self.assertTrue({'tests/conftest.py', 'tests/fixtures.py', 'tests/test_landing.py'} <= files)

    def test_literal_pytest_plugins_are_locked_and_reached(self):
        self.write('tests/plugin.py', 'from prod import signer\n')
        self.write('tests/conftest.py', 'pytest_plugins = ["tests.plugin"]\n')
        lock = self.lock()
        self.assertIn('tests/plugin.py', lock['harness'])
        reached = self.report('--reach', 'prod.signer')
        self.assertIn('tests/plugin.py', {file['path'] for file in reached['checks'][0]['files']})

    def test_relock_never_drops_old_paths_and_reexpands_retained_roots(self):
        old = self.lock()
        self.doc['checks'][0]['argv'] = ['{python}', '-c', 'print("new adapter")']
        self.save()
        self.write('tests/new_fixture.py', 'VALUE = 2\n')
        self.write('tests/fixtures.py', 'from tests import new_fixture\n')
        new = self.lock()
        self.assertTrue(set(old['harness']) <= set(new['harness']))
        self.assertIn('tests/new_fixture.py', new['harness'])
        self.assertEqual(self.cli('--touches', 'tests/new_fixture.py').returncode, 4)

    def test_deleted_retained_input_refuses_relock_without_truncating_lock(self):
        self.lock()
        before = (self.root / specification.LOCK).read_bytes()
        (self.root / 'tests/helper.py').unlink()
        self.assertEqual(self.cli('--check').returncode, 0)
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn('tests/helper.py', refused.stderr)
        self.assertEqual((self.root / specification.LOCK).read_bytes(), before)
        self.assertIsNone(specification.harness_record(self.root)['files']['tests/helper.py'])

    GONE = ['tests/helper.py', 'tests/test_engine.py']
    REASON = 'decision second-gate-removed'

    def remove_gate(self):
        """Lock, then let a recorded decision remove a gate: its two files are deleted
        and the test that ran them stops importing them."""
        lock = self.lock()
        self.write('tests/test_landing.py', 'def test_sign():\n    assert True\n')
        for rel in self.GONE:
            (self.root / rel).unlink()
        return lock

    def retire_gate(self):
        old = self.remove_gate()
        result = self.cli('--lock', '--retire', self.GONE[0], '--retire', self.GONE[1], '--because', self.REASON)
        self.assertEqual(result.returncode, 0, result.stderr)
        return old, json.loads((self.root / specification.LOCK).read_text()), result

    def test_missing_frozen_paths_refusal_names_each_one_and_the_retire_command(self):
        self.remove_gate()
        before = (self.root / specification.LOCK).read_bytes()
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('specification: frozen path no longer exists: tests/helper.py, tests/test_engine.py; '
                      'restore it, or retire it with --lock --retire tests/helper.py --retire tests/test_engine.py '
                      '--because <decision id or reason>\n', refused.stderr)
        self.assertEqual((self.root / specification.LOCK).read_bytes(), before)

    def test_retire_refuses_without_a_reason_an_unfrozen_path_or_a_reached_path(self):
        self.remove_gate()
        before = (self.root / specification.LOCK).read_bytes()
        both = ('--retire', self.GONE[0], '--retire', self.GONE[1])
        for args, needle in (
                (('--lock', *both), '--retire requires --because <decision id or reason>'),
                (('--lock', *both, '--because', '  '), '--retire requires --because <decision id or reason>'),
                (('--lock', '--because', self.REASON), '--because is only for --retire'),
                (('--check', *both, '--because', self.REASON), '--retire and --because are only for --lock'),
                (('--lock-harness', '--gate', 'verify', *both, '--because', self.REASON),
                 '--retire and --because are only for --lock'),
                # Retiring one of two missing paths still leaves the other to be named.
                (('--lock', '--retire', self.GONE[0], '--because', self.REASON),
                 'frozen path no longer exists: tests/test_engine.py; restore it, or retire it with '
                 '--lock --retire tests/test_engine.py --because'),
                (('--lock', *both, '--retire', 'src/prod/signer.py', '--because', self.REASON),
                 '--retire names a path the lock does not freeze: src/prod/signer.py'),
                (('--lock', *both, '--retire', '../outside.py', '--because', self.REASON), 'inside the repository'),
                # A conftest still imports the fixtures, so the packet still reaches them.
                (('--lock', *both, '--retire', 'tests/fixtures.py', '--because', self.REASON),
                 'cannot retire a path the packet still reaches: tests/fixtures.py (test-import via tests/conftest.py)'),
                (('--lock', *both, '--retire', 'tests/test_landing.py', '--because', self.REASON),
                 'cannot retire a path the packet still reaches: tests/test_landing.py (argv)')):
            with self.subTest(args=args):
                refused = self.cli(*args)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(needle, refused.stderr)
                self.assertEqual((self.root / specification.LOCK).read_bytes(), before)

    def test_retire_removes_only_the_named_paths_and_records_path_hash_and_reason(self):
        old, lock, result = self.retire_gate()
        records = {rel: {'sha256': old['harness'][rel], 'reason': self.REASON} for rel in self.GONE}
        for rel in self.GONE:
            self.assertIn('specification: retired ' + rel + ' ' + old['harness'][rel] + ': ' + self.REASON,
                          result.stdout)
        self.assertEqual(lock['retired'], records)
        self.assertEqual(lock['lockFormat'], 3)
        self.assertEqual(set(lock['harness']), set(old['harness']) - set(self.GONE))
        self.assertNotEqual(lock['harness']['tests/test_landing.py'], old['harness']['tests/test_landing.py'])
        # --check and --inputs show what left the lock and why, and neither treats it as drift.
        for args in (('--check',), ('--check', '--strict-harness')):
            with self.subTest(args=args):
                checked = self.cli(*args)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                for rel in self.GONE:
                    self.assertIn('specification: retired ' + rel + ' ' + old['harness'][rel] + ': ' + self.REASON,
                                  checked.stdout)
        report = self.report('--inputs', '--closure')
        self.assertEqual(report['retired'], [dict(path=rel, **records[rel]) for rel in self.GONE])
        self.assertEqual([row['path'] for row in report['inputs'] if row['path'] in self.GONE], [])
        self.assertEqual(self.cli('--touches', *self.GONE).returncode, 0)
        self.assertEqual(specification.harness_record(self.root)['changedPaths'], [])
        # The record is carried by every later lock, and a later retirement is added to it.
        self.assertEqual(self.cli('--lock').returncode, 0)
        self.assertEqual(json.loads((self.root / specification.LOCK).read_text())['retired'], records)
        # A frozen file that stays on disk, unreached, and an archive the packet stops
        # pointing into are retired the same way, each with the hash the lock last held.
        self.write('tests/conftest.py', '# fixtures inlined\n')
        self.doc['checks'][0]['scope'] = 'Signer behaviour, written out in the packet'
        self.save()
        later = self.cli('--lock', '--retire', './tests/fixtures.py', '--retire', 'archive.json',
                         '--because', ' fixtures  inlined\tby decision ')
        self.assertEqual(later.returncode, 0, later.stderr)
        lock = json.loads((self.root / specification.LOCK).read_text())
        records['tests/fixtures.py'] = {'sha256': old['harness']['tests/fixtures.py'],
                                        'reason': 'fixtures inlined by decision'}
        records['archive.json'] = {'sha256': old['references']['archive.json'],
                                   'reason': 'fixtures inlined by decision'}
        self.assertEqual(lock['retired'], records)
        self.assertEqual(list(lock['retired']), sorted(records))
        self.assertEqual([p for p in lock['harness'] if p in records], [])
        self.assertNotIn('retired tests/helper.py', later.stdout)
        self.assertTrue((self.root / 'tests/fixtures.py').is_file())
        self.assertEqual(self.cli('--check', '--strict-harness').returncode, 0)

    def test_retired_path_that_the_packet_reaches_again_is_frozen_again(self):
        old, lock, _ = self.retire_gate()
        # The helper comes back and the locked test imports it again.
        self.write('tests/helper.py', 'def sign():\n    return True\n')
        self.write('tests/test_landing.py', 'from tests import helper\n\ndef test_sign():\n    assert helper.sign()\n')
        strict = self.cli('--check', '--strict-harness')
        self.assertEqual(strict.returncode, 1)
        self.assertIn('locked harness inputs changed: tests/helper.py', strict.stderr)
        relocked = self.cli('--lock')
        self.assertEqual(relocked.returncode, 0, relocked.stderr)
        self.assertIn('specification: re-admitted tests/helper.py; it was retired because ' + self.REASON,
                      relocked.stdout)
        lock = json.loads((self.root / specification.LOCK).read_text())
        self.assertEqual(lock['harness']['tests/helper.py'],
                         hashlib.sha256(b'def sign():\n    return True\n').hexdigest())
        self.assertEqual(lock['retired'], {'tests/test_engine.py': {'sha256': old['harness']['tests/test_engine.py'],
                                                                   'reason': self.REASON}})
        self.assertEqual(lock['lockFormat'], 3)
        self.assertEqual(self.cli('--check', '--strict-harness').returncode, 0)
        # The harness-only lock freezes a returning path the same way, and the last record goes with it.
        self.write('tests/test_engine.py', 'def engine():\n    return True\n')
        self.write('tests/helper.py', 'from tests import test_engine\n\ndef sign():\n    return test_engine.engine()\n')
        self.gate()
        relocked = self.cli('--lock-harness', '--gate', 'verify')
        self.assertEqual(relocked.returncode, 0, relocked.stderr)
        self.assertIn('specification: re-admitted tests/test_engine.py; it was retired because ' + self.REASON,
                      relocked.stdout)
        lock = json.loads((self.root / specification.LOCK).read_text())
        self.assertNotIn('retired', lock)
        self.assertEqual(lock['lockFormat'], 2)
        self.assertTrue(set(self.GONE) <= set(lock['harness']))
        checked = self.cli('--check', '--strict-harness')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertNotIn('retired', checked.stdout)
        self.assertEqual(self.report('--inputs')['retired'], [])

    def test_lock_with_retirements_is_not_drift_and_an_older_lock_still_loads(self):
        old, lock, _ = self.retire_gate()
        # A lock written before retirement existed has neither the field nor the newer format.
        self.assertNotIn('retired', old)
        self.assertEqual(old['lockFormat'], 2)
        identity = {k: v for k, v in lock.items() if k not in ('harness', 'lockFormat', 'retired')}
        self.assertEqual(specification.load(self.root)[1], identity)
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(self.cli('--check', '--check-identity', digest).returncode, 0)
        # A run compiled against this packet records against the lock that carries the retirements.
        self.write('WORK.md', 'SPEC: .fluxpoint-spec.json\n\n## Evidence\n\n'
                   '| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        self.write('result.json', json.dumps({'outcome': 'COMPLETE', 'specification': identity}))
        recorded = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'), '--root', '.',
                                   '--run-id', 'after-retirement', '--result', 'result.json'],
                                  cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=60,
                                  env=dict(os.environ, PYTHONIOENCODING='utf-8', FPL_MEMORY_INDEX='0'))
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        artifact = json.loads((self.root / '.claude/fluxpoint/runs/after-retirement.json').read_text())
        self.assertEqual(artifact['harness']['changedPaths'], [])
        # A record that cannot be read as written refuses, whatever is asked of the lock.
        sample = dict(lock['retired']['tests/helper.py'])
        for change, needle in (
                (lambda bad: bad.update(lockFormat=2), 'lock retired'),
                (lambda bad: bad.update(lockFormat=4), 'unsupported specification lock format'),
                (lambda bad: bad.update(retired=[]), 'lock retired'),
                (lambda bad: bad['retired']['tests/helper.py'].update(reason=' '), 'lock retired'),
                (lambda bad: bad['retired']['tests/helper.py'].update(reason='two\nlines'), 'lock retired'),
                (lambda bad: bad['retired']['tests/helper.py'].update(sha256='abc'), 'lock retired'),
                (lambda bad: bad['retired']['tests/helper.py'].update(when='now'), 'lock retired'),
                (lambda bad: bad['retired'].update({'../outside.py': sample}), 'inside the repository'),
                (lambda bad: bad['retired'].update({'tests/test_landing.py': sample}), 'still frozen')):
            with self.subTest(needle=needle, change=change.__code__.co_firstlineno):
                bad = json.loads(json.dumps(lock))
                change(bad)
                self.write(specification.LOCK, json.dumps(bad, indent=2) + '\n')
                for args in (('--check',), ('--lock',), ('--inputs',)):
                    refused = self.cli(*args)
                    self.assertEqual(refused.returncode, 1, args)
                    self.assertIn(needle, refused.stderr)

    def test_packet_revision_retains_an_archive_no_longer_referenced(self):
        self.lock()
        self.doc['checks'][0]['scope'] = 'New directly written reviewed scope'
        self.save()
        revised = self.lock()
        self.assertNotIn('references', revised)
        self.assertIn('archive.json', revised['harness'])
        self.assertEqual(self.cli('--touches', 'archive.json').returncode, 4)

    def test_holding_checks_are_reported_without_changing_gate_commands(self):
        self.doc['checks'].append(dict(self.doc['checks'][0], id='other-check',
                                      argv=['{python}', '-m', 'pytest', 'tests/test_other.py']))
        self.doc['requirements'][0]['checks'].append('other-check')
        self.write('tests/test_other.py', 'from prod import other\ndef test_other(): assert other.OTHER\n')
        self.save()
        self.lock()
        reached = self.report('--reach', 'prod.signer')
        self.assertEqual([row['check'] for row in reached['checks']], ['spec-tests'])
        result = self.cli('--touches', 'tests/test_landing.py')
        self.assertEqual(json.loads(result.stdout)['conflicts'][0]['checks'], ['spec-tests'])
        before = (self.root / specification.LOCK).read_bytes()
        self.report('--reach', 'prod.signer')
        self.assertEqual((self.root / specification.LOCK).read_bytes(), before)
        self.assertEqual(specification.load(self.root)[0]['checks'][1]['argv'], self.doc['checks'][1]['argv'])

    def test_malformed_support_refuses_lock_harness_even_after_attested_gate(self):
        self.lock()
        self.write('.fluxpoint-gates.json', json.dumps({'version': 1, 'gates': {
            'verify': '"' + sys.executable + '" -c "print(123)"'}}))
        self.write('tests/helper.py', 'def invalid(:\n')
        self.commit()
        attest = subprocess.run([sys.executable, str(PLUGIN / 'scripts/attest.py'), '--run', 'verify'],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(attest.returncode, 0, attest.stderr)
        before = (self.root / specification.LOCK).read_bytes()
        self.assertEqual(self.cli('--check').returncode, 0)  # Identity remains separate.
        for args in (('--lock',), ('--lock-harness', '--gate', 'verify')):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('cannot parse locked Python input tests/helper.py', result.stderr)
            self.assertEqual((self.root / specification.LOCK).read_bytes(), before)

    def test_attested_harness_relock_grows_legacy_map_to_the_same_closure(self):
        lock = self.lock()
        expected = lock.pop('harness')
        self.write(specification.LOCK, json.dumps(lock))
        self.write('.fluxpoint-gates.json', json.dumps({'version': 1, 'gates': {
            'verify': '"' + sys.executable + '" -c "print(123)"'}}))
        self.commit()
        self.assertEqual(self.cli('--lock-harness', '--gate', 'verify').returncode, 1)
        attest = subprocess.run([sys.executable, str(PLUGIN / 'scripts/attest.py'), '--run', 'verify'],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(attest.returncode, 0, attest.stderr)
        result = self.cli('--lock-harness', '--gate', 'verify')
        self.assertEqual(result.returncode, 0, result.stderr)
        current = json.loads((self.root / specification.LOCK).read_text())
        self.assertEqual(current['harness'], expected)
        self.assertEqual({k: v for k, v in current.items() if k != 'harness'}, lock)

    def test_explicit_directory_only_expands_tracked_files(self):
        self.doc['harnessInputs'] = ['extra-fixtures']
        self.write('extra-fixtures/signer.json', '{"signed": false}')
        self.write('.gitignore', '*.log\n')
        self.save()
        self.commit()
        # An ignored file is outside the lock on purpose, and nothing is said about it.
        self.write('extra-fixtures/scratch.log', 'ignored\n')
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        lock = json.loads((self.root / specification.LOCK).read_text())
        self.assertIn('extra-fixtures/signer.json', lock['harness'])
        self.assertNotIn('extra-fixtures/scratch.log', lock['harness'])
        self.assertEqual(self.cli('--touches', 'extra-fixtures').returncode, 4)
        self.assertEqual(self.cli('--check').stderr, '')

    UNTRACKED = 'untracked file in a locked directory: stage it or ignore it: '

    def gate(self):
        """Commit a declared gate and attest it at HEAD, as --lock-harness requires."""
        self.write('.fluxpoint-gates.json', json.dumps({'version': 1, 'gates': {
            'verify': '"' + sys.executable + '" -c "print(123)"'}}))
        self.commit()
        attest = subprocess.run([sys.executable, str(PLUGIN / 'scripts/attest.py'), '--run', 'verify'],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(attest.returncode, 0, attest.stderr)

    def test_untracked_file_in_a_locked_directory_refuses_lock_and_warns_on_check(self):
        self.doc['harnessInputs'] = ['preflight']
        self.doc['checks'][0]['argv'] = ['{python}', 'preflight/check_a.py']
        self.write('preflight/check_a.py', 'print("a")\n')
        self.write('.gitignore', '*.log\n')
        self.save()
        self.gate()
        self.lock()
        before = (self.root / specification.LOCK).read_bytes()
        # A check no locked path imports, a nested one and an ignored scratch file arrive unstaged.
        self.write('preflight/check_b.py', 'print("b")\n')
        self.write('preflight/deeper/check_c.py', 'print("c")\n')
        self.write('preflight/run.log', 'scratch\n')
        unstaged = ['preflight/check_b.py', 'preflight/deeper/check_c.py']
        for args in (('--lock',), ('--lock-harness', '--gate', 'verify')):
            with self.subTest(args=args):
                refused = self.cli(*args)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn('specification: ' + self.UNTRACKED + ', '.join(unstaged), refused.stderr)
                self.assertNotIn('run.log', refused.stderr)
                self.assertEqual((self.root / specification.LOCK).read_bytes(), before)
        # Everywhere else it is a warning: an unstaged scratch file must not turn a gate red.
        for args in (('--check',), ('--check', '--strict-harness'), ('--run',)):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sorted(line for line in result.stderr.splitlines() if line),
                                 ['specification: warning: ' + self.UNTRACKED + rel for rel in unstaged])
        for args in (('--inputs',), ('--inputs', '--closure'), ('--reach', 'preflight/check_a.py')):
            with self.subTest(args=args):
                self.assertEqual(self.report(*args)['warnings'], [self.UNTRACKED + rel for rel in unstaged])
        touched = self.cli('--touches', 'preflight/check_a.py')
        self.assertEqual(touched.returncode, 4, touched.stderr)
        self.assertEqual(json.loads(touched.stdout)['warnings'], [self.UNTRACKED + rel for rel in unstaged])
        # Staging one file leaves the other named; ignoring that one clears the refusal.
        self.git('add', 'preflight/check_b.py')
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('specification: ' + self.UNTRACKED + 'preflight/deeper/check_c.py\n', refused.stderr)
        self.write('.gitignore', '*.log\npreflight/deeper/\n')
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        harness = json.loads((self.root / specification.LOCK).read_text())['harness']
        self.assertIn('preflight/check_b.py', harness)
        self.assertEqual([p for p in harness if 'check_c' in p or p.endswith('.log')], [])
        self.assertEqual(self.cli('--check').stderr, '')

    def test_untracked_file_under_a_directory_a_check_names_refuses_lock(self):
        self.doc['checks'][0]['argv'] = ['{python}', '-m', 'pytest', 'tests']
        self.save()
        self.commit()
        self.lock()
        self.write('tests/test_unstaged.py', 'def test_unstaged(): pass\n')
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('specification: ' + self.UNTRACKED + 'tests/test_unstaged.py\n', refused.stderr)
        self.assertIn('specification: warning: ' + self.UNTRACKED + 'tests/test_unstaged.py',
                      self.cli('--check').stderr)
        # An unstaged file that a locked test imports is in the closure: the lock freezes it.
        self.git('add', 'tests/test_unstaged.py')
        self.write('tests/test_landing.py', 'from tests import helper, unstaged_helper\n\n'
                   'def test_sign():\n    assert helper.sign()\n')
        self.write('tests/unstaged_helper.py', 'VALUE = 1\n')
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        harness = json.loads((self.root / specification.LOCK).read_text())['harness']
        self.assertTrue({'tests/test_unstaged.py', 'tests/unstaged_helper.py'} <= set(harness))
        self.assertEqual(self.cli('--check').stderr, '')
        # So is an unstaged file the packet names outright.
        self.doc['harnessInputs'] = ['tests/data/named.json']
        self.write('tests/data/named.json', '{}')
        self.save()
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tests/data/named.json', json.loads((self.root / specification.LOCK).read_text())['harness'])

    def test_packet_lock_baseline_gates_and_archives_under_a_locked_root_are_not_reported(self):
        # The whole repository is a directory input, and only the support files are staged.
        self.doc['checks'][0]['argv'] = ['{python}', '-m', 'pytest', '.']
        self.save()
        self.write(specification.BASELINE, '{}')
        self.write('.fluxpoint-gates.json', json.dumps({'version': 1, 'gates': {'verify': 'true'}}))
        self.git('init', '-q')
        self.git('add', 'tests', 'src', 'conftest.py', 'pyproject.toml')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Fixture')
        for attempt in ('first lock', 'lock over its own unstaged lock'):
            with self.subTest(attempt=attempt):
                result = self.cli('--lock')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.cli('--check').stderr, '')
        self.write('notes.txt', 'scratch\n')
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('specification: ' + self.UNTRACKED + 'notes.txt\n', refused.stderr)
        self.assertEqual(self.cli('--check').stderr.strip(), 'specification: warning: ' + self.UNTRACKED + 'notes.txt')

    def test_byproducts_of_running_the_checks_do_not_refuse_the_lock(self):
        # Running a Python check writes tests/__pycache__, and a file manager
        # drops .DS_Store. A repository that ignores neither could not lock
        # its harness right after the gate that ran its checks.
        self.doc['harnessInputs'] = ['preflight']
        self.doc['checks'][0]['argv'] = ['{python}', 'preflight/check_a.py']
        self.write('preflight/check_a.py', 'print("a")\n')
        self.write('.gitignore', '.claude/\n')
        self.save()
        self.gate()
        self.lock()
        self.write('preflight/__pycache__/check_a.cpython-311.pyc', 'bytecode')
        self.write('preflight/deeper/__pycache__/x.cpython-39.pyc', 'bytecode')
        self.write('preflight/.DS_Store', 'finder')
        for args in (('--lock',), ('--lock-harness', '--gate', 'verify')):
            with self.subTest(args=args):
                done = self.cli(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
        checked = self.cli('--check')
        self.assertNotIn(self.UNTRACKED, checked.stderr)
        # A real source file beside them is still named.
        self.write('preflight/check_b.py', 'print("b")\n')
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn(self.UNTRACKED + 'preflight/check_b.py', refused.stderr)
        self.assertNotIn('pyc', refused.stderr)

    def test_a_file_a_locked_test_can_load_is_never_passed_over_as_a_byproduct(self):
        # A compiled module beside the sources is imported when no source is,
        # and node_modules or a virtual environment is where a require or an
        # import looks. Passing those over left out of the lock a file that a
        # locked test runs, and an edit to it was no drift.
        self.doc['harnessInputs'] = ['preflight']
        self.doc['checks'][0]['argv'] = ['{python}', 'preflight/check_a.py']
        self.write('preflight/check_a.py', 'print("a")\n')
        self.write('.gitignore', '.claude/\n')
        self.save()
        self.gate()
        self.lock()
        for rel in ('preflight/oracle.pyc', 'preflight/venv/test_extra.py', 'preflight/.venv/test_extra.py',
                    'preflight/node_modules/helper/index.js', 'preflight/.tox/test_extra.py',
                    'preflight/__pycache__/hidden.py'):
            with self.subTest(rel=rel):
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('x', encoding='utf-8')
                refused = self.cli('--lock')
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(self.UNTRACKED + rel, refused.stderr)
                self.assertIn(self.UNTRACKED + rel, self.cli('--check').stderr)
                path.unlink()

    def test_an_untracked_nested_repository_is_named_as_what_it_is(self):
        # git reports a nested repository as one path. Staging it does not make
        # it a file a lock can freeze, so the advice has to say something else.
        self.doc['harnessInputs'] = ['preflight']
        self.doc['checks'][0]['argv'] = ['{python}', 'preflight/check_a.py']
        self.write('preflight/check_a.py', 'print("a")\n')
        self.save()
        self.gate()
        self.lock()
        nested = self.root / 'preflight' / 'vendored'
        nested.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=nested, check=True, capture_output=True)
        (nested / 'lib.py').write_text('X = 1\n', encoding='utf-8')
        subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'add', '.'], cwd=nested, check=True, capture_output=True)
        subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'x'], cwd=nested, check=True, capture_output=True)
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn('preflight/vendored', refused.stderr)
        self.assertIn('nested repository', refused.stderr)
        self.assertNotIn('stage it', refused.stderr)

    def test_a_working_directory_flag_that_names_the_store_drops_nothing_else(self):
        # A check may run in the store. Its script is still code the lock holds.
        self.write('tools/check_runs.py', 'print("runs")\n')
        self.write('.gitignore', '.claude/fluxpoint/\n')
        for flag in ('--dir', '--cwd', '--prefix'):
            for present in (False, True):
                with self.subTest(flag=flag, present=present):
                    store = self.root / '.claude' / 'fluxpoint' / 'runs'
                    if present:
                        store.mkdir(parents=True, exist_ok=True)
                        (store / 'wf_a.json').write_text('{}', encoding='utf-8')
                    self.doc['checks'][0]['argv'] = ['{python}', 'tools/check_runs.py', flag, '.claude/fluxpoint/runs']
                    self.save()
                    harness = self.lock()['harness']
                    self.assertIn('tools/check_runs.py', harness)
                    self.assertEqual([p for p in harness if p.startswith('.claude/')], [])

    def test_a_packages_own_state_store_is_never_an_input_either(self):
        # A session rooted in a package keeps its state in pkg/.claude/fluxpoint.
        # A root packet whose check runs in the package and reads that store
        # must not freeze it: the next run recorded there would be drift.
        self.write('pkg/run.py', 'print("pkg")\n')
        self.write('pkg/.claude/fluxpoint/runs/first.json', '{}')
        self.write('pkg/.claude/fluxpoint/ledger.jsonl', '{"n": 1}\n')
        self.doc['checks'][0]['argv'] = ['{python}', 'run.py', '--cwd', 'pkg', '--runs-dir', '.claude/fluxpoint/runs',
                                         '--ledger', '.claude/fluxpoint/ledger.jsonl']
        self.save()
        harness = self.lock()['harness']
        self.assertIn('pkg/run.py', harness)
        self.assertEqual([rel for rel in harness if '.claude/fluxpoint' in rel], [])
        self.write('pkg/.claude/fluxpoint/runs/second.json', '{}')
        self.write('pkg/.claude/fluxpoint/ledger.jsonl', '{"n": 1}\n{"n": 2}\n')
        checked = self.cli('--check', '--strict-harness')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        # Named from the root as well, and declared outright it refuses as the root's store does.
        self.doc['checks'][0]['argv'] = ['{python}', 'pkg/run.py', 'pkg/.claude/fluxpoint/ledger.jsonl']
        self.save()
        self.assertEqual([rel for rel in self.lock()['harness'] if '.claude/fluxpoint' in rel], [])
        self.doc['harnessInputs'] = ['pkg/.claude/fluxpoint/ledger.jsonl']
        self.save()
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn('state store', refused.stderr)

    def test_a_tracked_file_under_a_store_shaped_directory_is_a_fixture(self):
        # A project that tests tooling for this plugin keeps a recorded run as
        # a fixture, and commits it. What Git tracks under a `.claude/fluxpoint`
        # deeper in the tree is source: only what a session leaves there
        # untracked is state. The root's own store is state either way.
        fixture = 'tests/fixtures/app/.claude/fluxpoint/runs/r1.json'
        self.write(fixture, '{"runId": "r1"}')
        self.write('pkg/run.py', 'print("pkg")\n')
        self.write('pkg/.claude/fluxpoint/fixture.json', '{}')
        self.write('.claude/fluxpoint/decisions.jsonl', '{"id": "first"}\n')
        self.doc['harnessInputs'] = ['tests']
        self.doc['checks'][0]['argv'] = ['{python}', 'run.py', '--cwd', 'pkg', '--path', '.claude/fluxpoint/fixture.json']
        self.save()
        self.commit()
        # A run recorded in the package after the commit is state beside the fixture.
        self.write('pkg/.claude/fluxpoint/runs/later.json', '{}')
        harness = self.lock()['harness']
        self.assertIn(fixture, harness)
        self.assertIn('pkg/.claude/fluxpoint/fixture.json', harness)
        self.assertEqual([rel for rel in harness if rel.startswith('.claude/') or 'later.json' in rel], [])
        self.write(fixture, '{"runId": "r1", "edited": true}')
        drifted = self.cli('--check', '--strict-harness')
        self.assertEqual(drifted.returncode, 1, drifted.stdout)
        self.assertIn(fixture, drifted.stderr)
        # Named from the root, the package's tracked fixture is the same file.
        self.write(fixture, '{"runId": "r1"}')
        self.doc['checks'][0]['argv'] = ['{python}', 'pkg/run.py', 'pkg/.claude/fluxpoint/fixture.json']
        self.save()
        self.assertIn('pkg/.claude/fluxpoint/fixture.json', self.lock()['harness'])

    def test_directory_inputs_and_the_state_store_follow_one_coherent_rule(self):
        # No ignore rule covers the store here: the rule is the path, never the ignore file.
        self.root = self.root.resolve()
        store = self.root / '.claude' / 'fluxpoint'
        self.doc['harnessInputs'] = ['.claude', 'fixtures']
        self.doc['checks'][0]['argv'] = ['{python}', 'tests/test_landing.py', str(store / 'runs'),
                                         str(store / 'decisions.jsonl')]
        self.write('.claude/settings.json', '{}')
        self.write('fixtures/case.json', '{}')
        self.save()
        self.commit()
        self.write('.claude/fluxpoint/runs/first-run.json', '{}')
        self.write('.claude/fluxpoint/decisions.jsonl', '{"id": "first"}\n')
        # A directory input is the tracked files under it. The store is no input at all,
        # so its unstaged files are neither frozen nor reported.
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        harness = json.loads((self.root / specification.LOCK).read_text())['harness']
        self.assertTrue({'.claude/settings.json', 'fixtures/case.json'} <= set(harness))
        self.assertEqual([p for p in harness if p.startswith('.claude/fluxpoint')], [])
        self.assertEqual(self.cli('--check', '--strict-harness').stderr, '')
        # Beside the store, an unstaged file in the same directory input is named.
        self.write('.claude/notes.md', 'scratch\n')
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('specification: ' + self.UNTRACKED + '.claude/notes.md\n', refused.stderr)
        self.assertEqual(self.cli('--check').stderr.strip(),
                         'specification: warning: ' + self.UNTRACKED + '.claude/notes.md')
        (self.root / '.claude/notes.md').unlink()
        # A directory with nothing tracked would freeze nothing, so it refuses outright.
        self.write('.gitignore', 'only-ignored/\n')
        self.write('only-unstaged/a.json', '{}')
        self.write('only-ignored/a.json', '{}')
        (self.root / 'empty').mkdir()
        self.git('add', '.gitignore')
        for rel in ('only-unstaged', 'only-ignored', 'empty'):
            with self.subTest(rel=rel):
                self.doc['harnessInputs'] = ['.claude', 'fixtures', rel]
                self.save()
                refused = self.cli('--lock')
                self.assertEqual(refused.returncode, 1)
                self.assertIn('packet input directory has no tracked files: ' + rel, refused.stderr)

    def test_directory_without_tracked_files_refuses(self):
        self.doc['harnessInputs'] = ['extra-fixtures']
        self.write('extra-fixtures/untracked.json', '{}')
        self.save()
        self.assertEqual(self.cli('--lock').returncode, 1)

    def store_checkout(self):
        """A checkout whose checks name its own state store, by flag and by position."""
        self.root = self.root.resolve()  # Absolute argv paths are compared with the resolved root.
        self.store = self.root / '.claude' / 'fluxpoint'
        compiler = str(PLUGIN / 'scripts/compile-graph.py')
        self.doc['checks'][0]['argv'] = ['{python}', compiler, 'WORK.md', '--check',
                                         '--decisions', str(self.store / 'decisions.jsonl'),
                                         '--runs-dir', str(self.store / 'runs')]
        self.doc['checks'].append(dict(self.doc['checks'][0], id='store-reader', argv=[
            '{python}', 'tests/test_landing.py', str(self.store / 'runs'), str(self.store / 'decisions.jsonl'),
            '.claude/fluxpoint/runs/history', '--runs-dir=.claude/fluxpoint/runs']))
        self.doc['requirements'][0]['checks'].append('store-reader')
        self.write('.gitignore', '.claude/fluxpoint/\n')
        self.write('WORK.md', 'STATUS: READY\nSPEC: .fluxpoint-spec.json\n\n```json graph-ir\n'
                   + json.dumps(fixtures.graph()) + '\n```\n')
        # The Evidence row goes to a file no check names, so recording moves no input.
        self.write('EVIDENCE.md', '## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        self.save()
        self.commit()

    def assert_store_is_no_input(self, moment):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        with self.subTest(moment=moment):
            for args in (('--check',), ('--check', '--strict-harness'), ('--lock',)):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0, ' '.join(args) + ': ' + result.stderr)
            compiled = subprocess.run([sys.executable, str(PLUGIN / 'scripts/compile-graph.py'), 'WORK.md', '--check'],
                                      cwd=self.root, capture_output=True, text=True, encoding='utf-8', env=env, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            harness = json.loads((self.root / specification.LOCK).read_text())['harness']
            listed = {row['path'] for row in self.report('--inputs', '--closure')['inputs']}
            for paths in (set(harness), listed):
                self.assertEqual(sorted(p for p in paths if p.startswith('.claude/')), [])
            self.assertIn('tests/test_landing.py', harness)
            self.assertEqual(self.cli('--touches', '.claude/fluxpoint/runs',
                                      '.claude/fluxpoint/decisions.jsonl').returncode, 0)

    def test_recorded_run_and_decisions_in_the_state_store_are_never_packet_inputs(self):
        self.store_checkout()
        self.lock()
        self.assert_store_is_no_input('before the store exists')
        self.write('.claude/fluxpoint/decisions.jsonl', '{"id": "first"}\n')
        self.assert_store_is_no_input('a decision is recorded')
        lock = json.loads((self.root / specification.LOCK).read_text())
        identity = {k: v for k, v in lock.items() if k not in ('harness', 'lockFormat')}
        self.write('result.json', json.dumps({'outcome': 'COMPLETE', 'specification': identity}))
        recorded = subprocess.run([sys.executable, str(PLUGIN / 'scripts/record-run.py'), '--root', '.',
                                   '--run-id', 'first-run', '--result', 'result.json', '--evidence', 'EVIDENCE.md'],
                                  cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=60,
                                  env=dict(os.environ, PYTHONIOENCODING='utf-8', FPL_MEMORY_INDEX='0'))
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertIn('| first-run |', (self.root / 'EVIDENCE.md').read_text(encoding='utf-8'))
        self.assertTrue((self.store / 'runs/first-run.json').is_file())
        self.assertTrue((self.store / 'runs/history').is_dir())
        self.assert_store_is_no_input('a run is recorded')
        self.write('.claude/fluxpoint/decisions.jsonl', '{"id": "first"}\n{"id": "second"}\n')
        self.assert_store_is_no_input('another decision is recorded')
        shutil.rmtree(self.store / 'runs')
        (self.store / 'runs').mkdir()
        self.assert_store_is_no_input('the runs directory is emptied')

    def test_harness_inputs_naming_the_state_store_refuse_as_file_or_directory(self):
        self.store_checkout()
        self.write('.claude/fluxpoint/decisions.jsonl', '{"id": "first"}\n')
        self.write('.claude/fluxpoint/runs/first-run.json', '{}')
        for rel in ('.claude/fluxpoint', '.claude/fluxpoint/runs', '.claude/fluxpoint/decisions.jsonl',
                    '.claude/fluxpoint/runs/first-run.json', '.claude/fluxpoint/not-written-yet.json'):
            with self.subTest(rel=rel):
                self.doc['harnessInputs'] = ['tests/helper.py', rel]
                self.save()
                result = self.cli('--lock')
                self.assertEqual(result.returncode, 1)
                self.assertIn('harnessInputs names the plugin state store, which is never a packet input: ' + rel,
                              result.stderr)
        # The rule is the store, not the name: a sibling directory stays an ordinary input.
        self.write('.claude/fluxpoint-fixtures/case.json', '{}')
        self.git('add', '.claude/fluxpoint-fixtures/case.json')
        self.doc['harnessInputs'] = ['.claude/fluxpoint-fixtures']
        self.save()
        self.assertIn('.claude/fluxpoint-fixtures/case.json', self.lock()['harness'])
        # An explicit path flag refuses a missing input elsewhere, and never asks whether the store exists.
        self.doc.pop('harnessInputs')
        for argv, code in ((['node', 'adapter.js', '--path', '.claude/fluxpoint/not-written-yet.json'], 0),
                           (['node', 'adapter.js', '--config=.claude/fluxpoint/runs'], 0),
                           (['node', 'adapter.js', '--path', 'tests/not-written-yet.json'], 1)):
            with self.subTest(argv=argv):
                self.doc['checks'][0]['argv'] = argv
                self.save()
                self.assertEqual(self.cli('--lock').returncode, code)

    def test_tracked_file_in_the_state_store_is_not_expanded_from_a_directory_input(self):
        self.store_checkout()
        self.write('.claude/fluxpoint/decisions.jsonl', '{"id": "first"}\n')
        self.write('.claude/settings.json', '{}')
        # init ignores the store, yet a file in it can still be committed by force.
        self.git('add', '-f', '.claude/fluxpoint/decisions.jsonl', '.claude/settings.json')
        self.doc['harnessInputs'] = ['.claude']
        self.save()
        harness = self.lock()['harness']
        self.assertIn('.claude/settings.json', harness)
        self.assertEqual([p for p in harness if p.startswith('.claude/fluxpoint')], [])
        self.write('.claude/fluxpoint/decisions.jsonl', '{"id": "first"}\n{"id": "second"}\n')
        self.assertEqual(self.cli('--check', '--strict-harness').returncode, 0)

    def test_store_path_frozen_by_an_older_lock_leaves_at_the_next_lock(self):
        self.store_checkout()
        self.write('.fluxpoint-gates.json', json.dumps({'version': 1, 'gates': {
            'verify': '"' + sys.executable + '" -c "print(123)"'}}))
        self.commit()
        attest = subprocess.run([sys.executable, str(PLUGIN / 'scripts/attest.py'), '--run', 'verify'],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(attest.returncode, 0, attest.stderr)
        frozen = '.claude/fluxpoint/decisions.jsonl'
        for args in (('--lock',), ('--lock-harness', '--gate', 'verify')):
            with self.subTest(args=args):
                self.write(frozen, '{"id": "first"}\n')
                lock = self.lock()
                # An older lock froze the store file its check names.
                lock['harness'][frozen] = hashlib.sha256(b'{"id": "first"}\n').hexdigest()
                self.write(specification.LOCK, json.dumps(lock, indent=2) + '\n')
                self.assertEqual(self.cli('--check', '--strict-harness').returncode, 0)
                self.write(frozen, '{"id": "first"}\n{"id": "second"}\n')
                # The lock on disk is what is enforced until it is written again.
                drifted = self.cli('--check', '--strict-harness')
                self.assertEqual(drifted.returncode, 1)
                self.assertIn(frozen, drifted.stderr)
                relocked = self.cli(*args)
                self.assertEqual(relocked.returncode, 0, relocked.stderr)
                self.assertIn('specification: dropped ' + frozen + ' from the harness map: '
                              'the plugin state store is never a packet input', relocked.stdout)
                harness = json.loads((self.root / specification.LOCK).read_text())['harness']
                self.assertEqual(set(harness), set(lock['harness']) - {frozen})
                self.assertEqual(self.cli('--check', '--strict-harness').returncode, 0)
                self.assertNotIn('dropped', self.cli(*args).stdout)

    def test_go_package_patterns_are_not_directories(self):
        # Win32 strips a component's trailing dots, so on Windows '<root>/...' names the root.
        self.write('go.mod', 'module example.com/signer\n')
        self.write('pkg/sign_test.go', 'package pkg\n')
        self.commit()
        for argv in (['go', 'test', './...'], ['go', 'test', './pkg/...'], ['go', 'test', './pkg ']):
            with self.subTest(argv=argv):
                self.doc['checks'][0]['argv'] = argv
                self.save()
                self.assertEqual([p for p in self.lock()['harness'] if '...' in p], [])
        self.doc['checks'][0]['argv'] = ['go', 'test', './pkg']
        self.save()
        self.assertIn('pkg/sign_test.go', self.lock()['harness'])
        self.doc['checks'][0]['argv'] = ['node', 'adapter.js', '--path', './pkg/...']
        self.save()
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('check input does not exist: ./pkg/...', refused.stderr)
        self.doc['checks'][0]['argv'] = ['go', 'test', '../pkg']
        self.save()
        refused = self.cli('--lock')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('packet input must stay inside the repository', refused.stderr)

    def frontend(self, cwd=True):
        self.write('frontend/package.json', '{"name":"front"}')
        self.write('frontend/tests/landing/sign.test.ts', "import { value } from './helper'\n")
        self.write('frontend/tests/landing/helper.ts', 'export const value = 1\n')
        self.write('frontend/vitest.config.ts', 'export default {}\n')
        self.write('adapter.js', '// invokes a package test command\n')
        self.doc['checks'][0]['argv'] = (['npm', '--prefix', 'frontend', 'test', '--', '--path', 'tests/landing']
                                        if cwd else ['node', 'adapter.js', '--path', 'tests/landing'])
        self.save()
        self.commit()

    def test_package_cwd_and_unique_package_relative_directory_with_vitest_config(self):
        self.frontend()
        lock = self.lock()
        expected = {'frontend/tests/landing/sign.test.ts', 'frontend/tests/landing/helper.ts',
                    'frontend/vitest.config.ts'}
        self.assertTrue(expected <= set(lock['harness']))
        self.doc['checks'][0]['argv'] = ['node', 'adapter.js', '--path', 'tests/landing']
        self.save()
        self.assertTrue(expected <= set(self.lock()['harness']))

    def test_in_root_parent_imports_and_pytest_pythonpath_are_normalized(self):
        self.frontend()
        self.write('frontend/tests/landing/sign.test.ts', "import { value } from '../support/helper'\n")
        self.write('frontend/tests/support/helper.ts', 'export const value = 1\n')
        self.write('tests/pyproject.toml', '[tool.pytest.ini_options]\npythonpath = ["../src"]\n')
        self.commit()
        lock = self.lock()
        self.assertIn('frontend/tests/support/helper.ts', lock['harness'])
        # Exercise a package-local pytest config independently of the frontend check.
        self.doc['checks'][0]['argv'] = ['{python}', '-m', 'pytest', 'tests/test_landing.py']
        self.save()
        self.lock()
        self.assertTrue(self.report('--reach', 'prod.signer')['checks'])

    def test_parent_import_outside_repository_refuses(self):
        self.frontend()
        self.write('frontend/tests/landing/sign.test.ts', "import '../../../../outside'\n")
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 1)
        self.assertIn('inside the repository', result.stderr)

    def test_parent_import_outside_repository_refusal_names_file_and_line(self):
        self.frontend()
        self.write('frontend/tests/landing/sign.test.ts',
                   "import { value } from './helper'\n"
                   "import {\n  other,\n} from '../../../../outside'\n")
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 1)
        self.assertIn('import/config path must stay inside the repository', result.stderr)
        self.assertIn("'../../../../outside' at frontend/tests/landing/sign.test.ts:4", result.stderr)
        # The same refusal reaches a preflight over an existing lock.
        self.write('frontend/tests/landing/sign.test.ts', "import { value } from './helper'\n")
        self.lock()
        self.write('frontend/tests/landing/helper.ts',
                   "export const value = 1\nexport { other } from '../../../../outside'\n")
        for args in (('--touches', 'frontend/tests/landing/helper.ts'), ('--inputs', '--closure'),
                     ('--reach', 'frontend/tests/landing/helper.ts')):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("'../../../../outside' at frontend/tests/landing/helper.ts:2", result.stderr)

    def test_unresolved_relative_import_warns_with_file_and_line(self):
        self.frontend()
        self.write('frontend/tests/landing/sign.test.ts',
                   "import { value } from './helper'\n"
                   "import './fixture.css'\n"
                   "import {\n  gone,\n} from './missing_module'\n"
                   "export { p } from './.../provider'\n"
                   "const late = require('../support/absent')\n")
        self.write('frontend/tests/landing/fixture.css', 'body {}\n')
        self.commit()
        expected = ["import './.../provider' at frontend/tests/landing/sign.test.ts:6"
                    ' does not resolve to a repository file',
                    "import './missing_module' at frontend/tests/landing/sign.test.ts:5"
                    ' does not resolve to a repository file',
                    "import '../support/absent' at frontend/tests/landing/sign.test.ts:7"
                    ' does not resolve to a repository file']
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        for warning in expected:
            self.assertIn('specification: warning: ' + warning, result.stderr)
        # A specifier that resolves, with or without a suffix, is silent.
        self.assertNotIn('./helper', result.stderr)
        self.assertNotIn('fixture.css', result.stderr)
        harness = json.loads((self.root / specification.LOCK).read_text())['harness']
        self.assertIn('frontend/tests/landing/fixture.css', harness)
        self.assertEqual([p for p in harness if 'missing_module' in p or 'absent' in p or '...' in p], [])
        touched = self.cli('--touches', 'frontend/tests/landing/sign.test.ts')
        self.assertEqual(touched.returncode, 4, touched.stderr)
        reports = [json.loads(touched.stdout), self.report('--inputs', '--closure'),
                   self.report('--reach', 'frontend/tests/landing/helper.ts')]
        for report in reports:
            self.assertEqual([w for w in report['warnings'] if 'does not resolve' in w], sorted(expected))
        # The plain input list does not walk imports, so it has nothing to report.
        self.assertEqual(self.report('--inputs')['warnings'], [])

    def test_quoted_import_shapes_are_not_imports_and_hide_no_real_import(self):
        self.frontend()
        # Every decoy leaves the repository or names no file: read as an import,
        # it would refuse the lock or warn. Real imports sit between the shapes.
        self.write('frontend/tests/landing/sign.test.ts',
                   "#!/usr/bin/env node\n"
                   "/**\n"
                   " * Type-only re-exports keep `import type { ... } from '.../some_module'`\n"
                   " * and `export * from '../../../../jsdoc'`.\n"
                   " */\n"
                   "/*\nimport a from '../../../../block'\nrequire('./decoy-block')\n*/\n"
                   "const half = (w, n) => w / 2 / n // import('../../../../trailing')\n"
                   "const url = 'http://example.invalid/*' + \"import b from './decoy-string'\"\n"
                   "import { one } from '../support/one'\n"
                   "const glob = 'src/*.ts', tick = '`', quote = \"it's\"\n"
                   "const doc = `\nimport c from '../../../../template'\n// it's ${ `nested ${glob} from './decoy-nested'` } /*\n`\n"
                   "const brace = `${ {a: 1}.a }${ f('}') }${ half /* } */ } from '../../../../expression'`\n"
                   "const escaped = `\\` \\${ from '../../../../escaped'`\n"
                   "import { two } from '../support/two'\n"
                   "const scheme = /https?:\\/\\//, ends = (s) => s.replace(/\\/*$/, ''), cls = /[/'\"`]/\n"
                   "const quotes = (s) => s.split(/'/).concat(s.split(/`/))\n"
                   "function word(s) { return /'/.test(s) }\n"
                   "if (word(url)) /[/*]/.test(url);\n"
                   "const elided = Array.from('../../../../argument'), named = { from: '../../../../property' }\n"
                   "export { three } from '../support/three'\n"
                   "const four = require('../support/four'), five = await import('../support/five')\n")
        for name in ('one', 'two', 'three', 'four', 'five'):
            self.write('frontend/tests/support/' + name + '.ts', 'export const ' + name + ' = 1\n')
        self.commit()
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('warning', result.stderr)
        harness = json.loads((self.root / specification.LOCK).read_text())['harness']
        for name in ('one', 'two', 'three', 'four', 'five'):
            self.assertIn('frontend/tests/support/' + name + '.ts', harness)
        self.assertEqual(self.report('--inputs', '--closure')['warnings'], [])
        touched = self.cli('--touches', 'frontend/tests/support/five.ts')
        self.assertEqual(touched.returncode, 4, touched.stderr)
        self.assertEqual(json.loads(touched.stdout)['warnings'], [])

    def test_issue_comment_passes_touches_and_lock_harness(self):
        self.frontend()
        self.write('.fluxpoint-gates.json', json.dumps({'version': 1, 'gates': {
            'verify': '"' + sys.executable + '" -c "print(123)"'}}))
        self.lock()
        self.write('frontend/tests/landing/helper.ts',
                   "// Type-only re-exports keep `import type { ... } from '.../some_module'`\n"
                   "/** Type-only re-exports keep `import type { ... } from '.../some_module'` */\n"
                   'export const value = 1\n')
        self.commit()
        attest = subprocess.run([sys.executable, str(PLUGIN / 'scripts/attest.py'), '--run', 'verify'],
                                cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(attest.returncode, 0, attest.stderr)
        touched = self.cli('--touches', 'frontend/tests/landing/helper.ts')
        self.assertEqual(touched.returncode, 4, touched.stderr)
        self.assertEqual(json.loads(touched.stdout)['warnings'], [])
        relocked = self.cli('--lock-harness', '--gate', 'verify')
        self.assertEqual(relocked.returncode, 0, relocked.stderr)
        self.assertNotIn('warning', relocked.stderr)
        self.assertIn('harness changed frontend/tests/landing/helper.ts', relocked.stdout)

    def test_import_text_in_comments_and_strings_is_not_an_import(self):
        self.frontend()
        self.write('frontend/tests/landing/sign.test.ts',
                   "// Type-only re-exports keep `import type { ... } from '.../provider'` working.\n"
                   "// re-exported from '../../../../shared'\n"
                   "const example = \"import { y } from '../../../../outside'\""
                   " /* import { x } from '../../../../elsewhere/x' */\n"
                   "const trim = (s) => s.replace(/\\/*$/, '')\n"
                   "const tests = (dir) => `${dir}/*.test.ts`\n"
                   "const label = `${trim('a/')}` + '//'\n"
                   "const outside = '../../../../outside'\n"
                   "import { value } from '../support/helper'\n")
        self.write('frontend/tests/support/helper.ts', 'export const value = 1\n')
        self.commit()
        self.assertIn('frontend/tests/support/helper.ts', self.lock()['harness'])
        self.assertIn('frontend/tests/support/helper.ts',
                      {row['path'] for row in self.report('--inputs', '--closure')['inputs']})

    def test_regex_after_control_condition_does_not_hide_following_import(self):
        self.frontend()
        self.write('frontend/tests/landing/sign.test.ts',
                   "if ((true)) /[/*]/.test('x');\n"
                   "while (false) /[/*]/.test('x');\n"
                   "import { value } from '../support/helper'\n")
        self.write('frontend/tests/support/helper.ts', 'export const value = 1\n')
        self.commit()
        self.assertIn('frontend/tests/support/helper.ts', self.lock()['harness'])
        self.assertIn('frontend/tests/support/helper.ts',
                      {row['path'] for row in self.report('--inputs', '--closure')['inputs']})

    def test_dots_only_segment_under_an_existing_directory_is_not_an_import(self):
        self.frontend()
        self.write('frontend/tests/landing/helper.ts', "export { p } from './.../provider'\nexport const value = 1\n")
        self.commit()
        self.assertEqual([p for p in self.lock()['harness'] if '...' in p], [])
        self.assertEqual(specification.harness_record(self.root)['changedPaths'], [])

    def test_explicit_package_cwd_does_not_fall_back_to_a_root_test_path(self):
        self.frontend()
        self.doc['checks'][0]['argv'][-1] = 'tests/root-only'
        self.write('tests/root-only/test_a.py', 'def test_a(): pass\n')
        self.save()
        self.commit()
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 1)
        self.assertIn('does not exist in its package cwd', result.stderr)

    def test_ambiguous_package_directory_refuses(self):
        self.frontend(cwd=False)
        self.write('other/package.json', '{"name":"other"}')
        self.write('other/tests/landing/a.test.ts', 'export {}\n')
        self.commit()
        result = self.cli('--lock')
        self.assertEqual(result.returncode, 1)
        self.assertIn('ambiguous package-relative check input', result.stderr)

    def test_record_and_stop_name_a_transitive_fixture_edit_and_deletion(self):
        self.lock()
        self.write('tests/fixtures.py', 'VALUE = 3\n')
        (self.root / 'tests/helper.py').unlink()
        record = specification.harness_record(self.root)
        self.assertEqual(record['changedPaths'], ['tests/fixtures.py', 'tests/helper.py'])
        self.assertIsNone(record['files']['tests/helper.py'])
        self.assertEqual(record['files']['tests/fixtures.py'], hashlib.sha256(b'VALUE = 3\n').hexdigest())
        bash = shutil.which('bash')
        if bash:
            result = subprocess.run([bash, '-c', '. "$1"; fpl_packet_trust_paths', 'probe',
                                     (PLUGIN / 'scripts/lib.sh').as_posix()], cwd=self.root,
                                    env=dict(os.environ, FPL_PY=sys.executable), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('tests/fixtures.py', result.stdout.splitlines())
            self.assertIn('tests/helper.py', result.stdout.splitlines())

    def test_legacy_stop_derives_fixture_paths_even_if_support_later_fails_to_parse(self):
        lock = self.lock()
        del lock['harness']
        self.write(specification.LOCK, json.dumps(lock))
        for source in ('VALUE = 2\n', 'def invalid(:\n'):
            self.write('tests/fixtures.py', source)
            paths = specification.packet_inputs.trust_paths(self.root, self.doc)
            self.assertIn('tests/fixtures.py', paths)

    def test_input_symlink_escape_refuses(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'helper.py'
            target.write_text('VALUE = 1\n')
            path = self.root / 'tests/helper.py'
            path.unlink()
            try:
                path.symlink_to(target)
            except OSError:
                self.skipTest('symlinks unavailable')
            result = self.cli('--lock')
            self.assertEqual(result.returncode, 1)
            self.assertIn('inside the repository', result.stderr)


if __name__ == '__main__':
    unittest.main()
