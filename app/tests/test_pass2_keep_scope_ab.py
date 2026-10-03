"""The keep-scope A/B's verdict is computed, not judged: these pin the gate."""
import os
import tempfile
import unittest

from experiments import pass2_keep_scope_ab as ab


def _summary(line_calls=100, batch_calls=70, line_named=90.0, batch_named=90.0,
             repeat_named=89.6, line_unnamed=10.0, batch_unnamed=10.0):
    return {"line": {"calls": line_calls, "named_pct": line_named, "descriptive_pct": line_unnamed},
            "batch": {"calls": batch_calls, "named_pct": batch_named,
                      "descriptive_pct": batch_unnamed},
            "line_repeat": {"calls": 0, "named_pct": repeat_named, "descriptive_pct": 0}}


class GateTest(unittest.TestCase):
    def gate(self, **kwargs):
        return ab.get_gate(_summary(**kwargs), "batch", "line", "line_repeat")

    def test_passes_with_fewer_calls_and_no_accuracy_loss(self):
        self.assertTrue(self.gate()["passes"])

    def test_fails_when_calls_drop_less_than_twenty_percent(self):
        self.assertFalse(self.gate(batch_calls=81)["passes"])

    def test_named_may_drop_by_the_line_vs_line_spread_only(self):
        self.assertTrue(self.gate(batch_named=89.6)["passes"])        # spread 0.4
        self.assertFalse(self.gate(batch_named=89.5)["passes"])

    def test_the_allowance_never_exceeds_half_a_point(self):
        gate = self.gate(repeat_named=88.0, batch_named=89.4)          # spread 2.0
        self.assertEqual(0.5, gate["named_allowed_drop_pts"])
        self.assertFalse(gate["passes"])

    def test_unnamed_may_not_drop(self):
        self.assertFalse(self.gate(batch_unnamed=9.9)["passes"])


class LogCountTest(unittest.TestCase):
    def test_counts_responses_cost_and_subdivisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("  finish_reason=stop | tokens: prompt=1000000 completion=0 | took 1s\n"
                             "  Attribution batch exhausted; subdividing 25 -> 12 + 13\n"
                             "  finish_reason=stop | tokens: prompt=0 completion=1000000 | took 1s\n"
                             "  Attribution exhausted; kept the model's last answer for 25 entries\n")
            counts = ab.get_log_counts(path)
        self.assertEqual({"calls": 2, "cost_usd": 2.64, "subdivisions": 1, "kept_batches": 1},
                         counts)


if __name__ == "__main__":
    unittest.main()
