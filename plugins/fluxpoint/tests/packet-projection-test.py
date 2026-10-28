#!/usr/bin/env python3
"""Packet proposals size the eventual script without landing or freezing text."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
import specification

fixture_spec = importlib.util.spec_from_file_location('packet_fixtures', Path(__file__).with_name('specification-test.py'))
fixtures = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)


class PacketProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.doc = fixtures.packet()
        self.ir = fixtures.graph()
        self.save()
        self.graph()
        self.assertEqual(self.spec('--lock').returncode, 0)

    def save(self):
        (self.root / specification.SPEC).write_text(json.dumps(self.doc), encoding='utf-8')

    def graph(self, prose=''):
        (self.root / 'WORK.md').write_text('SPEC: .fluxpoint-spec.json\n' + prose
            + '\n```json graph-ir\n' + json.dumps(self.ir) + '\n```\n', encoding='utf-8')

    def spec(self, *args, root=None):
        return subprocess.run([sys.executable, str(PLUGIN / 'scripts/specification.py'), *args],
                              cwd=root or self.root, capture_output=True, text=True, encoding='utf-8')

    def compile(self, *args, root=None):
        return subprocess.run([sys.executable, str(PLUGIN / 'scripts/compile-graph.py'), 'WORK.md', *args],
                              cwd=root or self.root, capture_output=True, text=True, encoding='utf-8')

    def edit(self, anchor, text, file=specification.SPEC, op='replace'):
        return {'file': file, 'anchor': anchor, 'op': op, 'text': text, 'reason': 'Align the binding scope with independently measured behavior'}

    def proposals(self, edits):
        path = self.root / 'edits.json'
        path.write_text(json.dumps(edits), encoding='utf-8')
        return path.name

    def measure(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'(?:projected )?script ([\d,]+) bytes, headroom (-?[\d,]+) of ([\d,]+) bytes', result.stdout)
        self.assertIsNotNone(match, result.stdout)
        return tuple(int(v.replace(',', '')) for v in match.groups())

    def test_graph_and_overlay_contract_are_projected_as_one_proposal(self):
        overlay = self.root / 'contracts'
        overlay.mkdir()
        schema = json.loads((PLUGIN / 'contracts/HarnessCheckV1.schema.json').read_text())
        original = json.dumps(schema, indent=2)
        (overlay / 'HarnessCheckV1.schema.json').write_text(original)
        self.graph()
        graph = self.root / 'WORK.md'
        graph.write_text('CONTRACTS: contracts\n' + graph.read_text())
        edit_graph = self.edit('exit != 0', 'rowExit != 0', file='WORK.md')
        rejected = self.compile('--check', '--packet-edits', self.proposals([edit_graph]))
        self.assertNotEqual(rejected.returncode, 0)
        changed = json.loads(original)
        changed['properties']['rowExit'] = {'type': 'integer'}
        proposals = [edit_graph, self.edit(original, json.dumps(changed, indent=2), file='contracts/HarnessCheckV1.schema.json')]
        result = self.compile('--check', '--packet-edits', self.proposals(proposals))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('projected contracts: contracts/HarnessCheckV1.schema.json', result.stdout)
        self.assertEqual((overlay / 'HarnessCheckV1.schema.json').read_text(), original)

    def test_projection_matches_landed_utf8_size_and_writes_no_packet_or_lock(self):
        anchor = self.doc['checks'][0]['scope']
        replacement = anchor + ' — π and precise scope clauses' * 20
        proposals = self.proposals([self.edit(anchor, replacement)])
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        projected = self.compile('--check', '--packet-edits', proposals)
        size, headroom, limit = self.measure(projected)
        self.assertIn('projection only', projected.stdout)
        self.assertEqual(size + headroom, limit)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}, before)
        with tempfile.TemporaryDirectory() as scratch:
            scratch = Path(scratch)
            for name, data in before.items():
                (scratch / name).write_bytes(data)
            path = scratch / specification.SPEC
            path.write_bytes(path.read_bytes().replace(anchor.encode(), replacement.encode()))
            self.assertEqual(self.spec('--lock', root=scratch).returncode, 0)
            landed = self.compile('--out', 'actual.graph.js', root=scratch)
            self.assertEqual(landed.returncode, 0, landed.stderr)
            self.assertEqual((scratch / 'actual.graph.js').stat().st_size, size)

    def test_referenced_archive_text_and_identity_are_projected_together(self):
        archive = self.root / 'archive.json'
        archive.write_text('{"scope":"full binding text"}', encoding='utf-8')
        self.doc['checks'][0]['scope'] = 'archive.json#/scope'
        self.save()
        self.assertEqual(self.spec('--lock').returncode, 0)
        _, original = specification.load(self.root)
        edits = [self.edit('full binding text', 'expanded binding text', file='archive.json')]
        packet, identity, snapshots, _ = specification.project(self.root, edits, graph='WORK.md')
        self.assertEqual(packet['checks'][0]['scope'], 'expanded binding text')
        self.assertEqual(packet['checks'][0]['scopeRef'], 'archive.json#/scope')
        self.assertEqual(identity['sha256'], original['sha256'])
        self.assertNotEqual(identity['references'], original['references'])
        self.assertEqual(identity['references']['archive.json'], hashlib.sha256(snapshots['archive.json']).hexdigest())
        self.measure(self.compile('--check', '--packet-edits', self.proposals(edits)))
        self.assertEqual(specification.load(self.root)[1], original)

    def test_new_reference_targets_can_be_proposed_with_the_packet_rewire(self):
        (self.root / 'archive.json').write_text('{"scope":"old scope"}')
        edits = [self.edit(self.doc['checks'][0]['scope'], 'archive.json#/scope'),
                 self.edit('old scope', 'new scope', file='archive.json')]
        packet, identity, _, _ = specification.project(self.root, edits)
        self.assertEqual(packet['checks'][0]['scope'], 'new scope')
        self.assertIn('archive.json', identity['references'])

    def test_anchors_are_unique_in_original_text_and_conflicts_are_not_order_dependent(self):
        (self.root / 'anchors.json').write_text('abcdefg aaa')
        cases = [([self.edit('missing', 'x', 'anchors.json')], '0 times'),
                 ([self.edit('aa', 'x', 'anchors.json')], '2 times'),
                 ([self.edit('abcd', 'x', 'anchors.json'), self.edit('cdef', 'y', 'anchors.json')], 'overlaps'),
                 ([self.edit('abc', 'x', 'anchors.json'), self.edit('def', 'y', 'anchors.json', 'append'),
                   self.edit('aaa', 'z', 'anchors.json', 'append')], 'overlaps'),
                 ([self.edit('abc', 'new anchor', 'anchors.json'), self.edit('new anchor', 'x', 'anchors.json')], '0 times')]
        for edits, reason in cases:
            with self.subTest(edits=edits):
                with self.assertRaisesRegex(ValueError, reason):
                    specification.project_files(self.root, edits)
        edits = [self.edit('abc', 'A', 'anchors.json'), self.edit('def', 'B', 'anchors.json')]
        self.assertEqual(specification.project_files(self.root, edits), specification.project_files(self.root, edits[::-1]))

    def test_insert_after_append_and_line_endings_have_explicit_semantics(self):
        path = self.root / 'text.json'
        path.write_bytes(b'first\r\nlast\r\n')
        edits = [self.edit('first\r\n', 'middle\r\n', 'text.json', 'insertAfter'),
                 self.edit('last', 'tail\r\n', 'text.json', 'append')]
        self.assertEqual(specification.project_files(self.root, edits)['text.json'], b'first\nmiddle\nlast\ntail\n')
        self.assertEqual(path.read_bytes(), b'first\r\nlast\r\n')

    def test_alias_paths_cannot_bypass_overlap_detection(self):
        target = self.root / 'archive.json'
        target.write_text('one unique anchor')
        try:
            (self.root / 'alias.json').symlink_to(target)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            specification.project_files(self.root, [self.edit('unique', 'a', 'archive.json'),
                                                     self.edit('unique', 'b', 'alias.json')])

    def test_unrelated_files_locks_and_outside_paths_are_not_projection_targets(self):
        (self.root / 'code.py').write_text('unique code')
        for file, anchor in [('code.py', 'unique code'), (specification.LOCK, 'version'), ('../outside.json', 'x')]:
            with self.subTest(file=file):
                with self.assertRaises(ValueError):
                    specification.project(self.root, [self.edit(anchor, 'y', file=file)])

    def test_stale_source_is_labelled_and_projected_identity_uses_current_bytes(self):
        original_lock = (self.root / specification.LOCK).read_bytes()
        self.doc['goal'] = 'A deliberately unlocked revision for size review'
        self.save()
        edits = [self.edit(self.doc['checks'][0]['scope'], 'proposed scope')]
        projected = self.compile('--check', '--packet-edits', self.proposals(edits))
        self.measure(projected)
        self.assertIn('projection source is unlocked or stale', projected.stderr)
        self.assertIn(hashlib.sha256((self.root / specification.SPEC).read_bytes()).hexdigest(), projected.stdout)
        self.assertEqual((self.root / specification.LOCK).read_bytes(), original_lock)
        self.assertEqual(self.compile('--check').returncode, 1)

    def test_projection_cannot_emit_or_certify_a_launch_artifact(self):
        edits = self.proposals([])
        for flags in ((), ('--out', 'never.graph.js'), ('--check', '--out', 'never.graph.js'),
                      ('--check', '--check-script', 'never.graph.js')):
            with self.subTest(flags=flags):
                self.assertEqual(self.compile('--packet-edits', edits, *flags).returncode, 2)
        self.assertFalse((self.root / 'never.graph.js').exists())
        with self.assertRaisesRegex(ValueError, 'cannot be treated as a locked packet'):
            specification.load(self.root, snapshots={})

    def test_projection_reports_exact_limit_and_low_margin(self):
        edits = self.proposals([self.edit(self.doc['checks'][0]['scope'], 'expanded text' * 100)])
        size, _, _ = self.measure(self.compile('--check', '--packet-edits', edits))
        rejected = self.compile('--check', '--packet-edits', edits, '--max-script-bytes', str(size - 1))
        self.assertEqual(rejected.returncode, 1)
        self.assertIn(f'projected script {size:,} bytes, headroom -1', rejected.stderr)
        boundary = self.compile('--check', '--packet-edits', edits, '--max-script-bytes', str(size))
        self.assertEqual(self.measure(boundary), (size, 0, size))
        self.assertIn('below 16,384-byte margin', boundary.stderr)

    def test_graph_text_proposals_are_measured_but_cannot_redirect_the_packet(self):
        edits = [self.edit('Implement the requirements.', 'Implement the expanded scoped requirements.', file='WORK.md')]
        self.measure(self.compile('--check', '--packet-edits', self.proposals(edits)))
        edits = [self.edit('SPEC: .fluxpoint-spec.json', 'SPEC: different.json', file='WORK.md')]
        result = self.compile('--check', '--packet-edits', self.proposals(edits))
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot redirect', result.stderr)

    def test_consolidated_graph_edits_can_be_measured_for_every_graph_sharing_a_packet(self):
        peer = self.root / 'GRAPH.peer.md'
        peer.write_bytes((self.root / 'WORK.md').read_bytes())
        edits = self.proposals([self.edit('Implement the requirements.', 'Implement the expanded requirements.', file='GRAPH.peer.md')])
        self.measure(self.compile('--check', '--packet-edits', edits))
        result = subprocess.run([sys.executable, str(PLUGIN / 'scripts/compile-graph.py'), 'GRAPH.peer.md',
                                 '--check', '--packet-edits', edits], cwd=self.root, capture_output=True,
                                text=True, encoding='utf-8')
        self.measure(result)
        redirected = [self.edit('SPEC: .fluxpoint-spec.json', 'SPEC: other.json', file='GRAPH.peer.md')]
        result = self.compile('--check', '--packet-edits', self.proposals(redirected))
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot redirect graph GRAPH.peer.md', result.stderr)

    def test_schema_carries_only_optional_structured_proposals(self):
        schema = json.loads((PLUGIN / 'contracts/SliceV1.schema.json').read_text())
        self.assertNotIn('packetEdits', schema['required'])
        item = schema['properties']['packetEdits']['items']
        self.assertEqual(set(item['required']), {'file', 'anchor', 'op', 'text', 'reason'})
        self.assertEqual(set(item['properties']['op']['enum']), {'replace', 'append', 'insertAfter'})

    def test_prose_commit_pin_matches_exact_key_words_connectors_and_prefixes(self):
        self.ir['argDefaults'] = {'packetCommit': 'a' * 40}
        for text in ('packetCommit is bbbbbbb', 'packet commit at `bbbbbbb`', '`packetCommit` = bbbbbbb',
                     'packetCommit: `bbbbbbb`', 'packetCommit `bbbbbbb`'):
            with self.subTest(text=text):
                self.graph('## Constraints\n' + text + '\n')
                result = self.compile('--check')
                self.assertEqual(result.returncode, 1)
                self.assertIn('prose line 3: packetCommit pin bbbbbbb differs from argDefault', result.stderr)
        for value, text in [('a' * 40, 'packet commit is aaaaaaa'), ('a' * 7, 'packetCommit at ' + 'a' * 40)]:
            self.ir['argDefaults']['packetCommit'] = value
            self.graph('## Constraints\n' + text + '\n')
            self.assertEqual(self.compile('--check').returncode, 0)

    def test_historical_fences_and_unrelated_hex_literals_do_not_create_pin_findings(self):
        self.ir['argDefaults'] = {'packetCommit': 'a' * 40, 'meaning': 'not-a-commit'}
        self.graph('## Constraints\nAn unrelated commit is bbbbbbb; meaning is ccccccc.\n'
                   '```text\npacketCommit is bbbbbbb\n```\n'
                   '~~~text\npacketCommit is bbbbbbb\n~~~\n'
                   '## Evidence\npacket commit was bbbbbbb\npacketCommit is bbbbbbb\n'
                   '### Previous round\npacketCommit: bbbbbbb\n'
                   '## Notes\npacketCommit is aaaaaaa\n')
        self.assertEqual(self.compile('--check').returncode, 0)
        self.graph('## Evidence\npacketCommit is bbbbbbb\n## Constraints\npacketCommit is ccccccc\n')
        result = self.compile('--check')
        self.assertEqual(result.returncode, 1)
        self.assertIn('prose line 5', result.stderr)
        self.graph('## Evidence requirements\npacketCommit is bbbbbbb\n')
        self.assertEqual(self.compile('--check').returncode, 1)


if __name__ == '__main__':
    unittest.main()
