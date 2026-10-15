#!/usr/bin/env python3
"""Machine health fixtures: current counters, operator evidence, no workload control."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import box_health as health


def process(pid, ppid=10, cpu=0., born='2026-10-14T00:00:00Z', command='python worker.py', age=900.):
    return dict(pid=pid, ppid=ppid, name='python3', command=command, cpu_seconds=cpu,
                created=born, age_seconds=age)


def sample():
    before = [process(1, 0, command='/sbin/init'), process(10, 1, command='supervisor'), process(20, 10)]
    after = copy.deepcopy(before)
    after[-1]['cpu_seconds'] = 1.
    return dict(platform='linux', before=before, after=after, elapsed=5.,
                cpu=dict(total_seconds=50., busy_seconds=5.),
                ports=dict(status='ok', ranges=[dict(family='IPv4/IPv6', used=2, range_start=40000, range_size=100)]))


class HealthTests(unittest.TestCase):
    def check(self, data=None, loopback=None, **settings):
        return health.check_health(sampler=lambda seconds: copy.deepcopy(data or sample()),
            loopback=lambda threshold: loopback or dict(status='ok', medianMs=.02), **settings)

    def test_healthy_thresholds_and_five_second_default(self):
        with mock.patch.object(health, 'sample_processes', return_value=sample()) as sampler, \
             mock.patch.object(health, 'loopback_probe', return_value=dict(status='ok', medianMs=.01)):
            report = health.check_health()
        sampler.assert_called_once_with(5.)
        self.assertEqual(report['exitCode'], 0)
        self.assertEqual(report['pluginTimeoutSeconds'], 600)
        self.assertEqual(report['checks']['cpu']['percent'], 10)

    def test_orphan_core_usage_is_sampled_and_evidence_names_process(self):
        data = sample()
        data['before'].append(process(30, 999, cpu=100.))
        data['after'].append(process(30, 999, cpu=104.5))
        report = self.check(data)
        self.assertEqual(report['exitCode'], 1)
        finding = report['findings'][0]
        self.assertEqual(finding['code'], 'orphan-cpu')
        self.assertEqual(finding['processes'][0]['cpuPercentOfCore'], 90)
        text = health.format_report(report)
        for expected in ('PID 30', 'PPID 999', '2026-10-14T00:00:00Z', 'python worker.py'):
            self.assertIn(expected, text)
        self.assertEqual(self.check(data, orphan_cpu_threshold=91)['exitCode'], 0)
        # Lifetime CPU of 100 seconds is irrelevant when the current delta is 0.
        data['after'][-1]['cpu_seconds'] = 100.
        self.assertEqual(self.check(data)['exitCode'], 0)

    def test_pid_reuse_cannot_invent_cpu_load_and_reused_parent_is_reported(self):
        data = sample()
        data['after'][-1].update(created='2026-10-14T01:00:00Z', cpu_seconds=500)
        self.assertEqual(self.check(data)['exitCode'], 0)
        data = sample()
        data['after'][1]['created'] = '2026-10-14T01:00:00Z'
        data['after'][-1]['cpu_seconds'] = 5.
        self.assertIn('PID was reused', self.check(data)['findings'][0]['message'])

    def test_init_children_are_not_assumed_orphans(self):
        data = sample()
        for row in (data['before'][-1], data['after'][-1]):
            row.update(ppid=1, command='python -m service_manager')
        data['after'][-1]['cpu_seconds'] = 5.
        self.assertEqual(self.check(data)['exitCode'], 0)
        # An observed parent exit, unlike long-standing init parentage, is evidence.
        data['before'][-1]['ppid'] = 10
        data['after'] = [row for row in data['after'] if row['pid'] != 10]
        self.assertEqual(self.check(data)['findings'][0]['code'], 'orphan-cpu')

    def test_adopted_busy_loop_and_bounded_plugin_hook_are_suspected(self):
        data = sample()
        for row in (data['before'][-1], data['after'][-1]):
            row.update(ppid=1, command='python -c "while True: pass"')
        data['after'][-1]['cpu_seconds'] = 5.
        self.assertEqual(self.check(data)['findings'][0]['code'], 'orphan-cpu')
        hook = str(SCRIPTS / 'secret-guard.py')
        for row in (data['before'][-1], data['after'][-1]):
            row.update(command=f'python "{hook}" --hook', cpu_seconds=0.)
        # Quoted script arguments are normal on Windows/Git Bash.
        report = self.check(data)
        self.assertEqual(report['exitCode'], 1)
        self.assertEqual(report['findings'][0]['code'], 'orphan-plugin')
        self.assertEqual(self.check(data, plugin_timeout=1000)['exitCode'], 0)

    def test_detached_attested_gate_is_not_a_timed_out_hook(self):
        data = sample()
        for row in (data['before'][-1], data['after'][-1]):
            row.update(ppid=999, command=f'python {SCRIPTS / "attest.py"} --run harness', age_seconds=10000, cpu_seconds=0.)
        self.assertEqual(self.check(data)['exitCode'], 0)

    def test_total_cpu_lists_five_largest_current_processes(self):
        data = sample()
        data['cpu']['busy_seconds'] = 45.
        data['before'] += [process(100+i, 10, cpu=0.) for i in range(8)]
        data['after'] += [process(100+i, 10, cpu=float(i)) for i in range(8)]
        report = self.check(data)
        finding = next(f for f in report['findings'] if f['code'] == 'cpu-saturated')
        self.assertEqual([p['pid'] for p in finding['processes']], [107, 106, 105, 104, 103])
        self.assertEqual(self.check(data, cpu_threshold=91)['exitCode'], 0)

    def test_port_pressure_and_loopback_are_independent_findings(self):
        data = sample()
        data['ports']['ranges'][0]['used'] = 80
        report = self.check(data, loopback=dict(status='unhealthy', medianMs=60.))
        self.assertEqual({f['code'] for f in report['findings']}, {'ports-pressured', 'loopback-slow'})
        self.assertEqual(self.check(data, port_threshold=81)['exitCode'], 0)

    def test_unreadable_and_unsupported_never_become_zero_load(self):
        for state in ('unreadable', 'unsupported'):
            data = sample()
            data['ports'] = dict(status=state, detail='TCP table unavailable')
            report = self.check(data)
            self.assertEqual(report['exitCode'], 2)
            self.assertEqual(report['status'], state)
            self.assertIn('TCP table unavailable', health.format_report(report))
        report = health.check_health(sampler=mock.Mock(side_effect=PermissionError('process table denied')),
                                    loopback=lambda _: dict(status='ok', medianMs=.01))
        self.assertEqual(report['exitCode'], 2)
        self.assertIn('denied', health.format_report(report))
        report = health.check_health(sampler=mock.Mock(side_effect=NotImplementedError('unsupported platform')),
                                    loopback=lambda _: dict(status='ok', medianMs=.01))
        self.assertEqual(report['status'], 'unsupported')

    def test_invalid_process_and_partial_counters_fail_explicitly(self):
        for field, value in [('cpu_seconds', float('nan')), ('age_seconds', -1), ('ppid', '10')]:
            data = sample()
            data['after'][-1][field] = value
            self.assertEqual(self.check(data)['exitCode'], 2)
        data = sample()
        data['unreadable'] = ['PID 200: denied']
        self.assertEqual(self.check(data)['exitCode'], 2)
        data = sample()
        data['cpu']['total_seconds'] = 0
        self.assertEqual(self.check(data)['exitCode'], 2)

    def test_linux_proc_parser_and_dynamic_tcp_range(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)
            (proc / 'stat').write_text('cpu 100 0 20 300 10 0 0 0 1 1\nbtime 1600000000\n')
            (proc / 'uptime').write_text('1000.0 0.0\n')
            (proc / '123').mkdir()
            fields = ['S', '10'] + ['0']*17 + ['10000']
            fields[11], fields[12] = '200', '100'
            (proc / '123/stat').write_text('123 (python (worker)) ' + ' '.join(fields))
            (proc / '123/cmdline').write_bytes(b'python\0worker.py\0')
            with mock.patch.object(health.os, 'sysconf', return_value=100, create=True):
                captured = health.linux_snapshot(proc, clock=lambda: 5.)
            row = captured['processes'][0]
            self.assertEqual((row['pid'], row['ppid'], row['cpu_seconds'], row['age_seconds']), (123, 10, 3., 900.))
            self.assertEqual(captured['cpu']['total_seconds'], 4.3)
            (proc / 'sys/net/ipv4').mkdir(parents=True)
            (proc / 'sys/net/ipv4/ip_local_port_range').write_text('40000 40099\n')
            (proc / 'net').mkdir()
            (proc / 'net/tcp').write_text(' sl local_address rem_address st\n 0: 0100007F:9C40 00000000:0000 01\n')
            (proc / 'net/tcp6').write_text(' sl local_address rem_address st\n 0: 00000000000000000000000000000000:9C40 00000000:0000 06\n 1: 0:9C41 0:0000 06\n')
            ports = health.linux_ports(proc)
            self.assertEqual(ports['status'], 'ok')
            self.assertEqual(ports['ranges'][0]['used'], 2)
            (proc / 'net/tcp').write_text('broken')
            self.assertEqual(health.linux_ports(proc)['status'], 'unreadable')

    def test_loopback_real_socket_is_closed_and_no_process_control(self):
        report = health.loopback_probe(limit_ms=1000)
        self.assertEqual(report['status'], 'ok', report)
        self.assertGreaterEqual(report['medianMs'], 0)
        for filename in ('box_health.py', 'box_health_windows.py', 'box-health.py'):
            source = (SCRIPTS / filename).read_text(encoding='utf-8')
            self.assertNotIn('os.kill(', source)
            self.assertNotIn('Stop-Process', source)
            self.assertNotIn('taskkill', source)

    def test_cli_invalid_threshold_is_exit_two_without_sampling(self):
        for args in (['--sample-seconds', 'nan'], ['--sample-seconds', '61'], ['--port-threshold', '101']):
            result = subprocess.run([sys.executable, str(SCRIPTS / 'box-health.py'), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
