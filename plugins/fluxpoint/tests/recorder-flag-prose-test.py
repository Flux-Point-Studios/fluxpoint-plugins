#!/usr/bin/env python3
"""--check warns when a graph's prose records a verdict with a flag the recorder will ignore (#232).

A repair graph ended in a red-team verdict, and its "After the run" step
told the operator to run a certifier and, on exit 1, record the run with
`record-run.py --red-team BLOCK`. record-run.py takes the verdict from the
run summary whenever the summary carries one and reads the flag only as a
fallback, so the BLOCK never reached the record and the run was filed SHIP.
Nothing said so before the run: the step read as if it would work.

The compiler reads the graph file's prose as well as its IR. Executed two
ways: the function over a file's text, and the compiler's --check.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
_spec = importlib.util.spec_from_file_location('cg_recorder_flags', PLUGIN / 'scripts' / 'compile-graph.py')
cg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cg)


def graph(contract='DesignV1'):
    return {'version': 1, 'campaign': 'repair', 'treeGuard': False, 'budget': {'maxNodes': 4},
            'nodes': [{'id': 'fix', 'prompt': 'fix it', 'contract': 'DesignV1'},
                      {'id': 'verdict', 'after': 'fix', 'prompt': 'judge {{prev}}', 'contract': contract}]}


def work(ir, *prose):
    return ('# Campaign\n\n```json graph-ir\n' + json.dumps(ir, indent=2) + '\n```\n\n## After the run\n\n'
            + '\n'.join(prose) + '\n\n## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')


class Prose(unittest.TestCase):
    def found(self, ir, *prose):
        return cg.recorder_flag_warnings(work(ir, *prose), ir)

    def test_a_post_run_step_that_records_block_by_flag_is_named(self):
        found = self.found(graph('RedTeamV1'),
                           '1. Run `check_reruns.py` on the result.',
                           '2. On exit 1, record the run with `record-run.py --red-team BLOCK`.')
        self.assertEqual(len(found), 1, found)
        for part in ('--red-team BLOCK', "'verdict'", 'ignores the flag', 'prove:', '--veto'):
            self.assertIn(part, found[0])
        # The line a reader has to change, counted in the file they have open.
        text = work(graph('RedTeamV1'), '1. Run `check_reruns.py` on the result.',
                    '2. On exit 1, record the run with `record-run.py --red-team BLOCK`.')
        line = next(i for i, row in enumerate(text.splitlines(), 1) if '--red-team BLOCK' in row)
        self.assertIn(f'line {line}', found[0])

    def test_a_command_block_counts_and_every_line_is_listed(self):
        found = self.found(graph('RedTeamV1'), 'If the certifier refuses:', '', '```', 'record-run.py --run-id <id> --red-team=BLOCK',
                           '```', '', 'Otherwise `--red-team SHIP`.')
        self.assertEqual(len(found), 1, found)
        self.assertIn('lines ', found[0])

    def test_the_flag_is_a_fallback_when_no_node_returns_that_verdict(self):
        # No node in this graph returns a red-team verdict, so the summary
        # carries none and the flag is what the recorder reads.
        self.assertEqual(self.found(graph(), 'Record with `record-run.py --red-team BLOCK` if the review refuses.'), [])

    def test_the_harness_and_proof_audit_flags_follow_the_same_rule(self):
        harness = self.found(graph('HarnessCheckV1'), 'Then record it: `record-run.py --harness 1`.')
        self.assertEqual(len(harness), 1, harness)
        self.assertIn('--harness 1', harness[0])
        self.assertNotIn('--veto', harness[0])
        proof = self.found(graph('ProofV1'), 'Record `--proof-audit WEAKENED` when the audit script fails.')
        self.assertEqual(len(proof), 1, proof)
        self.assertEqual(self.found(graph('ProofV1'), 'Record with `--harness 0`.'), [])

    def test_naming_the_flag_without_a_value_is_not_an_instruction(self):
        self.assertEqual(self.found(graph('RedTeamV1'),
                                    '`--red-team` is ignored when the summary carries a verdict; use `--veto`.'), [])

    def test_a_line_that_says_not_to_use_the_flag_is_not_an_instruction_to_use_it(self):
        for line in ('Never pass `--red-team BLOCK`: the recorder ignores it.',
                     'Do not record the refusal with `--red-team BLOCK`; file a veto.',
                     "Don't use `--red-team BLOCK` here."):
            with self.subTest(line=line):
                self.assertEqual(self.found(graph('RedTeamV1'), line), [])
        # A negation about something else on the line does not excuse the instruction.
        for line in ('If the certifier does not pass, record `--red-team BLOCK`.',
                     'If the checks do not pass, record `--red-team BLOCK`.',
                     'If the certifier cannot certify the commit, record `--red-team BLOCK`.',
                     'Do not forget to record the refusal with `--red-team BLOCK`.',
                     'The certifier cannot be skipped, so on exit 1 record `--red-team BLOCK`.',
                     'The certifier cannot be skipped; on exit 1 record `--red-team BLOCK`.'):
            with self.subTest(line=line):
                self.assertEqual(len(self.found(graph('RedTeamV1'), line)), 1)

    def test_a_value_taken_from_the_shell_is_still_the_flag(self):
        for line in ('record-run.py --run-id "$RUN" --red-team "$VERDICT"', "record-run.py --red-team 'BLOCK'",
                     'record-run.py --red-team ${VERDICT}'):
            with self.subTest(line=line):
                self.assertEqual(len(self.found(graph('RedTeamV1'), '```', line, '```')), 1)
        for line in ('record-run.py --harness $?', 'record-run.py --harness "$rc"', 'record-run.py --harness=$rc'):
            with self.subTest(line=line):
                self.assertEqual(len(self.found(graph('HarnessCheckV1'), '```', line, '```')), 1)

    def test_another_tools_flag_of_the_same_name_is_not_the_recorders(self):
        # --harness is a common name. Only the recorder's is the fallback this warns about.
        for line in ('./ci/run-suite.sh --harness "$HARNESS_BIN"', 'pytest --harness=$PWD/harness', 'make test -- --harness 1'):
            with self.subTest(line=line):
                self.assertEqual(self.found(graph('HarnessCheckV1'), '```', line, '```'), [])
        self.assertEqual(len(self.found(graph('HarnessCheckV1'), 'Then `record-run.py --run-id <id> --harness 1`.')), 1)

    def test_a_table_of_operator_steps_is_prose(self):
        steps = ('| Step | Command |', '|---|---|', '| 1 | run `check_reruns.py` |',
                 '| 2 | on exit 1, `record-run.py --red-team BLOCK` |')
        self.assertEqual(len(self.found(graph('RedTeamV1'), *steps)), 1)

    def test_filed_rows_are_not_prose_whatever_their_heading_is_called(self):
        ir = graph('RedTeamV1')
        row = '| 2026-11-01 00:00 | graph | COMPLETE | recorded with --red-team BLOCK | n/a |'
        for heading in ('## Evidence log', '## Run history', 'Evidence\n--------'):
            with self.subTest(heading=heading):
                text = ('# Campaign\n\n```json graph-ir\n' + json.dumps(ir) + '\n```\n\n' + heading + '\n\n'
                        '| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n' + row + '\n')
                self.assertEqual(cg.recorder_flag_warnings(text, ir), [])

    def test_the_ir_and_the_filed_evidence_are_not_prose(self):
        ir = graph('RedTeamV1')
        ir['nodes'][1]['prompt'] = 'judge {{prev}}; the operator may pass --red-team BLOCK'
        text = work(ir) + '| 2026-11-01 00:00 | graph | COMPLETE | recorded with --red-team BLOCK | n/a |\n'
        self.assertEqual(cg.recorder_flag_warnings(text, ir), [])


class Check(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'WORK.md'
            path.write_text(text, encoding='utf-8')
            return subprocess.run([sys.executable, str(PLUGIN / 'scripts' / 'compile-graph.py'), str(path), '--check',
                                   '--gates-root', tmp], capture_output=True, text=True)

    def test_check_prints_the_warning_and_still_passes(self):
        ir = graph('HarnessCheckV1')
        done = self.check(work(ir, 'On a red probe, record with `record-run.py --harness 1`.'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('warning', done.stderr)
        self.assertIn('--harness 1', done.stderr)
        quiet = self.check(work(ir, 'Record the run with `record-run.py --run-id <id>`.'))
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertNotIn('--harness', quiet.stderr)


if __name__ == '__main__':
    unittest.main()
