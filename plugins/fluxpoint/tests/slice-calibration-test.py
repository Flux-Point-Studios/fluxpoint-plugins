#!/usr/bin/env python3
"""Predictions stay fixed, measured endpoints are reproducible, ratios are honest."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
RECORDER = PLUGIN / 'scripts/slice-metric.py'
METRICS = PLUGIN / 'scripts/metrics.py'
spec = importlib.util.spec_from_file_location('gain_metrics', METRICS)
mx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mx)


class Calibration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.value(100)
        self.manifest()

    def value(self, value):
        (self.root / 'value').write_text(str(value))

    def manifest(self, direction='down', command=None):
        metric = dict(name='latency.ms', direction=direction, command=command or [
            sys.executable, '-c', 'from pathlib import Path; print(Path("value").read_text())'])
        (self.root / '.fluxpoint-metrics.json').write_text(json.dumps(dict(version=1, metrics=[metric])))

    def run_record(self, *args, ok=True):
        run = subprocess.run([sys.executable, str(RECORDER), '--root', str(self.root),
                              '--json', *args], capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode == 0, ok, run.stdout + run.stderr)
        return json.loads(run.stdout) if ok else run.stderr

    def start(self, sid='lookup', role='implementer', predicted='20', campaign=None):
        args = ['--start', '--slice', sid, '--role', role, '--metric', 'latency.ms',
                '--predicted-delta', predicted]
        if campaign:
            args += ['--campaign', campaign]
        return self.run_record(*args)

    def finish(self, sid='lookup'):
        return self.run_record('--end', '--slice', sid)

    def report(self, *args):
        run = subprocess.run([sys.executable, str(METRICS), '--root', str(self.root), *args],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run

    def test_start_end_record_prediction_and_measured_gain(self):
        start = self.start()
        self.assertEqual((start['before'], start['predictedDelta'], start['status']), (100, 20, 'STARTED'))
        self.value(70)
        end = self.finish()
        self.assertEqual((end['after'], end['measuredDelta'], end['ratio']), (70, 30, 1.5))
        for key in ('predictedDelta', 'before', 'role', 'metric', 'definition', 'startedAt'):
            self.assertEqual(start[key], end[key])
        self.assertIn('endedAt', end)
        report = json.loads(self.report('--json').stdout)
        role = report['gainCalibration']['roles']['implementer']
        self.assertEqual((role['slices'], role['meanRatio']), (1, 1.5))
        self.assertIn('measured/predicted 1.50x', self.report().stdout)

    def test_upward_metric_and_regression_keep_direction(self):
        self.manifest(direction='up')
        self.start(predicted='10')
        self.value(110)
        self.assertEqual(self.finish()['measuredDelta'], 10)
        self.start('regression', predicted='5')
        self.value(100)
        self.assertEqual(self.finish('regression')['ratio'], -2)
        role = json.loads(self.report('--json').stdout)['gainCalibration']['roles']['implementer']
        self.assertEqual(role['meanRatio'], -.5)

    def test_pending_predictions_are_counted_without_zero_averaging(self):
        self.start('done')
        self.value(60)
        self.finish('done')
        self.start('pending')
        data = json.loads(self.report('--json').stdout)['gainCalibration']
        self.assertEqual(data['pendingSlices'], 1)
        self.assertEqual(data['roles']['implementer']['meanRatio'], 2)
        self.assertIn('excluded from ratios', self.report().stdout)

    def test_prediction_cannot_be_overwritten_and_end_is_idempotent(self):
        self.start()
        self.value(80)
        err = self.run_record('--start', '--slice', 'lookup', '--role', 'implementer',
                              '--metric', 'latency.ms', '--predicted-delta', '10', ok=False)
        self.assertIn('already recorded', err)
        end = self.finish()
        self.value(1)
        self.assertEqual(self.finish(), end)
        self.assertEqual(json.loads(self.report('--json').stdout)['gainCalibration']['completedSlices'], 1)

    def test_changed_definition_refuses_comparison_and_preserves_prediction(self):
        self.start()
        self.manifest(direction='up')
        err = self.run_record('--end', '--slice', 'lookup', ok=False)
        self.assertIn('definition changed', err)
        artifact = json.loads((self.root / '.claude/fluxpoint/slices/lookup.json').read_text())
        self.assertEqual(artifact['status'], 'STARTED')
        self.assertNotIn('after', artifact)

    def test_noise_fails_before_any_prediction_is_recorded(self):
        self.manifest(command=[sys.executable, '-c',
            'from pathlib import Path; p=Path("value"); n=int(p.read_text()); p.write_text(str(n+1)); print(n)'])
        err = self.run_record('--start', '--slice', 'noisy', '--role', 'implementer',
                              '--metric', 'latency.ms', '--predicted-delta', '5', ok=False)
        self.assertIn('nondeterministic', err)
        directory = self.root / '.claude/fluxpoint/slices'
        self.assertFalse((directory / 'noisy.json').exists())
        self.assertFalse((directory / 'noisy.lock').exists())

    def test_invalid_prediction_missing_target_and_path_escape_are_refused(self):
        for predicted in ('0', '-1', 'nan', 'inf'):
            with self.subTest(predicted=predicted):
                self.run_record('--start', '--slice', 'bad', '--role', 'implementer',
                                '--metric', 'latency.ms', '--predicted-delta', predicted, ok=False)
        self.run_record('--start', '--slice', 'bad', '--role', 'implementer',
                        '--metric', 'missing', '--predicted-delta', '1', ok=False)
        self.run_record('--start', '--slice', '../escape', '--role', 'implementer',
                        '--metric', 'latency.ms', '--predicted-delta', '1', ok=False)
        self.assertFalse((self.root / '.claude/fluxpoint/escape.json').exists())
        self.run_record('--end', '--slice', 'missing', ok=False)

    def test_completion_cannot_change_recorded_prediction(self):
        self.start()
        err = self.run_record('--end', '--slice', 'lookup', '--predicted-delta', '1', ok=False)
        self.assertIn('do not supply them again', err)

    def test_role_ratios_average_each_slice_without_mixing_units(self):
        def row(sid, role, metric, before, after, predicted):
            return (sid, dict(version=1, sliceId=sid, role=role, metric=metric,
                              campaign='optimize', status='COMPLETE', direction='down',
                              before=before, after=after, predictedDelta=predicted,
                              measuredDelta=before-after))
        records = [row('a', 'implementer', 'latency.ms', 100, 60, 20),
                   row('b', 'implementer', 'bundle.bytes', 1000000, 900000, 200000),
                   row('c', 'reviewer', 'latency.ms', 50, 55, 5)]
        calibration, findings = mx.fold_calibration(records)
        self.assertEqual(findings, [])
        self.assertEqual(calibration['roles']['implementer']['meanRatio'], 1.25)
        self.assertEqual(calibration['roles']['reviewer']['meanRatio'], -1)
        self.assertEqual(calibration['roles']['implementer']['metrics']['bundle.bytes']['slices'], 1)

    def test_malformed_artifacts_are_named_without_corrupting_other_samples(self):
        self.start()
        self.value(80)
        self.finish()
        directory = self.root / '.claude/fluxpoint/slices'
        (directory / 'broken.json').write_text('{broken')
        (directory / 'bad.json').write_text(json.dumps(dict(version=1, sliceId='bad', campaign='<loop>',
            role='implementer', metric='latency.ms', status='COMPLETE', before=100,
            after=80, direction='down', predictedDelta=0, measuredDelta=20)))
        report = self.report('--json')
        data = json.loads(report.stdout)
        self.assertEqual(data['gainCalibration']['completedSlices'], 1)
        self.assertTrue(any('broken' in f for f in data['findings']))
        self.assertTrue(any('bad' in f for f in data['findings']))
        self.assertIn('MALFORMED slice artifact bad', report.stderr)

    def test_campaign_filter_applies_to_slice_calibration(self):
        self.start('a', campaign='campaign-a')
        self.value(80)
        self.finish('a')
        self.start('b', role='reviewer', campaign='campaign-b')
        self.value(40)
        self.finish('b')
        data = json.loads(self.report('--json', '--campaign', 'campaign-a').stdout)
        self.assertEqual(set(data['gainCalibration']['roles']), {'implementer'})
        self.assertEqual(data['gainCalibration']['completedSlices'], 1)

    def test_existing_ignore_policy_and_malformed_completion_are_preserved(self):
        state = self.root / '.claude/fluxpoint'
        state.mkdir(parents=True)
        policy = '*\n!shared-settings.json\n'
        (state / '.gitignore').write_text(policy)
        self.start()
        self.assertEqual((state / '.gitignore').read_text(), policy)
        self.value(80)
        self.finish()
        path = state / 'slices/lookup.json'
        data = json.loads(path.read_text())
        data.pop('role')
        path.write_text(json.dumps(data))
        err = self.run_record('--end', '--slice', 'lookup', ok=False)
        self.assertIn('malformed starting measurement', err)
        self.assertNotIn('Traceback', err)

    def test_local_artifacts_leave_a_committed_repository_clean(self):
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.STDOUT).decode().strip()
        git('init', '-q')
        git('config', 'user.name', 'test')
        git('config', 'user.email', 't@t')
        git('add', '-A')
        git('commit', '-qm', 'base')
        start = self.start()
        self.assertEqual(start['startHead'], git('rev-parse', 'HEAD'))
        self.assertEqual(git('status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
