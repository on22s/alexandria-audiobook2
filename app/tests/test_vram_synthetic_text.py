"""CPU-only text fixtures preserve random sampling and bound length work."""
import builtins
import random
import unittest
from unittest.mock import patch

import tts_vram_benchmark as benchmark


def previous_make_text(target_chars):
    words = []
    while sum(len(word) + 1 for word in words) < target_chars:
        words.append(random.choice(benchmark._WORDS))
    return " ".join(words)[:target_chars]


class SyntheticTextTests(unittest.TestCase):
    def test_seeded_outputs_and_random_state_match_previous_generator(self):
        saved = random.getstate()
        try:
            for seed in range(10):
                for target in (-1, 0, 1, 5, 40, 100, 500, 10000):
                    with self.subTest(seed=seed, target=target):
                        random.seed(seed)
                        expected = previous_make_text(target)
                        expected_state = random.getstate()
                        random.seed(seed)
                        actual = benchmark._make_text(target)
                        self.assertEqual(expected, actual)
                        self.assertEqual(expected_state, random.getstate())
        finally:
            random.setstate(saved)

    def test_word_length_work_is_linear_for_large_custom_chunks(self):
        # Count actual length inspections, rather than a timing threshold.
        calls = []
        def measure_len(value):
            calls.append(value)
            return builtins.len(value)
        with patch.object(benchmark, 'len', side_effect=measure_len, create=True), \
                patch.object(benchmark.random, 'choice', return_value='fox') as choose:
            text = benchmark._make_text(4000)
        self.assertEqual(('fox ' * 1000).rstrip(), text)
        self.assertEqual(1000, choose.call_count)
        self.assertLessEqual(len(calls), 1000)
