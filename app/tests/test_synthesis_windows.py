"""Prepared spans conserve source and obey explicit limits before synthesis."""
from functools import lru_cache
import hashlib
from itertools import product
import random
import unittest

from synthesis_windows import plan_synthesis_windows


class SynthesisWindowTests(unittest.TestCase):
    def check_plan(self, text, chars, words=None):
        plan = plan_synthesis_windows(text, max_chars=chars, max_words=words)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), plan.text_sha256)
        self.assertEqual(text, ''.join(window.text for window in plan.windows))
        cursor = 0
        for window in plan.windows:
            self.assertEqual(cursor, window.start)
            self.assertEqual(text[window.start:window.end], window.text)
            self.assertLessEqual(len(window.text), chars)
            self.assertTrue(any(c.isalnum() for c in window.text))
            if words is not None:
                self.assertLessEqual(len(window.text.split()), words)
            cursor = window.end
        self.assertEqual(len(text), cursor)
        return plan

    def test_exact_unicode_crlf_and_mixed_sentence_spans(self):
        text = '𠀀東京です。\r\nCafé e\u0301lan arrived!\n\nCan’t stop; another line follows.'
        plan = self.check_plan(text, 30, 5)
        self.assertGreater(len(plan.windows), 1)
        for window in plan.windows[1:]:
            self.assertFalse(window.text.startswith('\u0301'))
            self.assertNotEqual('\r\n', text[window.start - 1:window.start + 1])

    def test_noop_empty_and_complete_final_span(self):
        self.assertEqual((), plan_synthesis_windows('', max_chars=20).windows)
        text = 'First. Second.'
        self.assertEqual([text], [w.text for w in self.check_plan(text, 30).windows])

    def test_punctuation_or_lexical_overflow_is_explicit_without_loss_or_cap_extension(self):
        for text, cap in [('Hello!!!!! World', 5), ('Unbreakable', 4), ('!!!', 10), ('Hi' + ' ' * 40 + 'Bye', 10)]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                plan_synthesis_windows(text, max_chars=cap)
        self.check_plan('Hello!!!!! World', 10)

    def test_explicit_word_budget_including_nonspaced_and_leading_whitespace(self):
        self.check_plan('  Alpha beta gamma delta epsilon.  ', 20, 2)
        self.check_plan('東京へ行きます。もう一度行きます。', 6, 1)
        for text in ("Don't split this.", 'O’Neill arrived.', 'well-known person.'):
            self.check_plan(text, 16, 2)

    def test_many_source_shapes_never_drop_duplicate_or_reorder(self):
        rng = random.Random(1435)
        for _ in range(150):
            pieces = [rng.choice(('Ann', 'Café', '東京', '𠀀', 'one,', 'two.', 'three!')) for _ in range(30)]
            text = rng.choice((' ', '\r\n', '\n\n')).join(pieces)
            self.check_plan(text, 35, 6)

    def test_invalid_budgets_and_bounded_text_are_refused(self):
        for value in (True, False, 0, -1, 1.5, '5', None, 1_000_001):
            with self.subTest(chars=value), self.assertRaises(ValueError):
                plan_synthesis_windows('A line.', max_chars=value)
        for value in (True, 0, -1, '5', 100_001):
            with self.subTest(words=value), self.assertRaises(ValueError):
                plan_synthesis_windows('A line.', max_chars=20, max_words=value)
        with self.assertRaises(ValueError):
            plan_synthesis_windows('A' * 1_000_001, max_chars=20)

    def test_sentence_preferences_only_choose_a_feasible_remaining_partition(self):
        for text, cap in [('First. Second.', 7), ('One. Two!!!!!!!!!', 12),
                          ('One.' + ',' * 40 + ' Two.', 30)]:
            with self.subTest(text=text):
                self.check_plan(text, cap)
        self.assertEqual(['First. ', 'Second.'], [w.text for w in self.check_plan('First. Second.', 7).windows])

    def test_latin_clusters_and_decimal_grouped_numbers_remain_indivisible(self):
        for text in ('e\u0301lan x', 'élan x', '1,234 x', '3.14 x'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                plan_synthesis_windows(text, max_chars=3)
        for text, token, cap in [('3.14 is pi.', '3.14', 7), ('1,234 items arrive.', '1,234', 10)]:
            plan = self.check_plan(text, cap)
            self.assertTrue(any(token in window.text for window in plan.windows))

    def test_closing_quotes_and_blank_lines_stay_with_correct_seam_class(self):
        plan = self.check_plan("'Hello.' Next.", 9)
        self.assertEqual("'Hello.' ", plan.windows[0].text)
        self.assertEqual('sentence', plan.windows[0].boundary)
        plan = self.check_plan('Ann.\n \nBob.', 8)
        self.assertEqual('paragraph', plan.windows[0].boundary)

    def test_small_ascii_partitions_match_independent_exhaustive_feasibility_oracle(self):
        def feasible(text, cap, words):
            @lru_cache(None)
            def visit(start):
                if start == len(text):
                    return True
                for end in range(start + 1, min(len(text), start + cap) + 1):
                    if end < len(text) and text[end - 1].isalnum() and text[end].isalnum():
                        continue
                    part = text[start:end]
                    if (any(c.isalnum() for c in part) and len(part.split()) <= words and visit(end)):
                        return True
                return False
            return visit(0)
        for size in range(1, 6):
            for parts in product(('a', ' ', '.', ','), repeat=size):
                text = ''.join(parts)
                for cap, words in ((2, 1), (3, 2), (4, 1)):
                    try:
                        self.check_plan(text, cap, words)
                        actual = True
                    except ValueError:
                        actual = False
                    self.assertEqual(feasible(text, cap, words), actual, (text, cap, words))

    def test_internal_window_count_is_bounded_before_any_provider_calls(self):
        with self.assertRaisesRegex(ValueError, 'window count limit'):
            plan_synthesis_windows('東' * 1025, max_chars=1)

    def test_attached_directional_and_spaced_closers_do_not_start_a_new_window(self):
        for text, cap, expected_first in [
                ('「東京です。」次です。', 8, '「東京です。」'),
                ('« Bonjour ! » Ensuite.', 15, '« Bonjour ! » '),
                ('Hi. “Next.” Again.', 10, 'Hi. '),
                ('"Bonjour ! " Suite.', 14, '"Bonjour ! " ')]:
            with self.subTest(text=text):
                plan = self.check_plan(text, cap)
                self.assertEqual(expected_first, plan.windows[0].text)
                self.assertTrue(all(not w.text.lstrip().startswith(('」', '』', '»', '”', '’'))
                                    for w in plan.windows[1:]))

    def test_repeated_ascii_openers_in_multiline_dialogue_belong_to_the_next_words(self):
        text = '"First paragraph.\n\n"Second paragraph ends."'
        plan = self.check_plan(text, 26)
        self.assertTrue(any(window.text.startswith('"Second') for window in plan.windows))

    def test_captured_plan_rejects_cumulative_source_cap_before_join_allocation(self):
        import tracemalloc
        from synthesis_windows import SynthesisPlan, SynthesisWindow, get_synthesis_plan_text
        shared = 'a' * 750000
        windows = tuple(SynthesisWindow(i * len(shared), (i + 1) * len(shared), shared,
                                       'end' if i == 3 else 'continuation') for i in range(4))
        malformed = SynthesisPlan('f' * 64, len(shared), None, windows)
        tracemalloc.start()
        try:
            with self.assertRaises(ValueError): get_synthesis_plan_text(malformed)
            _, peak = tracemalloc.get_traced_memory()
        finally: tracemalloc.stop()
        # A joined copy would allocate3MB; input strings were built before tracing.
        self.assertLess(peak, 200000)
