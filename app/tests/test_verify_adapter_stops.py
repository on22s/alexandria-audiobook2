"""The stop gate judges every line, not the median.

Fixtures are the measured duration ratios of 2026-09-28 (doc_checks_20260928/stops_*.json):
an LJSpeech voice LoRA at lr 2e-6, seed 1234, ran 16x and 18x on two of five lines and
passed the old median rule at 1.1x.
"""
import unittest
import statistics

from experiments.verify_adapter_stops import judge_stop_ratios

LR2E6_SEED1234 = [0.95, 16.22, 18.3, 1.1, 0.93]
LR2E6_SEED1235 = [30.49, 16.22, 1.16, 1.23, 3.24]
LR1E6_SHIPPED = [0.95, 1.09, 0.96, 1.2, 0.96]


class StopGateTests(unittest.TestCase):
    def test_partial_runaway_fails(self):
        passed, verdict = judge_stop_ratios(LR2E6_SEED1234, 3.0)
        self.assertFalse(passed)
        self.assertIn("2 of 5 lines", verdict)

    def test_the_old_median_rule_passed_it(self):
        """Pins why the rule changed: the fixture must keep fooling the median."""
        self.assertLessEqual(statistics.median(LR2E6_SEED1234), 3.0)

    def test_full_runaway_fails_and_working_adapter_passes(self):
        self.assertFalse(judge_stop_ratios(LR2E6_SEED1235, 3.0)[0])
        self.assertTrue(judge_stop_ratios(LR1E6_SHIPPED, 3.0)[0])

    def test_a_line_exactly_at_the_threshold_passes(self):
        self.assertTrue(judge_stop_ratios([1.0, 3.0, 1.0], 3.0)[0])


if __name__ == "__main__":
    unittest.main()
