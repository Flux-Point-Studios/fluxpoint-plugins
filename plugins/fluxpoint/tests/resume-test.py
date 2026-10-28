#!/usr/bin/env python3
"""Structured resume, exact argument carry and evidence pin regressions."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import embedded_args as ea
import evidence_pins as ep
import resume


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="resume test ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))
        return path

    def archive(self, embedded, effective=None):
        digest = ea.args_hash(embedded)
        source = ('// immutable executed source\nconst ARGS = ' + ea.canonical(embedded) +
                  ';\nconst ARGS_SHA256 = "' + digest + '";\n').encode("utf-8")
        path = self.file(ea.ARCHIVES + "/" + digest + "-" + hashlib.sha256(source).hexdigest() + ".graph.js", source)
        return {"argsSha256": digest, "inputs": copy.deepcopy(effective if effective is not None else embedded)}, path


class Pins(Fixture):
    def pin(self, name="evidence.json", content=b'{"proof":1}\n'):
        path = self.file(name, content)
        digest = hashlib.sha256(content).hexdigest()
        return path, digest, str(path) + "#sha256=" + digest

    def root_alias(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
            short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
            short_path.restype = wintypes.DWORD
            size = short_path(str(self.root), None, 0)
            if not size:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_unicode_buffer(size)
            result = short_path(str(self.root), buffer, size)
            if not result or result >= size:
                raise ctypes.WinError(ctypes.get_last_error())
            # Volumes without generated 8.3 names still exercise the native
            # case alias. The recorder resolves both to the existing parent.
            return Path(buffer.value if buffer.value != str(self.root) else str(self.root).swapcase())
        alias = self.root / "directory alias"
        alias.symlink_to(self.root, target_is_directory=True)
        return alias

    def test_store_aliases_resolve_before_present_and_missing_leaf_checks(self):
        _, digest, ref = self.pin()
        ep.prepare_pinned_inputs(self.root, {"note": '"' + ref + '"'})
        alias = self.root_alias()
        stored = self.root / ep.STORE / (digest + ".json")
        alias_ref = str(alias / ep.STORE / stored.name) + "#sha256=" + digest
        canonical_root = os.path.realpath(self.root)
        records = ep.validate_pinned_inputs(canonical_root, {"note": '"' + alias_ref + '"'})
        self.assertEqual(len(records), 1)
        self.assertTrue(os.path.samefile(records[0]["path"], stored))
        stored.unlink()
        with self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-MISSING"):
            ep.validate_pinned_inputs(canonical_root, {"note": '"' + alias_ref + '"'})

    def test_preparing_from_root_alias_writes_canonical_store_references(self):
        _, _, ref = self.pin()
        prepared = ep.prepare_pinned_inputs(self.root_alias(), {"note": '"' + ref + '"'})
        self.assertIn(os.path.realpath(self.root / ep.STORE), prepared["note"])
        records = ep.validate_pinned_inputs(os.path.realpath(self.root), prepared)
        self.assertEqual(os.path.normcase(os.path.dirname(records[0]["path"])),
                         os.path.normcase(os.path.realpath(self.root / ep.STORE)))

    def test_store_leaf_symlink_cannot_escape_content_store(self):
        path, digest, _ = self.pin()
        stored = self.root / ep.STORE / (digest + ".json")
        stored.parent.mkdir(parents=True)
        try:
            stored.symlink_to(path)
        except OSError as exc:
            if os.name != "nt":
                raise
            self.skipTest("Windows symlink privilege unavailable: " + str(exc))
        with self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-NOT-STORED"):
            ep.validate_pinned_inputs(self.root, {"note": '"' + str(stored) + "#sha256=" + digest + '"'})

    def test_copies_rewrites_and_rehashes_nested_notes(self):
        path, digest, ref = self.pin()
        args = {"repair": {"notes": ["Read \"" + ref + "\" and preserve café\r\n"]}}
        prepared = ep.prepare_pinned_inputs(self.root, args)
        records = ep.validate_pinned_inputs(self.root, prepared)
        self.assertEqual(len(records), 1)
        stored = Path(records[0]["path"])
        self.assertEqual(stored.name, digest + ".json")
        self.assertEqual(stored.read_bytes(), path.read_bytes())
        self.assertIn("café\r\n", prepared["repair"]["notes"][0])
        self.assertEqual(ep.prepare_pinned_inputs(self.root, prepared), prepared)
        stored.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-MISMATCH"):
            ep.validate_pinned_inputs(self.root, prepared)

    def test_missing_bad_hash_and_corrupt_existing_store_refused(self):
        path, digest, ref = self.pin()
        with self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-MISMATCH"):
            ep.prepare_pinned_inputs(self.root, {"note": '"' + str(path) + "#sha256=" + "0" * 64 + '"'})
        ep.prepare_pinned_inputs(self.root, {"note": '"' + ref + '"'})
        stored = self.root / ep.STORE / (digest + ".json")
        stored.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-MISMATCH"):
            ep.prepare_pinned_inputs(self.root, {"note": '"' + ref + '"'})
        path.unlink()
        with self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-MISSING"):
            ep.prepare_pinned_inputs(self.root, {"note": '"' + ref + '"'})

    def test_relative_pins_and_markdown_are_supported(self):
        path, digest, _ = self.pin("proof.json")
        args = ep.prepare_pinned_inputs(self.root, {"note": "Read (proof.json#sha256=" + digest + ") now."})
        self.assertIn(") now.", args["note"])
        self.assertEqual(len(ep.validate_pinned_inputs(self.root, args)), 1)
        sentence = ep.prepare_pinned_inputs(self.root, {"note": "Read proof.json#sha256=" + digest + "."})
        self.assertTrue(sentence["note"].endswith('".') if " " in str(self.root) else sentence["note"].endswith("."))

    def test_every_unpinned_absolute_path_requires_named_exception(self):
        inputs = {"note": 'Read /tmp/a.json, "C:\\proof files\\b.txt" and https://example.org/path. Also `\\\\host\\share\\c.txt`.'}
        expected = ["/tmp/a.json", "C:\\proof files\\b.txt", "\\\\host\\share\\c.txt"]
        self.assertEqual(ep.unpinned_paths(inputs), expected)
        with self.assertRaisesRegex(ValueError, "EVIDENCE-UNPINNED"):
            ep.prepare_pinned_inputs(self.root, inputs, expected[:-1])
        self.assertEqual(ep.prepare_pinned_inputs(self.root, inputs, expected), inputs)

    def test_malformed_pins_and_unstored_recorded_pins_refused(self):
        for ref in ("proof.json#sha256=abc", "proof.json#sha256=", "proof.json#sha256=" + "0" * 65):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-INVALID"):
                ep.prepare_pinned_inputs(self.root, {"note": ref})
        path, digest, ref = self.pin()
        with self.assertRaisesRegex(ValueError, "EVIDENCE-PIN-NOT-STORED"):
            ep.validate_pinned_inputs(self.root, {"note": '"' + ref + '"'})

    def test_urls_and_plugin_commands_are_context_while_local_paths_need_pins(self):
        path, digest, ref = self.pin()
        note = {"repair": 'Run /fluxpoint:red-team then /substrate:fix-checker. Read https://example.org/a/b and "' + ref + '".'}
        stored = ep.prepare_pinned_inputs(self.root, note)
        self.assertEqual(len(ep.validate_pinned_inputs(self.root, stored)), 1)
        self.assertIn("/fluxpoint:red-team", stored["repair"])
        with self.assertRaisesRegex(ValueError, "EVIDENCE-UNPINNED.*tmp/other"):
            ep.prepare_pinned_inputs(self.root, {"repair": note["repair"] + " Read /tmp/other."})

    def test_reference_bearing_dictionary_keys_are_refused(self):
        for key in ("/tmp/missing#sha256=" + "0" * 64, "/tmp/unpinned", "C:\\proof\\file.json"):
            args = {"repair": [{key: "read evidence"}]}
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "EVIDENCE-KEY-REFERENCE"):
                    ep.prepare_pinned_inputs(self.root, args)
                with self.assertRaisesRegex(ValueError, "EVIDENCE-KEY-REFERENCE"):
                    ep.validate_pinned_inputs(self.root, args)

    def test_record_refusal_keeps_irreversible_ledger_without_run_artifact(self):
        path, digest, ref = self.pin()
        prepared = ep.prepare_pinned_inputs(self.root, {"note": '"' + ref + '"'})
        stored = self.root / ep.STORE / (digest + ".json")
        stored.unlink()
        graph = self.file("WORK.md", "# Work\n")
        result = self.file("result.json", json.dumps({"inputs": prepared, "campaign": "test",
                    "ledger": [{"key": "effect", "node": "broadcast", "result": {"txHash": "abc"}}]}))
        proc = subprocess.run([sys.executable, str(SCRIPTS / "record-run.py"), "--root", str(self.root),
                    "--graph", str(graph), "--result", str(result), "--run-id", "wf_test"],
                    capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("EVIDENCE-PIN-MISSING", proc.stderr)
        self.assertIn('"effect"', (self.root / ".claude/fluxpoint/irreversible.jsonl").read_text())
        self.assertFalse((self.root / ".claude/fluxpoint/runs/wf_test.json").exists())


class ArgsArchive(Fixture):
    def test_recovers_full_text_and_effective_defaults(self):
        inputs = {"repair": "café\r\nold note\n", "zero": 0, "bool": False, "defaultNote": "none"}
        summary, _ = self.archive(inputs)
        self.assertEqual(ea.recover(self.root, summary), inputs)

    def test_unbound_recorded_inputs_are_refused_instead_of_carried(self):
        summary, _ = self.archive({"repair": "old"})
        summary["inputs"]["newRepair"] = "injected without --set"
        with self.assertRaisesRegex(ValueError, "RESUME-INPUT-UNBOUND.*newRepair"):
            ea.recover(self.root, summary)

    def test_summary_source_disagreement_is_drift(self):
        summary, _ = self.archive({"repair": "old"})
        summary["inputs"]["repair"] = "new"
        with self.assertRaisesRegex(ValueError, "RESUME-INPUT-DRIFT.*repair"):
            ea.recover(self.root, summary)

    def test_json_numeric_and_boolean_types_are_compared_exactly(self):
        summary, _ = self.archive({"enabled": True})
        summary["inputs"]["enabled"] = 1
        with self.assertRaisesRegex(ValueError, "RESUME-INPUT-DRIFT"):
            ea.recover(self.root, summary)

    def test_mutable_or_missing_archives_and_missing_digests_refused(self):
        summary, source = self.archive({"repair": "old"})
        source.write_text(source.read_text() + "// edited\n")
        with self.assertRaisesRegex(ValueError, "RESUME-SOURCE-HASH"):
            ea.recover(self.root, summary)
        source.unlink()
        with self.assertRaisesRegex(ValueError, "RESUME-SOURCE-MISSING"):
            ea.recover(self.root, summary)
        with self.assertRaisesRegex(ValueError, "RESUME-ARGS-MISSING"):
            ea.recover(self.root, {"inputs": {"repair": "old"}})

    def test_changed_graphs_can_share_identical_argument_bytes(self):
        summary, source = self.archive({"repair": "old"})
        other = source.read_bytes() + b"// different IR, identical notes\n"
        self.file(ea.ARCHIVES + "/" + summary["argsSha256"] + "-" + hashlib.sha256(other).hexdigest() + ".graph.js", other)
        self.assertEqual(ea.recover(self.root, summary), {"repair": "old"})

    def test_script_constant_tampering_is_refused_even_with_new_source_hash(self):
        summary, source = self.archive({"repair": "old"})
        broken = source.read_text().replace('"old"', '"new"')
        with self.assertRaisesRegex(ValueError, "RESUME-ARGS-HASH"):
            ea.parse_script(broken)
        with self.assertRaisesRegex(ValueError, "RESUME-ARGS-MISSING"):
            ea.parse_script(source.read_text() + source.read_text())


class Assembly(Fixture):
    def setUp(self):
        super().setUp()
        for args in (("init", "-q"), ("config", "user.name", "fixture"),
                     ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.file(".gitignore", ".claude/\n")
        self.ir = {"version": 1, "name": "test", "campaign": "test", "nodes": []}
        self.file("WORK.md", "```json graph-ir\n" + json.dumps(self.ir) + "\n```\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)
        self.summary, _ = self.archive({"repairA": "old A\r\n", "repairB": "old B\n", "defaultNote": "none"})
        self.summary["launch"] = {"since": "2026-10-18T00:00:00Z", "nonce": "original", "root": str(self.root)}
        self.save()
        self.args = argparse.Namespace(root=str(self.root), graph="WORK.md", run="wf_previous", args=None,
                    set=[], set_json=[], unpinned_ok=[], out=None, projects="runtime-projects")
        self.compiled_inputs = None
        real_module = resume._module
        self.compiler = real_module("compile-graph")
        self.memo_builder = mock.Mock(return_value={"_memo": {"checked": "receipt"}, "_prefix": []})
        self.addCleanup(mock.patch.stopall)
        def modules(name):
            if name == "replay_memo":
                return types.SimpleNamespace(build_memo=self.memo_builder)
            return real_module(name)
        mock.patch.object(resume, "_module", side_effect=modules).start()
        real_run = resume._run
        def commands(command, root):
            if str(command[0]) == sys.executable and str(command[1]).endswith("compile-graph.py"):
                self.compiled_inputs = json.loads(Path(command[command.index("--args") + 1]).read_text())
                return "rerun plan: live repair\n" if "--plan" in command else "compiled fixture\n"
            return real_run(command, root)
        mock.patch.object(resume, "_run", side_effect=commands).start()

    def save(self):
        self.file(".claude/fluxpoint/runs/wf_previous.json", json.dumps({"runId": "wf_previous", "summary": self.summary}))

    def test_only_named_note_changes_and_launch_identity_persists(self):
        note = self.file("new.txt", b"new A\r\nexact trailing\n")
        self.args.set = ["repairA=@" + str(note)]
        first = resume.assemble(self.args)
        second = resume.assemble(self.args)
        self.assertEqual(self.compiled_inputs, {"repairA": "new A\r\nexact trailing\n", "repairB": "old B\n", "defaultNote": "none"})
        self.assertEqual(first["resumeFromRunId"], "wf_previous")
        self.assertEqual(first["args"]["_resumeFromRunId"], "wf_previous")
        artifact = self.root / ".claude/fluxpoint/runs/wf_previous.json"
        source_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.assertEqual(first["args"]["_resumeArtifactSha256"], source_sha)
        self.assertEqual(self.memo_builder.call_args.kwargs["artifact_sha256"], source_sha)
        self.assertEqual(first["args"]["_launch"], self.summary["launch"])
        self.assertEqual(first["args"]["_base"]["sha"], subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root).decode().strip())
        self.assertNotEqual(first["args"]["_replayLaunchNonce"], second["args"]["_replayLaunchNonce"])
        self.assertEqual(len(first["args"]["_replayLaunchNonce"]), 64)
        self.assertEqual(first["args"]["_memo"], {"checked": "receipt"})
        self.assertNotIn("_priorResults", first["args"])
        self.assertFalse(any(not key.startswith("_") for key in first["args"]))

    def test_unlisted_carried_drift_is_refused(self):
        with mock.patch.object(resume, "prepare_pinned_inputs", side_effect=lambda root, args, allowances: dict(args, repairB="accident")):
            with self.assertRaisesRegex(ValueError, "RESUME-CARRIED-DRIFT.*repairB"):
                resume.assemble(self.args)

    def test_same_workflow_run_id_pins_each_source_attempt_separately(self):
        first = resume.assemble(self.args)
        first_sha = first["args"]["_resumeArtifactSha256"]
        updated = dict(self.summary["inputs"], repairA="second recorded attempt\n")
        newer, _ = self.archive(updated)
        newer["launch"] = self.summary["launch"]
        self.summary = newer
        self.save()
        second = resume.assemble(self.args)
        self.assertEqual(first["resumeFromRunId"], second["resumeFromRunId"])
        self.assertNotEqual(first_sha, second["args"]["_resumeArtifactSha256"])
        self.assertEqual(first["args"]["_resumeArtifactSha256"], first_sha)
        self.assertEqual(self.compiled_inputs["repairA"], "second recorded attempt\n")
        self.assertEqual(self.memo_builder.call_args.kwargs["artifact_sha256"], second["args"]["_resumeArtifactSha256"])

    def test_source_drift_fails_before_any_plan_or_compile(self):
        self.summary["inputs"]["repairB"] = "accident"
        self.save()
        with self.assertRaisesRegex(ValueError, "RESUME-INPUT-DRIFT.*repairB"):
            resume.assemble(self.args)
        self.assertIsNone(self.compiled_inputs)

    def test_user_cannot_supply_script_only_fields_or_duplicate_updates(self):
        path = self.file("memo.txt", "anything")
        for updates in (("_memo=@" + str(path),), ("repairA=@" + str(path),) * 2):
            self.args.set = list(updates)
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "RESUME-SET"):
                resume.assemble(self.args)

    def test_missing_stamp_for_required_launch_and_missing_artifact_refused(self):
        self.summary.pop("launch")
        self.save()
        with mock.patch.object(self.compiler, "launch_requirements", return_value={"args": [{"name": "_launch", "required": True}]}):
            # Reuse patched instance so this exercises the required-launch path.
            with mock.patch.object(resume, "_module", return_value=self.compiler):
                with self.assertRaisesRegex(ValueError, "RESUME-LAUNCH-MISSING"):
                    resume.assemble(self.args)
        (self.root / ".claude/fluxpoint/runs/wf_previous.json").unlink()
        with self.assertRaises(OSError):
            resume.assemble(self.args)

    def test_safe_run_ids_and_structured_rejudge(self):
        for value in ("../outside", "CON", "LPT1.json", "bad:stream", "trailing."):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "RESUME-RUN-ID"):
                resume._safe_run(value)
        path = self.file("rejudge.json", '{"gate":{"nonce":"retry-2","reason":"operator request"}}')
        self.summary["results"] = {"gate": {"verdict": "BLOCK"}, "unrelated": {"huge": "x" * 40000}}
        self.save()
        self.args.set_json = ["rejudge=@" + str(path)]
        workflow = resume.assemble(self.args)
        self.assertEqual(self.compiled_inputs["rejudge"]["gate"]["nonce"], "retry-2")
        self.assertEqual(workflow["args"]["_priorResults"], {"gate": {"verdict": "BLOCK"}})


class CompilerIntegration(Fixture):
    """Real compiler/launcher/recorder subprocesses and generated JavaScript."""
    def setUp(self):
        super().setUp()
        for args in (("init", "-q"), ("config", "user.name", "fixture"),
                     ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.file(".gitignore", ".claude/\n")
        self.ir = {"version": 1, "name": "embedded", "campaign": "embedded args integration",
                   "treeGuard": False, "argDefaults": {"note": "none", "unchanged": "default"},
                   "budget": {"maxNodes": 2, "cacheTtl": "1h"},
                   "nodes": [{"id": "review", "contract": "FindingsV1", "verify": "schema-only",
                              "prompt": "Read {{A.note}} and {{A.unchanged}} and {{A.zero}}."}]}
        self.graph = self.file("WORK.md", "# Campaign\n\n```json graph-ir\n" + json.dumps(self.ir) +
                   "\n```\n\n## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)

    def command(self, script, *args, want=0):
        proc = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, want, proc.stdout + "\n" + proc.stderr)
        return proc

    def initial(self):
        args = self.file("initial.json", json.dumps({"note": "café\r\ncarried text\n", "zero": 0}))
        return json.loads(self.command("resume.py", "--root", self.root, "--graph", "WORK.md", "--args", args).stdout)

    def execute(self, workflow, extra=None):
        if not shutil.which("node"):
            self.skipTest("Node is required to execute the generated Workflow fixture")
        source = Path(workflow["scriptPath"]).read_text(encoding="utf-8").replace("export const meta", "const meta")
        incoming = dict(workflow["args"], **(extra or {}))
        prefix = "const args=" + json.dumps(incoming) + ";\n" + r'''
const log=()=>{},phase=()=>{},budget={total:null,remaining:()=>1e9,spent:()=>0};
const parallel=async(xs)=>Promise.all(xs.map(f=>f()));
const agent=async(prompt,opts)=>({findings:[]});
'''
        script = self.file("execute.mjs", prefix + "\n(async()=>{\n" + source +
                           "\n})().then(x=>console.log(JSON.stringify({summary:x})))" +
                           ".catch(e=>console.log(JSON.stringify({error:String(e)})));\n")
        proc = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_initial_launch_record_and_resume_use_real_compiler_plan(self):
        first = self.initial()
        result = self.execute(first)
        self.assertNotIn("error", result)
        summary = result["summary"]
        self.assertEqual(summary["inputs"], {"note": "café\r\ncarried text\n", "zero": 0, "unchanged": "default"})
        self.assertEqual(summary["argsSha256"], ea.args_hash({"note": "café\r\ncarried text\n", "zero": 0, "unchanged": "default"}))
        outcome = self.file("result.json", json.dumps(summary))
        self.command("record-run.py", "--root", self.root, "--graph", self.graph,
                     "--run-id", "wf_integration", "--result", outcome)
        note = self.file("changed.txt", b"reviewed repair\r\n")
        resumed = self.command("resume.py", "--root", self.root, "--graph", self.graph,
                      "--run", "wf_integration", "--set", "note=@" + str(note))
        second = json.loads(resumed.stdout)
        self.assertIn("review", resumed.stderr)
        self.assertEqual(second["resumeFromRunId"], "wf_integration")
        inputs = self.execute(second)["summary"]["inputs"]
        self.assertEqual(inputs, {"note": "reviewed repair\r\n", "zero": 0, "unchanged": "default"})

    def test_inline_override_drop_guard_and_explicit_none(self):
        workflow = self.initial()
        overridden = self.execute(workflow, {"note": "accidental inline value"})
        self.assertIn("cannot be overridden inline: note", overridden["error"])
        refused = self.command("compile-graph.py", self.graph, "--out", workflow["scriptPath"], want=1)
        self.assertIn("embedded args would be dropped", refused.stderr)
        self.command("compile-graph.py", self.graph, "--out", workflow["scriptPath"], "--args", "none")
        self.assertNotIn("const ARGS = ", Path(workflow["scriptPath"]).read_text())

    def test_args_size_cap_pricing_and_reserved_field_refusal(self):
        large = self.file("large.json", json.dumps({"note": "x" * 12000, "zero": False}))
        priced = self.command("compile-graph.py", self.graph, "--args", large, "--check")
        self.assertNotIn("{{A.note}} (priced at default)", priced.stdout)
        default = self.command("compile-graph.py", self.graph, "--check")
        self.assertIn("{{A.note}} (priced at default)", default.stdout)
        cap = self.command("compile-graph.py", self.graph, "--args", large, "--check", "--max-script-bytes", "1000", want=1)
        self.assertIn("script rejected", cap.stderr)
        invalid = self.file("invalid.json", '{"_memo":{"forged":true}}')
        refused = self.command("compile-graph.py", self.graph, "--args", invalid, "--check", want=1)
        self.assertIn("only non-underscore keys", refused.stderr)


if __name__ == "__main__":
    unittest.main()
