"""Known-good and deliberately reversed controls for exploratory score reporting."""
import unittest

from experiments.casting_similarity_probe import get_probe_summary


class CastingSimilarityProbeTests(unittest.TestCase):
    def test_separating_scores_are_reported_but_not_identity_qualified(self):
        report = get_probe_summary([0.9, 0.2, 0.1, 0.8, 0.1, -0.1])
        self.assertEqual(2, report['above_random'])
        self.assertEqual(2, report['above_contrast'])
        self.assertAlmostEqual(0.85, report['mean_candidate'])
        self.assertFalse(report['validated_identity_separation'])

    def test_known_bad_controls_do_not_become_separation_successes(self):
        report = get_probe_summary([0.1, 0.3, 0.4, 0.2, 0.2, 0.2])
        self.assertEqual(0, report['above_random'])
        self.assertEqual(0, report['above_contrast'])
        self.assertAlmostEqual(0.3, report['mean_contrast'])

    def test_incomplete_unavailable_or_nonfinite_scores_are_errors(self):
        for scores in ([], [0.1, 0.2], [None, 0.2, 0.3], [True, 0.2, 0.3],
                       [float('nan'), 0.2, 0.3], [float('inf'), 0.2, 0.3]):
            with self.subTest(scores=scores), self.assertRaises(ValueError):
                get_probe_summary(scores)
