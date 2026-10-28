#!/usr/bin/env python3
"""Exercise ratchets against real temporary Git repos and executable full harnesses."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class Ratchets(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'scripts').mkdir()
        self.put('scripts/harness.sh', '#!/bin/bash\nexit 0\n')

    def put(self, p, text):
        (self.root / p).write_text(text, encoding='utf-8')

    def json(self, p, doc):
        self.put(p, json.dumps(doc))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, check=True)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')

    def cli(self, script, *args, ok=True):
        r = subprocess.run([sys.executable, str(SCRIPTS / (script + '.py')), '--root', str(self.root), *args],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
        return r

    def metric(self, direction='down'):
        self.json('.fluxpoint-metrics.json', {'version': 1, 'metrics': [
            {'name': 'cost', 'command': [sys.executable, '-c', 'print(open("value").read().strip())'],
             'direction': direction}]})
        self.put('value', '10')
        self.cli('metric-guard', '--baseline')
        self.commit()

    def test_dormant_and_extractors(self):
        self.cli('metric-guard', '--check')
        self.json('.fluxpoint-metrics.json', {'metrics': [
            {'name': 'cost', 'command': 'printf "cost: 7\\n"', 'extract': {'regex': r'cost: (\d+)'},
             'direction': 'down'}]})
        self.cli('metric-guard', '--baseline')
        r = self.cli('metric-guard', '--report', '--json')
        self.assertEqual(json.loads(r.stdout)['metrics'][0]['limit'], 7)
        self.json('.fluxpoint-metrics.json', {'metrics': [
            {'name': 'cost', 'command': 'printf 1.2', 'direction': 'down'}]})
        self.cli('metric-guard', '--baseline', ok=False)

    def test_noisy_never_armed(self):
        self.put('counter', '0')
        code = 'from pathlib import Path; p=Path("counter"); n=int(p.read_text())+1; p.write_text(str(n)); print(n)'
        self.json('.fluxpoint-metrics.json', {'metrics': [
            {'name': 'noise', 'command': [sys.executable, '-c', code], 'direction': 'down'}]})
        r = self.cli('metric-guard', '--baseline', ok=False)
        self.assertIn('nondeterministic', r.stderr)
        self.assertFalse((self.root / '.fluxpoint-metrics-baseline.json').exists())

    def test_ceiling_and_floor_tighten_then_revert(self):
        for direction, better, worse in [('down', '8', '10'), ('up', '12', '10')]:
            self.metric(direction)
            self.put('value', better)
            self.cli('metric-guard', '--tighten', ok=False)  # dirty
            self.commit()
            self.cli('metric-guard', '--tighten')
            self.commit()
            self.put('value', worse)
            self.commit()
            before = (self.root / '.fluxpoint-metrics-baseline.json').read_bytes()
            self.cli('metric-guard', '--check', ok=False)
            self.cli('metric-guard', '--tighten', ok=False)
            self.assertEqual(before, (self.root / '.fluxpoint-metrics-baseline.json').read_bytes())

    def test_red_harness_never_writes(self):
        self.metric()
        self.put('value', '8')
        self.put('scripts/harness.sh', '#!/bin/bash\nexit 1\n')
        self.commit()
        self.cli('metric-guard', '--tighten', ok=False)
        self.assertEqual(json.loads((self.root / '.fluxpoint-metrics-baseline.json').read_text())['metrics']['cost']['value'], 10)

    def test_tighten_json_reports_the_persisted_limit(self):
        self.metric()
        self.put('value', '8')
        self.commit()
        result = self.cli('metric-guard', '--tighten', '--json')
        self.assertEqual(json.loads(result.stdout)['metrics'][0]['limit'], 8)
        self.assertIn('10 -> 8', result.stderr)

    def test_committing_harness_cannot_tighten_an_unchecked_head(self):
        self.metric()
        self.put('value', '8')
        self.put('scripts/harness.sh', '#!/bin/bash\ngit commit --allow-empty -qm moved\nexit 0\n')
        self.commit()
        before = (self.root / '.fluxpoint-metrics-baseline.json').read_bytes()
        result = self.cli('metric-guard', '--tighten', ok=False)
        self.assertIn('HEAD changed', result.stderr)
        self.assertEqual(before, (self.root / '.fluxpoint-metrics-baseline.json').read_bytes())

    def test_committing_metric_cannot_tighten_after_making_harness_red(self):
        self.json('.fluxpoint-metrics.json', {'metrics': [
            {'name': 'cost', 'command': [sys.executable, 'measure.py'], 'direction': 'down'}]})
        self.put('value', '10')
        self.put('measure.py', """from pathlib import Path
