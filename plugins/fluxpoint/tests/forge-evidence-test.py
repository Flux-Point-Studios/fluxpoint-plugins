#!/usr/bin/env python3
"""Source-backed forge claims, exercised with hostile API and actual CLI fixtures."""
import base64
import copy
import hashlib
import importlib.util
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
import attest
import forge_evidence as forge

HEAD, BASE = 'a' * 40, 'b' * 40
PREFIX = 'repos/{owner}/{repo}'
NONCE = 'fixture-nonce'


def blob(data):
    data = data.encode()
    oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return oid, {'sha': oid, 'encoding': 'base64', 'size': len(data),
                 'content': base64.b64encode(data).decode()}


def fixture():
    ci = {'forge': 'github', 'repository': 'fixture/repo', 'contexts': ['suite'],
          'witness': {'statusCreators': ['ci-bot'], 'checkRunApps': [42]},
          'packetSha': BASE,
          'covers': {'suite': {'contexts': ['suite'],
                               'config': ['.ci/lane.yml', 'scripts/gate.py', 'pkg/pyproject.toml']}}}
    api = {'user': {'login': 'agent'}, PREFIX + '/pulls/7': {'head': {'sha': HEAD}},
           PREFIX + '/commits/' + HEAD + '/statuses?per_page=100&page=1': [
               {'id': 100, 'context': 'suite', 'state': 'success', 'creator': {'login': 'ci-bot'}}],
           PREFIX + '/commits/' + HEAD + '/check-runs?filter=all&per_page=100&page=1': {'total_count': 0, 'check_runs': []}}
    tree = []
    for name, content in {'.ci/lane.yml': 'commands:\n - python scripts/gate.py\n - pytest pkg/tests\n',
                          'scripts/gate.py': 'print("run suite")\n', 'pkg/pyproject.toml': '[project]\nname="pkg"\n',
                          'pkg/tests/test_ok.py': 'def test_ok(): assert True\n'}.items():
        oid, body = blob(content)
        tree.append({'path': name, 'type': 'blob', 'mode': '100644', 'sha': oid})
        api[PREFIX + '/git/blobs/' + oid] = body
    for sha in (HEAD, BASE):
        api[PREFIX + '/git/trees/' + sha + '?recursive=1'] = {'truncated': False, 'tree': copy.deepcopy(tree)}
    return ci, api


