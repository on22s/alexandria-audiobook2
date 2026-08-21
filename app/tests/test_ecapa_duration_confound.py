"""A verdict that must flip when the data flips.

The measured answer is that the ratio-to-ceiling is duration-flat (rho -0.038,
-0.041, -0.033) while both raw series climb (up to +0.483). That is the answer
that lets goal 2.1 stand. A verdict string hard-coded to say so would keep
saying so after the data changed, which is the failure mode this file exists
to prevent: the fixtures below drive it BOTH ways.
"""
import json
import os
import tempfile
import unittest

from experiments.ecapa_duration_confound import (analyse, quartiles,
                                                 ratio_rows, series_rows,
                                                 spearman)


def artifact(rows):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"rows": rows}, handle)
    handle.close()
    return handle.name


def row(seconds, clone, ceiling):
    return {"human_seconds": seconds,
            "clone": {"ecapa": clone},
            "human_vs_human": {"ecapa": ceiling}}


class SpearmanTest(unittest.TestCase):
    def test_monotone_series_reach_plus_or_minus_one(self):
        self.assertAlmostEqual(1.0, spearman([1, 2, 3, 4], [1, 2, 3, 4]), 6)
        self.assertAlmostEqual(-1.0, spearman([1, 2, 3, 4], [4, 3, 2, 1]), 6)

    def test_a_constant_series_is_undefined_not_zero(self):
        self.assertIsNone(spearman([1, 2, 3, 4], [7, 7, 7, 7]))


class QuartileTest(unittest.TestCase):
    def test_quartiles_run_shortest_clips_first(self):
        pairs = [(i, i * 10.0) for i in range(1, 9)]
        self.assertEqual([15.0, 35.0, 55.0, 75.0], quartiles(pairs))

    def test_too_few_rows_to_quarter_is_none(self):
        self.assertIsNone(quartiles([(1, 1.0), (2, 2.0)]))


class RowExtractionTest(unittest.TestCase):
    def test_rows_missing_either_half_are_skipped(self):
        rows = [row(5.0, 0.7, 0.8),
                {"human_seconds": 6.0, "clone": {}},
                {"clone": {"ecapa": 0.9}, "human_vs_human": {"ecapa": 0.9}}]
        self.assertEqual([(5.0, 0.7)], series_rows({"rows": rows}, "clone"))
        self.assertEqual(1, len(ratio_rows({"rows": rows})))

    def test_a_zero_ceiling_does_not_divide(self):
        self.assertEqual([], ratio_rows({"rows": [row(5.0, 0.7, 0.0)]}))


class VerdictTest(unittest.TestCase):
    """The two fixtures the real data sits between."""

    def _analyse(self, rows):
        path = artifact(rows)
        try:
            return analyse(path)
        finally:
            os.unlink(path)

    def test_a_flat_ratio_over_climbing_raw_series(self):
        """Both halves rise together, so their ratio does not - the real shape."""
        # The clone must rise IN PROPORTION to the ceiling, which is what makes
        # the ratio flat. A first version added a fixed increment to each and
        # produced a ratio with rho 1.0 - the fixture, not the code, was wrong,
        # and it is kept in mind here because that is the easy mistake to make
        # when reasoning about a ratio.
        ceiling = [0.70 + 0.0117 * i for i in range(12)]
        jitter = [0.004 if i % 2 else -0.004 for i in range(12)]
        rows = [row(2.0 + i, 0.857 * ceiling[i] + jitter[i], ceiling[i])
                for i in range(12)]
        out = self._analyse(rows)
        self.assertGreater(out["clone"]["spearman"], 0.9)
        self.assertGreater(out["human_vs_human"]["spearman"], 0.9)
        self.assertLess(abs(out["ratio"]["spearman"]), 0.5)

    def test_a_ratio_that_does_track_duration_is_visible(self):
        """The confounded case must not be reported as flat."""
        rows = [row(2.0 + i, 0.50 + 0.03 * i, 0.90) for i in range(12)]
        out = self._analyse(rows)
        self.assertGreater(out["ratio"]["spearman"], 0.9)

    def test_median_and_range_describe_the_clips_that_were_scored(self):
        out = self._analyse([row(2.0 + i, 0.6, 0.8) for i in range(12)])
        self.assertEqual([2.0, 13.0], out["seconds_range"])
        self.assertEqual(8.0, out["median_seconds"])
