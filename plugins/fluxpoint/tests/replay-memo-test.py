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
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import replay_memo as rm
import run_artifacts


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
        self.compiler = json.loads((SCRIPTS.parent / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
        self.tree = dict(root="/checkout", head="a" * 40, porcelain="", repository="o/r", branch="main", tip="b" * 40)
        self.calls = [dict(prompt="Build café", opts={"label": "build", "effort": "high"}, result={"outcome": "committed"}),
                      dict(prompt="Judge exact tree", opts={"label": "gate", "effort": "high"}, result={"sha": "b" * 40, "exit": 0})]
        self.records = []
        for index, call in enumerate(self.calls):
            payload = dict(campaign="test", compiler=self.compiler, launch=self.launch, id=call["opts"]["label"],
                           prompt=call["prompt"], opts=call["opts"], tree=self.tree if index else None,
                           ancestors=[self.records[0]["key"]] if index else [])
            self.records.append(dict(node=payload["id"], key=rm.memo_key(payload), payload=payload,
                                     prompt=call["prompt"], result=call["result"], mode="live",
                                     replay="tree" if index else "inputs"))
        self.records[0].update(verifiedReading=self.tree, verifiedBy=["gate"], verifiedByNode="gate")
        self.records[1]["verifies"] = "build"
        self.records[0]['verifiedReadingCallIndex'] = 1
        self.records[0]['callIndex'] = 0
        self.records[1].update(readingCallIndex=1, reading=self.tree, callIndex=2)
        self.calls.insert(1, self.sentinel('before gate'))
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
        _, self.snapshot = run_artifacts.write(self.root, self.run, dict(runId=self.run, summary=self.summary))
        self.write_rows(self.directory / "journal.jsonl", self.events)

    def source(self, agent='a0'):
        return dict(runId=self.run, artifactSha256=self.snapshot, agentId=agent)

    def sentinel(self, point, request=None, nonce='live-attempt-1', tree=None):
        tree = self.tree if tree is None else tree
        prompt = rm._sentinel_base() + '\nLIVE TREE READING NONCE: ' + nonce + ' point ' + point
        if request:
            prompt += '\nREJUDGE NONCE: ' + request['nonce'] + '\nREJUDGE LAUNCH NONCE: ' + nonce

        if tree['branch'] is not None:
            prompt += ('\nAlso run exactly: git -C ' + json.dumps(tree['repository']) + ' ls-remote origin '
                       + json.dumps('refs/heads/' + tree['branch'])
                       + '. Return the sole full hexadecimal commit in tip, and the exact declared repository and branch as repository and branch. '
                       'No matches or multiple matches means tip must be empty. Never use a worker-reported sha.')
        return dict(prompt=prompt, opts=dict(label='tree-check:' + point, effort='low'), result=copy.deepcopy(tree), mode='live')

    def current(self, summary, run='wf_current'):
        calls = summary.setdefault('callRecords', [])
        if not calls:
            for record in summary.get('memoRecords', []):
                if record.get('mode') == 'prefix':
                    record['source'] = self.source()
                    calls.append(dict(copy.deepcopy(self.calls[0]), source=self.source(), mode='prefix'))
                elif record.get('mode') == 'live':
                    original = next(c for c in self.calls if c['opts'].get('label') == record['node'])
                    calls.append(dict(copy.deepcopy(original), mode='live'))
                elif record.get('mode') == 'memo':
                    point = ('replay ' if record['replay'] == 'inputs' else 'before ') + record['node']
                    record['readingCallIndex'] = len(calls)
                    calls.append(self.sentinel(point, nonce='current-attempt-2'))
        directory = self.projects / 'project/session/subagents/workflows' / run
        directory.mkdir(parents=True, exist_ok=True)
        events = []
        for i, call in enumerate(calls):
            if call.get('source'):
                continue
            aid = 'current' + str(i)
            events.extend([dict(type='started', agentId=aid, opts=call['opts']),
                           dict(type='result', agentId=aid, result=call['result'])])
            self.write_rows(directory / ('agent-' + aid + '.jsonl'), [
                dict(type='user', message=dict(role='user', content=call['prompt']))])
        self.write_rows(directory / 'journal.jsonl', events)
        return run

    def verify(self, current):
        run = self.current(current)
        return rm.verify_replays(self.root, current, projects=self.projects, run_id=run)

    def build(self):
        return rm.build_memo(self.root, self.run, self.launch, self.compiler, projects=self.projects)

    def test_corroborates_completed_calls_and_deduplicates_api_usage(self):
        result = self.build()
        self.assertEqual(len(result["_prefix"]), 3)
        self.assertEqual(len(result["_memo"]), 2)
        builder = result["_memo"][self.records[0]["key"]]
        self.assertEqual(builder["agentId"], "a0")
        self.assertEqual(builder["verifiedReading"], self.tree)
        self.assertEqual(builder["cost"], {"minutes": 2, "tokens": 15})
        prefix = result["_prefix"][0]
        self.assertNotIn("prompt", prefix)
        self.assertEqual(prefix["promptSha256"], hashlib.sha256(self.calls[0]["prompt"].encode()).hexdigest())

    def test_only_opted_in_nodes_are_exported_to_the_memo(self):
        self.records[1]["fresh"] = True
        self.save()
        self.assertEqual(len(self.build()["_memo"]), 1)
        self.records[0].pop("replay")
        self.save()
        self.assertEqual(self.build()["_memo"], {})

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

    def test_gate_verdict_must_cover_the_sentinel_tip(self):
        self.calls[2]["result"]["sha"] = "c" * 40
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, "sentinel's exact branch tip"):
            self.build()

    def test_gate_commit_hex_case_matches_the_runtime_comparison(self):
        self.calls[2]['result']['sha'] = 'B' * 40
        self.save()
        result = self.build()
        self.assertEqual(result['_memo'][self.records[1]['key']]['result']['sha'], 'B' * 40)

    def test_malformed_rejudge_inputs_are_named_provenance_errors(self):
        for value in ('not-a-map', None, []):
            with self.subTest(value=value):
                self.summary['inputs'] = {'rejudge': value}
                self.save()
                with self.assertRaisesRegex(rm.ReplayError, 'rejudge input must be an object'):
                    self.build()

    def test_recording_refuses_live_ancestor_and_missing_live_read(self):
        replay = copy.deepcopy(self.records[1])
        replay.update(mode="memo", source=self.source('a2'), reading=self.tree)
        current = dict(campaign="test", launch=self.launch, memoRecords=[self.records[0], replay],
                       results={"gate": replay["result"]})
        with self.assertRaisesRegex(rm.ReplayError, "live or missing ancestor"):
            self.verify(current)
        current["memoRecords"][0] = dict(self.records[0], mode="prefix")
        current["memoRecords"][1].pop("reading")
        current.pop('callRecords')
        with self.assertRaises(rm.ReplayError):
            self.verify(current)

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
        replay.update(mode="memo", source=self.source(), reading=self.tree)
        current = dict(campaign="test", launch=self.launch, memoRecords=[replay],
                       results={"build": replay["result"]},
                       provenance=[dict(node="build", status="REPLAYED-MEMO")])
        checked = self.verify(current)
        self.assertEqual(checked[0]["node"], "build")
        current["memoRecords"][0]["result"] = {"outcome": "forged"}
        current["results"]["build"] = {"outcome": "forged"}
        with self.assertRaisesRegex(rm.ReplayError, "differs from source"):
            self.verify(current)
        current["memoRecords"] = []
        with self.assertRaisesRegex(rm.ReplayError, "no independently verified"):
            self.verify(current)

    def test_same_run_id_earlier_attempt_is_not_a_cycle(self):
        original_hash = self.snapshot
        self.calls[0]["source"] = self.source()
        self.calls[0]['mode'] = 'prefix'
        self.save()
        self.assertNotEqual(self.snapshot, original_hash)
        memo = self.build()['_memo']
        self.assertEqual(memo[self.records[0]['key']]['artifactSha256'], original_hash)

    def test_rejudge_keeps_verbatim_context_and_prior_verdict(self):
        request = dict(nonce="review-2", reason="Host investigation completed | keep prior red", note="Rerun this gate.\nRetain both attempts.")
        current = dict(campaign="test", launch=self.launch, inputs={"rejudge": {"gate": request}},
                       results={"gate": {"sha": "b" * 40, "exit": 0}},
                       tree={"checks": [dict(point="before gate", **self.tree)]},
                       rejudges=[dict(gate="gate", tree=self.tree, priorVerdict=self.calls[2]["result"],
                                      sourceRunId=self.run, sourceArtifactSha256=self.snapshot, readingCallIndex=0, **request)])
        current['callRecords'] = [self.sentinel('before gate', request),
            dict(prompt='Judge now\nREJUDGE NONCE: '+request['nonce']+'\nREJUDGE LAUNCH NONCE: live-attempt-1\nOPERATOR REJUDGE NOTE (verbatim):\n'+request['note'],
                 opts={'label':'gate','effort':'high'}, result=current['results']['gate'], mode='live')]
        run = self.current(current)
        row = rm.verify_rejudges(self.root, current, projects=self.projects, run_id=run)[0]
        self.assertEqual(row["reason"], request["reason"])
        self.assertEqual(row["note"], request["note"])
        current["rejudges"][0]["priorVerdict"] = {"exit": 1}
        with self.assertRaisesRegex(rm.ReplayError, "prior verdict"):
            rm.verify_rejudges(self.root, current, projects=self.projects, run_id=run)
        current["rejudges"] = []
        with self.assertRaisesRegex(rm.ReplayError, "not reached"):
            rm.verify_rejudges(self.root, current, projects=self.projects, run_id=run)

    def test_recording_refuses_forged_replay_but_retains_artifact(self):
        graph = self.root / "WORK.md"
        ir = {"nodes": [{"id": "build", "mutates": True, "replay": "inputs"},
                        {"id": "gate", "independent": True, "replay": "tree", "verifies": "build"}]}
        graph.write_text("# Run\n```json graph-ir\n" + json.dumps(ir) + "\n```\n\n## Evidence\n"
                         "| When (UTC) | Source | Outcome | Claim | Proof |\n| --- | --- | --- | --- | --- |\n")
        replay = copy.deepcopy(self.records[0])
        replay.update(mode="memo", source=self.source(), result={"outcome": "forged"})
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

    def test_source_and_current_readings_require_raw_fixed_sentinel_calls(self):
        self.records[1]['readingCallIndex'] = 0
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, 'fixed sentinel'):
            self.build()
        self.records[1]['readingCallIndex'] = 1
        self.records[0]['verifiedReadingCallIndex'] = 0
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, 'fixed sentinel'):
            self.build()

    def test_sentinel_raw_result_and_target_cannot_be_replaced_by_summary_claim(self):
        for field in ('tip', 'root', 'porcelain'):
            original = copy.deepcopy(self.calls[1]['result'])
            self.calls[1]['result'][field] = 'c' * 40 if field == 'tip' else 'changed'
            self.events[3]['result'] = self.calls[1]['result']
            self.save()
            with self.subTest(field=field), self.assertRaisesRegex(rm.ReplayError, 'raw sentinel'):
                self.build()
            self.calls[1]['result'] = original
            self.events[3]['result'] = original

    def test_missing_journal_opts_requires_exact_bound_transcript_header(self):
        for event in self.events:
            if event['type'] == 'started':
                event.pop('opts', None)
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, 'no matching completed'):
            self.build()
        for i, call in enumerate(self.calls):
            call['prompt'] = 'Fluxpoint-Call-Opts-SHA256: ' + rm.memo_key(call['opts']) + '\n' + call['prompt']
            self.write_rows(self.directory / ('agent-a' + str(i) + '.jsonl'), [dict(type='user', message=dict(role='user', content=call['prompt']))])
        for record in self.records:
            call = self.calls[0 if record['node'] == 'build' else 2]
            record['prompt'] = record['payload']['prompt'] = call['prompt']
        self.records[0]['key'] = rm.memo_key(self.records[0]['payload'])
        self.records[1]['payload']['ancestors'] = [self.records[0]['key']]
        self.records[1]['key'] = rm.memo_key(self.records[1]['payload'])
        self.save()
        self.assertEqual(len(self.build()['_memo']), 2)
        self.calls[0]['opts']['effort'] = 'low'
        self.save()
        with self.assertRaisesRegex(rm.ReplayError, 'no matching completed'):
            self.build()

    def test_failed_null_receipt_fences_prefix_without_losing_independent_memo(self):
        self.calls.insert(0, dict(prompt='Failed worker', opts={'label':'failed'}, result=None, mode='live'))
        self.records[0]['verifiedReadingCallIndex'] += 1
        self.records[1]['readingCallIndex'] += 1
        self.records[0]['callIndex'] += 1
        self.records[1]['callIndex'] += 1
        self.save()
        result = self.build()
        self.assertEqual(result['_prefix'], [])
        self.assertEqual(len(result['_memo']), 2)

    def test_history_is_immutable_after_same_id_latest_is_replaced(self):
        original = self.snapshot
        self.summary['newAttempt'] = 2
        self.save()
        self.assertNotEqual(self.snapshot, original)
        old, checked = run_artifacts.read(self.root, self.run, original)
        self.assertEqual(checked, original)
        self.assertNotIn('newAttempt', old['summary'])
        history = self.store / 'history' / self.run / (original + '.json')
        history.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            run_artifacts.read(self.root, self.run, original)

    def test_fabricated_prior_verdict_has_no_completed_call(self):
        self.summary['results']['gate'] = {'sha':'b'*40, 'exit':0, 'invented':True}
        self.save()
        request = dict(nonce='review-2', reason='Retain prior result')
        current = dict(campaign='test', launch=self.launch, inputs={'rejudge':{'gate':request}},
                       results={'gate':{'sha':'b'*40,'exit':0}}, tree={'checks':[dict(point='before gate', **self.tree)]},
                       callRecords=[self.sentinel('before gate', request), dict(prompt='Gate\nREJUDGE NONCE: review-2',
                            opts={'label':'gate','effort':'high'}, result={'sha':'b'*40,'exit':0})],
                       rejudges=[dict(gate='gate',tree=self.tree,priorVerdict=self.summary['results']['gate'],
                            sourceRunId=self.run,sourceArtifactSha256=self.snapshot,readingCallIndex=0,**request)])
        run = self.current(current)
        with self.assertRaisesRegex(rm.ReplayError, 'no corroborated completed'):
            rm.verify_rejudges(self.root, current, projects=self.projects, run_id=run)

    def test_run_ids_cannot_escape_store(self):
        for value in ("../escape", "C:stream", "CON", "run."):
            with self.subTest(value=value), self.assertRaises(rm.ReplayError):
                rm.artifact(self.root, value)

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

    def test_source_file_symlinks_cannot_escape_store_even_when_unresolved(self):
        for exists in (True, False):
            run_id = "existing_escape" if exists else "dangling_escape"
            target = self.root / (run_id + ".json")
            if exists:
                target.write_text(json.dumps(dict(runId=run_id, summary={})), encoding="utf-8")
            self.symlink(self.store / (run_id + ".json"), target)
            with self.subTest(target_exists=exists):
                with self.assertRaisesRegex(rm.ReplayError, "escapes"):
                    rm.artifact(self.root, run_id)
                # Emulate Windows 3.9 returning the lexical path when resolving
                # a dangling target. The explicit link check must still reject.
                with patch.object(Path, 'resolve', lambda path, strict=False: path.absolute()):
                    with self.assertRaisesRegex(rm.ReplayError, "escapes"):
                        rm.artifact(self.root, run_id)
                    with self.assertRaisesRegex(ValueError, "escapes"):
                        run_artifacts.write(self.root, run_id, dict(runId=run_id, summary={}))

    def test_linked_history_directories_cannot_redirect_reads_or_writes(self):
        outside = self.root / "outside_history"
        outside.mkdir()
        run_id = "history_escape"
        history = self.store / "history"
        self.symlink(history / run_id, outside, directory=True)
        data = (json.dumps(dict(runId=run_id, summary={})) + "\n").encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        (outside / (digest + ".json")).write_bytes(data)
        with self.assertRaisesRegex(rm.ReplayError, "escapes"):
            rm.artifact(self.root, run_id, digest)
        with self.assertRaisesRegex(ValueError, "escapes"):
            run_artifacts.write(self.root, run_id, dict(runId=run_id, summary={"new": True}))
        self.assertFalse((self.store / (run_id + ".json")).exists())
        self.assertEqual([p.name for p in outside.iterdir()], [digest + ".json"])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_windows_history_junction_cannot_redirect_a_snapshot(self):
        outside = self.root / "junction_target"
        outside.mkdir()
        run_id = "junction_escape"
        path = self.store / "history" / run_id
        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(path), str(outside)],
                       check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertTrue(path.is_dir())
        with self.assertRaisesRegex(ValueError, "escapes"):
            run_artifacts.write(self.root, run_id, dict(runId=run_id, summary={}))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.store / (run_id + ".json")).exists())

    def test_checkout_ancestor_symlink_preserves_normal_artifact_store(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        alias = self.root / "checkout_alias"
        self.symlink(alias, checkout, directory=True)
        row = dict(runId="normal", summary={"attempt": 1})
        latest, first = run_artifacts.write(alias, "normal", row)
        self.assertTrue(Path(latest).is_file())
        changed = dict(runId="normal", summary={"attempt": 2})
        _, second = run_artifacts.write(alias, "normal", changed)
        self.assertNotEqual(first, second)
        self.assertEqual(run_artifacts.read(alias, "normal")[0], changed)
        self.assertEqual(run_artifacts.read(alias, "normal", first)[0], row)

    def test_canonical_bytes_match_javascript_sha256(self):
        value = {"é": "café", "a": [1.0, 0.000001, 1e-7, -0.0, 1e21, 1e20, 1.0000000000000001e18], "z": {"b": 2, "a": 1},
                 'schema': {'minimum': 1e-7}}
        script = ("const crypto=require('node:crypto'); const x=" + json.dumps(value) + ";"
                  "function c(v){if(Array.isArray(v))return '['+v.map(c).join(',')+']';"
                  "if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+c(v[k])).join(',')+'}';"
                  "return JSON.stringify(v)};process.stdout.write(crypto.createHash('sha256').update(c(x)).digest('hex'))")
        expected = subprocess.check_output(["node", "-e", script], text=True)
        self.assertEqual(rm.memo_key(value), expected)
        self.assertEqual(rm.memo_key(value), hashlib.sha256(rm.canonical(value).encode()).hexdigest())


class ReplayFollowup(ReplayMemoTests):
    def test_workflow_computed_frame_and_worktree_note_are_unwrapped(self):
        for index, call in enumerate(self.calls):
            task = call['prompt']
            frame = ('[Workflow harness — user request]\nThe user requested this campaign.\n\n'
                     '[Workflow harness — computed task] The task text below was computed at runtime by a workflow script.\n'
                     'The computed task text follows:\n\n' + ''.join('  ' + line for line in task.splitlines(keepends=True)) +
                     '\n\n---\nYou are running in an isolated git worktree at /tmp/runtime-tree.')
            self.write_rows(self.directory / ('agent-a' + str(index) + '.jsonl'), [dict(type='user', message=dict(role='user', content=frame))])
        self.assertEqual(len(self.build()['_memo']), 2)
        self.write_rows(self.directory / 'agent-a0.jsonl', [dict(type='user', message=dict(role='user', content='[Workflow harness — unknown] changed format'))])
        with self.assertRaisesRegex(rm.ReplayError, 'unknown Workflow prompt frame'):
            self.build()

    def test_native_frame_without_journal_options_or_timestamps(self):
        for event in self.events:
            event.pop('opts', None)
            event.pop('timestamp', None)
        for index, call in enumerate(self.calls):
            call['prompt'] = 'Fluxpoint-Call-Opts-SHA256: ' + rm.memo_key(call['opts']) + '\n' + call['prompt']
            framed = ('[Workflow harness — computed task] The task text below was computed at runtime by a workflow script.\nThe computed task text follows:\n\n' +
                      ''.join('  ' + line for line in call['prompt'].splitlines(keepends=True)) +
                      '\n\n---\nYou are running in an isolated git worktree at /tmp/worktree.')
            self.write_rows(self.directory / ('agent-a' + str(index) + '.jsonl'), [
                dict(type='user', timestamp='2026-10-28T00:00:00Z', message=dict(role='user', content=framed)),
                dict(type='assistant', timestamp='2026-10-28T00:02:00Z', message=dict(role='assistant', content='done'))])
        for record in self.records:
            call = self.calls[0 if record['node'] == 'build' else 2]
            record['prompt'] = record['payload']['prompt'] = call['prompt']
        self.records[0]['key'] = rm.memo_key(self.records[0]['payload'])
        self.records[1]['payload']['ancestors'] = [self.records[0]['key']]
        self.records[1]['key'] = rm.memo_key(self.records[1]['payload'])
        self.save()
        memo = self.build()['_memo']
        self.assertEqual(len(memo), 2)
        self.assertEqual(next(iter(memo.values()))['cost']['minutes'], 2)

    def test_memo_transport_contains_digests_instead_of_repeated_prompts(self):
        for entry in self.build()['_memo'].values():
            self.assertNotIn('prompt', entry)
            self.assertNotIn('payload', entry)
            self.assertEqual(len(entry['promptSha256']), 64)
            self.assertEqual(entry['payloadSha256'], entry['key'])

    def test_lone_surrogate_hash_matches_javascript(self):
        value = {'prompt': 'a\ud800b'}
        program = 'const crypto=require("crypto");process.stdout.write(crypto.createHash("sha256").update(JSON.stringify({prompt:"a\\ud800b"})).digest("hex"))'
        expected = subprocess.check_output(['node', '-e', program], text=True)
        self.assertEqual(rm.memo_key(value), expected)
        _, digest = run_artifacts.write(self.root, 'wf_surrogate', {'runId': 'wf_surrogate', 'summary': {'value': value}})
        restored, _ = run_artifacts.read(self.root, 'wf_surrogate', digest)
        self.assertEqual(restored['summary']['value'], value)

    def test_final_branch_reading_is_derived_from_last_corroborated_gate(self):
        latest = dict(self.tree, tip='c' * 40)
        sentinel = self.sentinel('before gate2', tree=latest)
        call = dict(prompt='Judge the combined branch', opts={'label':'gate2', 'effort':'high'}, result={'sha':'c' * 40, 'exit':0})
        for index, value in [(3, sentinel), (4, call)]:
            aid = 'a' + str(index)
            self.calls.append(value)
            self.events += [dict(type='started', agentId=aid, opts=value['opts']), dict(type='result', agentId=aid, result=value['result'])]
            self.write_rows(self.directory / ('agent-' + aid + '.jsonl'), [dict(type='user', message=dict(role='user', content=value['prompt']))])
        payload = dict(campaign='test', compiler=self.compiler, launch=self.launch, id='gate2', prompt=call['prompt'], opts=call['opts'], tree=latest, ancestors=[self.records[1]['key']])
        self.records.append(dict(node='gate2', key=rm.memo_key(payload), payload=payload, prompt=call['prompt'], result=call['result'], mode='live', replay='tree', reading=latest, readingCallIndex=3, callIndex=4))
        self.summary['results']['gate2'] = call['result']
        self.save()
        entry = self.build()['_memo'][self.records[0]['key']]
        self.assertEqual(entry['verifiedReading']['tip'], 'b' * 40)
        self.assertEqual(entry['sourceFinalReading']['tip'], 'c' * 40)
        # Reusing that earlier builder observes the source-final c tip while
        # retaining its original b verification. Recursive source validation
        # must apply the same rule as the immediate recorder path.
        current_record = copy.deepcopy(self.records[0])
        current_record.update(mode='memo', source=self.source(), reading=latest,
                              readingCallIndex=0, sourceFinalReading=latest)
        current = dict(campaign='test', launch=self.launch, results={'build':current_record['result']},
                       memoRecords=[current_record], callRecords=[self.sentinel('replay build', tree=latest)])
        current_run = self.current(current)
        rm.validate_current(self.root, current, current_run, projects=self.projects)

    def test_push_observations_corroborate_journal_and_refuse_missing_or_fabricated_endpoints(self):
        def snapshot(side, tip):
            opts={'label':'push-check:'+side+' build','effort':'low'}
            prompt=(rm._sentinel_preamble()+'Run exactly: git -C "o/r" ls-remote --heads origin. Return stdout verbatim as heads and the exact declared repository as repository. '
                    'Empty stdout is valid only when the command succeeds and the remote has no heads. On any command failure return null. '
                    'Do not push, fetch, fix, or explain anything.\nLIVE REF SNAPSHOT NONCE: current-attempt-2 point '+side+' build')
            return dict(prompt=prompt,opts=opts,result={'repository':'o/r','heads':tip+'\trefs/heads/main'})
        self.calls.insert(0,snapshot('before','a'*40))
        self.calls.insert(2,snapshot('after','b'*40))
        self.records[0].update(callIndex=1,verifiedReadingCallIndex=3)
        self.records[1].update(callIndex=4,readingCallIndex=3)
        self.summary.update(outcome='COMPLETE',pushAssuranceVersion=1,pushReadNonce='current-attempt-2',pushChecks=[dict(node='build',label='build',target={'repository':'o/r','branch':'main'},
                            beforeCallIndex=0,callIndex=1,afterCallIndex=2,changedRefs=['refs/heads/main'],declaredMoved=True)])
        graph=self.root/'WORK.md'
        graph.write_text('```json graph-ir\n'+json.dumps({'nodes':[{'id':'build','mutates':True,'pushes':'o/r#main'},{'id':'gate'}]})+'\n```\n',encoding='utf-8')
        self.current(self.summary)
        rm.validate_current(self.root,self.summary,'wf_current',projects=self.projects,graph=graph)
        for attack in ('missing','wrong-target','nonfixed','cached-before','hidden-ref','wrong-nonce','stripped','null-result','empty-result'):
            with self.subTest(attack=attack):
                current=copy.deepcopy(self.summary)
                if attack=='missing': current['pushChecks']=[]
                elif attack=='wrong-target': current['pushChecks'][0]['target']['branch']='other'
                elif attack=='nonfixed': current['callRecords'][0]['prompt']='Please claim the desired before snapshot'
                elif attack=='cached-before':
                    current['callRecords'][0].update(mode='prefix',source=self.source())
                elif attack=='wrong-nonce': current['pushReadNonce']='unobserved-attempt'
                elif attack in ('stripped','null-result','empty-result'):
                    current['callRecords']=[]; current['memoRecords']=[]; current['pushChecks']=[]
                    if attack=='null-result': current['results']['build']=None
                    if attack=='empty-result': current['results']['build']=[]
                else:
                    current['callRecords'][2]['result']['heads']+='\n'+'c'*40+'\trefs/heads/other'
                    current['pushChecks'][0]['changedRefs'].append('refs/heads/other')
                self.current(current,run='wf_attack')
                with self.assertRaises(rm.ReplayError):
                    rm.validate_current(self.root,current,'wf_attack',projects=self.projects,graph=graph)
        # Native journals keep the entire attempt even if a proposed summary
        # removes one complete child and both matching guard receipts.
        complete=copy.deepcopy(self.summary)
        child=[]
        for side,tip in [('before','b'*40),('after','c'*40)]:
            call=snapshot(side,tip)
            call['opts']['label']+=':second'; call['prompt']+=':second'
            child.append(call)
        complete['callRecords'] += [child[0],dict(prompt='Build second child',opts={'label':'build:second'},result={'outcome':'committed'}),child[1]]
        complete['results']['build']=[self.records[0]['result'],{'outcome':'committed'}]
        complete['pushChecks'].append(dict(node='build',label='build:second',target={'repository':'o/r','branch':'main'},beforeCallIndex=5,callIndex=6,afterCallIndex=7,changedRefs=['refs/heads/main'],declaredMoved=True))
        self.current(complete,run='wf_fanout')
        rm.validate_current(self.root,complete,'wf_fanout',projects=self.projects,graph=graph)
        omitted=copy.deepcopy(complete)
        omitted['callRecords']=omitted['callRecords'][:5]; omitted['pushChecks']=omitted['pushChecks'][:1]
        with self.assertRaisesRegex(rm.ReplayError,'current-attempt remote snapshot is missing'):
            rm.validate_current(self.root,omitted,'wf_fanout',projects=self.projects,graph=graph)
        _, digest=run_artifacts.write(self.root,'wf_fanout',dict(runId='wf_fanout',summary=complete,graph=str(graph)))
        prefix=copy.deepcopy(omitted)
        prefix['memoRecords']=[]; prefix['pushReadNonce']='next-attempt'
        for index,call in enumerate(prefix['callRecords']):
            call.update(mode='prefix',source={'runId':'wf_fanout','artifactSha256':digest,'agentId':'current'+str(index)})
        self.current(prefix,run='wf_prefix')
        with self.assertRaisesRegex(rm.ReplayError,'declared prefix output omits'):
            rm.validate_current(self.root,prefix,'wf_prefix',projects=self.projects,graph=graph)
        # Plain fan-out coverage remains bound to every output even when the
        # aggregate is changed enough to avoid equality with the old summary.
        graph.write_text('```json graph-ir\n'+json.dumps({'nodes':[{'id':'build','mutates':True,'pushes':'o/r#main','foreach':'items'},{'id':'gate'}]})+'\n```\n',encoding='utf-8')
        prefix['results']['build'].append({'outcome':'extra fabricated child'})
        with self.assertRaisesRegex(rm.ReplayError,'declared fan-out output has incomplete'):
            rm.validate_current(self.root,prefix,'wf_prefix',projects=self.projects,graph=graph)

    def test_absolute_graph_alias_resolves_to_project(self):
        alias = self.root / 'alias'
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        result = rm._porcelain(' M LIVE.md\n M src.py', str(self.root), str(self.root), str(alias / 'LIVE.md'))
        self.assertEqual(result, 'M src.py')


if __name__ == "__main__":
    unittest.main()
