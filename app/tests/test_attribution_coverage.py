"""Sparse alignment must not win by hiding unaligned gold rows."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import attribution_accuracy as accuracy
import compare_attribution_arms as comparison


class AttributionCoverageTests(unittest.TestCase):
    def fixture(self, aligned=100, correct=90):
        gold = {'entries': [{'id': str(i), 'entry_index': i,
                            'line': f'Unique line {i}.', 'expected_speaker': 'ALICE'}
                           for i in range(100)]}
        named = [{'text': f'Unique line {i}.', 'speaker': 'ALICE' if i < correct else 'BOB'}
                 for i in range(aligned)]
        return gold, named

    def test_sparse_perfect_has_lower_end_to_end_accuracy_and_is_ineligible(self):
        gold, sparse = self.fixture(1, 1)
        _, complete = self.fixture()
        low = accuracy.summarize(accuracy.score_run(sparse, gold))
        high = accuracy.summarize(accuracy.score_run(complete, gold))
        self.assertEqual(1, low['accuracy'])
        self.assertEqual(.01, low['alignment_coverage'])
        self.assertEqual(.01, low['end_to_end_accuracy'])
        self.assertFalse(low['comparison_eligible'])
        self.assertEqual(.9, high['end_to_end_accuracy'])
        self.assertTrue(high['comparison_eligible'])

    def test_empty_missing_and_exact_threshold(self):
        self.assertFalse(accuracy.summarize([])['comparison_eligible'])
        gold, _ = self.fixture()
        missing = accuracy.summarize(accuracy.score_run([], gold))
        self.assertEqual(0, missing['end_to_end_accuracy'])
        self.assertEqual(0, missing['alignment_coverage'])
        for coverage, expected in [(0, False), (.7999, False), (.8, True),
                                   (1, True), (float('nan'), False), (float('inf'), False)]:
            with self.subTest(coverage=coverage):
                self.assertEqual(expected, accuracy.is_attribution_comparison_eligible(coverage))

    def test_disputed_rows_are_excluded_from_both_denominators(self):
        gold, named = self.fixture(1, 1)
        for row in gold['entries'][1:]:
            row['disputed'] = True
        stats = accuracy.summarize(accuracy.score_run(named, gold))
        self.assertEqual(1, stats['scored'])
        self.assertEqual(1, stats['alignment_coverage'])
        self.assertEqual(1, stats['end_to_end_accuracy'])
        self.assertTrue(stats['comparison_eligible'])
        self.assertFalse(accuracy.summarize(accuracy.score_run(named, gold, True))['comparison_eligible'])

    def invoke_cli(self, aligned):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gold, current = self.fixture(aligned, aligned)
            _, baseline = self.fixture()
            for name, data in [('gold.json', gold), ('current.json', {'named': current}),
                               ('baseline.json', {'named': baseline})]:
                (root / name).write_text(json.dumps(data))
            argv = ['score', str(root / 'current.json'), '--gold', str(root / 'gold.json'),
                    '--baseline', str(root / 'baseline.json')]
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()) as output:
                accuracy.main()
            return output.getvalue()

    def test_actual_score_cli_withholds_sparse_comparison_but_displays_both_metrics(self):
        output = self.invoke_cli(1)
        self.assertIn('coverage: 1.0%; end-to-end: 1/100 (1.0%)', output)
        self.assertIn('COMPARISON withheld', output)
        self.assertNotIn('DELTA', output)

    def test_actual_score_cli_reports_end_to_end_delta_when_eligible(self):
        output = self.invoke_cli(80)
        self.assertIn('DELTA end-to-end: -10.0%', output)
        self.assertNotIn('COMPARISON withheld', output)

    def test_comparison_artifact_marks_low_coverage_without_dropping_manual_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, sparse = self.fixture(1, 0)
            _, full = self.fixture(100, 100)
            (root / 'a.json').write_text(json.dumps(sparse))
            (root / 'b.json').write_text(json.dumps(full))
            with patch.object(sys, 'argv', ['compare', str(root / 'a.json'), str(root / 'b.json'),
                                           '--output', str(root / 'out.json')]), \
                    contextlib.redirect_stdout(io.StringIO()):
                comparison.main()
            result = json.loads((root / 'out.json').read_text())
            self.assertFalse(result['comparison_eligible'])
            self.assertEqual(.01, result['alignment_coverage'])
            self.assertEqual(1, result['disagreement_count'])
            self.assertEqual('BOB', result['sample'][0]['arm_a'])
