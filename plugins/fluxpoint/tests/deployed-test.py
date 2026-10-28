#!/usr/bin/env python3
"""A suite may certify only the dependency resolution it actually executes."""
import json
import os
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import attest


class DeployedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Match `python -m venv` on POSIX. Copying a relocatable interpreter
        # can lose its standard-library prefix; a symlink preserves it.
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(self.root / "venv")
        self.python = self.root / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")
        site = Path(subprocess.check_output([str(self.python), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], text=True).strip())
        dist = site / "demo_package-0.13.1.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-package\nVersion: 0.13.1\n")
        self.pins = self.root / "requirements.txt"
        self.pins.write_text("# production\ndemo-package==0.13.1\n")
        script = self.root / "suite.py"
        script.write_text("from pathlib import Path\nPath('ran').write_text('yes')\n")
        self.doc = {"version": 1, "gates": {"api-deployed": f'"{self.python.as_posix()}" suite.py'},
                    "deployed": {"api": {"pins": "requirements.txt", "python": str(self.python), "gate": "api-deployed"}}}
        self.save()
        # A cited local verdict certifies committed, clean content. The venv
        # is measured by deployed_resolution; the suite's output is an artifact.
        (self.root / ".gitignore").write_text("venv/\nran\n")
        self.git("init", "-q")
        self.git("config", "user.name", "deployed fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.commit("declared deployed gate")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, message):
        self.git("add", ".gitignore", attest.GATES, "requirements.txt", "suite.py")
        self.git("commit", "-qm", message)

    def save(self):
        (self.root / attest.GATES).write_text(json.dumps(self.doc))

    def execute(self, token):
        return attest._execute(str(self.root), "api-deployed", attest.now(), token, "n")

    def test_matching_resolution_runs_and_records(self):
        self.assertEqual(attest.load_gates(str(self.root))[1], [])
        self.assertEqual(self.execute("matching"), 0)
        self.assertTrue((self.root / "ran").exists())
        row = attest.read(str(self.root))[-1]
        self.assertFalse(row["dirty"])
        self.assertEqual(row["treeSha"], self.git("rev-parse", "HEAD"))
        self.assertTrue(row["resolutionChecked"])
        self.assertEqual(row["dependencies"]["api"], {"demo-package": "0.13.1"})
        self.assertIn("api", row["pinsSha256"])

    def test_mismatch_mints_red_without_starting_suite(self):
        self.pins.write_text("demo-package==0.19.2\nmissing==1.0\n")
        self.commit("deploy different exact pins")
        self.assertEqual(self.execute("mismatch"), 1)
        self.assertFalse((self.root / "ran").exists())
        row = attest.read(str(self.root))[-1]
        self.assertEqual(row["exit"], 1)
        log = (self.root / attest.BACKGROUND / "mismatch.log").read_text()
        self.assertIn("demo-package 0.13.1 != 0.19.2", log)
        self.assertIn("missing not installed != 1.0", log)
        summary = {'launch': {'since': row['started'], 'nonce': 'n'},
                   'prove': {'check': 'api-deployed'}, 'contracts': {'check': 'HarnessCheckV1'},
                   'results': {'check': {'exit': 1, 'attestId': row['attestId']}}}
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]['status'], 'ATTESTED')

    def test_uncommitted_resolution_mismatch_remains_red_but_cannot_attest_a_commit(self):
        self.pins.write_text("demo-package==0.19.2\n")
        self.assertEqual(self.execute("dirty-mismatch"), 1)
        self.assertFalse((self.root / "ran").exists())
        row = attest.read(str(self.root))[-1]
        self.assertEqual(row["exit"], 1)
        self.assertTrue(row["dirty"])
        summary = {"launch": {"since": row["started"], "nonce": "n"},
                   "prove": {"check": "api-deployed"}, "contracts": {"check": "HarnessCheckV1"},
                   "results": {"check": {"exit": 1, "attestId": row["attestId"]}}}
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]["status"], "STALE")

    def test_unknown_tree_runs_but_cannot_attest_committed_content(self):
        # Hide the repository without relying on platform-specific removal of
        # open files. Gate execution still produces a real, retained verdict.
        (self.root / ".git").rename(self.root / "hidden-git")
        self.assertEqual(self.execute("unknown-tree"), 0)
        row = attest.read(str(self.root))[-1]
        self.assertTrue(row["dirty"])
        self.assertEqual(row["treeSha"], "")
        summary = {"launch": {"since": row["started"], "nonce": "n"},
                   "prove": {"check": "api-deployed"}, "contracts": {"check": "HarnessCheckV1"},
                   "results": {"check": {"exit": 0, "attestId": row["attestId"]}}}
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]["status"], "STALE")

    def test_nonexact_or_empty_pins_refuse(self):
        for text in ("demo-package>=0.13\n", "# nothing\n", "-r other.txt\n"):
            self.pins.write_text(text)
            self.assertEqual(self.execute("badpins"), 1)
        self.assertFalse((self.root / "ran").exists())

    def test_missing_lane_and_wrong_interpreter_refuse(self):
        for key, value in (("pins", "missing.txt"), ("python", "missing/python"), ("gate", "missing")):
            lane = self.doc["deployed"]["api"]
            old, lane[key] = lane[key], value
            self.save()
            self.assertTrue(attest.load_gates(str(self.root))[1])
            lane[key] = old
        self.doc["gates"]["api-deployed"] = "python suite.py"
        self.save()
        self.assertTrue(any("declared python" in s for s in attest.load_gates(str(self.root))[1]))

    def test_compound_shell_commands_cannot_borrow_checked_interpreter(self):
        for suffix in (f' -c "pass" && "{sys.executable}" suite.py',
                       ' suite.py || true', ' suite.py | cat', ' suite.py > ignored.log',
                       ' -c "pass"; echo green'):
            with self.subTest(suffix=suffix):
                self.doc['gates']['api-deployed'] = f'"{self.python.as_posix()}"' + suffix
                self.save()
                self.assertTrue(attest.load_gates(str(self.root))[1])
                self.assertEqual(attest.run_gate(str(self.root), 'api-deployed'), 2)
        self.assertFalse((self.root / 'ran').exists())

    def test_probe_uses_the_suites_python_startup_flags(self):
        self.doc['gates']['api-deployed'] = f'"{self.python.as_posix()}" -S suite.py'
        self.save()
        self.assertEqual(self.execute('no-site-packages'), 1)
        self.assertFalse((self.root / 'ran').exists())
        row = attest.read(str(self.root))[-1]
        self.assertFalse(row['resolutionChecked'])
        self.assertIsNone(row['dependencies']['api']['demo-package'])

    def test_leading_cd_and_literal_code_use_same_interpreter_and_directory(self):
        (self.root / 'api').mkdir()
        self.doc['gates']['api-deployed'] = (f'cd api && "{self.python.as_posix()}" -I -c '
            '"from pathlib import Path; Path(\'ran\').write_text(\'yes\')"')
        self.save()
        self.assertEqual(attest.load_gates(str(self.root))[1], [])
        self.assertEqual(self.execute('cd-suite'), 0)
        self.assertTrue((self.root / 'api/ran').exists())

    def test_changed_command_or_lane_cannot_reuse_resolution_witness(self):
        self.execute('original-lane')
        row = attest.read(str(self.root))[-1]
        summary = {'launch': {'since': row['started'], 'nonce': 'n'},
                   'prove': {'check': 'api-deployed'}, 'contracts': {'check': 'HarnessCheckV1'},
                   'results': {'check': {'exit': 0, 'attestId': row['attestId']}}}
        self.doc['gates']['api-deployed'] = f'"{self.python.as_posix()}" -c "pass"'
        self.save()
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]['status'], 'STALE')
        self.doc['gates']['api-deployed'] = f'"{self.python.as_posix()}" suite.py'
        (self.root / 'other-pins.txt').write_bytes(self.pins.read_bytes())
        self.doc['deployed']['api']['pins'] = 'other-pins.txt'
        self.save()
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]['status'], 'STALE')

    def test_hook_cannot_prove_resolution_and_changed_pins_are_stale(self):
        self.execute("proof")
        row = attest.read(str(self.root))[-1]
        summary = {"launch": {"since": row["started"], "nonce": "n"},
                   "prove": {"check": "api-deployed"}, "contracts": {"check": "HarnessCheckV1"},
                   "results": {"check": {"exit": 0, "attestId": row["attestId"]}}}
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]["status"], "ATTESTED")
        path = Path(attest.path_for(str(self.root)))
        row["witness"] = "hook"
        path.write_text(json.dumps(row) + "\n")
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]["status"], "MISMATCH")
        row["witness"] = "wrapper"
        path.write_text(json.dumps(row) + "\n")
        self.pins.write_text("demo-package==0.13.1 # changed pins identity\n")
        self.assertEqual(attest.verify_claims(str(self.root), summary)[0][0]["status"], "STALE")

    def test_irreversible_effect_needs_every_deployed_guard(self):
        spec = importlib.util.spec_from_file_location("cg", SCRIPTS / "compile-graph.py")
        cg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cg)
        contracts = cg.load_contracts(str(SCRIPTS.parent / "contracts"))
        gates = cg.load_gates(str(self.root))
        ir = {"version": 1, "campaign": "submit tested deployment", "requiredArgs": ["confirm"],
              "budget": {"maxNodes": 8}, "nodes": [
                  {"id": "build", "prompt": "build proposal", "contract": "HarnessCheckV1"},
                  {"id": "pins", "after": "build", "prompt": "run deployed gate against {{prev}}", "contract": "ExecutionV1",
                   "verify": "prove:api-deployed", "independent": True, "verifies": "build", "haltWhen": "exit != 0"},
                  {"id": "submit", "after": "pins", "prompt": "submit using {{prev}}", "contract": "HarnessCheckV1", "irreversible": True}]}
        self.assertFalse(cg.validate(ir, contracts, gates=gates))
        for missing in (True, False):
            bad = copy.deepcopy(ir)
            if missing:
                bad["nodes"].pop(1)
                bad["nodes"][-1]["after"] = "build"
            else:
                bad["nodes"][1]["haltWhen"] = "exit == 0"
            self.assertTrue(any("deployed.api" in f for f in cg.validate(bad, contracts, gates=gates)))


if __name__ == "__main__":
    unittest.main()
