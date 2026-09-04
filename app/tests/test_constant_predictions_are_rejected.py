"""A predictor that returns one value regardless of input measured nothing.

#448 made an ALL-EMPTY arm fail validation. This is the other shape of the
same nothing, and that guard cannot see it: on 2026-09-04 a stage1_only arm
answered UNKNOWN on all 88 lines of BOTH arms - every batch accepted, every
row carrying a "prediction", `meta.validation` of "ok", and a diagnostic that
answered nothing while reporting a clean 0.0-point tie.

The model had emitted clean JSON, `[{"n": 0, "speaker": "CARISSA"}, ...]`. The
serialiser looked for `ENTRY 0: CARISSA`, found nothing, and fell through to a
constant.
"""
import unittest

from experiments.manifest import ExperimentRecord


def _record(rows):
    r = ExperimentRecord.__new__(ExperimentRecord)
    r.rows = rows
    r.meta = {"git": {"harness_sha256": "x"}, "model": "m"}
    r.started = 0.0
    return r


def _rows(arm, n, value):
    return [{"arm": arm, "id": i, "predicted": value, "correct": False,
             "in_candidates": True} for i in range(n)]


class ConstantPredictions(unittest.TestCase):
    def test_a_constant_answer_is_rejected(self):
        problems = _record(_rows("tuned", 88, "UNKNOWN")).validate({})
        self.assertTrue(any("every one of 88 predictions is" in p
                            for p in problems), problems)

    def test_variety_passes(self):
        rows = _rows("tuned", 40, "A")
        for r in rows[:10]:
            r["predicted"] = "B"
        self.assertFalse(any("constant predictor" in p
                             for p in _record(rows).validate({})), "flagged")

    def test_a_small_arm_is_not_judged(self):
        """Ten lines can share an answer by chance; 88 cannot."""
        problems = _record(_rows("tuned", 10, "UNKNOWN")).validate({})
        self.assertFalse(any("constant predictor" in p for p in problems))

    def test_the_empty_case_still_reports_as_empty(self):
        """All-None must keep its own clearer message, not become 'constant'."""
        problems = _record(_rows("tuned", 88, None)).validate({})
        self.assertTrue(any("every prediction is empty" in p for p in problems))
        self.assertFalse(any("constant predictor" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
