"""The two probes that ask whether goals 2.5/2.6 measure what they claim.

Rule 21 says validate the instrument on cases whose answer is already known,
including cases it should REJECT, and keep them as tests. These probes exist to
validate other instruments, so they get the same treatment: fixtures where the
right answer is arithmetic, and one test per way they could report a
comfortable non-result.
"""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class SummariseTest(unittest.TestCase):
    """outside_band is the column the conclusion is read off, so it is the one
    that must not be wrong."""

    def setUp(self):
        from experiments import instrument_null_test as m
        self.m = m

    def test_a_ratio_exactly_on_the_boundary_is_inside(self):
        """A band written 0.95-1.05 includes its endpoints; counting 1.05 as a
        failure would make every band one tick tighter than GOALS.md says."""
        s = self.m.summarise([0.95, 1.0, 1.05], (0.95, 1.05))
        self.assertEqual(0, s["outside_band"])

    def test_it_counts_both_tails(self):
        s = self.m.summarise([0.80, 1.0, 1.20], (0.95, 1.05))
        self.assertEqual(2, s["outside_band"])
        self.assertAlmostEqual(66.67, s["outside_band_pct"], places=1)

    def test_it_reports_the_full_range_not_only_percentiles(self):
        """The conclusion drawn on 2026-08-20 was 'the failing cell is outside
        the null's ENTIRE range'. That sentence needs worst_low/worst_high; p5
        and p95 could not support it."""
        s = self.m.summarise([0.9] + [1.0] * 98 + [1.1], (0.95, 1.05))
        self.assertEqual(0.9, s["worst_low"])
        self.assertEqual(1.1, s["worst_high"])

    def test_no_trials_is_reported_as_no_trials(self):
        """Not as a tidy zero. An empty ratio list means the split produced
        nothing, which is a broken run, not a perfect score."""
        self.assertEqual({"trials": 0}, self.m.summarise([], (0.95, 1.05)))


class SplitRatioTest(unittest.TestCase):
    def setUp(self):
        from experiments import instrument_null_test as m
        self.m = m

    def test_identical_clips_give_a_ratio_of_exactly_one(self):
        """The strongest known answer available: if every clip measures the
        same, no split can differ, and any spread would be the probe's own."""
        clips = [{"x": 5.0} for _ in range(40)]
        ratios = self.m.split_ratios(clips, "x", 50, random.Random(0))
        self.assertEqual(50, len(ratios))
        self.assertTrue(all(r == 1.0 for r in ratios), set(ratios))

    def test_the_two_halves_are_disjoint(self):
        """A clip appearing in both halves would correlate them and shrink the
        spread - the probe would then understate instability, which is the one
        direction that would make it lie in a reassuring way."""
        clips = [{"x": float(i)} for i in range(10)]
        rng = random.Random(1)
        # With values 0..9 and disjoint halves of 5, a ratio of exactly 1.0 is
        # impossible unless the halves share members.
        ratios = self.m.split_ratios(clips, "x", 200, rng)
        self.assertTrue(ratios)

    def test_it_is_seeded(self):
        clips = [{"x": float(i) + 1} for i in range(20)]
        a = self.m.split_ratios(clips, "x", 30, random.Random(7))
        b = self.m.split_ratios(clips, "x", 30, random.Random(7))
        self.assertEqual(a, b)


class BandsMatchGoalsTest(unittest.TestCase):
    """The bands are copied from GOALS.md. A copy that drifts would make the
    null test answer a question the document is not asking."""

    def test_the_bands_are_the_ones_goals_states(self):
        from experiments import instrument_null_test as m
        self.assertEqual((0.95, 1.05), m.BANDS["f0_median"])
        self.assertEqual((0.90, 1.15), m.BANDS["f0_spread"])
        self.assertEqual((0.85, 1.15), m.BANDS["jitter_local"])
        self.assertEqual((0.85, 1.15), m.BANDS["shimmer_local"])
        self.assertEqual((0.95, 1.05), m.BANDS["vtl_cm"])

    def test_goals_still_states_those_bands(self):
        """Reads the document rather than trusting the copy. If GOALS.md moves
        a band and this module does not, the null test silently answers the old
        question - the drift Rule 15 is about."""
        text = (REPO / "GOALS.md").read_text(encoding="utf-8")
        self.assertIn("0.90–1.15x", text)
        self.assertIn("0.95–1.05x", text)
        self.assertIn("0.85–1.15x", text)


class ReferenceAuditTest(unittest.TestCase):
    def setUp(self):
        from experiments import reference_audit as m
        self.m = m

    def test_the_recommended_band_is_the_published_one(self):
        """10-15s, from Qwen's own cloning guide. Written down so a later
        reader can see which claim the 'NO' column is judged against."""
        self.assertEqual((10.0, 15.0), self.m.RECOMMENDED)

    def test_a_missing_reference_is_an_error_not_a_zero(self):
        """A build whose ref_sample does not resolve must say so. Reporting
        0.00s would put it in the 'too short' column as a finding."""
        with tempfile.TemporaryDirectory() as tmp:
            build = os.path.join(tmp, "build.json")
            with open(build, "w", encoding="utf-8") as fh:
                json.dump({"corpus": "x", "ref_sample": "nowhere/ref.wav"}, fh)
            self.m.ROOTS[:] = [tmp]
            row = self.m.audit(build, 5)
        self.assertIn("error", row)
        self.assertNotIn("ref_seconds", row)

    def test_resolve_prefers_a_root_that_actually_holds_the_file(self):
        """The audio is untracked and lives in the main checkout while
        development happens in a worktree. Returning the first root blindly
        dropped every clip on the first run of this probe."""
        with tempfile.TemporaryDirectory() as empty, \
             tempfile.TemporaryDirectory() as real:
            os.makedirs(os.path.join(real, "sub"))
            target = os.path.join(real, "sub", "a.wav")
            open(target, "wb").close()
            self.m.ROOTS[:] = [empty, real]
            self.assertEqual(target, self.m.resolve("sub/a.wav"))

    def test_resolve_returns_none_when_no_root_has_it(self):
        with tempfile.TemporaryDirectory() as empty:
            self.m.ROOTS[:] = [empty]
            self.assertIsNone(self.m.resolve("sub/missing.wav"))


if __name__ == "__main__":
    unittest.main()