import subprocess
harness = Path('scripts/harness.sh')
if Path('trigger').exists() and 'exit 0' in harness.read_text():
    harness.write_text('#!/bin/bash\\nexit 1\\n')
    subprocess.run(['git', 'add', 'scripts/harness.sh'], check=True)
    subprocess.run(['git', 'commit', '-qm', 'measurement changed harness'], check=True)
print(Path('value').read_text())
""")
        self.cli('metric-guard', '--baseline')
        self.commit()
        self.put('value', '8')
        self.put('trigger', 'go')
        self.commit()
        before = (self.root / '.fluxpoint-metrics-baseline.json').read_bytes()
        result = self.cli('metric-guard', '--tighten', ok=False)
        self.assertIn('HEAD changed', result.stderr)
        self.assertEqual(before, (self.root / '.fluxpoint-metrics-baseline.json').read_bytes())

    def test_noisy_last_metric_and_malformed_extractors_preserve_whole_baseline(self):
        self.metric()
        before = (self.root / '.fluxpoint-metrics-baseline.json').read_bytes()
        manifest = json.loads((self.root / '.fluxpoint-metrics.json').read_text())
        for command, extract in [([sys.executable, '-c', 'print("true")'], {'pointer': ''}),
                                 ([sys.executable, '-c', 'print("1.5")'], {'pointer': ''}),
                                 ([sys.executable, '-c', 'print("1 2")'], {'regex': r'(\d+)'}),
                                 ([sys.executable, '-c', 'print("10")'], {'regex': r'not a capture'})]:
            with self.subTest(extract=extract, command=command):
                self.json('.fluxpoint-metrics.json', {'metrics': [*manifest['metrics'],
                    {'name': 'invalid', 'command': command, 'extract': extract, 'direction': 'up'}]})
                self.cli('metric-guard', '--baseline', ok=False)
                self.assertEqual(before, (self.root / '.fluxpoint-metrics-baseline.json').read_bytes())
        self.put('counter', '0')
        code = 'from pathlib import Path; p=Path("counter"); n=int(p.read_text())+1; p.write_text(str(n)); print(n)'
        self.json('.fluxpoint-metrics.json', {'metrics': [*manifest['metrics'],
            {'name': 'noise', 'command': [sys.executable, '-c', code], 'direction': 'up'}]})
        self.cli('metric-guard', '--baseline', ok=False)
        self.assertEqual(before, (self.root / '.fluxpoint-metrics-baseline.json').read_bytes())

    def test_tighten_does_not_arm_an_empty_guard_baseline(self):
        self.json('.fluxpoint-guards.json', {'guards': []})
        self.json('.fluxpoint-guards-baseline.json', {})
        self.commit()
        before = (self.root / '.fluxpoint-guards-baseline.json').read_bytes()
        self.cli('guard-guard', '--tighten', ok=False)
        self.assertEqual(before, (self.root / '.fluxpoint-guards-baseline.json').read_bytes())

    def test_proof_tightening_locks_improvement_in_full(self):
        self.put('proof.lean', 'theorem example : True := by sorry\n')
        self.commit()
        self.cli('proof-guard', '--baseline')
        self.put('scripts/harness.sh', f'#!/bin/bash\n"{sys.executable}" "{SCRIPTS / "proof-guard.py"}" --check\n')
        self.commit()
        self.put('proof.lean', 'theorem example : True := by trivial\n')
        self.commit()
        self.cli('proof-guard', '--tighten')
        self.commit()
        self.put('proof.lean', 'theorem example : True := by sorry\n')
        self.commit()
        r = subprocess.run(['bash', 'scripts/harness.sh', '--full'], cwd=self.root, capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        before = (self.root / '.fluxpoint-proof-baseline.json').read_bytes()
        self.cli('proof-guard', '--tighten', ok=False)
        self.assertEqual(before, (self.root / '.fluxpoint-proof-baseline.json').read_bytes())

    def test_seam_tightens_preserving_siblings(self):
        self.put('module.test.js', "jest.mock('./owned');\n")
        self.commit()
        self.cli('seam-guard', '--baseline')
        self.put('module.test.js', 'test("real", () => {});\n')
        self.commit()
        self.cli('seam-guard', '--tighten')
        self.commit()
        self.put('module.test.js', "jest.mock('./owned');\n")
        self.commit()
        self.cli('seam-guard', '--tighten', ok=False)
        self.cli('seam-guard', '--check', ok=False)

    def test_guard_floor_and_ids_cannot_fall(self):
        self.put('guard.py', 'if safe: pass\n')
        self.put('test_guard.py', 'def test_safe(): pass\n')
        guard = {'id': 'safe', 'protects': 'safe action', 'guard': {'file': 'guard.py', 'contains': 'if safe:'},
                 'proof': {'file': 'test_guard.py', 'test': 'test_safe'},
                 'mutation': {'find': 'if safe:', 'replace': 'if False:'}, 'expect': 'unsafe'}
        self.json('.fluxpoint-guards.json', {'guards': [guard]})
        self.cli('guard-guard', '--baseline')
        self.commit()
        self.json('.fluxpoint-guards.json', {'guards': [guard, dict(guard, id='second')]})
        self.commit()
        self.cli('guard-guard', '--tighten')
        self.commit()
        self.json('.fluxpoint-guards.json', {'guards': [guard]})
        self.commit()
        self.cli('guard-guard', '--tighten', ok=False)

    def test_mutation_tightens_without_loosening_other_dimension(self):
        self.json('.fluxpoint-mutation.json', {'version': 1, 'tool': 'stryker'})
        def report(killed, survived):
            self.json('report.json', {'files': {'a.js': {'mutants': [
                *[{'status': 'Killed'} for _ in range(killed)],
                *[{'status': 'Survived'} for _ in range(survived)]]}}})
        report(8, 2)
        self.commit()
        self.cli('mutation-guard', '--measure', '--from', str(self.root / 'report.json'))
        self.commit()
        report(9, 1)
        self.commit()
        self.cli('mutation-guard', '--tighten', '--from', str(self.root / 'report.json'))
        self.commit()
        prior = (self.root / '.fluxpoint-proof-baseline.json').read_bytes()
        report(19, 2)  # higher score, more survivors: still forbidden
        self.commit()
        self.cli('mutation-guard', '--tighten', '--from', str(self.root / 'report.json'), ok=False)
        self.assertEqual(prior, (self.root / '.fluxpoint-proof-baseline.json').read_bytes())

    def test_partial_mutation_measurement_cannot_tighten(self):
        self.json('.fluxpoint-mutation.json', {'version': 1, 'tool': 'stryker'})
        def report(statuses):
            self.json('report.json', {'files': {'a.js': {'mutants': [{'status': s} for s in statuses]}}})
        report(['Killed'] * 8 + ['Survived'] * 2)
        self.commit()
        self.cli('mutation-guard', '--measure', '--from', str(self.root / 'report.json'))
        self.commit()
        before = (self.root / '.fluxpoint-proof-baseline.json').read_bytes()
        report(['Killed'] * 9 + ['Survived', 'NewUnknownStatus'])
        self.commit()
        result = self.cli('mutation-guard', '--tighten', '--from', str(self.root / 'report.json'), ok=False)
        self.assertIn('NewUnknownStatus', result.stderr)
        self.assertEqual(before, (self.root / '.fluxpoint-proof-baseline.json').read_bytes())

    def test_mutation_tighten_never_lowers_a_floor_within_measurement_tolerance(self):
        self.json('.fluxpoint-mutation.json', {'version': 1, 'tool': 'stryker'})
        self.json('report.json', {'files': {'a.js': {'mutants': [
            *[{'status': 'Killed'} for _ in range(9)], {'status': 'Survived'}]}}})
        self.commit()
        self.cli('mutation-guard', '--measure', '--from', str(self.root / 'report.json'))
        doc = json.loads((self.root / '.fluxpoint-proof-baseline.json').read_text())
        doc['mutation']['score'] += 1e-10
        self.json('.fluxpoint-proof-baseline.json', doc)
        self.commit()
        before = (self.root / '.fluxpoint-proof-baseline.json').read_bytes()
        self.cli('mutation-guard', '--tighten', '--from', str(self.root / 'report.json'), ok=False)
        self.assertEqual(before, (self.root / '.fluxpoint-proof-baseline.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
