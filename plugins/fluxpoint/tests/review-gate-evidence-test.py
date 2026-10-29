#!/usr/bin/env python3
"""A suite witness can support many lenses without another execution."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import attest
import plugin_identity
spec = importlib.util.spec_from_file_location('review_compiler', SCRIPTS / 'compile-graph.py')
cg = importlib.util.module_from_spec(spec); spec.loader.exec_module(cg)
CONTRACTS = cg.load_contracts(str(SCRIPTS.parent / 'contracts'))
HEAD = 'a' * 40

class ReviewEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='review-gates-'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.fluxpoint-gates.json').write_text(json.dumps({'version': 1, 'gates': {'suite': 'true'}}))
        self.launch = {'since': '2026-01-01T09:00:00Z', 'nonce': 'same-launch', 'root': str(self.root),
                       'manifestSha256': attest.fault_policy.manifest_sha(str(self.root))}
        self.row = {'attestId': 'att_suite', 'gate': 'suite', 'exit': 0, 'treeSha': HEAD, 'headSha': HEAD,
                    'when': '2026-01-01T10:00:00Z', 'nonce': 'same-launch', 'dirty': False, 'logSha256': 'b' * 64,
                    'manifestSha256': self.launch['manifestSha256'], 'commandSha': attest.sha('true')}
        attest.append_row(str(self.root), self.row)
        self.citation = {'gate': 'suite', 'attestId': 'att_suite', 'exit': 0, 'sha': HEAD,
                         'nonce': 'same-launch', 'logSha256': 'b' * 64}

    def review(self):
        return {'verdict': 'SHIP', 'findings': [], 'worstSeverity': 'NONE', 'worstSeverityRank': 0,
                'coverage': {'subject': [{'repository': 'app', 'base': 'c' * 40, 'head': HEAD, 'files': 1, 'additions': 1, 'deletions': 0}],
                             'attackers': 1, 'lenses': ['security'], 'sampled': False,
                             'suites': ['suite'], 'citedGates': [dict(self.citation)]}}

    def summary(self):
        return {'campaign': 'Reuse tests', 'outcome': 'COMPLETE', 'launch': self.launch, 'plugin': plugin_identity.current(),
                'prove': {'gate': 'suite'}, 'contracts': {'gate': 'ExecutionV1', 'lens1': 'RedTeamV1', 'lens2': 'RedTeamV1'},
                'results': {'gate': {'gate': 'suite', 'exit': 0, 'attestId': 'att_suite', 'sha': HEAD},
                            'lens1': self.review(), 'lens2': self.review()}}

    def test_two_lenses_reuse_one_upstream_execution_without_claiming_independent_runs(self):
        checks, findings = attest.verify_claims(str(self.root), self.summary())
        self.assertFalse(findings)
        reviews = [c for c in checks if c.get('reviewEvidence')]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(c['status'] == 'ATTESTED' for c in reviews), reviews)

    def test_stale_wrong_head_red_unknown_and_wrong_log_citations_refuse(self):
        for field, value in [('nonce', 'another-launch'), ('sha', 'd' * 40), ('exit', 1), ('attestId', 'missing'), ('logSha256', 'e' * 64)]:
            summary = self.summary(); summary['results']['lens1']['coverage']['citedGates'][0][field] = value
            checks, _ = attest.verify_claims(str(self.root), summary)
            mine = [c for c in checks if c.get('reviewEvidence') and c['node'].startswith('lens1')]
            self.assertTrue(mine and any(c['status'] != 'ATTESTED' for c in mine), (field, checks))

    def test_a_claimed_suite_needs_a_reused_or_own_witness(self):
        summary = self.summary(); summary['results']['lens1']['coverage']['citedGates'] = []
        checks, _ = attest.verify_claims(str(self.root), summary)
        self.assertTrue(any(c.get('reviewEvidence') and c['status'] == 'MISMATCH' for c in checks))

    def test_own_rerun_requires_a_named_claim(self):
        summary = self.summary(); coverage = summary['results']['lens1']['coverage']
        coverage['citedGates'] = []; coverage['executedGates'] = [dict(self.citation)]
        checks, _ = attest.verify_claims(str(self.root), summary)
        self.assertTrue(any(c.get('reviewEvidence') and 'claim' in c['detail'] for c in checks))
        own = dict(self.row, attestId='att_own')
        attest.append_row(str(self.root), own)
        coverage['executedGates'][0]['attestId'] = 'att_own'
        coverage['executedGates'][0]['reason'] = 'Test the named claim that the parser accepts a negative count.'
        checks, _ = attest.verify_claims(str(self.root), summary)
        self.assertTrue(any(c.get('reviewEvidence') and c['node'].startswith('lens1') and c['status'] == 'ATTESTED' for c in checks), checks)

    def test_a_reused_citation_must_be_offered_by_an_upstream_gate(self):
        summary = self.summary(); summary['results']['gate']['attestId'] = 'another-row'
        checks, _ = attest.verify_claims(str(self.root), summary)
        self.assertTrue(any(c.get('reviewEvidence') and c['status'] == 'MISMATCH' and 'upstream' in c['detail'] for c in checks))

    def test_show_returns_source_row_without_running_the_gate(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'attest.py'), '--root', str(self.root), '--show', 'att_suite', '--nonce', 'same-launch'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['logSha256'], self.citation['logSha256'])
        self.assertEqual(len(attest.read(str(self.root))), 1)

    def run_graph(self, review=None):
        ir = {'version': 1, 'campaign': 'Reuse gate', 'treeGuard': False, 'budget': {'maxNodes': 3}, 'nodes': [
            {'id': 'gate', 'contract': 'ExecutionV1', 'verify': 'prove:suite', 'prompt': 'Check suite'},
            {'id': 'lens', 'contract': 'RedTeamV1', 'prompt': 'Review independently'}]}
        code = cg.emit(ir, CONTRACTS)
        out = self.root / 'out.json'; script = self.root / 'run.js'
        results = {'gate': self.summary()['results']['gate'], 'lens': review or self.review()}
        prelude = 'const prompts={};const args={_launch:'+json.dumps(self.launch)+'};const results='+json.dumps(results)+';const agent=async(p,o)=>{prompts[o.label]=p;return results[o.label]};const log=()=>{},phase=()=>{},budget={total:null,spent:()=>0};const parallel=async xs=>Promise.all(xs.map(f=>f()));'
        script.write_text('const fs=require("fs");'+prelude+'\n(async()=>{\n'+code.replace('export const meta =','const meta =')+'\n})().then(r=>fs.writeFileSync('+json.dumps(str(out))+',JSON.stringify({r,prompts})));')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out.read_text())

    def test_compiled_reviewer_receives_compact_upstream_pointers(self):
        value = self.run_graph()
        self.assertEqual(value['r']['outcome'], 'COMPLETE')
        self.assertIn('att_suite', value['prompts']['lens'])
        self.assertIn('do not rerun', value['prompts']['lens'])
        self.assertIn('--show', value['prompts']['lens'])
        self.assertEqual(value['r']['reviewGateEvidence']['lens'][0]['attestId'], 'att_suite')

    def test_emitted_coherence_halts_suite_claim_without_its_witness(self):
        review = self.review(); review['coverage']['citedGates'] = []
        value = self.run_graph(review)
        self.assertEqual(value['r']['outcome'], 'HALTED')
        self.assertTrue(any('no execution witness' in row.get('detail', '') for row in value['r']['provenance']))

    def test_runtime_identity_cannot_be_invented(self):
        summary = self.summary(); summary['results']['lens1']['coverage']['citedGates'][0]['runtime'] = {'version': 'unmeasured'}
        checks, _ = attest.verify_claims(str(self.root), summary)
        self.assertTrue(any(c.get('reviewEvidence') and c['status'] == 'MISMATCH' and 'runtime' in c['detail'] for c in checks))

    def test_reused_execution_cannot_masquerade_as_an_own_rerun(self):
        summary = self.summary(); coverage = summary['results']['lens1']['coverage']
        coverage['citedGates'] = []
        coverage['executedGates'] = [dict(self.citation, reason='Claiming this was a new independent experiment.')]
        checks, _ = attest.verify_claims(str(self.root), summary)
        self.assertTrue(any(c.get('reviewEvidence') and c['status'] == 'MISMATCH' and 'upstream execution' in c['detail'] for c in checks))

    def test_record_run_cannot_file_fabricated_suite_coverage_clean(self):
        (self.root / 'WORK.md').write_text('## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        (self.root / '.gitignore').write_text('.claude/\n')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'fixture'], check=True)
        summary = self.summary(); summary['results']['lens1']['coverage']['citedGates'][0]['logSha256'] = 'e' * 64
        result_path = self.root / 'summary.json'; result_path.write_text(json.dumps(summary))
        result = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'), '--root', str(self.root), '--graph', 'WORK.md', '--run-id', 'wf_review', '--result', str(result_path)], cwd=self.root, capture_output=True, text=True)
        artifact = self.root / '.claude/fluxpoint/runs/wf_review.json'
        self.assertTrue(artifact.exists(), result.stderr)
        recorded = json.loads(artifact.read_text())
        self.assertEqual(recorded['outcome'], 'TAMPERED-EXECUTION', result.stdout + result.stderr)
        self.assertGreaterEqual(recorded['attestation']['tally']['mismatch'], 1)

    def test_recorder_respects_which_rows_were_available_to_each_reviewer(self):
        summary = self.summary()
        summary['reviewGateEvidence'] = {'lens1': [], 'lens2': [self.citation]}
        checks, _ = attest.verify_claims(str(self.root), summary)
        self.assertTrue(any(c.get('reviewEvidence') and c['node'].startswith('lens1') and c['status'] == 'MISMATCH' for c in checks))
        self.assertTrue(any(c.get('reviewEvidence') and c['node'].startswith('lens2') and c['status'] == 'ATTESTED' for c in checks))

    def review_only(self):
        summary = self.summary()
        summary.pop('prove')
        summary['contracts'].pop('gate')
        summary['results'].pop('gate')
        for result in summary['results'].values():
            result['coverage']['citedGates'] = []
            result['coverage']['executedGates'] = [dict(self.citation, attestId='att_fabricated', reason='Test the named parser regression with its valid control.')]
        return summary

    def test_review_only_suite_claim_requires_a_valid_manifest(self):
        for manifest in (None, {'version': 999, 'gates': {'suite': 'true'}}):
            path = self.root / '.fluxpoint-gates.json'
            if manifest is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(manifest))
            checks, findings = attest.verify_claims(str(self.root), self.review_only())
            self.assertTrue(findings or any(c['status'] != 'ATTESTED' for c in checks), (manifest, checks, findings))

    def test_recorder_refuses_review_only_suite_when_manifest_is_missing(self):
        (self.root / 'WORK.md').write_text('## Evidence\n\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')
        (self.root / '.fluxpoint-gates.json').unlink()
        (self.root / '.gitignore').write_text('.claude/\n')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'fixture'], check=True)
        summary = self.review_only()
        result_path = self.root / 'summary.json'; result_path.write_text(json.dumps(summary))
        result = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'), '--root', str(self.root), '--graph', 'WORK.md', '--run-id', 'wf_review_only', '--result', str(result_path)], cwd=self.root, capture_output=True, text=True)
        artifact = self.root / '.claude/fluxpoint/runs/wf_review_only.json'
        self.assertTrue(artifact.exists(), result.stderr)
        recorded = json.loads(artifact.read_text())
        self.assertEqual(recorded['outcome'], 'INCOMPLETE', result.stdout + result.stderr)

    def test_malformed_subject_cannot_crash_the_source_crosscheck(self):
        for subjects in (None, [{'head': []}], 'invalid'):
            summary = self.summary(); summary['results']['lens1']['coverage']['subject'] = subjects
            checks, _ = attest.verify_claims(str(self.root), summary)
            self.assertTrue(any(c.get('reviewEvidence') and c['status'] == 'MISMATCH' for c in checks))

if __name__ == '__main__': unittest.main()
