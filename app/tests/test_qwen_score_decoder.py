"""Borrowed score-path decoder (#1844) — worker-only, NumPy, fail-first.

The decoder must return the maximum-score monotonic path with strictly
positive word bins from REAL model scores (donor: hangry-labs/Qwen3-ASR-STT
@97ad227a, Apache-2.0). Optimum is proven on small cases by exhaustive
brute force; malformed / non-finite / impossible inputs fail closed
instead of returning invented times.
"""
import itertools
import unittest

import numpy as np

from qwen_score_decoder import decode_timestamp_bins


def _path_score(scores, path):
    return sum(float(scores[i, b]) for i, b in enumerate(path))


def _is_valid(path):
    if any(path[i] >= path[i + 1] for i in range(0, len(path), 2)):
        return False
    return all(path[i] <= path[i + 1] for i in range(len(path) - 1))


def _brute_best(scores):
    rows, bins = scores.shape
    best = None
    for path in itertools.product(range(bins), repeat=rows):
        if _is_valid(list(path)):
            value = _path_score(scores, path)
            if best is None or value > best:
                best = value
    return best


class DecoderOptimumTests(unittest.TestCase):
    def test_already_positive_argmax_preserved(self):
        # Argmax bins [0,2,2,5]: every word positive, monotonic -> kept.
        scores = np.full((4, 6), -10.0)
        scores[0, 0] = 5.0
        scores[1, 2] = 5.0
        scores[2, 2] = 5.0
        scores[3, 5] = 5.0
        self.assertEqual([0, 2, 2, 5], decode_timestamp_bins(scores))

    def test_zero_word_resolved_by_score(self):
        # Both argmax at bin 1 (zero word); score prefers end at bin 3.
        scores = np.array([[0.0, 10.0, 0.0, 0.0],
                           [0.0, 10.0, 0.0, 9.0]])
        self.assertEqual([1, 3], decode_timestamp_bins(scores))

    def test_exhaustive_optimum_two_words(self):
        rng = np.random.default_rng(1844)
        for _ in range(30):
            scores = rng.normal(size=(4, 4))
            raw = [int(np.argmax(scores[i])) for i in range(4)]
            if _is_valid(raw):
                continue  # valid argmax path is itself the optimum
            result = decode_timestamp_bins(scores)
            self.assertTrue(_is_valid(result))
            self.assertTrue(all(0 <= b < 4 for b in result))
            self.assertAlmostEqual(_brute_best(scores), _path_score(scores, result))

    def test_exhaustive_optimum_three_words(self):
        rng = np.random.default_rng(7)
        for _ in range(15):
            scores = rng.normal(size=(6, 5))
            raw = [int(np.argmax(scores[i])) for i in range(6)]
            if _is_valid(raw):
                continue
            result = decode_timestamp_bins(scores)
            self.assertTrue(_is_valid(result))
            self.assertAlmostEqual(_brute_best(scores), _path_score(scores, result))

    def test_accepts_nested_lists(self):
        scores = [[0.0, 10.0, 0.0, 0.0], [0.0, 10.0, 0.0, 9.0]]
        self.assertEqual([1, 3], decode_timestamp_bins(scores))

    def test_word_may_start_where_previous_ends(self):
        scores = np.full((4, 5), -10.0)
        scores[0, 0] = 5.0
        scores[1, 1] = 5.0   # word 1: 0-1
        scores[2, 1] = 5.0   # word 2 starts at 1 (previous end)
        scores[3, 3] = 5.0
        self.assertEqual([0, 1, 1, 3], decode_timestamp_bins(scores))


class DecoderFailureTests(unittest.TestCase):
    def test_odd_token_count_fails(self):
        with self.assertRaises(ValueError):
            decode_timestamp_bins(np.zeros((3, 4)))

    def test_non_2d_fails(self):
        with self.assertRaises(ValueError):
            decode_timestamp_bins(np.zeros((4,)))
        with self.assertRaises(ValueError):
            decode_timestamp_bins(np.zeros((2, 2, 2)))

    def test_empty_fails(self):
        with self.assertRaises(ValueError):
            decode_timestamp_bins(np.zeros((0, 4)))

    def test_nan_fails(self):
        scores = np.zeros((2, 4))
        scores[0, 1] = np.nan
        with self.assertRaises(ValueError):
            decode_timestamp_bins(scores)

    def test_inf_fails(self):
        scores = np.zeros((2, 4))
        scores[1, 2] = np.inf
        with self.assertRaises(ValueError):
            decode_timestamp_bins(scores)

    def test_oversize_fails_before_allocation(self):
        # One row over the bin bound must raise, not allocate a huge DP.
        scores = np.zeros((2, 200000))
        with self.assertRaises(ValueError):
            decode_timestamp_bins(scores)

    def test_impossible_path_fails(self):
        # 3 words need bins up to at least 3; only 2 bins exist.
        with self.assertRaises(ValueError):
            decode_timestamp_bins(np.zeros((6, 2)))

    def test_single_bin_fails(self):
        with self.assertRaises(ValueError):
            decode_timestamp_bins(np.zeros((2, 1)))

    def test_all_negative_infinity_fails(self):
        with self.assertRaises(ValueError):
            decode_timestamp_bins(np.full((4, 4), -np.inf))

    def test_shape_checked_before_conversion(self):
        class Sentinel:
            shape = (2, 200000)
            dtype = float

            def __array__(self, *args, **kwargs):
                raise AssertionError("oversize scores were converted")

        with self.assertRaises(ValueError):
            decode_timestamp_bins(Sentinel())

    def test_real_5000_bin_geometry_decodes(self):
        scores = np.zeros((2, 5000))
        scores[0, 100] = 5.0
        scores[1, 200] = 5.0
        self.assertEqual([100, 200], decode_timestamp_bins(scores))


if __name__ == "__main__":
    unittest.main()
