#!/usr/bin/env python3
"""Windows adapter fixtures; no Windows host or PowerShell required."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "box_health_windows.py"
SPEC = importlib.util.spec_from_file_location("box_health_windows", MODULE)
windows = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(windows)


def process(pid=101, created="2026-10-15T00:00:00.0000000Z", kernel="10000000"):
    return {"pid": pid, "ppid": 50, "created": created, "kernel": kernel,
            "user": "20000000", "command": 'node "C:\\project\\worker.js"', "name": "node.exe"}


def payload():
    return {"before": [process()], "after": [process(kernel="20000000")],
            "before_at": "2026-10-15T00:01:00.0000000Z",
            "after_at": "2026-10-15T00:01:05.0000000Z", "elapsed": 5,
            "cpu_before": [100000000, 200000000, 50000000],
            "cpu_after": [120000000, 260000000, 70000000],
            "ports": {"status": "ok", "range_output": ["Protocol tcp Dynamic Port Range",
                      "Start Port : 49152", "Number of Ports : 16384"],
                      "local_ports": [80, 49152, 49152, 65535]}}


class WindowsSampleTests(unittest.TestCase):
    def sample(self, data=None, result=None):
        result = result or subprocess.CompletedProcess([], 0, json.dumps(data or payload()), "")
        with mock.patch.object(windows.time, "monotonic", side_effect=[0, 6]), \
                mock.patch.object(windows.subprocess, "run", return_value=result) as run:
            sampled = windows.sample()
        return sampled, run

    def test_one_read_only_powershell_invocation_and_counter_units(self):
        sampled, run = self.sample()
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("-NoProfile", command)
        self.assertIn("Start-Sleep -Milliseconds 5000", command[-1])
        self.assertEqual(command[-1].count("Get-CimInstance Win32_Process"), 1)
        for mutation in ("Stop-Process", "Remove-", "Set-CimInstance", "taskkill"):
            self.assertNotIn(mutation, command[-1])
        self.assertEqual(sampled["before"][0]["cpu_seconds"], 3)
        self.assertEqual(sampled["after"][0]["cpu_seconds"], 4)
        self.assertEqual(sampled["after"][0]["age_seconds"], 65)
        self.assertEqual(sampled["cpu"], {"busy_seconds": 6, "total_seconds": 8})
        self.assertEqual(sampled["ports"]["used"], 2)
        self.assertEqual(sampled["elapsed"], 5)
        script = command[-1]
        self.assertLess(script.index('$cpuBefore = [BoxHealthCpu]::Read()'), script.index('Start-Sleep -Milliseconds'))
        self.assertLess(script.index('Start-Sleep -Milliseconds'), script.index('$cpuAfter = [BoxHealthCpu]::Read()'))
        self.assertLess(script.index('$cpuAfter = [BoxHealthCpu]::Read()'), script.index('$ports ='))
        self.assertNotIn('netstat.exe -ano -p tcp', script)
        self.assertIn('netstat.exe -ano', script)

    def test_cpu_window_excludes_slow_startup_and_port_queries(self):
        data = payload()
        data['cpu_before'] = [0, 0, 0]
        data['cpu_after'] = [10000000, 100000000, 0]  # 90% during the actual five-second sample
        completed = subprocess.CompletedProcess([], 0, json.dumps(data), '')
        with mock.patch.object(windows.time, 'monotonic', side_effect=[0, 60]), \
             mock.patch.object(windows.subprocess, 'run', return_value=completed):
            sampled = windows.sample()
        self.assertEqual(sampled['elapsed'], 5)
        self.assertEqual(sampled['cpu']['busy_seconds']/sampled['cpu']['total_seconds'], .9)
        for bad in ([], [0, 0], [True, 0, 0], ['bad', 0, 0]):
            data['cpu_before'] = bad
            with self.subTest(counters=bad), self.assertRaisesRegex(ValueError, 'unreadable'):
                self.sample(data)

    def test_pid_reuse_keeps_creation_identity(self):
        data = payload()
        data["after"][0]["created"] = "2026-10-15T00:01:02Z"
        sampled, _ = self.sample(data)
        self.assertEqual(sampled["before"][0]["pid"], sampled["after"][0]["pid"])
        self.assertNotEqual(sampled["before"][0]["created"], sampled["after"][0]["created"])
        self.assertEqual(sampled["after"][0]["age_seconds"], 3)

    def test_null_command_falls_back_to_name_and_idle_is_omitted(self):
        data = payload()
        data["before"][0]["command"] = None
        data["before"].append(process(pid=0, created=None))
        sampled, _ = self.sample(data)
        self.assertEqual(len(sampled["before"]), 1)
        self.assertIn("command line unavailable", sampled["before"][0]["command"])
        self.assertIn("node.exe", sampled["before"][0]["command"])
        self.assertFalse(sampled["before"][0]["command_available"])

    def test_invalid_process_tables_fail_instead_of_reporting_healthy(self):
        for field, value in (("created", None), ("created", "2027-01-01T00:00:00Z"),
                             ("kernel", "-1"), ("pid", True)):
            data = payload()
            data["after"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "process table unreadable"):
                self.sample(data)
        for bad in ([], {}, None):
            data = payload()
            data["before"] = bad
            with self.subTest(table=bad), self.assertRaisesRegex(ValueError, "process table unreadable"):
                self.sample(data)

    def test_power_shell_failures_are_explicit(self):
        for result in (subprocess.CompletedProcess([], 1, "", "Access denied"),
                       subprocess.CompletedProcess([], 0, "invalid json", "")):
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, "process table unreadable"):
                self.sample(result=result)
        with mock.patch.object(windows.subprocess, "run", side_effect=FileNotFoundError("powershell.exe")):
            with self.assertRaisesRegex(ValueError, "process table unreadable"):
                windows.sample()

    def test_invalid_seconds_do_not_launch_powershell(self):
        for value in (0, -1, float("nan"), float("inf"), "bad", 2147484):
            with self.subTest(value=value), mock.patch.object(windows.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    windows.sample(value)
                run.assert_not_called()

    def test_optional_port_failure_does_not_hide_process_data(self):
        for status in ("unsupported", "unreadable"):
            data = payload()
            data["ports"] = {"status": status, "detail": "Access denied"}
            sampled, _ = self.sample(data)
            self.assertEqual(sampled["ports"]["status"], status)
            self.assertEqual(sampled["ports"]["detail"], "Access denied")
            self.assertTrue(sampled["after"])

    def test_netstat_fallback_localization_ipv6_and_unique_ports(self):
        raw = {"status": "ok", "range_output": ["Startanschluss : 49152", "Anzahl : 16384"],
               "netstat_output": ["Active Connections", "Proto Local Address Foreign Address State PID",
                                  "TCP 127.0.0.1:49152 127.0.0.1:1 ESTABLISHED 123",
                                  "TCP [::1]:49152 [::1]:1 TIME_WAIT 0",
                                  "TCP [::]:65535 [::]:0 LISTENING 123",
                                  "TCP 0.0.0.0:80 0.0.0.0:0 LISTENING 123"]}
        parsed = windows._ports(raw)
        self.assertEqual(parsed, {"status": "ok", "used": 2, "range_start": 49152,
                                  "range_size": 16384, "detail": ""})

    def test_malformed_ports_are_unreadable(self):
        raw = payload()["ports"]
        for field, value in (("range_output", ["unknown"]), ("local_ports", [70000]),
                             ("local_ports", None), ("status", "healthy")):
            broken = copy.deepcopy(raw)
            broken[field] = value
            with self.subTest(field=field):
                self.assertEqual(windows._ports(broken)["status"], "unreadable")
        self.assertEqual(windows._ports(None)["status"], "unreadable")

    def test_ipv4_and_ipv6_ranges_count_their_own_connections(self):
        raw = {"status": "ok", "ranges": [
            {"family": "ipv4", "range_output": ["Start : 49152", "Count : 16384"]},
            {"family": "ipv6", "range_output": ["Start : 60000", "Count : 1000"]}],
            "connections": [{"LocalAddress": "127.0.0.1", "LocalPort": 49152},
                            {"LocalAddress": "::1", "LocalPort": 49152},
                            {"LocalAddress": "::", "LocalPort": 60000}]}
        parsed = windows._ports(raw)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["ranges"], [
            {"family": "IPv4", "used": 1, "range_start": 49152, "range_size": 16384},
            {"family": "IPv6", "used": 1, "range_start": 60000, "range_size": 1000}])
        del raw["connections"]
        raw["netstat_output"] = ["TCP 127.0.0.1:49152 1.2.3.4:1 TIME_WAIT 0",
                                 "TCP [::1]:49152 [::1]:1 TIME_WAIT 0",
                                 "TCPv6 [::]:60000 [::]:0 LISTENING 100"]
        self.assertEqual(windows._ports(raw), parsed)


if __name__ == "__main__":
    unittest.main()
