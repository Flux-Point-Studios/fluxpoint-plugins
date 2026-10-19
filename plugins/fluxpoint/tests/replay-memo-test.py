#!/usr/bin/env python3
"""Replay provenance against actual on-disk journals and agent transcripts."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import replay_memo as rm


class ReplayMemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.run = "wf_source"
        self.directory = self.projects / "project/session/subagents/workflows" / self.run
        self.directory.mkdir(parents=True)
        self.store = self.root / ".claude/fluxpoint/runs"
        self.store.mkdir(parents=True)
        self.launch = {"since": "2026-10-19T19:00:00Z", "nonce": "a" * 64}
        self.compiler = "1.46.0"
        self.tree = dict(root="/checkout", head="a" * 40, porcelain="", repository="o/r", branch="main", tip="b" * 40)
        self.calls = [dict(prompt="Build café", opts={"label": "build", "effort": "high"}, result={"outcome": "committed"}),
                      dict(prompt="Judge exact tree", opts={"label": "gate", "effort": "high"}, result={"sha": "b" * 40, "exit": 0})]
        self.records = []
        for index, call in enumerate(self.calls):
            payload = dict(campaign="test", compiler=self.compiler, launch=self.launch, id=call["opts"]["label"],
                           prompt=call["prompt"], opts=call["opts"], tree=self.tree if index else None,
                           ancestors=[self.records[0]["key"]] if index else [])
            self.records.append(dict(node=payload["id"], key=rm.memo_key(payload), payload=payload,
                                     prompt=call["prompt"], result=call["result"], mode="live"))
        self.records[0].update(verifiedReading=self.tree, verifiedBy=["gate"], verifiedByNode="gate")
        self.records[1]["verifies"] = "build"
        self.summary = dict(campaign="test", launch=self.launch, callRecords=self.calls, memoRecords=self.records,
                            results={r["node"]: r["result"] for r in self.records})
        events = []
        for i, call in enumerate(self.calls):
            aid = "a" + str(i)
            events.extend([dict(type="started", agentId=aid, opts=call["opts"], timestamp="2026-10-19T19:01:00Z"),
                           dict(type="result", agentId=aid, result=call["result"], timestamp="2026-10-19T19:03:00Z")])
            usage = {"role": "assistant", "id": "msg1", "usage": {"input_tokens": 10, "output_tokens": 5}}
            self.write_rows(self.directory / ("agent-" + aid + ".jsonl"), [
                dict(type="user", message=dict(role="user", content=[dict(type="text", text=call["prompt"])])),
                dict(type="assistant", message=usage), dict(type="assistant", message=usage)])
        self.events = events
        self.save()

    def write_rows(self, path, rows):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def save(self):
        (self.store / (self.run + ".json")).write_text(json.dumps(dict(runId=self.run, summary=self.summary)), encoding="utf-8")
        self.write_rows(self.directory / "journal.jsonl", self.events)

    def build(self):
        return rm.build_memo(self.root, self.run, self.launch, self.compiler, projects=self.projects)

    def test_corroborates_completed_calls_and_deduplicates_api_usage(self):
        result = self.build()
        self.assertEqual(len(result["_prefix"]), 2)
        self.assertEqual(len(result["_memo"]), 2)
        builder = result["_memo"][self.records[0]["key"]]
        self.assertEqual(builder["agentId"], "a0")
        self.assertEqual(builder["verifiedReading"], self.tree)
        self.assertEqual(builder["cost"], {"minutes": 2, "tokens": 15})

    def test_live_transcript_and_journal_are_re_read(self):
        self.build()
        path = self.directory / "agent-a0.jsonl"
        rows = [dict(type="user", message=dict(role="user", content="forged prompt"))]
        self.write_rows(path, rows)
        with self.assertRaisesRegex(rm.ReplayError, "no matching completed"):
            self.build()

    def test_fabricated_result_options_hash_or_launch_cannot_replay(self):
        original = copy.deepcopy(self.summary)
        for field in ("result", "options", "key", "launch"):
            with self.subTest(field=field):
                self.summary = copy.deepcopy(original)
                if field == "result":
                    self.summary["callRecords"][0]["result"] = {"outcome": "invented"}
                elif field == "options":
                    self.summary["callRecords"][0]["opts"]["effort"] = "low"
                elif field == "key":
                    self.summary["memoRecords"][0]["key"] = "0" * 64
                else:
                    self.summary["launch"] = {"nonce": "different"}
                self.save()
                with self.assertRaises(rm.ReplayError):
                    self.build()

    def test_a_builder_claimed_sha_is_not_a_verifying_sentinel(self):
        self.records[0]["verifiedReading"] = dict(self.tree, tip="c" * 40)
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, "verifying gate"):
            self.build()

    def test_unrelated_gate_cannot_witness_builder(self):
        self.records[1]["verifies"] = "another-builder"
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, "verifying gate"):
            self.build()

    def test_missing_failed_and_ambiguous_sources_fail_closed(self):
        source = self.directory / "agent-a0.jsonl"
        source.rename(source.with_suffix(".old"))
        with self.assertRaisesRegex(rm.ReplayError, "exactly one transcript"):
            self.build()
        source.with_suffix(".old").rename(source)
        duplicate = self.directory.parent / source.name
        duplicate.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(rm.ReplayError, "exactly one transcript"):
            self.build()
        duplicate.unlink()
        self.events[1] = dict(type="failed", agentId="a0")
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, "no matching completed"):
            self.build()

    def test_recorder_independently_verifies_memo_and_omitted_receipts(self):
        replay = copy.deepcopy(self.records[0])
        replay.update(mode="memo", source={"runId": self.run, "agentId": "a0"})
        current = dict(campaign="test", launch=self.launch, memoRecords=[replay],
                       provenance=[dict(node="build", status="REPLAYED-MEMO")])
        checked = rm.verify_replays(self.root, current, projects=self.projects)
        self.assertEqual(checked[0]["node"], "build")
        current["memoRecords"][0]["result"] = {"outcome": "forged"}
        with self.assertRaisesRegex(rm.ReplayError, "corroborate"):
            rm.verify_replays(self.root, current, projects=self.projects)
        current["memoRecords"] = []
        with self.assertRaisesRegex(rm.ReplayError, "no independently verified"):
            rm.verify_replays(self.root, current, projects=self.projects)

    def test_provenance_cannot_cycle_between_artifacts(self):
        self.calls[0]["source"] = {"runId": self.run, "agentId": "a0"}
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, "cyclic"):
            self.build()

    def test_rejudge_keeps_verbatim_context_and_prior_verdict(self):
        request = dict(nonce="review-2", reason="Host investigation completed | keep prior red", note="Rerun this gate.\nRetain both attempts.")
        current = dict(campaign="test", launch=self.launch, inputs={"rejudge": {"gate": request}},
                       results={"gate": {"sha": "b" * 40, "exit": 0}},
                       tree={"checks": [dict(point="before gate", **self.tree)]},
                       rejudges=[dict(gate="gate", tree=self.tree, priorVerdict=self.calls[1]["result"],
                                      sourceRunId=self.run, **request)])
        row = rm.verify_rejudges(self.root, current)[0]
        self.assertEqual(row["reason"], request["reason"])
        self.assertEqual(row["note"], request["note"])
        current["rejudges"][0]["priorVerdict"] = {"exit": 1}
        with self.assertRaisesRegex(rm.ReplayError, "prior verdict"):
            rm.verify_rejudges(self.root, current)
        current["rejudges"] = []
        with self.assertRaisesRegex(rm.ReplayError, "not reached"):
            rm.verify_rejudges(self.root, current)

    def test_recording_refuses_forged_replay_but_retains_artifact(self):
        graph = self.root / "WORK.md"
        ir = {"nodes": [{"id": "build", "mutates": True, "replay": "inputs"},
                        {"id": "gate", "independent": True, "replay": "tree", "verifies": "build"}]}
        graph.write_text("# Run\n```json graph-ir\n" + json.dumps(ir) + "\n```\n\n## Evidence\n"
                         "| When (UTC) | Source | Outcome | Claim | Proof |\n| --- | --- | --- | --- | --- |\n")
        replay = copy.deepcopy(self.records[0])
        replay.update(mode="memo", source={"runId": self.run, "agentId": "a0"}, result={"outcome": "forged"})
        summary = dict(campaign="test", outcome="COMPLETE", launch=self.launch, memoRecords=[replay],
                       provenance=[dict(node="build", status="REPLAYED-MEMO")], results={})
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.root))
        command = [sys.executable, str(SCRIPTS / "record-run.py"), "--root", str(self.root),
                   "--graph", str(graph), "--run-id", "wf_bad"]
        result = subprocess.run(command, input=json.dumps(summary), text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("REPLAY-PROVENANCE", result.stderr)
        self.assertNotIn("wf_bad |", graph.read_text())
        stored = json.loads((self.store / "wf_bad.json").read_text())
        self.assertEqual(stored["outcome"], "REFUSED-REPLAY")
        self.assertTrue(stored["replayProblems"])

    def test_legacy_run_never_supplies_unverifiable_memo(self):
        self.summary.pop("memoRecords")
        self.summary.pop("callRecords")
        self.save()
        self.assertEqual(self.build(), {"_memo": {}, "_prefix": []})

    def test_run_ids_and_source_symlinks_cannot_escape_store(self):
        for value in ("../escape", "C:stream", "CON", "run."):
            with self.subTest(value=value), self.assertRaises(rm.ReplayError):
                rm.artifact(self.root, value)
        path = self.store / "escape.json"
        try:
            path.symlink_to(self.root / "outside.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(rm.ReplayError, "escapes"):
            rm.artifact(self.root, "escape")

    def test_canonical_bytes_match_javascript_sha256(self):
        value = {"é": "café", "a": [1.0, 0.000001, 1e-7, -0.0, 1e21], "z": {"b": 2, "a": 1}}
        script = ("const crypto=require('node:crypto'); const x=" + json.dumps(value) + ";"
                  "function c(v){if(Array.isArray(v))return '['+v.map(c).join(',')+']';"
                  "if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+c(v[k])).join(',')+'}';"
                  "return JSON.stringify(v)};process.stdout.write(crypto.createHash('sha256').update(c(x)).digest('hex'))")
        expected = subprocess.check_output(["node", "-e", script], text=True)
        self.assertEqual(rm.memo_key(value), expected)
        self.assertEqual(rm.memo_key(value), hashlib.sha256(rm.canonical(value).encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