class ForgeTests(unittest.TestCase):
    def setUp(self):
        self.ci, self.api = fixture()
        self.status = PREFIX + '/commits/' + HEAD + '/statuses?per_page=100&page=1'
        self.checks = PREFIX + '/commits/' + HEAD + '/check-runs?filter=all&per_page=100&page=1'

    def get(self, path):
        if path not in self.api:
            raise RuntimeError('missing fixture endpoint: ' + path)
        return copy.deepcopy(self.api[path])

    def observe(self, gate='ci'):
        return forge.observe('.', self.ci, 'pr:7', gate, self.get)

    def test_pinned_status_and_app_are_evidence(self):
        got = self.observe('suite')
        self.assertEqual(got['exit'], 0)
        self.assertEqual(got['evidence'][0]['creator'], 'ci-bot')
        self.api[self.status] = []
        self.api[self.checks] = {'total_count': 1, 'check_runs': [
            {'id': 9, 'name': 'suite', 'app': {'id': 42}, 'status': 'completed',
             'conclusion': 'success', 'head_sha': HEAD}]}
        self.assertEqual(self.observe()['exit'], 0)
        self.assertEqual(self.observe()['evidence'][0]['appId'], 42)

    def test_unpinned_green_never_counts_and_historical_red_dominates(self):
        self.api[self.status][0]['creator']['login'] = 'writer'
        self.assertIsNone(self.observe()['exit'])
        self.api[self.status].insert(0, {'id': 101, 'context': 'suite', 'state': 'success', 'creator': {'login': 'ci-bot'}})
        self.assertEqual(self.observe()['exit'], 0)
        self.api[self.status].append({'id': 1, 'context': 'suite', 'state': 'error', 'creator': {'login': 'writer'}})
        self.assertEqual(self.observe()['exit'], 1)

    def test_newest_pinned_row_controls_pending(self):
        self.api[self.status].insert(0, {'id': 101, 'context': 'suite', 'state': 'pending', 'creator': {'login': 'ci-bot'}})
        self.assertIsNone(self.observe()['exit'])
        self.api[self.status].insert(0, {'id': 102, 'context': 'suite', 'state': 'success', 'creator': {'login': 'writer'}})
        self.assertIsNone(self.observe()['exit'])

    def test_self_identity_case_insensitive_refused(self):
        self.api['user']['login'] = 'CI-BOT'
        with self.assertRaisesRegex(forge.PolicyError, 'own gh login'):
            self.observe()

    def test_check_skipped_unpinned_and_wrong_head_never_green(self):
        self.api[self.status] = []
        row = {'id': 9, 'name': 'suite', 'app': {'id': 42}, 'status': 'completed', 'conclusion': 'skipped', 'head_sha': HEAD}
        self.api[self.checks] = {'total_count': 1, 'check_runs': [row]}
        self.assertIsNone(self.observe()['exit'])
        row.update(conclusion='success', app={'id': 99})
        self.assertIsNone(self.observe()['exit'])
        row['head_sha'] = BASE
        with self.assertRaisesRegex(ValueError, 'head'):
            self.observe()

    def test_strict_malformed_rows_fail_closed(self):
        for row in (None, {}, {'id': True, 'context': 'suite', 'state': 'success', 'creator': {'login': 'ci-bot'}},
                    {'id': 1, 'context': 'suite', 'state': 'success', 'creator': None}):
            self.api[self.status] = [row]
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.observe()

    def test_real_status_list_pagination_keeps_old_red(self):
        self.api[self.status] = [{'id': 200-i, 'context': 'suite', 'state': 'success', 'creator': {'login': 'ci-bot'}} for i in range(100)]
        self.api[self.status.replace('&page=1', '&page=2')] = [{'id': 1, 'context': 'suite', 'state': 'failure', 'creator': {'login': 'writer'}}]
        self.assertEqual(self.observe()['exit'], 1)

    def test_partial_or_duplicate_listing_refused(self):
        self.api[self.checks] = {'total_count': 2, 'check_runs': [{'id': 1}]}
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.observe()
        self.api[self.checks] = {'total_count': 0, 'check_runs': []}
        self.api[self.status] *= 2
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.observe()

    def test_ref_must_be_named_forge_ref_not_chosen_sha(self):
        self.api[PREFIX + '/git/ref/heads/topic'] = {'ref': 'refs/heads/topic', 'object': {'type': 'commit', 'sha': HEAD}}
        self.assertEqual(forge.GitHub('.', self.get).target('ref:topic'), HEAD)
        with self.assertRaises(ValueError):
            forge.GitHub('.', self.get).target('ref:' + HEAD)

    def test_moved_head_during_observation_refused(self):
        count = [0]
        def moving(path):
            if path.endswith('/pulls/7'):
                count[0] += 1
                return {'head': {'sha': HEAD if count[0] == 1 else BASE}}
            return self.get(path)
        with self.assertRaisesRegex(ValueError, 'moved'):
            forge.observe('.', self.ci, 'pr:7', api=moving)

    def test_coverage_missing_invoked_and_implicit_files_refused(self):
        config = self.ci['covers']['suite']['config']
        for remove, message in [('scripts/gate.py', 'referenced'), ('pkg/pyproject.toml', 'implicit')]:
            config.remove(remove)
            with self.assertRaisesRegex(ValueError, message):
                self.observe('suite')
            config.append(remove)

    def test_python_module_and_import_closure_cannot_omit_changed_decision_file(self):
        for content in ('python -m scripts.gate\n', 'python -m "scripts.gate"\n', 'python3 -X utf8 -m scripts.gate\n', 'py -3.9 -m scripts.gate\n'):
            ci, api = fixture()
            oid, body = blob(content)
            api[PREFIX + '/git/blobs/' + oid] = body
            for sha in (HEAD, BASE):
                api[PREFIX + '/git/trees/' + sha + '?recursive=1']['tree'][0]['sha'] = oid
            ci['covers']['suite']['config'].remove('scripts/gate.py')
            with self.subTest(command=content), self.assertRaisesRegex(ValueError, 'scripts/gate.py'):
                forge.observe('.', ci, 'pr:7', 'suite', lambda p: copy.deepcopy(api[p]))
        tracked = {'pkg/gate.py', 'pkg/helper.py', 'pkg/__init__.py'}
        self.assertIn('pkg/helper.py', forge.executable_references('pkg/gate.py', 'from . import helper\n', tracked))
        with self.assertRaisesRegex(ValueError, 'dynamic'):
            forge.executable_references('.ci.yml', 'python -m "$MODULE"\n', tracked)
        with self.assertRaisesRegex(ValueError, 'inline'):
            forge.executable_references('.ci.yml', 'python -c \"import scripts.gate\"\n', tracked)
        with self.assertRaisesRegex(ValueError, 'dynamic'):
            forge.executable_references('gate.py', 'import importlib\nimportlib.import_module(name)\n', tracked)

    def test_config_change_symlink_truncation_and_corrupt_blob_refused(self):
        path = PREFIX + '/git/trees/' + HEAD + '?recursive=1'
        original = copy.deepcopy(self.api)
        for mutate in ('blob', 'mode', 'truncated', 'body'):
            self.api = copy.deepcopy(original)
            if mutate == 'blob': self.api[path]['tree'][0]['sha'] = 'd'*40
            if mutate == 'mode': self.api[path]['tree'][0]['mode'] = '120000'
            if mutate == 'truncated': self.api[path]['truncated'] = True
            if mutate == 'body': self.api[PREFIX + '/git/blobs/' + self.api[path]['tree'][0]['sha']]['content'] = 'ZGVjZXB0aW9u'
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.observe('suite')

    def test_policy_rejects_missing_pins_invalid_base_and_node_override(self):
        for update in ({'witness': None}, {'packetSha': 'abc'}, {'base': HEAD}, {'covers': {'typo': self.ci['covers']['suite']}}):
            ci = dict(self.ci, **update)
            self.assertTrue(forge.problems(ci, {'suite': 'true'}))


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ci, self.api = fixture()
        self.manifest = {'version': 1, 'gates': {'suite': 'true'}, 'ci': self.ci}
        self.fixture = self.root / 'api.json'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text('#!' + sys.executable + '\nimport json,os,sys\nd=json.load(open(os.environ["FORGE_FIXTURE"]))\nif sys.argv[3:] != ["--hostname","github.com"]: sys.exit(2)\np=sys.argv[2].replace("repos/fixture/repo/","repos/{owner}/{repo}/")\nif p not in d: sys.exit(1)\nprint(json.dumps(d[p]))\n')
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''), FORGE_FIXTURE=str(self.fixture))
        self.write()
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', 'https://github.com/fixture/repo.git')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / '.gitignore').write_text('api.json\nbin/\n.claude/\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def write(self):
        self.fixture.write_text(json.dumps(self.api))
        (self.root / '.fluxpoint-gates.json').write_text(json.dumps(self.manifest))

    def cli(self, *args, input=None):
        self.write()
        command = [sys.executable, str(SCRIPTS / 'attest.py'), '--root', str(self.root), *args]
        if os.name == 'nt':
            # Windows cannot execute a shebang gh stub. Mock only transport
            # inside the real CLI process; all validation/recording is actual.
            script = ('import sys,json,os;sys.path.insert(0,' + repr(str(SCRIPTS)) + ');import attest;'
                      'attest.forge.GitHub.request=lambda self,path,timeout: json.load(open(os.environ["FORGE_FIXTURE"]))[path.replace("repos/fixture/repo/","repos/{owner}/{repo}/")];'
                      'sys.exit(attest.main())')
            command = [sys.executable, '-c', script, '--root', str(self.root), *args]
        p = subprocess.run(command,
                           env=self.env, input=json.dumps(input) if input is not None else None, capture_output=True, text=True, timeout=10)
        return p

    def rows(self):
        return attest.read(str(self.root))

    def summary(self, row, steps=False):
        claim = {'gate': row['gate'], 'exit': row['exit'], 'attestId': row['attestId'], 'sha': row['headSha'], 'target': row['target']}
        return {'campaign': 'fixture', 'outcome': 'COMPLETE', 'results': {'gate': {'sha': row['headSha'], 'citations': [claim]} if steps else claim},
                'contracts': {'gate': 'HeadCheckV1' if steps else 'ExecutionV1'},
                'cites' if steps else 'prove': {'gate': ['suite'] if steps else 'ci'},
                'launch': {'since': '2000-01-01T00:00:00Z', 'nonce': NONCE}}

    def test_cli_reuse_and_record_time_reread_ignores_forged_log_green(self):
        p = self.cli('--reuse', 'suite', '--pr', '7', '--nonce', NONCE)
        self.assertEqual(p.returncode, 0, p.stderr)
        row = self.rows()[-1]
        summary = self.summary(row, True)
        p = self.cli('--verify', input=summary)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.api[PREFIX + '/commits/' + HEAD + '/statuses?per_page=100&page=1'][0]['state'] = 'failure'
        p = self.cli('--verify', input=summary)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('forge re-read refused', p.stdout)

    def test_moved_pr_pending_unknown_creator_and_self_pin_refused(self):
        self.assertEqual(self.cli('--ci', '--pr', '7', '--nonce', NONCE).returncode, 0)
        summary = self.summary(self.rows()[-1])
        self.api[PREFIX + '/pulls/7']['head']['sha'] = BASE
        self.assertNotEqual(self.cli('--verify', input=summary).returncode, 0)
        self.api[PREFIX + '/pulls/7']['head']['sha'] = HEAD
        self.api[PREFIX + '/commits/' + HEAD + '/statuses?per_page=100&page=1'][0]['creator']['login'] = 'agent'
        self.assertEqual(self.cli('--ci', '--pr', '7').returncode, 3)
        self.ci['witness']['statusCreators'] = ['agent']
        p = self.cli('--ci', '--pr', '7')
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(self.cli('--check-ci').returncode, 2)

    def test_environment_and_mutable_origin_cannot_redirect_repository(self):
        self.env.update(GH_REPO='attacker/other', GH_HOST='attacker.invalid')
        self.assertEqual(self.cli('--ci', '--pr', '7').returncode, 0)
        self.git('remote', 'set-url', 'origin', 'https://attacker.invalid/fixture/repo.git')
        self.assertEqual(self.cli('--ci', '--pr', '7').returncode, 0)

    def test_independent_any_contract_requires_each_step(self):
        self.assertEqual(self.cli('--reuse', 'suite', '--pr', '7', '--nonce', NONCE).returncode, 0)
        summary = self.summary(self.rows()[-1], True)
        summary['results']['gate']['citations'] = []
        self.assertIn('exactly one', self.cli('--verify', input=summary).stdout)
        summary['results'] = {}
        self.assertIn('no result', self.cli('--verify', input=summary).stdout)

    def test_step_cannot_cite_green_for_a_different_parent_sha(self):
        self.assertEqual(self.cli('--reuse', 'suite', '--pr', '7', '--nonce', NONCE).returncode, 0)
        summary = self.summary(self.rows()[-1], True)
        summary['results']['gate']['sha'] = BASE
        p = self.cli('--verify', input=summary)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('node exact judged sha', p.stdout)

    def test_local_dirty_and_legacy_rows_cannot_back_claim(self):
        self.manifest['ci'] = self.ci  # local gate still has no actual CI claim
        head = self.git('rev-parse', 'HEAD')
        row = {'attestId': 'att_local', 'gate': 'suite', 'exit': 0, 'headSha': head,
               'treeSha': head, 'when': '2099-01-01T00:00:00Z', 'nonce': NONCE, 'witness': 'hook'}
        attest.append_row(str(self.root), row)
        summary = {'prove': {'gate': 'suite'}, 'results': {'gate': dict(gate='suite', exit=0, attestId='att_local')},
                   'launch': {'since': '2000-01-01T00:00:00Z', 'nonce': NONCE}}
        self.assertIn('content binding', self.cli('--verify', input=summary).stdout)
        row['attestId'], row['dirty'] = 'att_dirty', True
        attest.append_row(str(self.root), row)
        summary['results']['gate']['attestId'] = 'att_dirty'
        self.assertIn('dirty', self.cli('--verify', input=summary).stdout)

    def record_run(self, summary, run):
        self.write()
        graph = self.root / 'WORK.md'
        graph.write_text('## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        args = ['--root', str(self.root), '--graph', str(graph), '--run-id', run,
                '--state-dir', str(self.root / '.claude' / 'fluxpoint' / 'runs')]
        command = [sys.executable, str(SCRIPTS / 'record-run.py'), *args]
        if os.name == 'nt':
            code = ('import sys,json,os,runpy;sys.path.insert(0,' + repr(str(SCRIPTS)) + ');import forge_evidence;'
                    'forge_evidence.GitHub.request=lambda self,path,timeout: json.load(open(os.environ["FORGE_FIXTURE"]))[path.replace("repos/fixture/repo/","repos/{owner}/{repo}/")];'
                    'sys.argv=[' + repr(str(SCRIPTS / 'record-run.py')) + ']+sys.argv[1:];'
                    'runpy.run_path(sys.argv[0],run_name="__main__")')
            command = [sys.executable, '-c', code, *args]
        p = subprocess.run(command, env=self.env, input=json.dumps(summary), capture_output=True, text=True, timeout=10)
        artifact = self.root / '.claude' / 'fluxpoint' / 'runs' / (run + '.json')
        self.assertTrue(artifact.exists(), p.stdout + p.stderr)
        return json.loads(artifact.read_text()), p

    def test_record_run_revalidates_memoized_steps_and_red_cannot_file_complete(self):
        self.assertEqual(self.cli('--reuse', 'suite', '--pr', '7', '--nonce', NONCE).returncode, 0)
        summary = self.summary(self.rows()[-1], True)
        # A resumed result still has a green pointer, while source facts changed.
        summary['resumeFromRunId'] = 'wf-earlier'
        self.api[PREFIX + '/commits/' + HEAD + '/statuses?per_page=100&page=1'][0]['state'] = 'failure'
        artifact, p = self.record_run(summary, 'wf-forge-stale')
        self.assertEqual(artifact['outcome'], 'TAMPERED-EXECUTION', p.stdout + p.stderr)
        self.assertEqual(self.cli('--reuse', 'suite', '--pr', '7', '--nonce', NONCE).returncode, 1)
        artifact, p = self.record_run(self.summary(self.rows()[-1], True), 'wf-forge-red')
        self.assertEqual(artifact['outcome'], 'INCOMPLETE', p.stdout + p.stderr)

    def test_actual_wrapper_records_before_and_after_dirt(self):
        self.manifest.pop('ci')
        self.write()
        self.git('add', '.fluxpoint-gates.json')
        self.git('commit', '-qm', 'local gate')
        self.assertFalse(attest.dirty(str(self.root)))
        p = self.cli('--run', 'suite', '--nonce', NONCE)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIs(self.rows()[-1]['dirty'], False)
        self.manifest['gates']['suite'] = 'printf changed > untracked.txt'
        self.write()
        self.git('add', '.fluxpoint-gates.json')
        self.git('commit', '-qm', 'mutating gate')
        p = self.cli('--run', 'suite', '--nonce', NONCE)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIs(self.rows()[-1]['dirty'], True)


class CompilerCitesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('forge_compile', SCRIPTS / 'compile-graph.py')
        cls.cg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cg)
        cls.contracts = cls.cg.load_contracts(str(SCRIPTS.parent / 'contracts'))
        cls.contracts['HeadCheckV1'] = {'type': 'object', 'required': ['verdict'],
                                      'additionalProperties': False,
                                      'properties': {'verdict': {'type': 'string'}}}

    def graph(self):
        return {'version': 1, 'campaign': 'independent multi-step gate', 'treeGuard': True,
                'budget': {'maxNodes': 8}, 'nodes': [
                    {'id': 'gate', 'prompt': 'Check packet, runtime and suite independently.',
                     'contract': 'HeadCheckV1', 'independent': True, 'cites': ['suite']} ]}

    def test_any_contract_cites_validation_and_launch_requirements(self):
        ir = self.graph()
        self.assertEqual(self.cg.validate(ir, self.contracts, gates={'suite'}), [])
        self.assertTrue(any(x['name'] == '_launch' for x in self.cg.launch_requirements(ir)['args']))
        for mutation in ({'independent': False}, {'cites': ['suite', 'suite']}, {'cites': ['typo']}):
            bad = copy.deepcopy(ir)
            bad['nodes'][0].update(mutation)
            self.assertTrue(self.cg.validate(bad, self.contracts, gates={'suite'}))

    def run_graph(self, claims):
        js = self.cg.emit(self.graph(), self.contracts, {})
        agent = {'verdict': 'PASS', 'sha': HEAD, 'citations': claims}
        code = ('const args={_launch:{since:"2000-01-01T00:00:00Z",nonce:"fixture-nonce",root:"/tmp"}};'
                'const log=()=>{},phase=()=>{},parallel=async t=>Promise.all(t.map(f=>f()));'
                'const budget={total:null,spent:()=>0,remaining:()=>1e9};'
                'const prompts=[]; const agent=async(p,o)=>{if(String(o.label).startsWith("tree-check:"))return {root:"/tmp",head:' + json.dumps(HEAD) + ',porcelain:""};prompts.push({p,o});return ' + json.dumps(agent) + '};'
                '(async()=>{' + js.replace('export const meta', 'const meta') +
                '})().then(r=>console.log(JSON.stringify({r,prompts}))).catch(e=>{console.error(e);process.exit(1)})')
        p = subprocess.run(['node', '--input-type=module'], input=code, text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_executable_emission_requires_unique_green_steps_and_extends_schema(self):
        claim = {'gate': 'suite', 'exit': 0, 'attestId': 'att_x', 'sha': HEAD, 'target': 'pr:7'}
        got = self.run_graph([claim])
        self.assertEqual(got['r']['outcome'], 'COMPLETE')
        self.assertEqual(got['r']['cites'], {'gate': ['suite']})
        prompt = got['prompts'][0]
        self.assertIn('--reuse <gate>', prompt['p'])
        self.assertIn('--cancel', prompt['p'])
        self.assertIn('citations', prompt['o']['schema']['required'])
        self.assertIn('verdict', prompt['o']['schema']['required'])
        for claims in ([], [dict(claim, exit=1)], [claim, claim]):
            self.assertEqual(self.run_graph(claims)['r']['outcome'], 'UNPROVEN')


if __name__ == '__main__':
    unittest.main()
