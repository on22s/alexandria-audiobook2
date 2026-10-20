"""Native diagnostic CLI artifacts expose paired cohorts and failed attempts."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import pair_pdnc_pilots, symbolization


class ReportingCoverageTests(unittest.TestCase):
    def test_pdnc_report_prints_shared_accuracies_and_unmatched_and_failed_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'pilot.json'; output = root / 'paired.json'
            source.write_text(json.dumps({'meta': {}, 'summary': {}, 'rows': [
                {'arm': 'baseline', 'id': 'a', 'correct': True},
                {'arm': 'baseline', 'id': 'b', 'correct': True},
                {'arm': 'evidence', 'id': 'b', 'correct': False},
                {'arm': 'evidence', 'id': 'c', 'correct': True},
                {'arm': 'evidence', 'id': 'a', 'correct': None, 'status': 'transport_failed'}]}))
            printed = io.StringIO()
            with patch.object(sys, 'argv', ['paired', '--pilots', str(source), '--out', str(output)]), \
                    contextlib.redirect_stdout(printed):
                pair_pdnc_pilots.main()
            row = json.loads(output.read_text())['pilots'][0]
            cohort = row['paired_cohort']
            self.assertEqual(1, cohort['shared_rows'])
            self.assertEqual((1., 0.), (cohort['first']['shared_accuracy'], cohort['second']['shared_accuracy']))
            self.assertEqual((1, 1), (cohort['first']['unmatched_rows'], cohort['second']['unmatched_rows']))
            self.assertEqual({'baseline': 0, 'evidence': 1}, row['transport_failures'])
            self.assertEqual({'baseline': [], 'evidence': ['a']}, row['transport_failure_ids'])
            self.assertEqual((1., .5), (row['baseline_accuracy'], row['arm_accuracy']))
            self.assertIn('shared n=1', printed.getvalue())
            self.assertIn('arm   0.0%', printed.getvalue())
            self.assertIn('unmatched 1/1', printed.getvalue())
            self.assertIn('transport failures 0/1', printed.getvalue())

    def test_symbolization_cli_serializes_every_attempt_and_reports_transport_dropout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); app = root / 'app'; fixtures = app / 'fixtures'; fixtures.mkdir(parents=True)
            matrix = root / 'matrix'; (matrix / 'inputs').mkdir(parents=True)
            run = matrix / symbolization.INPUT_RUN / 'book'; run.mkdir(parents=True)
            lines = ['The first quote.', 'The second quote.', 'The third quote.', 'The fourth quote.']
            (fixtures / 'attribution_gold_book.json').write_text(json.dumps({
                'entries': [{'line': line, 'expected_speaker': 'ALICE'} for line in lines],
                'roster_additions': {'names': ['ALICE']}}))
            (matrix / 'inputs/book.txt').write_text('ALICE speaks. ' + ' '.join(lines))
            (run / 'result.json.threepass_checkpoint.json').write_text(json.dumps({
                'segmented': [{'type': 'SPOKEN', 'text': line} for line in lines], 'named': []}))
            output = root / 'symbolization.json'; printed = io.StringIO(); calls = []
            def attribute(client, model, frozen, params, roster, **kwargs):
                calls.append(list(roster))
                if len(calls) == 1: raise TimeoutError('fixture transport failure')
                return [{'speaker': roster[0]} for _ in frozen]
            with patch.object(symbolization, 'APP', str(app) + '/'), \
                    patch.object(symbolization, 'M', str(matrix) + '/'), \
                    patch.object(symbolization, 'BATCH', 2), \
                    patch.object(symbolization, 'OpenAI', return_value=object()), \
                    patch.object(symbolization, 'attribute_batch', side_effect=attribute), \
                    patch.object(sys, 'argv', ['symbols', '--book', 'book', '--out', str(output)]), \
                    contextlib.redirect_stdout(printed):
                symbolization.main()
            document = json.loads(output.read_text())
            self.assertEqual(4, len(calls))
            self.assertEqual(8, len(document['rows']))
            failures = [row for row in document['rows'] if row['status'] == 'transport_failed']
            self.assertEqual({('names', '0'), ('names', '1')}, {(row['arm'], row['id']) for row in failures})
            self.assertTrue(all(row['error_type'] == 'TimeoutError' for row in failures))
            self.assertEqual({'requested': 4, 'answered': 2, 'unanswered': 0, 'transport_failed': 2, 'correct': 2},
                             document['summary']['names'])
            self.assertEqual({'2': True, '3': True}, document['answers']['names'])
            self.assertEqual(2, document['paired_cohort']['shared_rows'])
            self.assertEqual((0, 2), (document['paired_cohort']['first']['unmatched_rows'],
                                     document['paired_cohort']['second']['unmatched_rows']))
            self.assertIn('2/4', printed.getvalue())
            self.assertIn('transport failures 2/0', printed.getvalue())

    def test_disjoint_or_all_failed_cohorts_report_no_shared_accuracy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'pilot.json'
            for failed in (False, True):
                rows = [{'arm': arm, 'id': str(index), 'correct': None if failed else True,
                         **({'status': 'transport_failed'} if failed else {})}
                        for index, arm in enumerate(('baseline', 'evidence'))]
                path.write_text(json.dumps({'meta': {}, 'summary': {}, 'rows': rows}))
                summary = pair_pdnc_pilots.score([str(path)])
                row = summary['pilots'][0]
                self.assertEqual(0, row['paired_cohort']['shared_rows'])
                self.assertIsNone(row['paired_cohort']['first']['shared_accuracy'])
                self.assertIsNone(row['paired_cohort']['second']['shared_accuracy'])
                self.assertEqual(1 if failed else 0, row['transport_failures']['baseline'])
                self.assertIn('n/a', pair_pdnc_pilots.render(summary))

    def test_duplicate_or_nonboolean_pilot_rows_refuse_instead_of_changing_denominators(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'pilot.json'
            valid = {'arm': 'baseline', 'id': 'same', 'correct': True}
            for rows in ([valid, dict(valid, correct=False)], [dict(valid, correct='false')]):
                path.write_text(json.dumps({'rows': rows}))
                with self.assertRaises(ValueError):
                    pair_pdnc_pilots.arms_of(str(path))
