"""Persisted paired rows, not declared counts, determine campaign completion."""
import copy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

from experiments.pdnc_results import get_pdnc_result, print_goal13_summary
from tests import test_llm_campaign_ownership as campaign


class PdncResultTests(unittest.TestCase):
    def setUp(self):
        self.fixture = {'book': 'Known', 'entries': [
            {'id': 'a', 'expected_speaker': 'Alice'}, {'id': 'b', 'expected_speaker': 'Bob'},
            {'id': 'c', 'expected_speaker': 'Alice'}]}
        self.document = {'Known': {arm: {'n': 3, 'correct': correct, 'rows': [
            {'id': entry['id'], 'expected': entry['expected_speaker'],
             'predicted': entry['expected_speaker'] if index < correct else None,
             'correct': index < correct}
            for index, entry in enumerate(self.fixture['entries'])]}
            for arm, correct in (('base', 1), ('lora', 2))}}

    def test_known_paired_counts_are_derived_without_mutating_source(self):
        before = copy.deepcopy(self.document)
        self.assertEqual({'book': 'Known', 'n': 3, 'base': 1, 'lora': 2},
                         get_pdnc_result(self.document, self.fixture, 100000))
        self.assertEqual(before, self.document)

    def test_declared_counts_and_paired_row_identity_cannot_hide_truncation(self):
        for defect in ('sparse', 'missing-arm', 'legacy-counts-only', 'wrong-book',
                       'duplicate', 'wrong-id', 'wrong-speaker', 'bool-n',
                       'inflated-correct', 'string-correct', 'no-prediction-field'):
            with self.subTest(defect=defect):
                doc = copy.deepcopy(self.document)
                base, lora = doc['Known']['base'], doc['Known']['lora']
                if defect == 'sparse': lora['rows'].pop()
                elif defect == 'missing-arm': del doc['Known']['lora']
                elif defect == 'legacy-counts-only': del base['rows']
                elif defect == 'wrong-book': doc['Other'] = doc.pop('Known')
                elif defect == 'duplicate': lora['rows'][2] = copy.deepcopy(lora['rows'][0])
                elif defect == 'wrong-id': lora['rows'][2]['id'] = 'unknown'
                elif defect == 'wrong-speaker': lora['rows'][2]['expected'] = 'Other'
                elif defect == 'bool-n': base['n'] = True
                elif defect == 'inflated-correct': base['correct'] = 3
                elif defect == 'string-correct': base['rows'][0]['correct'] = 'false'
                elif defect == 'no-prediction-field': del lora['rows'][0]['predicted']
                with self.assertRaises(ValueError):
                    get_pdnc_result(doc, self.fixture, 100000)

    def test_current_limit_requires_exact_sample_not_another_run_size(self):
        with self.assertRaisesRegex(ValueError, 'requested sample'):
            get_pdnc_result(self.document, self.fixture, 2)

    def test_known_row_counts_produce_the_expected_percentages_and_gap(self):
        measured = get_pdnc_result(self.document, self.fixture, 100000)
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            print_goal13_summary([{'half': 'heldout', **measured},
                                 {'half': 'development', **measured, 'lora': 1}])
        self.assertIn('33.3%', report.getvalue())
        self.assertIn('66.7%', report.getvalue())
        self.assertIn('DEV MINUS HELD-OUT (lora arm): -33.3 points', report.getvalue())


class PdncChainResumeTests(unittest.TestCase):
    setUp = campaign.LlmCampaignOwnershipTests.setUp
    setUpFixture = campaign.LlmCampaignOwnershipTests.setUpFixture
    launch = campaign.LlmCampaignOwnershipTests.launch
    stop_fixture_process = campaign.LlmCampaignOwnershipTests.stop_fixture_process
    cleanup_server = campaign.LlmCampaignOwnershipTests.cleanup_server

    def test_corrupt_cached_json_is_regenerated_as_complete_rows(self):
        path = self.root / 'ab_test_runtime/experiments/pdnc_eval__goal13_heldout_emma.json'
        path.write_text('{"emma":')
        process = self.launch(campaign.CHAINS[0])
        try:
            stdout, stderr = process.communicate(timeout=15)
        finally:
            self.cleanup_server()
        self.assertEqual(0, process.returncode, stdout + stderr)
        self.assertNotIn('SKIP goal13_heldout_emma', stdout)
        self.assertEqual(8, len((self.root / 'evaluators').read_text().splitlines()))
        doc = json.loads(path.read_text())
        self.assertEqual(1, len(doc['emma']['base']['rows']))
        self.assertEqual(1, len(doc['emma']['lora']['rows']))
        self.assertIn('9/9 stages ok', stdout)
        self.assertIn('DEV MINUS HELD-OUT', stdout)

    def test_summary_refuses_declared_n_that_disagrees_with_rows_without_percentages(self):
        for stem in campaign.STEMS:
            half = 'heldout' if stem in campaign.STEMS[:5] else 'development'
            row = {'id': stem+'-0', 'expected': 'Alice', 'predicted': 'Alice', 'correct': True}
            doc = {stem: {arm: {'n': 1, 'correct': 1, 'rows': [row]} for arm in ('base', 'lora')}}
            if stem == 'emma': doc[stem]['base']['n'] = 1000
            (self.root / f'ab_test_runtime/experiments/pdnc_eval__goal13_{half}_{stem}.json').write_text(json.dumps(doc))
        script = self.root / 'app/experiments/pdnc_results.py'
        result = subprocess.run([sys.executable, str(script), 'summary', '--repo', str(self.root),
            '--runtime', str(self.root / 'ab_test_runtime'), '--heldout', ' '.join(campaign.STEMS[:5]),
            '--development', ' '.join(campaign.STEMS[5:]), '--limit', '100000'],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('declared n differs', result.stderr)
        self.assertNotIn('%', result.stdout)
        self.assertNotIn('DEV MINUS', result.stdout)

    def test_missing_comparison_inputs_make_actual_campaign_summary_fail(self):
        evaluator = self.root / 'app/experiments/pdnc_eval.py'
        evaluator.write_text('raise SystemExit(0)\n')
        process = self.launch(campaign.CHAINS[0])
        try:
            stdout, stderr = process.communicate(timeout=15)
        finally:
            self.cleanup_server()
        self.assertEqual(1, process.returncode, stdout + stderr)
        self.assertIn('goal13_summary = failed:1', stdout)
        self.assertNotIn('DEV MINUS', stdout)
        self.assertIn('8/9 stages ok', stdout)
