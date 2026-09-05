"""The tone-spread statistic must separate one voice from a mixture.

Rule 21: a metric is validated on cases whose answer is already known, and the
cases it should REJECT matter as much as the ones it should accept. These
fixtures are synthetic embeddings with a known structure - one tight cluster,
two clusters, and uniform noise - so the statistic can be checked against an
answer that does not depend on the audio pipeline being right.
"""
import os
import sys
import unittest

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

from experiments.dataset_tone_spread import (  # noqa: E402
    _norm, correlate, tone_stats)


def _cluster(centre, n, jitter, rng):
    base = np.zeros(16)
    base[centre] = 1.0
    return base + rng.normal(0, jitter, size=(n, 16))


class ToneStats(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)

    def test_one_voice_is_tight(self):
        tight, spread, n = tone_stats(_cluster(0, 150, 0.05, self.rng))
        self.assertEqual(n, 150)
        self.assertGreater(tight, 0.9)
        self.assertLess(spread, 0.1)

    def test_two_voices_are_looser_than_one(self):
        one = tone_stats(_cluster(0, 150, 0.05, self.rng))[0]
        two = tone_stats(np.vstack([_cluster(0, 75, 0.05, self.rng),
                                    _cluster(1, 75, 0.05, self.rng)]))[0]
        self.assertLess(two, one,
                        "a dataset holding two voices must not look as tight "
                        "as one holding a single voice")

    def test_more_voices_are_looser_still(self):
        """Monotonic, not merely different - otherwise the number cannot be
        read as 'how much of a mixture is this'."""
        vals = []
        for k in (1, 2, 4, 8):
            rows = np.vstack([_cluster(i, 120 // k, 0.05, self.rng)
                              for i in range(k)])
            vals.append(tone_stats(rows)[0])
        self.assertEqual(vals, sorted(vals, reverse=True), vals)

    def test_a_degenerate_input_returns_no_statistic(self):
        """One clip has no spread; returning 1.0 would read as a perfect
        single-voice dataset."""
        self.assertEqual(tone_stats(np.ones((1, 16)))[:2], (None, None))
        self.assertEqual(tone_stats(np.zeros((0, 16)))[:2], (None, None))


class Correlate(unittest.TestCase):
    def test_it_reports_both_coefficients_and_their_p_values(self):
        rng = np.random.default_rng(3)
        x = rng.normal(size=60)
        out = correlate(x, x * 2 + rng.normal(0, 0.1, 60))
        self.assertGreater(out["r"], 0.9)
        self.assertLess(out["p"], 1e-9)
        self.assertGreater(out["rho"], 0.9)
        self.assertEqual(out["n"], 60)

    def test_a_constant_column_correlates_with_nothing(self):
        out = correlate([1.0] * 20, list(range(20)))
        self.assertIsNone(out["r"])
        self.assertEqual(out["n"], 20)

    def test_too_few_points_is_not_a_correlation(self):
        self.assertIsNone(correlate([1, 2], [3, 4])["r"])


class NameMatching(unittest.TestCase):
    def test_punctuation_differences_do_not_break_the_match(self):
        """The bug this fixed matched 36 of 75 datasets and looked like
        missing data rather than a naming mismatch."""
        self.assertEqual(
            _norm("narrator_water_moon:_a_novel_[b0d26l1r1d]_char1_vol01"),
            _norm("narrator_water_moon_a_novel_b0d26l1r1d_char1_vol01"))

    def test_genuinely_different_names_still_differ(self):
        self.assertNotEqual(_norm("narrator_a_char1_vol01"),
                            _norm("narrator_a_char2_vol01"))


if __name__ == "__main__":
    unittest.main()
