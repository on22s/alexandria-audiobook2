"""Exact-source evidence and fresh-output regressions; no model or GPU work."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from experiments import roster_source_evidence as r
from source_evidence import validate_source_evidence_packet


def claim(quote, start=None, end=None):
    return {'source_quote': quote, 'start_char': start, 'end_char': end}


def packet(source, evidence):
    return {'source_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
            'entries': [{'id': 'character_' + 'a' * 20, 'canonical_name': 'ALICE', 'evidence': evidence}]}


class RosterSourceEvidenceTests(unittest.TestCase):
    def test_unicode_character_offsets_and_crlf_are_exact_without_mutating_input(self):
        source = '前😀言\r\n“Wait here,” Alice said.'
        start = source.index('Alice'); original = packet(source, [claim('Alice', start, start + 5)])
        saved = copy.deepcopy(original); report = r.get_source_evidence_review(source, original)
        self.assertEqual(report['entries'][0]['evidence'][0]['status'], 'exact')
        self.assertEqual(report['counts']['exact'], 1)
        self.assertEqual(report['source_characters'], len(source))
        self.assertEqual(original, saved)
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            r.get_source_evidence_review(source.replace('\r\n', '\n'), original)

    def test_unanchored_matches_distinguish_unique_ambiguous_and_unsupported(self):
        source = 'Alice waited. Bob waited. Alice left.'
        report = r.get_source_evidence_review(source, packet(source,
            [claim('Bob'), claim('Alice'), claim('Nobody')]))
        evidence = report['entries'][0]['evidence']
        self.assertEqual([row['status'] for row in evidence], ['unique_candidate', 'ambiguous', 'unsupported'])
        self.assertEqual(evidence[0]['candidate_locations'], [{'start_char': 14, 'end_char': 17}])
        self.assertEqual(len(evidence[1]['candidate_locations']), 2)
        self.assertEqual(evidence[2]['candidate_locations'], [])

    def test_repeated_quote_requires_its_exact_anchor_and_overlaps_are_ambiguous(self):
        source = 'aaaa'
        report = r.get_source_evidence_review(source, packet(source, [claim('aaa'), claim('aaa', 1, 4)]))
        first, second = report['entries'][0]['evidence']
        self.assertEqual(first['status'], 'ambiguous')
        self.assertEqual(first['candidate_locations'], [{'start_char': 0, 'end_char': 3}, {'start_char': 1, 'end_char': 4}])
        self.assertEqual(second['status'], 'exact')

    def test_wrong_offsets_never_silently_relocate_a_quote(self):
        source = 'Alice waited.'; original = packet(source, [claim('Alice', 1, 6)])
        row = r.get_source_evidence_review(source, original)['entries'][0]['evidence'][0]
        self.assertEqual(row['status'], 'offset_mismatch')
        self.assertEqual((row['start_char'], row['end_char']), (1, 6))
        self.assertEqual(row['candidate_locations'], [{'start_char': 0, 'end_char': 5}])

    def test_absent_quote_with_offsets_is_unsupported_rather_than_relocatable(self):
        source = 'Alice waited.'
        row = r.get_source_evidence_review(source, packet(source, [claim('Nobody', 0, 6)]))['entries'][0]['evidence'][0]
        self.assertEqual(row['status'], 'unsupported')
        self.assertEqual(row['candidate_locations'], [])
        self.assertEqual((row['start_char'], row['end_char']), (0, 6))

    def test_invalid_unicode_is_refused_before_source_searches(self):
        class NoScan(str):
            def find(self, *args):
                raise AssertionError('Invalid packet must refuse before source scans')
        source = NoScan('Alice waited.'); valid = packet(source, [claim('Alice')])
        for field in ('canonical_name', 'source_quote'):
            invalid = copy.deepcopy(valid)
            target = invalid['entries'][0] if field == 'canonical_name' else invalid['entries'][0]['evidence'][0]
            target[field] = chr(0xD800)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'valid UTF-8'):
                r.get_source_evidence_review(source, invalid)
        with self.assertRaisesRegex(ValueError, 'valid UTF-8'):
            r.get_source_evidence_review(chr(0xD800), valid)

    def test_source_evidence_is_never_an_identity_or_alias_approval(self):
        source = 'Bob waited.'
        report = r.get_source_evidence_review(source, packet(source, [claim('Bob', 0, 3)]))
        row = report['entries'][0]
        self.assertEqual(row['canonical_name'], 'ALICE')
        self.assertEqual(row['evidence'][0]['status'], 'exact')
        self.assertEqual(row['identity_status'], 'not_evaluated')
        self.assertIn('not approved', report['qualification'])

    def test_malformed_structures_ids_and_numeric_ranges_are_rejected(self):
        source = 'Alice waited.'; valid = packet(source, [claim('Alice', 0, 5)])
        bad = [None, [], {**valid, 'extra': True}, {**valid, 'entries': []},
               {**valid, 'entries': valid['entries'] * 2}]
        for value in (None, '', 1, 'character_wrong'):
            item = copy.deepcopy(valid); item['entries'][0]['id'] = value; bad.append(item)
        for start, end in ((True, 5), (0, False), (0.0, 5), (0, float('nan')), (-1, 5),
                           (5, 5), (0, 100), (None, 5), (0, None)):
            bad.append(packet(source, [claim('Alice', start, end)]))
        item = copy.deepcopy(valid); item['entries'][0]['evidence'][0]['domain'] = 'script_entry'; bad.append(item)
        for item in bad:
            with self.subTest(value=item), self.assertRaises(ValueError):
                r.get_source_evidence_review(source, item)

    def test_source_entry_evidence_and_quote_limits_refuse_before_scanning(self):
        source = 'Alice'
        bad = [{**packet(source, [claim('Alice')]), 'entries': [{}] * (r.MAX_ENTRIES + 1)},
               packet(source, [claim('Alice')] * (r.MAX_EVIDENCE + 1)),
               packet(source, [claim('x' * (r.MAX_QUOTE_CHARS + 1))])]
        for item in bad:
            with self.assertRaises(ValueError):
                r.get_source_evidence_review(source, item)
        with self.assertRaises(ValueError):
            r.get_source_evidence_review('x' * (r.MAX_SOURCE_CHARS + 1), {})

    def test_stale_source_is_refused_even_when_its_quote_survives(self):
        source = 'Alice waited.'; original = packet(source, [claim('Alice', 0, 5)])
        for revision in ('Preface. ' + source, source + ' Bob arrived.'):
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                r.get_source_evidence_review(revision, original)


class HistoricalEvidenceBoundaryTests(unittest.TestCase):
    def test_historical_validation_checks_ranges_without_relocating_quotes(self):
        source = 'Alice waited.'
        value = packet(source, [claim('Nobody', 0, 6)])
        before = copy.deepcopy(value)
        # Structural validity is deliberately not an exact-quote or identity claim.
        validate_source_evidence_packet(len(source), value['source_sha256'], value)
        self.assertEqual(value, before)
        value['entries'][0]['evidence'][0]['end_char'] = len(source) + 1
        with self.assertRaisesRegex(ValueError, 'range'):
            validate_source_evidence_packet(len(source), value['source_sha256'], value)

    def test_malformed_recorded_source_coordinates_are_refused(self):
        value = packet('Alice', [claim('Alice', 0, 5)])
        for length in (True, 5.0, 0, -1, r.MAX_SOURCE_CHARS + 1):
            with self.subTest(length=length), self.assertRaises(ValueError):
                validate_source_evidence_packet(length, value['source_sha256'], value)
        for digest in (None, 1, 'a' * 63, 'A' * 64, 'z' * 64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                validate_source_evidence_packet(5, digest, {**value, 'source_sha256': digest})
        with self.assertRaisesRegex(ValueError, 'fingerprint differs'):
            validate_source_evidence_packet(5, 'b' * 64, value)


class RosterSourceEvidenceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'book.txt'; self.imported = self.root / 'claims.json'
        self.source.write_bytes('Alice waited.\r\n'.encode('utf-8'))
        self.imported.write_text(json.dumps(packet(self.source.read_bytes().decode('utf-8'), [claim('Alice', 0, 5)])))
        self.out = self.root / 'evaluation'

    def run_cli(self):
        return subprocess.run([sys.executable, '-m', 'experiments.roster_source_evidence',
            '--source', str(self.source), '--packet', str(self.imported), '--out-dir', str(self.out)],
            cwd=Path(r.__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)

    def test_real_cli_preserves_inputs_and_binds_report_to_captured_bytes(self):
        before = {p: p.read_bytes() for p in (self.source, self.imported)}
        result = self.run_cli(); self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.out / 'report.json').read_text())
        self.assertEqual(report['counts']['exact'], 1)
        self.assertEqual(report['provenance']['script'], 'roster_source_evidence.py')
        self.assertIn('git', report['provenance'])
        shared = Path(r.__file__).resolve().parents[1] / 'source_evidence.py'
        self.assertEqual(report['implementation'], {
            'app/source_evidence.py': hashlib.sha256(shared.read_bytes()).hexdigest()})
        for key, path in [('source', self.source), ('packet', self.imported)]:
            self.assertEqual(path.read_bytes(), before[path])
            self.assertEqual(report['inputs'][key]['sha256'], hashlib.sha256(before[path]).hexdigest())
        if os.name != 'nt':
            self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)

    def test_existing_output_and_invalid_input_never_overwrite_recoverable_files(self):
        self.out.mkdir(); kept = self.out / 'report.json'; kept.write_bytes(b'keep')
        result = self.run_cli(); self.assertEqual(result.returncode, 2); self.assertEqual(kept.read_bytes(), b'keep')
        self.out = self.root / 'new-output'
        self.imported.write_text('{"entries":NaN}')
        result = self.run_cli(); self.assertEqual(result.returncode, 2); self.assertFalse(self.out.exists())

    def test_escaped_invalid_unicode_is_a_clear_cli_refusal_without_output(self):
        value = packet(self.source.read_bytes().decode('utf-8'), [claim(chr(0xD800))])
        self.imported.write_text(json.dumps(value))
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn('Evidence quote must be valid UTF-8 text', result.stderr)
        self.assertNotIn('evaluated', result.stdout)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
