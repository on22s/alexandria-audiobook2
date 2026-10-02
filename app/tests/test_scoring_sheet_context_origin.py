"""Displayed context cannot silently depend on the first model's segmentation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import build_scoring_sheet as sheet


class ScoringSheetContextTests(unittest.TestCase):
    def runs(self):
        return {'A': [{'text': 'Alice entered.', 'speaker': 'NARRATOR'},
                      {'text': 'Hello there.', 'speaker': 'ALICE'},
                      {'text': 'Alice left.', 'speaker': 'NARRATOR'}],
                'B': [{'text': 'Bob entered.', 'speaker': 'NARRATOR'},
                      {'text': 'Hello there.', 'speaker': 'BOB'},
                      {'text': 'Bob left.', 'speaker': 'NARRATOR'}]}

    def test_disagreeing_context_is_model_specific_not_arbitrarily_unified(self):
        runs = self.runs()
        before = copy.deepcopy(runs)
        row = sheet.build_sheet(runs, window=1)[0]
        self.assertEqual([], row['context_before'])
        self.assertEqual([], row['context_after'])
        self.assertEqual('model_specific', row['context_origin'])
        self.assertTrue(row['context_conflict'])
        self.assertEqual(['Alice entered.'], row['context_by_model']['A']['before'])
        self.assertEqual(['Bob entered.'], row['context_by_model']['B']['before'])
        self.assertEqual(before, runs)

    def test_authoritative_context_is_independent_of_model_names(self):
        source = [{'text': 'Carol entered.'}, {'text': 'Hello there.'}, {'text': 'Carol left.'}]
        runs = self.runs()
        before = copy.deepcopy(source)
        for models in (runs, {'Z': runs['A'], 'AA': runs['B']}):
            row = sheet.build_sheet(models, window=1, source_entries=source)[0]
            self.assertEqual(['Carol entered.'], row['context_before'])
            self.assertEqual(['Carol left.'], row['context_after'])
            self.assertEqual('source_entries', row['context_origin'])
            self.assertTrue(row['context_conflict'])
        self.assertEqual(before, source)

    def test_ambiguous_or_missing_source_match_refuses_instead_of_guessing(self):
        for source in ([{'text': 'Unrelated.'}], [{'text': 'Hello there.'}, {'text': 'Hello there.'}]):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'unique source'):
                sheet.build_sheet(self.runs(), source_entries=source)

    def test_actual_cli_writes_authoritative_context_from_native_entries_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for model, entries in self.runs().items():
                path = root / model / 'book' / 'result.json.threepass_checkpoint.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'entries': entries}))
            source = root / 'source.json'
            source.write_text(json.dumps([{'text': 'Carol entered.'}, {'text': 'Hello there.'}, {'text': 'Carol left.'}]))
            original = source.read_bytes()
            output = root / 'sheet.json'
            with patch('sys.argv', ['build_scoring_sheet.py', tmp, 'book', '--window', '1',
                                   '--source-entries', str(source), '--output', str(output)]):
                sheet.main()
            row = json.loads(output.read_text())['rows'][0]
            self.assertEqual(['Carol entered.'], row['context_before'])
            self.assertEqual(['Carol left.'], row['context_after'])
            self.assertEqual({'A': 'ALICE', 'B': 'BOB'}, row['answers'])
            self.assertEqual(original, source.read_bytes())
            before_output = output.read_bytes()
            source.write_text(json.dumps([{'text': 'Hello there.'}, {'text': 'Hello there.'}]))
            with patch('sys.argv', ['build_scoring_sheet.py', tmp, 'book', '--source-entries',
                                   str(source), '--output', str(output)]), self.assertRaises(ValueError):
                sheet.main()
            self.assertEqual(before_output, output.read_bytes())
