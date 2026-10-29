#!/usr/bin/env python3
"""CI coverage tracks the executable closure and verified provider launch."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
import forge_evidence as forge
spec = importlib.util.spec_from_file_location('forge_fixture', HERE / 'forge-evidence-test.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
HEAD, BASE, PREFIX = fixtures.HEAD, fixtures.BASE, fixtures.PREFIX


class CoverageFollowups(unittest.TestCase):
    def setUp(self):
        self.ci, self.api = fixtures.fixture()
        self.ci['config'] = list(self.ci['covers']['suite']['config'])
        self.status = PREFIX + '/commits/' + HEAD + '/statuses?per_page=100&page=1'

    def put(self, path, text, config=False, commits=(HEAD, BASE)):
        oid, body = fixtures.blob(text)
        self.api[PREFIX + '/git/blobs/' + oid] = body
        for commit in commits:
            tree = self.api[PREFIX + '/git/trees/' + commit + '?recursive=1']['tree']
            tree[:] = [r for r in tree if r['path'] != path]
            tree.append(dict(path=path, type='blob', mode='100644', sha=oid))
        if config:
            for paths in (self.ci['config'], self.ci['covers']['suite']['config']):
                if path not in paths:
                    paths.append(path)

    def observe(self, gate='suite'):
        return forge.observe('.', self.ci, 'pr:7', gate, api=lambda p: copy.deepcopy(self.api[p]))

    def test_dot_directory_script_is_required_and_pinned(self):
        for command in ('bash .ci/run.sh', 'bash ./.ci/run.sh', 'python .ci/gate.py'):
            with self.subTest(command=command):
                self.setUp()
                path = '.ci/gate.py' if 'python' in command else '.ci/run.sh'
                self.put('.ci/lane.yml', 'commands:\n - ' + command + '\n')
                self.put(path, 'print("run")\n' if path.endswith('.py') else 'exit 0\n')
                with self.assertRaisesRegex(ValueError, 'omits tracked file'):
                    self.observe()
                self.put(path, 'print("run")\n' if path.endswith('.py') else 'exit 0\n', config=True)
                self.assertEqual(self.observe()['exit'], 0)
                self.put(path, '# skipped checks\n', commits=(HEAD,))
                with self.assertRaisesRegex(ValueError, 'changed from pinned'):
                    self.observe()

    def test_comments_identifiers_and_literal_getattr_are_not_imports(self):
        tracked = {'.woodpecker/release.yml', 'tools/config.py', 'tools/helper.py'}
        self.assertEqual(forge.executable_references('.woodpecker/test.yml',
            '# see release.yml\ncommands:\n - git config --global user.name ci\n', tracked), set())
        self.assertEqual(forge.executable_references('conftest.py',
            'def pytest_configure(config):\n    return getattr(config, "workerinput", None)\n', tracked), set())
        self.assertEqual(forge.executable_references('conftest.py',
            '# import tools.config\nfrom tools import helper\n', tracked), {'tools/helper.py'})
        for code in ('getattr(obj, name)', 'getattr(obj, "import_module")', 'hasattr(obj, name)',
                     'hasattr(obj, "exec_module")', 'importlib.import_module(name)'):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'dynamic'):
                forge.executable_references('conftest.py', code, set())

    def test_literal_paths_in_python_strings_remain_required(self):
        self.assertEqual(forge.executable_references('gate.py', 'open(".ci/data.json")\n', {'.ci/data.json'}), {'.ci/data.json'})

    def test_all_pytest_config_names_are_implicit(self):
        for name in ('pytest.toml', '.pytest.toml', '.pytest.ini', 'pkg/tests/pytest.toml'):
            with self.subTest(name=name):
                self.setUp()
                self.put(name, '[pytest]\n')
                with self.assertRaisesRegex(ValueError, 'implicit build/test config'):
                    self.observe()

    def test_prove_ci_requires_its_own_complete_pinned_config(self):
        self.assertEqual(self.observe('ci')['exit'], 0)
        del self.ci['config']
        with self.assertRaisesRegex(ValueError, 'coverage.*ci'):
            self.observe('ci')
        self.ci['config'] = list(self.ci['covers']['suite']['config'])
        self.put('.ci/lane.yml', 'commands:\n - true\n', commits=(HEAD,))
        with self.assertRaisesRegex(ValueError, 'changed from pinned'):
            self.observe('ci')

    def test_woodpecker_restart_manual_and_wrong_commit_cannot_supply_green(self):
        url = 'https://ci.example.invalid/repos/28/pipeline/34/1'
        endpoint = 'woodpecker:https://ci.example.invalid/api/repos/28/pipelines/34'
        self.api[self.status][0]['target_url'] = url
        self.api[endpoint] = dict(number=34, commit=HEAD, parent=0, event='pull_request', status='success')
        self.assertEqual(self.observe()['exit'], 0)
        for change in ({'parent': 33}, {'parent': None}, {'event': 'manual'}, {'commit': BASE}):
            self.api[endpoint] = dict(number=34, commit=HEAD, parent=0, event='push', status='success', **{})
            self.api[endpoint].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'pipeline'):
                self.observe()


if __name__ == '__main__':
    unittest.main()
