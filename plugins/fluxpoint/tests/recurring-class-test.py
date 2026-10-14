#!/usr/bin/env python3
"""Recurring variants stop a compiled sweep before it buys another patch."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cg", PLUGIN / "scripts/compile-graph.py")
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(PLUGIN / "contracts"))
IR = {"version": 1, "campaign": "reframe recurring defects", "treeGuard": False,
      "budget": {"maxNodes": 100, "nodeFloorTokens": 0, "verifyFloorTokens": 0},
      "nodes": [{"id": "sweep", "prompt": "Find fresh variants, prior keys: {{seen}}",
                 "contract": "FindingsV1", "verify": "skeptic:1", "verifyOver": "findings",
                 "repeat": {"maxRounds": 6, "untilDryRounds": 2, "dedupeBy": ["file", "line"],
                            "classBy": ["defectClass"]}, "onRed": "halt"}]}


def execute(sequence, killed=False, on_red="halt"):
    ir = copy.deepcopy(IR)
    ir["nodes"][0]["onRed"] = on_red
    assert not cg.validate(ir, CONTRACTS), cg.validate(ir, CONTRACTS)
    script = cg.emit(ir, CONTRACTS).replace("export const meta", "const meta")
    wrapper = ("const args={},log=()=>{},phase=()=>{},workflow={};"
               "const budget={spent:()=>0,remaining:()=>1e9};"
               "const parallel=async ts=>Promise.all(ts.map(t=>t()));"
               "const pipeline=async()=>[];let rounds=0;"
               f"const sequence={json.dumps(sequence)};"
               "const agent=async(p,o)=>{if(o.schema?.properties?.refuted) return "
               f"{{refuted:{json.dumps(killed)},reason:'independent reproduction'}};"
               "const cls=sequence[rounds++];return {findings:cls===undefined||cls===false?[]:"
               "[{file:'route-'+rounds+'.py',line:rounds,claim:'outsider input crosses a missing bound',"
               "failure_path:'route accepts unbounded stranger rows',severity:'MEDIUM',"
               "...(cls===null?{}:{defectClass:cls})}]}};"
               "(async()=>{" + script + "})().then(r=>console.log(JSON.stringify({r,rounds})))")
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "run.mjs"
        path.write_text(wrapper)
        result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)


class RecurringTests(unittest.TestCase):
    def test_second_consecutive_surviving_variant_halts(self):
        result = execute(["outsider-decode"] * 6)
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["r"]["outcome"], "RECURRING-CLASS")
        entry = result["r"]["recurringClasses"][0]
        self.assertEqual(entry["class"], "outsider-decode")
        self.assertEqual(entry["rounds"], [1, 2])
        self.assertEqual(len(entry["keys"]), 2)

    def test_gap_and_different_classes_do_not_accumulate(self):
        self.assertEqual(execute(["a", "b", "a", False, False])["r"]["outcome"], "COMPLETE")

    def test_refuted_variants_do_not_count(self):
        result = execute(["a"] * 6, killed=True)
        self.assertEqual(result["rounds"], 6)
        self.assertEqual(result["r"]["recurringClasses"], [])

    def test_drop_mode_records_incomplete_and_stops(self):
        result = execute(["a"] * 6, on_red="drop+log")
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["r"]["outcome"], "INCOMPLETE")

    def test_omitted_class_cannot_silently_converge(self):
        self.assertEqual(execute([None])["r"]["outcome"], "MISSING-DEFECT-CLASS")

    def test_bad_declarations_are_rejected(self):
        for change in ({"classBy": ["absent"]}, {"classBy": []}, {"reframeAfter": True}, {"reframeAfter": 1}):
            ir = copy.deepcopy(IR)
            ir["nodes"][0]["repeat"].update(change)
            self.assertTrue(cg.validate(ir, CONTRACTS), change)
        ir = copy.deepcopy(IR)
        ir["nodes"][0]["memory"] = {"emit": "audit", "classBy": ["file"]}
        self.assertTrue(any("agree" in f for f in cg.validate(ir, CONTRACTS)))


if __name__ == "__main__":
    unittest.main()
