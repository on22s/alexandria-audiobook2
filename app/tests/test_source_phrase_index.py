"""Indexed source evidence keeps literal counts and duplicate verdicts."""
import random
import unittest
from unittest.mock import patch
import script_preflight as preflight


class SourcePhraseIndexTests(unittest.TestCase):
    def test_known_overlap_boundaries_unicode_and_whitespace_cases(self):
        cases = [('a a a', 'a a', 1), ('a a a a', 'a a', 2),
                 ('alpha alphabet alpha', 'alpha', 2), ('a\tb', 'a b', 0),
                 ('a  b', 'a  b', 1), ('東京 東京', '東京', 2),
                 ('A a', 'a', 1), ('a', '', 0), ('a b', 'a ', 0)]
        for source, phrase, expected in cases:
            with self.subTest(source=source, phrase=phrase):
                self.assertEqual(expected, preflight.get_source_phrase_occurrences(source, phrase))
                self.assertEqual(expected, preflight.get_source_phrase_counter(source)(phrase))

    def test_random_queries_match_original_literal_regex(self):
        rng = random.Random(722)
        for _ in range(100):
            source = ' '.join(rng.choice(['a', 'aa', 'b', 'é', '東京'])
                              for _ in range(rng.randrange(40)))
            counter = preflight.get_source_phrase_counter(source)
            for _ in range(10):
                phrase = ' '.join(rng.choice(['a', 'aa', 'b', 'é', '東京', 'missing'])
                                  for _ in range(rng.randint(1, 6)))
                self.assertEqual(preflight.get_source_phrase_occurrences(source, phrase), counter(phrase))

    def test_index_is_lazy_reused_and_bound_to_each_source(self):
        actual = preflight.re.finditer
        with patch.object(preflight.re, 'finditer', wraps=actual) as finditer:
            first = preflight.get_source_phrase_counter('a a b')
            finditer.assert_not_called()
            self.assertEqual(2, first('a'))
            self.assertEqual(1, first('b'))
            self.assertEqual(1, finditer.call_count)
            second = preflight.get_source_phrase_counter('a b b')
            self.assertEqual(2, second('b'))
            self.assertEqual(1, first('b'))
            self.assertEqual(2, finditer.call_count)

    def test_whole_audit_findings_equal_regex_for_faithful_and_unsupported_repeats(self):
        pair = ['The morning air was sharp and cold.', 'Haruhiro rubbed his eyes and sat up.']
        entries = [{'speaker': 'NARRATOR', 'text': text, 'instruct': 'Read naturally.'}
                   for text in pair * 2]
        for repetitions in (0, 1, 2):
            source = '\n'.join(pair * repetitions)
            indexed = preflight.audit_script(entries, source)
            with patch.object(preflight, 'get_source_phrase_counter',
                              side_effect=lambda src: lambda phrase: preflight.get_source_phrase_occurrences(src, phrase)):
                baseline = preflight.audit_script(entries, source)
            self.assertEqual(baseline, indexed)
            finding = next(f for f in indexed['findings'] if f.get('details', {}).get('block_size') == 2)
            self.assertEqual(repetitions, finding['details']['source_occurrences'])
