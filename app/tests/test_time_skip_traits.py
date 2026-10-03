"""The time-skip check's scorer: chapters from headings, flicker, one-band tolerance."""
import unittest

from experiments import time_skip_traits as ts


def _say(speaker, age, text="x"):
    return {"speaker": speaker, "text": text, "speaker_gender": "male", "speaker_age_group": age,
            "speaker_ageless": False}


def _head(n):
    return {"speaker": "NARRATOR", "text": f"Chapter {n}: Title"}


class TimeSkipScorerTest(unittest.TestCase):
    def test_chapters_come_from_heading_entries(self):
        self.assertEqual([0, 3, 3, 8], ts.get_chapters([_say("A", "adult"), _head(3),
                                                        _say("A", "toddler"), _head(8)]))

    def test_flicker_counts_two_band_reversals_only(self):
        self.assertEqual(1, ts.get_flips(["child", "adult", "child"]))
        self.assertEqual(0, ts.get_flips(["teen", "young_adult", "teen"]))   # one band
        self.assertEqual(0, ts.get_flips(["infant", "toddler", "child"]))    # growing up

    def test_a_correct_timeline_passes_and_an_adult_mind_fails(self):
        good = [_head(1), _say("RUDEUS", "infant"), _head(3), _say("RUDEUS", "toddler"),
                _head(5), _say("RUDEUS", "young_child"), _head(8), _say("RUDEUS", "child")]
        result = ts.score_run(good)
        self.assertTrue(all(result["checks"].values()), result["checks"])
        adult_mind = [_head(n) if i % 2 == 0 else _say("RUDEUS", "adult")
                      for n in (1, 3, 5, 8) for i in (0, 1)]
        bad = ts.score_run(adult_mind)
        self.assertFalse(bad["checks"]["1_timeline"])
        self.assertEqual(4, bad["adult_lines_after_prologue"])


if __name__ == "__main__":
    unittest.main()
