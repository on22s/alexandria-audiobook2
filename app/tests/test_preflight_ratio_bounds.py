import ast
import copy
import difflib
from pathlib import Path
import random
import unittest
from unittest.mock import patch
import script_preflight as p


class PreflightRatioBoundTests(unittest.TestCase):
    def test_disjoint_native_audit_skips_ratios_and_reuses_exact_scan(self):
        entries = [{'text': ' '.join(f'p{i}q{j}' for j in range(30)), 'speaker': 'NARRATOR'} for i in range(120)]
        original = copy.deepcopy(entries)
        ratio = difflib.SequenceMatcher.ratio
        calls = []
        def measured(matcher):
            calls.append(1)
            return ratio(matcher)
        with patch.object(difflib.SequenceMatcher, 'ratio', new=measured), patch.object(p, 'find_adjacent_duplicate_blocks', wraps=p.find_adjacent_duplicate_blocks) as exact:
            report = p.audit_script(entries)
        self.assertEqual(1, exact.call_count)
        self.assertEqual(0, len(calls))
        self.assertNotIn('adjacent_near_duplicate', {row['code'] for row in report['findings']})
        self.assertEqual(original, entries)

    def test_threshold_equal_repeated_and_reordered_tokens_keep_exact_findings(self):
        first = 'alpha beta gamma delta epsilon zeta eta theta iota kappa'
        second = 'alpha beta gamma delta epsilon zeta eta theta iota changed'
        found = p.find_adjacent_near_duplicate_entries([first, second], '', .9)
        self.assertEqual(1, len(found))
        self.assertEqual(.9, found[0]['details']['similarity'])
        self.assertEqual('manual_review', found[0]['severity'])
        repeated = 'red red red blue green gold silver violet white black'
        reordered = 'black white violet silver gold green blue red red red'
        self.assertEqual([], p.find_adjacent_near_duplicate_entries([repeated, reordered], '', .9))

    def test_supplied_exact_findings_are_readonly_and_default_result_matches(self):
        texts = [' '.join(f'word{i}' for i in range(30))] * 2
        exact = p.find_adjacent_duplicate_blocks(texts, '')
        original = copy.deepcopy(exact)
        expected = p.find_adjacent_near_duplicate_entries(texts, '')
        with patch.object(p, 'find_adjacent_duplicate_blocks', side_effect=AssertionError('must reuse supplied exact evidence')):
            current = p.find_adjacent_near_duplicate_entries(texts, '', exact_findings=exact)
        self.assertEqual(expected, current)
        self.assertEqual(original, exact)

    def test_seeded_bounds_preserve_full_ratio_verdicts(self):
        rng = random.Random(716)
        vocab = 'amber copper velvet quartz forest river ocean lantern'.split()
        for index in range(160):
            first = [rng.choice(vocab) for _ in range(12)]
            second = list(first)
            for _ in range(index % 5):
                second[rng.randrange(12)] = rng.choice(vocab)
            if index % 4 == 0:
                rng.shuffle(second)
            texts = [' '.join(first), ' '.join(second)]
            exact = p.find_adjacent_duplicate_blocks(texts, '')
            occupied = {number for row in exact for number in row['entry_numbers']}
            ratio = difflib.SequenceMatcher(None, first, second, autojunk=False).ratio()
            expected = ratio >= .9 and not occupied
            with self.subTest(index=index):
                findings = p.find_adjacent_near_duplicate_entries(texts, '', exact_findings=exact)
                self.assertEqual(expected, bool(findings))
                if findings:
                    self.assertEqual(round(ratio, 4), findings[0]['details']['similarity'])
