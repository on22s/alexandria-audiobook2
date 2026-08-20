"""The reference rebuilder, tested on the way it was actually wrong.

Its first version stopped at the first consecutive run reaching the target and
accepted anything up to BAND[1]+3. With ~9s LJSpeech utterances that produced a
16.8s reference - past the 15s ceiling where Qwen's guide says quality
"plateaus and eventually degrades" and warns that long prompts can hang
generation. A tool built to fix a too-short reference had overshot into the
other end of the same curve, and the printed number looked like a success.

So the band test is the first test here, and it uses durations that reproduce
exactly that arithmetic.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class CandidateBandTest(unittest.TestCase):
    def setUp(self):
        from experiments import reference_rebuild as m
        self.m = m

    def _runs(self, durations):
        entries = [{"audio_filepath": f"{i}.wav", "text": f"t{i}"}
                   for i in range(len(durations))]

        class Info:
            def __init__(self, seconds):
                self.frames = int(seconds * 100)
                self.samplerate = 100

        def info(path):
            return Info(durations[int(os.path.basename(path).split(".")[0])])

        with mock.patch.dict(sys.modules, {"soundfile": mock.Mock(info=info)}):
            return self.m.candidates(entries, "/x", self.m.TARGET_SECONDS)

    def test_no_candidate_may_exceed_the_upper_bound(self):
        """The 16.8s bug: two ~9s utterances jumped clean over the band."""
        runs = self._runs([9.0] * 6)
        for _start, _paths, _texts, seconds in runs:
            self.assertLessEqual(seconds, self.m.BAND[1],
                                 f"{seconds}s is past the {self.m.BAND[1]}s "
                                 f"ceiling the guide warns about")

    def test_no_candidate_may_fall_below_the_lower_bound(self):
        runs = self._runs([3.0] * 10)
        for _s, _p, _t, seconds in runs:
            self.assertGreaterEqual(seconds, self.m.BAND[0])

    def test_a_corpus_that_cannot_reach_the_band_yields_nothing(self):
        """One 2s utterance and nothing after it cannot make 10s. Returning a
        short run anyway would silently rebuild a reference no better than the
        one being replaced."""
        self.assertEqual([], self._runs([2.0]))

    def test_the_join_gap_counts_toward_the_length(self):
        """The clip that gets written includes a pause at every join, so a
        length computed without them describes a different file than the one
        the arm will use."""
        runs = self._runs([5.0, 5.2])
        self.assertTrue(runs)
        _s, paths, _t, seconds = runs[0]
        self.assertEqual(2, len(paths))
        self.assertAlmostEqual(5.0 + 5.2 + self.m.GAP_SECONDS, seconds, places=2)


class DistanceTest(unittest.TestCase):
    def setUp(self):
        from experiments import reference_rebuild as m
        self.m = m

    def test_being_high_is_penalised_exactly_as_much_as_being_low(self):
        """Selection must not prefer a reference that errs upward - the Chinese
        arm's failure is an upward tract-length error, and a distance that
        favoured high candidates would choose the worst one for that goal."""
        centre = {"f0_median": 100.0, "vtl_cm": 10.0}
        high = self.m.distance({"f0_median": 110.0, "vtl_cm": 10.0}, centre)
        low = self.m.distance({"f0_median": 90.0, "vtl_cm": 10.0}, centre)
        self.assertAlmostEqual(high, low, places=9)

    def test_an_exact_match_scores_zero(self):
        centre = {"f0_median": 180.3, "vtl_cm": 13.17}
        self.assertEqual(0.0, self.m.distance(dict(centre), centre))

    def test_it_uses_both_measures_not_just_pitch(self):
        """Chinese fails on tract length while its reference's pitch is already
        correct. A distance driven by f0 alone would rank every candidate the
        same and pick arbitrarily."""
        centre = {"f0_median": 100.0, "vtl_cm": 10.0}
        pitch_only = self.m.distance({"f0_median": 100.0, "vtl_cm": 12.0}, centre)
        self.assertGreater(pitch_only, 0.0)

    def test_missing_measures_do_not_crash_the_ranking(self):
        centre = {"f0_median": 100.0, "vtl_cm": 10.0}
        self.assertIsNone(self.m.distance({}, centre))


class BandConstantTest(unittest.TestCase):
    def test_the_band_is_the_published_one(self):
        """10-15s, from Qwen's cloning guide. reference_audit judges against
        the same pair; two copies would drift."""
        from experiments import reference_rebuild as rebuild
        from experiments import reference_audit as audit
        self.assertEqual(audit.RECOMMENDED, rebuild.BAND)


if __name__ == "__main__":
    unittest.main()
