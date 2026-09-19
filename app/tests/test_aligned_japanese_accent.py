"""The aligned Japanese accent scorer, checked on cases whose answer is known
before it runs on 150 lines (Rule 21). No audio: the mora/phrase layer is
pyopenjtalk-only and the scores take mora medians directly."""
import unittest

try:
    import pyopenjtalk  # noqa: F401
    HAVE_OPENJTALK = True
except ImportError:
    HAVE_OPENJTALK = False

from experiments.aligned_japanese_accent import (
    PHONE_LABELS, aggregate, mora_units, phrases_of, score_phrase)

MMS_LABELS = set("abcdefghijklmnopqrstuvwxyz'-*")


@unittest.skipUnless(HAVE_OPENJTALK, "pyopenjtalk not installed")
class MoraUnits(unittest.TestCase):
    def test_the_minimal_pair_differs_only_in_the_nucleus(self):
        # With the particle: 箸が falls after mora 1, 橋が after mora 2, and
        # 端が (edge, heiban) never falls. In isolation OpenJTalk writes 橋
        # and 端 identically (2_2), so the bare word cannot be the fixture.
        self.assertEqual(phrases_of(mora_units("箸が")), [{"moras": 3, "start": 0, "accent": 1}])
        self.assertEqual(phrases_of(mora_units("橋が")), [{"moras": 3, "start": 0, "accent": 2}])
        self.assertEqual(phrases_of(mora_units("端が")), [{"moras": 3, "start": 0, "accent": 0}])

    def test_a_nucleus_on_the_last_mora_is_read_as_unaccented(self):
        # OpenJTalk encodes heiban as accent type == mora count (桜 -> 4_4).
        self.assertEqual(phrases_of(mora_units("桜")), [{"moras": 3, "start": 0, "accent": 0}])
        self.assertEqual(phrases_of(mora_units("学校"))[0]["accent"], 0)

    def test_geminate_nasal_and_long_vowel_each_count_one_mora(self):
        gakkoo = mora_units("学校")            # ga-k-ko-o
        self.assertEqual([m["phones"] for m in gakkoo], ["g a", "k", "k o", "o"])
        sensee = mora_units("先生")            # se-N-se-e
        self.assertEqual([m["phones"] for m in sensee], ["s e", "N", "s e", "e"])

    def test_every_label_is_one_the_aligner_knows(self):
        for label in (l for labels in PHONE_LABELS.values() for l in labels):
            self.assertIn(label, MMS_LABELS)
        for m in mora_units("ちょっと、キャベツとニュースを買った。"):
            for label in m["labels"]:
                self.assertIn(label, MMS_LABELS)

    def test_phrases_follow_the_accent_field_not_punctuation(self):
        morae = mora_units("「さんぼう本部へんさんの地図をまたくりひらいて見るでもなかろう、")
        phrases = phrases_of(morae)
        self.assertEqual(sum(p["moras"] for p in phrases), len(morae))
        self.assertEqual([p["start"] for p in phrases][:3], [0, 12, 15])
        self.assertEqual(phrases[0], {"moras": 12, "start": 0, "accent": 8})


class ScorePhrase(unittest.TestCase):
    def test_an_accent_two_phrase_with_l_h_l_scores_everything(self):
        s = score_phrase({"moras": 3, "accent": 2}, [-2.0, 3.0, -1.0])
        self.assertTrue(s["drop"])
        self.assertTrue(s["rise"])
        self.assertGreater(s["correlation"], 0.9)

    def test_a_flat_phrase_has_no_drop_and_no_correlation(self):
        s = score_phrase({"moras": 3, "accent": 2}, [1.0, 1.0, 1.0])
        self.assertFalse(s["drop"])
        self.assertFalse(s["rise"])
        self.assertIsNone(s["correlation"])

    def test_heiban_never_enters_drop_even_when_the_voice_falls(self):
        # The reject case: an unaccented phrase has no nucleus, so a fall
        # inside it is not evidence of an accent being realised.
        s = score_phrase({"moras": 3, "accent": 0}, [-1.0, 4.0, 0.0])
        self.assertIsNone(s["drop"])
        self.assertTrue(s["rise"])

    def test_accent_one_has_no_rise_and_drops_from_the_first_mora(self):
        s = score_phrase({"moras": 2, "accent": 1}, [3.0, -3.0])
        self.assertTrue(s["drop"])
        self.assertIsNone(s["rise"])

    def test_unvoiced_morae_make_the_score_none_not_false(self):
        s = score_phrase({"moras": 3, "accent": 2}, [None, 3.0, None])
        self.assertIsNone(s["drop"])
        self.assertIsNone(s["rise"])
        self.assertIsNone(s["correlation"])


class Aggregate(unittest.TestCase):
    def test_nones_are_ignored_and_coverage_is_reported(self):
        rows = [{"morae": 4, "morae_voiced": 3, "alignment_score": -0.5,
                 "phrases": [{"drop": True, "rise": None, "correlation": 0.5},
                             {"drop": None, "rise": False, "correlation": None}]},
                {"morae": 2, "morae_voiced": 2, "alignment_score": -1.5,
                 "phrases": [{"drop": False, "rise": True, "correlation": -0.5}]}]
        a = aggregate(rows)
        self.assertEqual(a["phrases"], 3)
        self.assertEqual((a["drop_phrases"], a["drop_agreement"]), (2, 0.5))
        self.assertEqual((a["rise_phrases"], a["rise_agreement"]), (2, 0.5))
        self.assertEqual((a["correlation_phrases"], a["correlation_mean"]), (2, 0.0))
        self.assertEqual(a["coverage"], 5 / 6)
        self.assertEqual(a["alignment_score_mean"], -1.0)


if __name__ == "__main__":
    unittest.main()
