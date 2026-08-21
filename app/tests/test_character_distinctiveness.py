"""Two numbers that answer different questions, kept side by side.

The overlap of two F0 distributions measures shared RANGE; the distance between
their medians measures separation of CENTRES. Reporting only the first inverts
the reading of the real data: NARRATOR and NATSUKI SUBARU overlap 0.49, which
looks like half the same voice, and sit 5.64 semitones apart, which nobody
would miss - while LITTLE GIRL and SATELLA overlap less, 0.39, and sit 0.90
semitones apart, which is the pair actually worth listening to.

A run that printed overlap alone would have pointed at the wrong pair, and it
did, for about ten minutes.
"""
import json
import os
import tempfile
import unittest

from experiments.character_distinctiveness import (load_manifest, median,
                                                   overlap, semitones)


class OverlapTest(unittest.TestCase):
    def test_identical_samples_overlap_completely(self):
        a = [100, 110, 120, 130]
        self.assertAlmostEqual(1.0, overlap(a, list(a)), 3)

    def test_disjoint_samples_do_not_overlap(self):
        self.assertAlmostEqual(0.0, overlap([100, 101, 102], [400, 401, 402]), 3)

    def test_it_is_symmetric(self):
        a, b = [100, 120, 140, 160], [130, 150, 170, 190]
        self.assertAlmostEqual(overlap(a, b), overlap(b, a), 6)

    def test_a_sample_too_small_to_bin_is_none_not_zero(self):
        """0.0 would read as 'measured, and they are disjoint'."""
        self.assertIsNone(overlap([100], [200, 210, 220]))
        self.assertIsNone(overlap([], [200, 210]))

    def test_none_values_do_not_sink_the_estimate(self):
        a = [100, 110, None, 120]
        self.assertIsNotNone(overlap(a, [100, 110, 120]))


class SemitoneTest(unittest.TestCase):
    def test_an_octave_is_twelve_semitones(self):
        self.assertAlmostEqual(12.0, semitones(220.0, 110.0), 6)

    def test_it_is_unsigned_and_symmetric(self):
        self.assertAlmostEqual(semitones(130.9, 181.4), semitones(181.4, 130.9), 6)
        self.assertGreater(semitones(130.9, 181.4), 0)

    def test_the_two_real_pairs_that_motivated_this_file(self):
        """Overlap ranks these one way; pitch centres rank them the other."""
        narrator_vs_subaru = semitones(130.9, 181.4)
        girl_vs_satella = semitones(212.6, 224.0)
        self.assertAlmostEqual(5.64, narrator_vs_subaru, 1)
        self.assertAlmostEqual(0.90, girl_vs_satella, 1)
        self.assertGreater(narrator_vs_subaru, girl_vs_satella)

    def test_a_missing_or_impossible_pitch_is_none(self):
        for a, b in ((None, 100), (100, None), (0, 100), (-1, 100)):
            self.assertIsNone(semitones(a, b), (a, b))


class MedianTest(unittest.TestCase):
    def test_it_ignores_missing_values(self):
        self.assertEqual(20, median([10, None, 20, None, 30]))

    def test_all_missing_is_none(self):
        self.assertIsNone(median([None, None]))

    def test_an_even_count_averages_the_middle_pair(self):
        self.assertEqual(15, median([10, 20]))


class ManifestTest(unittest.TestCase):
    def test_clips_are_grouped_by_the_chunk_speaker(self):
        root = tempfile.mkdtemp()
        audio = os.path.join(root, "audio")
        os.makedirs(audio)
        for uid in ("aaa", "bbb", "ccc"):
            open(os.path.join(audio, uid + ".wav"), "wb").close()
        open(os.path.join(audio, "orphan.wav"), "wb").close()
        chunks = os.path.join(root, "chunks.json")
        with open(chunks, "w", encoding="utf-8") as handle:
            json.dump([{"uid": "aaa", "speaker": "NARRATOR"},
                       {"uid": "bbb", "speaker": "NARRATOR"},
                       {"uid": "ccc", "speaker": "SATELLA"}], handle)
        by_speaker, unmatched = load_manifest(chunks, audio)
        self.assertEqual(2, len(by_speaker["NARRATOR"]))
        self.assertEqual(1, len(by_speaker["SATELLA"]))
        self.assertEqual(1, unmatched,
                         "a wav with no chunk must be counted, not dropped silently")

    def test_a_blank_speaker_is_bucketed_not_discarded(self):
        root = tempfile.mkdtemp()
        audio = os.path.join(root, "audio")
        os.makedirs(audio)
        open(os.path.join(audio, "aaa.wav"), "wb").close()
        chunks = os.path.join(root, "chunks.json")
        with open(chunks, "w", encoding="utf-8") as handle:
            json.dump([{"uid": "aaa", "speaker": ""}], handle)
        by_speaker, _ = load_manifest(chunks, audio)
        self.assertEqual(["?"], list(by_speaker))


class MeanOverlapFeaturesTest(unittest.TestCase):
    """Clip length is reported but must not dilute the summary.

    statistic_discriminability puts `seconds` at an F-ratio of 2.24 between
    these characters, against 43.63 for f0 median and 22.43 for energy. With
    it averaged in, NARRATOR vs Subaru - twelve semitones apart - read 0.355
    and ranked mid-table; without it, 0.201 and last. Averaging a near-noise
    feature into the summary changed which pair looked most alike.
    """

    def test_the_summary_averages_only_the_discriminating_features(self):
        import experiments.character_distinctiveness as mod
        source = open(mod.__file__, encoding="utf-8").read()
        start = source.index('values = [row[k] for k in (')
        window = source[start:start + 200]
        self.assertIn('"f0_overlap"', window)
        self.assertIn('"rms_overlap"', window)
        self.assertNotIn('"seconds_overlap"', window,
                         "clip length is back in the mean; it has an F-ratio "
                         "of 2.24 and inverted the pair ranking last time")

    def test_clip_length_is_still_reported_per_pair(self):
        """Excluded from the mean is not the same as thrown away.

        Checked against the ARTIFACT, not the source: the key is built as
        "%s_overlap" % key, so grepping the file for the literal string finds
        nothing and a source-level test would fail for the wrong reason. It
        did, once.
        """
        import pathlib
        repo = pathlib.Path(__file__).parent.parent.parent
        path = repo / "ab_test_runtime/experiments/character_distinctiveness.json"
        if not path.exists():
            self.skipTest("no artifact in this checkout")
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(["f0_overlap", "rms_overlap"],
                         doc.get("mean_overlap_features"))
        self.assertIn("seconds_overlap", doc["pairs"][0],
                      "clip length must still be reported, just not averaged")
