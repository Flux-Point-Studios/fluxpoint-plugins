#!/usr/bin/env python3
"""Real Git repair windows, checked records, and explicit operator acceptance."""
import copy
import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class UncheckedRepairsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Repair Test')
        self.git('config', 'user.email', 'repair@example.invalid')
        self.base = self.commit('base')
        self.first = self.commit('repair one\n\nFluxpoint-Executor: codex')
        self.second = self.commit('repair two')
        self.runs = self.root / '.claude/fluxpoint/runs'
        self.runs.mkdir(parents=True)
        self.artifact('block', {'recordedAt': '2026-10-15 12:00:00', 'redTeam': 'BLOCK',
                               'summary': {'tree': {'baseline': {'head': self.base}}}})
        self.graph = self.root / 'WORK.md'
        self.set_graph([{'id': 'review', 'contract': 'RedTeamV1'}])

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        (self.root / 'file').write_text(message)
        self.git('add', 'file')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def artifact(self, name, data):
        (self.runs / (name + '.json')).write_text(json.dumps(data))

    def set_graph(self, nodes):
        self.graph.write_text('```json graph-ir\n' + json.dumps({'nodes': nodes}) + '\n```\n\n'
                              '| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |\n'
                              '|---|---|---|---|---|---|\n')

    def repair_graph(self):
        self.set_graph([
            {'id': 'fix', 'mutates': True, 'foreach': 'slices', 'contract': 'SliceV1'},
            {'id': 'check', 'after': 'fix', 'verifies': 'fix', 'independent': True,
             'foreach': 'slices', 'isolation': 'measure', 'contract': 'FixCheckV1',
             'repeat': {'until': "verdict == 'PASS'", 'maxRounds': 3}},
        ])
        content = self.graph.read_text()
        content = content.replace('"nodes":', '"lists": {"slices": [{"key": "slice", "targetClasses": ["legacy-defect"]}]}, "nodes":', 1)
        self.graph.write_text(content + '\n| When (UTC) | Source | Outcome | Claim | Proof |\n|---|---|---|---|---|\n')

    def pass_summary(self, commits):
        check = {'verdict': 'PASS', 'findings': [], 'blockers': [],
                 'evidence': ['pytest regression.py: original attack rejected, valid control passed'],
                 'commits': commits}
        patch = {'item': 'slice', 'round': 1,
                 'patch': str(self.root) + '/.claude/fluxpoint/rounds/launch/' + '1' * 64 + '/slice/r1/patch.diff'}
        row = {'node': 'check', 'item': 'slice', 'round': 1,
               'verdict': 'PASS', 'findings': [], 'blockers': [],
               'evidence': check['evidence'], 'commits': commits, 'check': check,
               'context': {'base': self.base, 'item': 'slice', 'round': 1,
                           'patches': [patch], 'current': patch, 'targetClasses': ['legacy-defect']}}
        summary = {'outcome': 'COMPLETE', 'repairAssuranceVersion': 2, 'contracts': {'check': 'FixCheckV1'},
                'launch': {'root': str(self.root), 'nonce': 'launch'},
                'tree': {'baseline': {'head': self.base}}, 'fixOrigins': [row]}
        # A positive fixture includes the new complete diff proof; negative
        # cases below still mutate one independent obligation at a time.
        from repair_diff import manifest, AXES
        data = subprocess.run(['git', '-C', str(self.root), 'diff', '--binary', self.base, commits[-1]],
                              capture_output=True, check=True).stdout
        path = Path(patch['patch'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        measured = manifest(data)
        from repair_diff import compact_manifest, manifest_digest
        patch['patchSha256'] = measured['patchSha256']
        observed = {'command': 'pytest regression.py', 'result': 'executed attack and valid control passed'}
        check['targetClasses'] = ['legacy-defect']
        check['siblings'] = [dict(finding='legacy-defect', invariant='all sibling paths preserve the invariant',
            axis=axis, surface='independent-' + axis,
            attack=dict(observed, command='pytest regression.py # sibling-' + axis), control=observed,
            outcome='DEFENDED', reason='independently executed sibling case') for axis in AXES]
        check['diffCoverage'] = {'patchSha256': measured['patchSha256'], 'hunks': [
            dict(id=h['id'], surface='other', attack=dict(observed, command='pytest regression.py # hunk-' + h['path']), control=observed) for h in measured['hunks']]}
        nonce = '1' * 64
        check['checkerWitness'] = dict(status='CLEANED', spawnId='check:slice:r1', base=self.base,
                                       patchSha256=measured['patchSha256'], readNonce=nonce,
                                       snapshot='/snapshot/checker', token='f' * 64,
                                       observations=[dict(base=self.base, patchSha256=measured['patchSha256'], attack=row['attack'], control=row['control'], reproduces=False) for row in check['siblings'] + check['diffCoverage']['hunks']])
        proof = dict(version=1, base=self.base, item='slice', round=1, patch=patch['patch'],
                     patchSha256=measured['patchSha256'], manifestSha256=manifest_digest(compact_manifest(data)), readNonce=nonce, clean=True, blockers=[], refuters=[], findings=[])
        lenses = ['authority', 'value', 'state', 'input', 'operations', 'composition', 'coverage-skeptic']
        proof['attackers'] = [dict(spawnId='repair-diff:slice:r1:attack:' + lens, lens=lens, completed=True,
                                   base=self.base, patchSha256=measured['patchSha256'], evidence=['executed independent fixture attacks and valid controls'], observations=[
                                       dict(base=self.base, patchSha256=measured['patchSha256'], attack=observed, control=observed, reproduces=False)]) for lens in lenses]
        proof['snapshots'] = [dict(status='CLEANED', token=format(i + 1, '064x'), snapshot='/snapshot/' + str(i),
                                  spawnId=a['spawnId'], observations=a['observations']) for i, a in enumerate(proof['attackers'])]
        row['diffAttack'] = check['diffAttack'] = proof
        summary['repairDiffReviews'] = [proof]
        summary['repairReadNonce'] = nonce
        return summary

    def journal(self, summary):
        import repair_journal
        directory = self.root / 'runtime/projects/project/session/subagents/workflows/repair'
        directory.mkdir(parents=True, exist_ok=True)
        requirements = repair_journal.native_requirements(summary)
        events = []
        for i, (label, result) in enumerate(repair_journal.expected_calls(summary)):
            agent = 'repair' + str(i)
            events += [dict(type='started', agentId=agent, label=label), dict(type='result', agentId=agent, result=result)]
            requirement = requirements[label]
            rows = [dict(type='user', message=dict(role='user', content='Fixed repair seat ' + label))]
            outputs = requirement.get('observations') if requirement['mode'] == 'execute' else [result]
            for number, output in enumerate(outputs):
                argv = [sys.executable, str(SCRIPTS / 'repair-proof.py'), requirement['mode']]
                if requirement['mode'] == 'execute':
                    flags = dict(snapshot=requirement['snapshot'], token=requirement['token'], control=output['control']['command'], attack=output['attack']['command'])
                    if output.get('seriesPath'):
                        flags['series'] = output['seriesPath']
                else:
                    flags = requirement['flags']
                for key, value in flags.items():
                    argv += ['--' + key, str(value)]
                ident = 'native' + str(number)
                rows += [dict(type='assistant', cwd=str(self.root), message=dict(role='assistant', content=[dict(type='tool_use', id=ident, name='Bash', input=dict(command=shlex.join(argv)))])),
                    dict(type='user', message=dict(role='user', content=[dict(type='tool_result', tool_use_id=ident, content=json.dumps(output) + '\n', is_error=False)]))]
            for number, finding in enumerate(requirement.get('origins', [])):
                evidence = finding['originEvidence']['base']
                argv = [sys.executable, str(SCRIPTS / 'repair-replay.py'), '--root', str(self.root), '--context', 'origin-context.json',
                    '--defect-class', finding['defectClass'], '--control', evidence['controlCommand'], '--command', evidence['command']]
                output = {k: finding[k] for k in ('origin', 'originEvidence')}
                ident = 'origin' + str(number)
                rows += [dict(type='assistant', cwd=str(self.root), message=dict(role='assistant', content=[dict(type='tool_use', id=ident, name='Bash', input=dict(command=shlex.join(argv)))])),
                    dict(type='user', message=dict(role='user', content=[dict(type='tool_result', tool_use_id=ident, content=json.dumps(output) + '\n', is_error=False)]))]
            (directory / ('agent-' + agent + '.jsonl')).write_text(''.join(json.dumps(row) + '\n' for row in rows))
            (directory / ('agent-' + agent + '.meta.json')).write_text(json.dumps(dict(worktreePath=str(self.root))))
        (directory / 'journal.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))

    def record_repair(self, commits):
        self.repair_graph()
        summary = self.pass_summary(commits)
        self.journal(summary)
        p = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'),
                            '--root', str(self.root), '--graph', str(self.graph), '--run-id', 'repair'],
                           input=json.dumps(summary), text=True, capture_output=True,
                           env=dict(os.environ, FPL_MEMORY_INDEX='0', CLAUDE_CONFIG_DIR=str(self.root / 'runtime')), timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.set_graph([{'id': 'review', 'contract': 'RedTeamV1'}])
        return json.loads((self.runs / 'repair.json').read_text())

    def test_repair_seat_result_must_match_the_runtime_journal(self):
        import repair_journal
        summary = self.pass_summary([self.first])
        self.journal(summary)
        projects = self.root / 'runtime/projects'
        verified = repair_journal.corroborate(summary, 'repair', projects=projects)
        self.assertTrue(verified['seats'])
        directory = projects / 'project/session/subagents/workflows/repair'
        journal = directory / 'journal.jsonl'
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        for row in rows:
            if row.get('type') == 'result' and row.get('result', {}).get('verdict') == 'PASS':
                row['result']['verdict'] = 'FAIL'
        journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        with self.assertRaisesRegex(ValueError, 'differs from journal'):
            repair_journal.corroborate(summary, 'repair', projects=projects)

    def test_native_certificate_preserves_full_helper_result_and_required_projection(self):
        import repair_journal
        summary = self.pass_summary([self.first])
        self.journal(summary)
        directory = self.root / 'runtime/projects/project/session/subagents/workflows/repair'
        events = [json.loads(line) for line in (directory / 'journal.jsonl').read_text().splitlines()]
        agents = {event['agentId'] for event in events if event.get('label', '').startswith('repair-witness:materialize:')}
        for event in events:
            if event['type'] == 'result' and event['agentId'] in agents:
                event['result']['snapshotTree'] = '2' * 64
                event['result']['round'] = 1
        (directory / 'journal.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
        for agent in agents:
            path = directory / ('agent-' + agent + '.jsonl')
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            output = rows[2]['message']['content'][0]
            actual = json.loads(output['content'])
            actual['snapshotTree'] = '2' * 64
            actual['round'] = 1.0  # Native JSON and JS report serialize equal numbers differently.
            output['content'] = json.dumps(actual)
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        certificate = repair_journal.corroborate(summary, 'repair', projects=self.root / 'runtime/projects')
        self.assertIsNone(repair_journal.certificate_issue(summary, 'repair', certificate))
        materialize = next(seat for seat in certificate['seats'] if seat['agentId'] in agents)
        self.assertNotEqual(materialize['resultSha256'], materialize['requiredSha256'])

    def test_cleanup_log_cannot_add_an_observation_the_checker_never_ran(self):
        import repair_journal
        summary = self.pass_summary([self.first])
        self.journal(summary)
        observations = summary['fixOrigins'][0]['check']['checkerWitness']['observations']
        forged = copy.deepcopy(observations[0])
        forged['attack']['command'] = 'never executed this command'
        observations.append(forged)
        with self.assertRaisesRegex(ValueError, 'no distinct native Bash'):
            repair_journal.corroborate(summary, 'repair', projects=self.root / 'runtime/projects')

    def test_missing_checker_native_tool_output_cannot_be_replaced_by_a_claim(self):
        import repair_journal
        summary = self.pass_summary([self.first])
        self.journal(summary)
        directory = self.root / 'runtime/projects/project/session/subagents/workflows/repair'
        events = [json.loads(line) for line in (directory / 'journal.jsonl').read_text().splitlines()]
        agent = next(e['agentId'] for e in events if e.get('label') == 'check:slice:r1')
        transcript = directory / ('agent-' + agent + '.jsonl')
        rows = [json.loads(line) for line in transcript.read_text().splitlines()]
        rows = [row for row in rows if not any(item.get('type') == 'tool_result' and item.get('tool_use_id') == 'native0' for item in row.get('message', {}).get('content', []) if isinstance(item, dict))]
        transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            repair_journal.corroborate(summary, 'repair', projects=self.root / 'runtime/projects')

    def test_native_helpers_cannot_be_echoed_retargeted_or_hidden(self):
        import repair_journal
        for mutation in ('echo', 'token', 'hidden', 'shape', 'interpreter', 'plugin'):
            with self.subTest(mutation=mutation):
                summary = self.pass_summary([self.first])
                self.journal(summary)
                directory = self.root / 'runtime/projects/project/session/subagents/workflows/repair'
                events = [json.loads(line) for line in (directory / 'journal.jsonl').read_text().splitlines()]
                agent = next(e['agentId'] for e in events if e.get('label') == 'check:slice:r1')
                transcript = directory / ('agent-' + agent + '.jsonl')
                rows = [json.loads(line) for line in transcript.read_text().splitlines()]
                use = rows[1]['message']['content'][0]
                if mutation == 'echo':
                    use['input']['command'] = 'printf fabricated-json'
                elif mutation == 'interpreter':
                    use['input']['command'] = use['input']['command'].replace(shlex.quote(sys.executable), '/tmp/checker-owned/python', 1)
                elif mutation == 'plugin':
                    summary['plugin'] = {'root': '/tmp/checker-owned/plugin'}
                elif mutation == 'token':
                    use['input']['command'] = use['input']['command'].replace('f' * 64, 'e' * 64)
                elif mutation == 'shape':
                    value = json.loads(rows[2]['message']['content'][0]['content'])
                    value['attack'] = None
                    rows[2]['message']['content'][0]['content'] = json.dumps(value)
                else:
                    tool, result = copy.deepcopy(rows[1:3])
                    tool['message']['content'][0]['id'] = 'hidden'
                    result['message']['content'][0]['tool_use_id'] = 'hidden'
                    value = json.loads(result['message']['content'][0]['content'])
                    value['reproduces'] = True
                    result['message']['content'][0]['content'] = json.dumps(value)
                    rows += [tool, result]
                transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                with self.assertRaisesRegex(ValueError, 'native Bash|disappeared|output differs|installation differs'):
                    repair_journal.corroborate(summary, 'repair', projects=self.root / 'runtime/projects')

    def test_immutable_legacy_positive_loses_credit_but_remains_repairable(self):
        import run_artifacts
        record = self.record_repair([self.first, self.second])
        record.pop('repairAssuranceVersion')
        record['summary'].pop('repairAssuranceVersion')
        record['repairJournal']['version'] = 1
        record['repairJournal'].pop('assurance')
        for seat in record['repairJournal']['seats']:
            seat.pop('native')
            seat.pop('requiredSha256')
        path, digest = run_artifacts.write(self.root, 'repair', record)
        original = Path(path).read_bytes()
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual([row['commit'] for row in status['unchecked']], [self.first, self.second])
        self.assertEqual(status['warnings'][0]['code'], 'LEGACY-REPAIR-UNCHECKED')
        self.assertIn('LEGACY-REPAIR-UNCHECKED', result.stderr)
        self.repair_graph()
        repaired = self.run_check()
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(len(json.loads(repaired.stdout)['unchecked']), 2)
        self.assertEqual(Path(path).read_bytes(), original)
        self.assertEqual(run_artifacts.read(self.root, 'repair', digest)[0], record)

    def test_legacy_receipts_need_coherent_immutable_history(self):
        import run_artifacts
        record = self.record_repair([self.first])
        record.pop('repairAssuranceVersion')
        record['summary'].pop('repairAssuranceVersion')
        record['repairJournal'] = None
        run_artifacts.write(self.root, 'repair', record)
        self.assertEqual(self.run_check().returncode, 1)
        record['repairCheckers']['check']['fixer']['id'] = 'check'
        run_artifacts.write(self.root, 'repair', record)
        self.assertEqual(self.run_check().returncode, 2)
        record['repairCheckers']['check']['fixer']['id'] = 'fix'
        record['recordedAt'] = '2026-10-15 16:00:00'
        self.artifact('repair', record)  # Coherent legacy bytes predate immutable history.
        old = self.run_check()
        self.assertEqual(old.returncode, 1, old.stderr)
        self.assertIn('No immutable history snapshot', old.stderr)
        self.repair_graph()
        self.assertEqual(self.run_check().returncode, 0)

    def test_new_native_assurance_cannot_be_downgraded_or_malformed(self):
        import run_artifacts
        record = self.record_repair([self.first])
        self.assertEqual(record['repairAssuranceVersion'], 2)
        self.assertEqual(record['repairJournal']['version'], 2)
        for mutation in ('marker', 'marker-float', 'journal', 'journal-float', 'digest', 'duplicate', 'source', 'unpublished'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(record)
                if mutation == 'marker': bad.pop('repairAssuranceVersion')
                if mutation == 'marker-float': bad['repairAssuranceVersion'] = 2.0
                if mutation == 'journal': bad['repairJournal'] = None
                if mutation == 'journal-float': bad['repairJournal']['version'] = 2.0
                if mutation == 'digest': bad['repairJournal']['seats'][0]['requiredSha256'] = '0' * 64
                if mutation == 'duplicate': bad['repairJournal']['seats'].append(bad['repairJournal']['seats'][0])
                if mutation == 'source': bad['repairJournal']['seats'][0]['native']['sources'][0]['sha256'] = 'broken'
                if mutation == 'unpublished':
                    bad['repairJournal']['seats'][0]['native']['calls'][0]['nativeSha256'] = '0' * 64
                    self.artifact('repair', bad)
                else:
                    run_artifacts.write(self.root, 'repair', bad)
                self.assertEqual(self.run_check().returncode, 2)

    def test_outside_scope_residual_requires_native_base_replay(self):
        import repair_journal
        summary = self.pass_summary([self.first])
        row = summary['fixOrigins'][0]
        context = row['context']
        observed = dict(command='pytest existing_case.py', result='exit 1: existing harm', controlCommand='pytest valid.py', controlResult='exit 0: valid', controlPassed=True, reproduces=True)
        finding = dict(id='P1', source='repair-diff:slice:r1:attack:authority', finding='Existing separate defect', severity='HIGH',
            defectClass='outside-class', invariant='existing invariant', classRewire='separate repair needed', measurement='replayed base and current',
            origin='pre-existing', disposition='OUTSIDE-TARGET-CLASSES',
            originEvidence=dict(base=dict(snapshot=self.base, **observed), targetClasses=context['targetClasses'], rounds=[dict(context['current'], **observed)]))
        row['diffAttack']['followups'] = [finding]
        self.journal(summary)
        projects = self.root / 'runtime/projects'
        self.assertTrue(repair_journal.corroborate(summary, 'repair', projects=projects)['seats'])
        directory = projects / 'project/session/subagents/workflows/repair'
        events = [json.loads(line) for line in (directory / 'journal.jsonl').read_text().splitlines()]
        agent = next(e['agentId'] for e in events if e.get('label') == finding['source'])
        transcript = directory / ('agent-' + agent + '.jsonl')
        rows = [json.loads(line) for line in transcript.read_text().splitlines()]
        for event in rows:
            content = event.get('message', {}).get('content')
            if isinstance(content, list):
                for item in content:
                    if item.get('type') == 'tool_use' and item.get('id') == 'origin0':
                        item['input']['command'] = 'printf fabricated-origin'
        transcript.write_text(''.join(json.dumps(event) + '\n' for event in rows))
        with self.assertRaisesRegex(ValueError, 'no native origin replay'):
            repair_journal.corroborate(summary, 'repair', projects=projects)

    def test_handwritten_pass_without_runtime_seats_cannot_discharge_commit(self):
        self.repair_graph()
        summary = self.pass_summary([self.first])
        p = subprocess.run([sys.executable, str(SCRIPTS / 'record-run.py'), '--root', str(self.root),
            '--graph', str(self.graph), '--run-id', 'repair'], input=json.dumps(summary), text=True,
            capture_output=True, env=dict(os.environ, FPL_MEMORY_INDEX='0', CLAUDE_CONFIG_DIR=str(self.root / 'missing-runtime')))
        self.assertEqual(p.returncode, 0, p.stderr)
        artifact = json.loads((self.runs / 'repair.json').read_text())
        self.assertEqual(artifact['checked'], [])
        self.assertEqual(artifact['outcome'], 'INCOMPLETE')
        self.assertIn('REPAIR-JOURNAL-UNPROVEN', p.stderr)

    def run_check(self):
        return subprocess.run([sys.executable, str(SCRIPTS / 'unchecked-repairs.py'),
                               '--root', str(self.root), '--json'], text=True,
                              capture_output=True, timeout=20)

    def test_recorded_repair_keeps_patch_evidence_after_round_cleanup(self):
        artifact = self.record_repair([self.first])
        patch = artifact['summary']['fixOrigins'][0]['context']['current']['patch']
        Path(patch).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([r['commit'] for r in json.loads(result.stdout)['unchecked']], [self.second])

    def test_retained_bytes_stay_bound_and_old_window_receipts_are_skipped(self):
        artifact = self.record_repair([self.first])
        self.assertTrue(artifact['repairEvidence']['files'])
        patch = artifact['summary']['fixOrigins'][0]['context']['current']['patch']
        Path(patch).unlink()
        digest = artifact['repairEvidence']['files'][patch]
        retained = self.runs / 'evidence' / digest
        retained.write_bytes(b'changed retained bytes')
        result = self.run_check()
        self.assertEqual(result.returncode, 2)
        self.assertIn('digest mismatch', result.stderr)
        self.artifact('new-block', {'recordedAt': '2026-10-16 12:00:00', 'redTeam': 'BLOCK',
            'summary': {'tree': {'baseline': {'head': self.first}}}})
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([r['commit'] for r in json.loads(result.stdout)['unchecked']], [self.second])

    def test_recorded_series_is_retained_separately_from_a_rotating_source(self):
        import repair_checks
        import hashlib
        summary = self.pass_summary([self.first])
        series = self.root / 'series.json'
        series.write_bytes(b'[0,1,2]')
        row = summary['fixOrigins'][0]
        row['check']['diffCoverage']['operational'] = {'alarmRate': {'seriesPath': str(series), 'seriesSha256': hashlib.sha256(series.read_bytes()).hexdigest()}}
        saved = repair_checks.retain_evidence(self.root, summary)
        series.write_bytes(b'[0,1,2,3]')
        original = repair_checks._evidence_file(self.root, str(series), saved)
        self.assertEqual(original.read_bytes(), b'[0,1,2]')

    def test_any_producer_is_checked_and_only_final_trailers_are_reported(self):
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stderr)
        rows = json.loads(p.stdout)['unchecked']
        self.assertEqual([r['commit'] for r in rows], [self.first, self.second])
        self.assertEqual([r['executor'] for r in rows], ['codex', 'unlabelled'])

    def test_checked_prefix_and_explicit_decision_discharge_window(self):
        self.record_repair([self.first[:12]])
        decision = module('decision')
        rec = {'question': 'Accept the remaining unchecked repair?', 'options': [
            {'option': s, 'argued_by': 'operator', 'strongest_objection': 'This alternative carries a concrete verification risk.'}
            for s in ('accept', 'check')], 'chosen': 'accept',
            'rationale': 'The operator explicitly accepts this repair after reviewing its remaining verification risk.',
            'overturned_prior': False, 'frozen_by': 'none', 'reversible': True,
            'evidence': ['manual review'], 'acceptedCommits': [self.second]}
        p = subprocess.run([sys.executable, str(SCRIPTS / 'decision.py'), '--record', '--root',
                            str(self.root), '--graph', str(self.graph), '--id', 'accept-repair'],
                           input=json.dumps(rec), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_check()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['unchecked'], [])
        self.assertTrue(decision.validate(dict(rec, acceptedCommits=['not-a-sha']), decision.load_schema(str(SCRIPTS.parent))))

    def test_repair_graph_can_run_but_irreversible_graph_cannot(self):
        self.set_graph([{'id': 'check', 'contract': 'FixCheckV1'}])
        p = self.run_check()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)['unchecked']), 2)
        self.set_graph([{'id': 'ship', 'irreversible': True}])
        self.assertEqual(self.run_check().returncode, 1)

    def test_latest_local_block_and_subject_repository_binding(self):
        self.artifact('new', {'recordedAt': '2026-10-15 13:00:00', 'redTeam': 'BLOCK',
            'summary': {'subject': [{'repository': str(self.root), 'head': self.first}]}})
        p = self.run_check()
        self.assertEqual([r['commit'] for r in json.loads(p.stdout)['unchecked']], [self.second])

    def test_malformed_records_and_nonancestor_block_stop_preflight(self):
        (self.runs / 'broken.json').write_text('{')
        self.assertEqual(self.run_check().returncode, 2)
        (self.runs / 'broken.json').unlink()
        self.git('checkout', '-q', '--orphan', 'other')
        self.git('commit', '-q', '--allow-empty', '-m', 'unrelated')
        self.assertEqual(self.run_check().returncode, 2)

    def test_recorder_copies_only_coherent_pass_from_actual_pair(self):
        recorder = module('record-run')
        self.repair_graph()
        summary = self.pass_summary([self.second])
        summary['outcome'] = 'HALTED'  # A later halt does not undo an exact earlier check.
        self.assertEqual(recorder.checked_commits(summary, self.graph), [self.second])
        for mutation in ('blocker', 'finding', 'no-check', 'no-evidence', 'wrong-round', 'stale-patch', 'different-commits', 'target-override'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(summary)
                row = bad['fixOrigins'][0]
                if mutation == 'blocker': row['check']['blockers'] = ['not run']
                if mutation == 'finding': row['check']['findings'] = [{'severity': 'HIGH'}]
                if mutation == 'no-check': row.pop('check')
                if mutation == 'no-evidence': row['check']['evidence'] = []
                if mutation == 'wrong-round': row['round'] = 2
                if mutation == 'stale-patch': row['context']['current']['patch'] = '/old/patch.diff'
                if mutation == 'different-commits': row['check']['commits'] = [self.first]
                if mutation == 'target-override': row['context']['targetClasses'] = ['new-defect']
                self.assertEqual(recorder.checked_commits(bad, self.graph), [])
        self.set_graph([{'id': 'check', 'contract': 'FixCheckV1', 'independent': True,
                         'repeat': {'until': "verdict == 'PASS'", 'maxRounds': 3}}])
        self.assertEqual(recorder.checked_commits(summary, self.graph), [])

    def test_positive_artifact_requires_frozen_coherent_receipts(self):
        self.artifact('bare', {'checked': [self.first, self.second]})
        self.assertEqual(self.run_check().returncode, 2)
        (self.runs / 'bare.json').unlink()
        record = self.record_repair([self.first])
        self.assertEqual(json.loads(self.run_check().stdout)['unchecked'][0]['commit'], self.second)
        for mutation in ('missing-descriptors', 'self-verifier', 'extra-commit', 'contradictory-pass'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(record)
                if mutation == 'missing-descriptors': bad.pop('repairCheckers')
                if mutation == 'self-verifier': bad['repairCheckers']['check']['fixer']['id'] = 'check'
                if mutation == 'extra-commit': bad['checked'].append(self.second)
                if mutation == 'contradictory-pass': bad['summary']['fixOrigins'][0]['check']['blockers'] = ['no check ran']
                self.artifact('repair', bad)
                self.assertEqual(self.run_check().returncode, 2)
        import run_artifacts
        run_artifacts.write(self.root, 'repair', record)
        self.assertEqual(self.run_check().returncode, 1)  # Graph changed after recording; descriptor stays valid.

    def test_foreign_review_does_not_borrow_local_tree_head(self):
        self.artifact('foreign', {'recordedAt': '2026-10-15 14:00:00', 'redTeam': 'BLOCK',
            'summary': {'coverage': {'subject': [{'repository': '/another/repository', 'head': self.second}]},
                        'tree': {'baseline': {'head': self.second}}}})
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual([r['commit'] for r in json.loads(p.stdout)['unchecked']], [self.first, self.second])

    def test_local_subject_missing_head_cannot_silently_remove_block_window(self):
        self.artifact('bad-local', {'recordedAt': '2026-10-15 15:00:00', 'redTeam': 'BLOCK',
            'summary': {'coverage': {'subject': [{'repository': str(self.root), 'head': None}]},
                        'tree': {'baseline': {'head': self.second}}}})
        self.assertEqual(self.run_check().returncode, 2)

    def test_integration_checker_requires_its_real_merge_link(self):
        helper = module('repair_checks')
        self.repair_graph()
        content = self.graph.read_text()
        ir = json.loads(content.split('```json graph-ir\n')[1].split('\n```')[0])
        ir['nodes'][1]['repeat']['integrate'] = ['merge', 'review']
        ir['nodes'].extend([
            {'id': 'merge', 'mutates': True, 'after': 'check'},
            {'id': 'review', 'contract': 'FixCheckV1', 'independent': True,
             'verifies': 'merge', 'after': 'merge', 'isolation': 'measure',
             'haltWhen': "verdict != 'PASS'"}])
        self.assertIn('review', helper.descriptors(ir))
        ir['nodes'][-1]['verifies'] = 'fix'
        self.assertNotIn('review', helper.descriptors(ir))


if __name__ == '__main__':
    unittest.main()
