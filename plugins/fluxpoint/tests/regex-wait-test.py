#!/usr/bin/env python3
"""Real isolated readiness matching: regex CPU cannot exceed an await budget."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('bounded_regex', SCRIPTS / 'bounded_regex.py')
regex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(regex)


class BoundedRegexTests(unittest.TestCase):
    def test_real_match_nonmatch_and_empty_line_are_distinct(self):
        self.assertEqual(regex.search_lines(r'^READY \d+$', ['starting', 'READY 42', 'later'], 5), 'READY 42')
        self.assertIsNone(regex.search_lines(r'^READY$', ['not ready', 'still starting'], 5))
        self.assertEqual(regex.search_lines(r'^$', [''], 5), '')
        self.assertEqual(regex.search_lines(r'café', ['a café is ready'], 5), 'a café is ready')

    def test_invalid_regex_is_explicit_not_a_nonmatch(self):
        with self.assertRaisesRegex(ValueError, 'regex worker.*error'):
            regex.search_lines('[', ['READY'], 5)

    def test_pathological_expression_times_out_and_worker_is_reaped(self):
        created = []
        original = subprocess.Popen
        def track(*args, **kwargs):
            child = original(*args, **kwargs)
            created.append(child)
            return child
        began = time.monotonic()
        with mock.patch.object(regex.subprocess, 'Popen', side_effect=track):
            with self.assertRaisesRegex(regex.RegexTimeout, 'matching budget'):
                regex.search_lines(r'^(a+)+$', ['a'*64+'!'], .25)
        self.assertLess(time.monotonic()-began, 5)
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)
        self.assertIsNotNone(created[0].poll())
        self.assertEqual(regex.search_lines('safe', ['safe control'], 5), 'safe control')

    def test_interrupted_caller_kills_and_reaps_its_waiting_worker(self):
        created = []
        original = subprocess.Popen
        def track(*args, **kwargs):
            child = original(*args, **kwargs)
            communicate = child.communicate
            first = [True]
            def interrupted(*call_args, **call_kwargs):
                if first[0]:
                    first[0] = False
                    raise KeyboardInterrupt()
                return communicate(*call_args, **call_kwargs)
            child.communicate = interrupted
            created.append(child)
            return child
        with mock.patch.object(regex.subprocess, 'Popen', side_effect=track):
            with self.assertRaises(KeyboardInterrupt):
                regex.search_lines('ready', ['ready'], 5)
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)

    def test_no_budget_never_launches_a_worker(self):
        for value in (0, -1, float('nan'), float('inf'), True):
            with self.subTest(value=value), mock.patch.object(regex.subprocess, 'Popen') as spawn:
                with self.assertRaises(regex.RegexTimeout):
                    regex.search_lines('ready', ['ready'], value)
                spawn.assert_not_called()

    def test_line_and_batch_limits_apply_before_search_or_launch(self):
        with mock.patch.object(regex.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'line exceeds'):
                regex.search_lines('x', ['x'*(regex.MAX_LINE+1)], 5)
            with self.assertRaisesRegex(ValueError, 'batch exceeds'):
                regex.search_lines('x', ['x'*regex.MAX_LINE]*5, 5)
            with self.assertRaisesRegex(ValueError, '65536'):
                regex.search_lines('x'*(regex.MAX_PATTERN+1), ['x'], 5)
            spawn.assert_not_called()

    def test_worker_starts_isolated_and_inherits_no_tool_pipes(self):
        original = subprocess.Popen
        observed = []
        def track(*args, **kwargs):
            observed.append((args, kwargs))
            return original(*args, **kwargs)
        with mock.patch.object(regex.subprocess, 'Popen', side_effect=track):
            self.assertEqual(regex.search_lines('READY', ['READY'], 5), 'READY')
        argv = observed[0][0][0]
        self.assertEqual(argv[1:3], ['-I', '-S'])
        self.assertEqual(argv[-1], '--worker')
        self.assertEqual(observed[0][1]['stdin'], subprocess.PIPE)
        self.assertEqual(observed[0][1]['stdout'], subprocess.PIPE)


if __name__ == '__main__':
    unittest.main()
