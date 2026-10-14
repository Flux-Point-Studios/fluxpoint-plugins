#!/usr/bin/env python3
"""Workflow observer regressions using on-disk journals and real Git state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import inbox
import watchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.run = self.projects / "p/session/subagents/workflows/run1"
        self.run.mkdir(parents=True)
        self.journal = self.run / "journal.jsonl"
        self.clock = time.time()

    def write(self, path, records, minutes=0):
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        os.utime(path, (self.clock - minutes * 60,) * 2)

    def observe(self, **kw):
        return watchdog.inspect(str(self.root), "run1", self.run, now=self.clock, **kw)

    def test_stall_and_live_activity(self):
        self.write(self.journal, [{"type": "started", "agentId": "a"}])
        transcript = self.run / "agent-a.jsonl"
        self.write(transcript, [{"cwd": str(self.root)}], 41)
        code, messages = self.observe()
        self.assertEqual(code, 1)
        self.assertIn("agent-stalled a", messages[0])
        self.assertEqual(len(inbox.open_items(str(self.root))), 1)
        self.observe()
        self.assertEqual(len(inbox.read(str(self.root))), 1)
        self.write(transcript, [{"cwd": str(self.root)}])
        self.assertIsNone(self.observe()[0])

    def test_completed_agent_needs_no_transcript_and_waits_for_quiet(self):
        events = [{"type": "started", "agentId": "a"}, {"type": "result", "agentId": "a"}]
        self.write(self.journal, events)
        self.assertIsNone(self.observe()[0])
        self.write(self.journal, events, 16)
        self.assertEqual(self.observe()[0], 0)

    def test_failure_records_all_agents_and_resume_tree(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
            subprocess.run(["git", "-C", str(self.root), "config", key, value], check=True)
        tracked = self.root / "code.py"
        tracked.write_text("before\n")
        subprocess.run(["git", "-C", str(self.root), "add", "code.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        tracked.write_text("after\n")
        subprocess.run(["git", "-C", str(self.root), "commit", "-qam", "agent fix"], check=True)
        tracked.write_text("still in progress\n")
        events = []
        for agent in ("a", "b"):
            events.extend([{"type": "started", "agentId": agent, "label": "builder"},
                           {"type": "failed", "agentId": agent}])
            self.write(self.run / f"agent-{agent}.jsonl", [
                {"cwd": str(self.root), "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "check"}}]}},
                {"error": "rate_limit", "apiErrorStatus": 429,
                 "quotaLimits": {"resetsAt": "2026-10-15T00:00:00Z"}}])
        self.write(self.journal, events)
        self.assertEqual(self.observe(base=base)[0], 1)
        items = inbox.open_items(str(self.root))
        self.assertEqual(len(items), 2)
        self.assertIn("resetsAt 2026", items[0]["detail"])
        notes = list((self.root / ".claude/fluxpoint/resume").glob("*.md"))
        self.assertEqual(len(notes), 2)
        note = notes[0].read_text()
        for expected in ("builder", '"name": "Bash"', " M code.py", "agent fix", str(self.root)):
            self.assertIn(expected, note)

    def test_internal_format_errors_are_not_green(self):
        self.write(self.journal, [{"type": "started", "agentId": "a"}])
        with self.assertRaisesRegex(ValueError, "transcript"):
            self.observe()
        (self.run / "agent-a.jsonl").write_text("truncated{")
        with self.assertRaisesRegex(ValueError, "cannot parse"):
            self.observe()
        self.journal.write_text("[]\n")
        with self.assertRaisesRegex(ValueError, "object"):
            self.observe()

    def test_resolve_and_cli_unknown_or_pending(self):
        self.write(self.journal, [])
        self.assertEqual(watchdog.resolve_run("run1", self.projects), self.run.resolve())
        command = [sys.executable, str(SCRIPTS / "watchdog.py"), "--projects", str(self.projects),
                   "--root", str(self.root), "--run", "run1", "--once"]
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 3)
        self.journal.write_text("{broken\n")
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
        with self.assertRaises(ValueError):
            watchdog.resolve_run("../run1", self.projects)


if __name__ == "__main__":
    unittest.main()
