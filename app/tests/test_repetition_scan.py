"""Repeated-run planning must stay bounded and retain source/evidence behavior."""
import unittest
from unittest.mock import patch

import source_normalization as normalization


class RepetitionScanTests(unittest.TestCase):
    def test_repeated_suffix_work_is_bounded(self):
        calls = []
        class CountedText(str):
            def casefold(self):
                calls.append(None)
                return super().casefold()
        actual = normalization._WORD_RE
        class Match:
            def __init__(self, match):
                self.match = match
            def group(self, *args):
                return CountedText(self.match.group(*args))
            def start(self):
                return self.match.start()
            def end(self):
                return self.match.end()
        class Words:
            def finditer(self, text):
                return (Match(match) for match in actual.finditer(text))
        text = 'alpha beta ' * 128
        with patch.object(normalization, '_WORD_RE', Words()):
            output, evidence = normalization.normalize_extreme_phrase_repetitions(text)
        self.assertEqual('alpha beta alpha beta alpha beta… ', output)
        self.assertEqual(128, evidence[0]['repetitions'])
        self.assertLessEqual(len(calls), 256 * 20, 'token suffixes repeatedly reconsidered')

    def test_phrase_widths_case_and_whitespace_keep_exact_receipts(self):
        units = ('alpha', 'alpha beta', 'alpha beta gamma',
                 'alpha beta gamma delta', 'alpha beta gamma delta epsilon',
                 "can't stop", 'alpha, beta')
        for unit in units:
            for count in (19, 20, 21, 43):
                for separator in (' ', '\n\t'):
                    with self.subTest(unit=unit, count=count, separator=separator):
                        source = 'Intro.\n' + separator.join([unit] * count) + '! End.'
                        output, evidence = normalization.normalize_extreme_phrase_repetitions(source)
                        if count < 20:
                            self.assertEqual((source, []), (output, evidence))
                        else:
                            replacement = separator.join([unit] * 3) + '…'
                            self.assertEqual('Intro.\n' + replacement + '! End.', output)
                            self.assertEqual([{'offset': 7, 'line': 2, 'column': 1,
                                'before': separator.join([unit] * count), 'after': replacement,
                                'rule': 'extreme_phrase_repetition', 'phrase_words': len(unit.split()),
                                'repetitions': count}], evidence)
        source = 'Yes YES yes ' * 10
        self.assertEqual('Yes YES yes… ', normalization.normalize_extreme_phrase_repetitions(source)[0])

    def test_punctuation_breaks_runs_and_disjoint_runs_keep_offsets(self):
        source = ' '.join(['alpha'] * 19) + '. ' + ' '.join(['alpha'] * 19)
        self.assertEqual((source, []), normalization.normalize_extreme_phrase_repetitions(source))
        first, second = ' '.join(['alpha beta'] * 25), ' '.join(['gamma'] * 30)
        source = first + '.\n' + second + '!'
        output, evidence = normalization.normalize_extreme_phrase_repetitions(source)
        self.assertEqual('alpha beta alpha beta alpha beta….\ngamma gamma gamma…!', output)
        self.assertEqual([0, len(first) + 2], [row['offset'] for row in evidence])
        self.assertEqual([1, 2], [row['line'] for row in evidence])
        self.assertEqual([first, second], [row['before'] for row in evidence])

    def test_novel_sized_alternating_run_finishes_with_full_evidence(self):
        source = 'alpha beta ' * 50000
        output, evidence = normalization.normalize_extreme_phrase_repetitions(source)
        self.assertEqual('alpha beta alpha beta alpha beta… ', output)
        self.assertEqual(50000, evidence[0]['repetitions'])
        self.assertEqual(source.rstrip(), evidence[0]['before'])
