"""Choosing a spread of references, and reporting what it can and cannot show.

The arms differ in typicality but share a 10-15s BAND, not one duration - the
measured LJSpeech spread runs 10.5s to 14.3s. A correlation that actually
tracks duration must stay visible, so ref_seconds is carried through to the
comparison and the scope string refuses to claim duration was held equal.
"""
import json
import os
import tempfile
import unittest

from experiments.reference_spread import pick_spread
from experiments.reference_spread_compare import mean_metric, spearman


def scored(distances):
    return [{"distance": d} for d in sorted(distances)]


class PickSpreadTest(unittest.TestCase):
    def test_the_nearest_and_the_farthest_are_always_included(self):
        """An interior sample cannot answer whether similarity tracks distance."""
        picks = pick_spread(scored(range(10)), 4)
        self.assertEqual(0, picks[0]["distance"])
        self.assertEqual(9, picks[-1]["distance"])
        self.assertEqual(4, len(picks))

    def test_the_arms_are_evenly_spaced_over_the_ranking(self):
        self.assertEqual([0, 3, 6, 9],
                         [p["distance"] for p in pick_spread(scored(range(10)), 4)])

    def test_fewer_candidates_than_arms_yields_fewer_arms_not_repeats(self):
        """Two arms from the same reference would be a guaranteed null."""
        picks = pick_spread(scored([0.1, 0.9]), 4)
        self.assertEqual(2, len(picks))
        self.assertEqual([0.1, 0.9], [p["distance"] for p in picks])

    def test_a_single_arm_is_the_nearest(self):
        self.assertEqual([0], [p["distance"] for p in pick_spread(scored(range(5)), 1)])


class SpearmanTest(unittest.TestCase):
    def test_a_perfect_order_is_plus_or_minus_one(self):
        self.assertAlmostEqual(1.0, spearman([1, 2, 3, 4], [10, 20, 30, 40]), 6)
        self.assertAlmostEqual(-1.0, spearman([1, 2, 3, 4], [40, 30, 20, 10]), 6)

    def test_it_ranks_rather_than_fits(self):
        """One extreme value must not swing the answer the way a fit would."""
        self.assertAlmostEqual(1.0, spearman([1, 2, 3, 4], [1, 2, 3, 4000]), 6)

    def test_a_flat_series_is_undefined_not_zero(self):
        """Reporting 0.0 would read as 'measured no relationship'."""
        self.assertIsNone(spearman([1, 2, 3, 4], [5, 5, 5, 5]))

    def test_too_few_arms_is_undefined(self):
        self.assertIsNone(spearman([1, 2], [3, 4]))


class MeanMetricTest(unittest.TestCase):
    def _write(self, doc):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(doc, handle)
        handle.close()
        return handle.name

    def test_the_summary_is_used_when_present(self):
        path = self._write({"summary": {"clone": {"ecapa": 0.73, "n": 150}}})
        self.assertEqual((0.73, 150), mean_metric(path, "ecapa"))
        os.unlink(path)

    def test_it_falls_back_to_averaging_the_rows(self):
        path = self._write({"rows": [{"clone": {"ecapa": 0.6}},
                                     {"clone": {"ecapa": 0.8}}]})
        value, n = mean_metric(path, "ecapa")
        self.assertAlmostEqual(0.7, value)
        self.assertEqual(2, n)
        os.unlink(path)

    def test_a_missing_metric_is_none_not_zero(self):
        """0.0 would enter the correlation as a real, terrible score."""
        path = self._write({"rows": [{"clone": {"mcd": 400}}]})
        self.assertEqual((None, 0), mean_metric(path, "ecapa"))
        os.unlink(path)
