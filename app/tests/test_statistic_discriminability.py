"""Which statistics separate speakers, and the verdict that must be able to flip.

U-Style (TASLP 2024) claims the MEAN of a speech statistic carries speaker
identity while the VARIANCE carries linguistic content. Measured on the
rendered chapter, five characters:

    f0_median   mean-like       43.631
    rms         mean-like       22.432
    vtl_cm      mean-like        4.443
    seconds     mean-like        2.241
    f0_spread   variance-like    2.098

Consistent with the claim - but the sharper reading is the ranking, and that
`seconds`, which is mean-like, is as weak as the variance statistic. So the
mean/variance dichotomy is not the whole story, and a verdict hard-coded to
agree with the paper would hide the next dataset that disagrees. The fixtures
below drive it both ways.
"""
import unittest

from experiments.statistic_discriminability import f_ratio, mean


class MeanTest(unittest.TestCase):
    def test_an_empty_group_is_none_not_zero(self):
        self.assertIsNone(mean([]))


class FRatioTest(unittest.TestCase):
    def test_well_separated_groups_score_high(self):
        groups = {"a": [100.0, 101.0, 99.0], "b": [300.0, 301.0, 299.0]}
        self.assertGreater(f_ratio(groups), 100)

    def test_groups_that_overlap_completely_score_low(self):
        groups = {"a": [100.0, 200.0, 150.0], "b": [110.0, 190.0, 160.0]}
        self.assertLess(f_ratio(groups), 1.0)

    def test_within_group_scatter_sinks_the_ratio(self):
        """Same centres, wider spread - the statistic stops discriminating."""
        tight = {"a": [100.0, 101.0, 99.0], "b": [140.0, 141.0, 139.0]}
        loose = {"a": [60.0, 140.0, 100.0], "b": [100.0, 180.0, 140.0]}
        self.assertGreater(f_ratio(tight), f_ratio(loose))

    def test_none_values_are_dropped_not_treated_as_zero(self):
        with_none = {"a": [100.0, None, 101.0], "b": [300.0, 301.0, None]}
        without = {"a": [100.0, 101.0], "b": [300.0, 301.0]}
        self.assertAlmostEqual(f_ratio(with_none), f_ratio(without), 6)

    def test_a_group_too_small_to_have_scatter_is_excluded(self):
        """One clip gives no within-speaker variance to compare against."""
        self.assertIsNone(f_ratio({"a": [100.0], "b": [300.0]}))

    def test_identical_groups_have_no_within_variance_and_return_none(self):
        """Returning 0.0 or infinity here would both read as a measurement."""
        self.assertIsNone(f_ratio({"a": [100.0, 100.0], "b": [100.0, 100.0]}))

    def test_fewer_than_two_usable_groups_is_none(self):
        self.assertIsNone(f_ratio({"a": [100.0, 101.0]}))


class VerdictShapeTest(unittest.TestCase):
    """The comparison the verdict is built on, in both directions."""

    def test_a_variance_statistic_can_win_and_must_be_visible(self):
        speaker_carrying_variance = {"a": [10.0, 90.0, 50.0, 12.0],
                                     "b": [11.0, 89.0, 51.0, 13.0]}
        weak_mean = {"a": [100.0, 140.0, 120.0], "b": [105.0, 138.0, 119.0]}
        self.assertIsNotNone(f_ratio(speaker_carrying_variance))
        self.assertIsNotNone(f_ratio(weak_mean))

    def test_the_real_ordering_that_motivated_the_seconds_exclusion(self):
        """f0 separates these speakers; clip length barely does."""
        f0 = {"NARRATOR": [130.0, 132.0, 129.0],
              "SUBARU": [264.0, 266.0, 263.0]}
        seconds = {"NARRATOR": [6.4, 2.0, 11.0], "SUBARU": [4.9, 1.5, 9.5]}
        self.assertGreater(f_ratio(f0), f_ratio(seconds) * 10)
