#!/usr/bin/env python3
"""Real v1.1.23 fixture parsing plus manifest, noise and protocol-budget integrations."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import aiken_units

FIXTURE = PLUGIN / 'tests' / 'fixtures' / 'aiken-v1.1.23' / 'check.json'


class AikenMetrics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'bin').mkdir()
        self.env = dict(os.environ, PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'])
        self.fake()

    def fake(self, version='aiken v1.1.23+8949565', noisy=False, output=None):
        source = FIXTURE
        if output is not None:
            source = self.root / 'response.json'
            source.write_text(json.dumps(output))
        script = f'''#!{sys.executable}
import json,sys
from pathlib import Path
if '--version' in sys.argv:
 print({version!r})
else:
 p=Path('calls'); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
 doc=json.loads(Path({str(source)!r}).read_text())
 if {noisy!r}: doc['modules'][0]['tests'][0]['execution_units']['mem'] += n
 print(json.dumps(doc))
'''
        path = self.root / 'bin' / 'aiken'
        path.write_text(script, encoding='utf-8')
        path.chmod(0o755)

    def cli(self, script, *args, ok=True):
        r = subprocess.run([sys.executable, str(SCRIPTS / script), '--root', str(self.root), *args],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
        return r

    def test_captured_units_and_property_exclusion(self):
        doc = json.loads(FIXTURE.read_text())
        costs, excluded = aiken_units.parse(doc)
        self.assertEqual(costs, {'cost.unit_twice': {'mem': 200, 'cpu': 16100}})
        self.assertEqual(excluded, ['cost.property_twice'])
        doc['modules'][0]['tests'][1]['execution_units'] = {'mem': 999, 'cpu': 999}
        self.assertEqual(aiken_units.parse(doc)[0], costs)
        with self.assertRaises(ValueError):
            aiken_units.extract(doc, {'test': 'cost.property_twice', 'unit': 'mem'})

    def test_malformed_failed_duplicate_and_fractional_costs_refused(self):
        original = json.loads(FIXTURE.read_text())
        for mutate in [lambda d: d['summary'].update(failed=1),
                       lambda d: d['modules'][0]['tests'][0].update(status='fail'),
                       lambda d: d['modules'][0]['tests'][0]['execution_units'].update(mem=1.5),
                       lambda d: d['modules'][0]['tests'].append(copy.deepcopy(d['modules'][0]['tests'][0]))]:
            doc = copy.deepcopy(original)
            mutate(doc)
            with self.assertRaises(ValueError):
                aiken_units.parse(doc)

    def test_init_enforces_per_test_ceiling_and_batches_commands(self):
        self.cli('aiken-metrics.py', '--baseline')
        manifest = json.loads((self.root / '.fluxpoint-metrics.json').read_text())
        self.assertEqual(len(manifest['metrics']), 2)
        self.assertNotIn('property_twice', json.dumps(manifest))
        self.assertEqual((self.root / 'calls').read_text(), '2')
        self.cli('metric-guard.py', '--check')
        self.assertEqual((self.root / 'calls').read_text(), '4')
        baseline_path = self.root / '.fluxpoint-metrics-baseline.json'
        baseline = json.loads(baseline_path.read_text())
        baseline['metrics']['aiken.cost.unit_twice.cpu']['value'] = 16099
        baseline_path.write_text(json.dumps(baseline))
        self.cli('metric-guard.py', '--check', ok=False)

    def test_noise_and_unknown_version_never_arm(self):
        self.fake(noisy=True)
        self.cli('aiken-metrics.py', '--baseline', ok=False)
        self.assertFalse((self.root / '.fluxpoint-metrics.json').exists())
        self.fake(version='aiken v1.2.0')
        self.cli('aiken-metrics.py', '--baseline', ok=False)
        self.assertFalse((self.root / '.fluxpoint-metrics-baseline.json').exists())

    def test_new_unit_test_and_missing_unit_metric_are_unarmed(self):
        self.cli('aiken-metrics.py', '--baseline')
        doc = json.loads(FIXTURE.read_text())
        doc['modules'][0]['tests'].append({'title': 'new_expensive', 'status': 'pass',
                                          'execution_units': {'mem': 999999999, 'cpu': 999999999}})
        for summary in (doc['summary'], doc['modules'][0]['summary']):
            summary['total'] += 1
            summary['passed'] += 1
            summary['kind']['unit'] += 1
        self.fake(output=doc)
        result = self.cli('metric-guard.py', '--check', ok=False)
        self.assertIn('unarmed Aiken unit cost', result.stderr)
        self.assertIn('cost.new_expensive.cpu', result.stderr)
        self.assertIn('cost.new_expensive.mem', result.stderr)
        self.fake()
        manifest_path = self.root / '.fluxpoint-metrics.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['metrics'] = [m for m in manifest['metrics'] if m['extract']['aiken']['unit'] == 'mem']
        manifest_path.write_text(json.dumps(manifest))
        result = self.cli('metric-guard.py', '--check', ok=False)
        self.assertIn('cost.unit_twice.cpu', result.stderr)
        before = (self.root / '.fluxpoint-metrics-baseline.json').read_bytes()
        self.cli('metric-guard.py', '--baseline', ok=False)
        self.assertEqual(before, (self.root / '.fluxpoint-metrics-baseline.json').read_bytes())

    def test_failed_property_or_incomplete_summary_cannot_pass_as_unit_evidence(self):
        original = json.loads(FIXTURE.read_text())
        for mutate in [lambda d: d['modules'][0]['tests'][1].update(status='fail'),
                       lambda d: d['modules'][0]['tests'][1].update(iterations='100'),
                       lambda d: d['summary'].update(total=999),
                       lambda d: d['modules'][0]['summary'].update(passed=True),
                       lambda d: d.update(summary=None)]:
            doc = copy.deepcopy(original)
            mutate(doc)
            with self.assertRaises(ValueError):
                aiken_units.parse(doc)

    def test_measured_costs_replace_hand_entered_budget(self):
        (self.root / 'plutus.json').write_text(json.dumps({'validators': [{'title': 'probe', 'compiledCode': '00'}]}))
        (self.root / '.fluxpoint-budget.json').write_text(json.dumps({'exUnits': {'mem': 1, 'steps': 1}}))
        params = self.root / 'params.json'
        params.write_text(json.dumps({'maxTxExecutionUnits': {'memory': 199, 'steps': 20000}}))
        r = self.cli('plutus-budget.py', '--check', '--params', str(params), '--aiken-output', str(FIXTURE), ok=False)
        self.assertIn('measured mem 200', r.stderr)
        self.assertIn('not a whole-transaction', r.stdout)
        self.assertIn('property statistics excluded', r.stdout)


if __name__ == '__main__':
    unittest.main()
