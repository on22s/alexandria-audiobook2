"""An arm that scored nothing must not be written as 0.0% accuracy.

WHAT THIS COST. On 2026-09-05 two FP8 artifacts were reported as "base 0.0,
tuned 0.0 - nothing was scored", a story was built around an FP8 kernel
failure, and work was queued to fix it. Both artifacts held 383 rows per arm,
zero errors, and 71 predictions that differed between arms. The runs were fine.

The reading came from `correct / max(n, 1)`, which hands back a confident 0.0
for an empty arm - so a missing measurement and a measured zero are the same
number downstream, and no reader can tell them apart. That is the shape Rule 21
names: a fallback that returns a plausible value turns a failure into a result.

An empty arm now yields None and fails validation, so it cannot be written at
all. These tests pin both halves, and one asserts the OLD behaviour would fail
them, so the fixtures cannot quietly stop discriminating.
"""
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

from experiments.manifest import ExperimentRecord  # noqa: E402

GOLD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "fixtures", "attribution_gold_mushoku16.json")
LOADED = {"loaded": True, "context_length": 32768, "parallel": 1,
          "optimized": True}


def _record():
    return ExperimentRecord(
        name="unit", repo=REPO, model_name="test-model",
        base_url="http://localhost:1234/v1", gold_path=GOLD,
        decoding={"temperature": 0.0, "max_tokens": 24},
        environment=LOADED)


class EmptyArmHasNoAccuracy(unittest.TestCase):

    def _filled(self, correct):
        r = _record()
        for i, ok in enumerate(correct):
            r.add("base", f"id{i}", "line", "GOLD",
                  "GOLD" if ok else "OTHER", 1 if ok else 0,
                  candidates=["GOLD", "OTHER"])
        return r

    def test_a_scored_arm_reports_its_accuracy(self):
        r = self._filled([True, True, False, False])
        self.assertAlmostEqual(r.summary()["base"]["accuracy"], 0.5)

    def test_a_genuine_zero_is_still_zero(self):
        """The distinction only matters if a real 0.0 survives it."""
        r = self._filled([False, False, False])
        self.assertEqual(r.summary()["base"]["accuracy"], 0.0)
        self.assertEqual(r.summary()["base"]["n"], 3)

    def test_an_arm_with_no_rows_has_no_accuracy(self):
        r = _record()
        r.rows.append({"arm": "empty", "id": "x", "line": "l", "gold": "G",
                       "predicted": None, "correct": 0, "in_candidates": 0,
                       "candidates": []})
        r.rows.clear()
        self.assertEqual(r.summary(), {})

    def test_validate_refuses_a_record_with_no_rows(self):
        r = _record()
        problems = r.validate()
        self.assertTrue(any("nothing was measured" in p for p in problems),
                        problems)

    def test_write_refuses_a_record_that_measured_nothing(self):
        r = _record()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                r.write(os.path.join(tmp, "out.json"))

    def test_the_old_formula_would_have_passed_these(self):
        """Guards the guard: `correct / max(n, 1)` reports 0.0 for an empty arm,
        which is exactly the value a reader cannot distinguish from a real
        result. If this ever stops being true the fixtures are not
        discriminating any more."""
        n, correct = 0, 0
        old = correct / max(n, 1)
        self.assertEqual(old, 0.0)
        new = correct / n if n else None
        self.assertIsNone(new)


if __name__ == "__main__":
    unittest.main()


class AnsweredNothingIsNotZero(unittest.TestCase):
    """383 rows, every prediction None, and a stored accuracy of 0.0.

    The guard above was written for an EMPTY arm and cannot see a full one
    that generated no text. Two FP8 arms on 2026-09-04 held 383 rows each with
    every `predicted` and every `raw_response` None, and were written as
    accuracy 0.0 - so a dead run and a model that got everything wrong are the
    same number downstream. `n` counts attempts; only `answered` counts
    attempts that produced something.

    Scanning the 84 stored distill_eval artifacts with the retroactive check
    flags 4 of them, 8 arms: these two FP8 pairs and two 6-row smoke runs.
    """

    def test_an_arm_that_answered_nothing_has_no_accuracy(self):
        rec = _record()
        for i in range(8):
            rec.add("tuned", f"g{i}", "line", "ALICE", None, 0,
                    candidates=["ALICE", "BOB"])
        bucket = rec.summary()["tuned"]
        self.assertEqual(bucket["n"], 8)
        self.assertEqual(rec._answered(rec.rows)["tuned"], 0)
        self.assertNotIn("answered", bucket,
                         "the bucket shape must not change: every reader that "
                         "compares a recomputed summary to a stored one would "
                         "see all artifacts differ at once")
        self.assertIsNone(bucket["accuracy"],
                          "a run that generated nothing must not report 0.0")
        self.assertIsNone(bucket["conditional"])

    def test_the_old_rule_would_have_called_it_zero(self):
        """Pins the discrimination, so the fixture cannot quietly stop working."""
        rec = _record()
        for i in range(8):
            rec.add("tuned", f"g{i}", "line", "ALICE", None, 0,
                    candidates=["ALICE", "BOB"])
        b = rec.summary()["tuned"]
        old = b["correct"] / max(b["n"], 1)          # what it used to compute
        self.assertEqual(old, 0.0)
        self.assertIsNone(b["accuracy"], "the new rule must disagree with 0.0")

    def test_such_an_arm_fails_validation(self):
        rec = _record()
        for i in range(8):
            rec.add("tuned", f"g{i}", "line", "ALICE", None, 0,
                    candidates=["ALICE", "BOB"])
        problems = rec.validate()
        self.assertTrue(any("not one prediction" in p for p in problems),
                        f"expected a no-prediction refusal, got {problems}")

    def test_one_real_answer_is_enough_to_be_a_measurement(self):
        """A model that mostly failed still measured something; do not over-refuse."""
        rec = _record()
        for i in range(7):
            rec.add("tuned", f"g{i}", "line", "ALICE", None, 0,
                    candidates=["ALICE", "BOB"])
        rec.add("tuned", "g7", "line", "ALICE", "ALICE", 1,
                candidates=["ALICE", "BOB"])
        b = rec.summary()["tuned"]
        self.assertEqual(rec._answered(rec.rows)["tuned"], 1)
        self.assertAlmostEqual(b["accuracy"], 1 / 8)
        self.assertFalse(any("not one prediction" in p for p in rec.validate()))

    def test_whitespace_is_not_an_answer(self):
        rec = _record()
        for i in range(4):
            rec.add("tuned", f"g{i}", "line", "ALICE", "   ", 0,
                    candidates=["ALICE", "BOB"])
        self.assertEqual(rec._answered(rec.rows)["tuned"], 0)
        self.assertIsNone(rec.summary()["tuned"]["accuracy"])

    def test_a_stored_artifact_is_reported_not_refused(self):
        """Dead runs already on disk are INDEXED, not treated as unreadable.

        Kept separate from validate_stored_summary on purpose: that answers
        "does the summary follow from the rows", and the artifact audit treats
        a No as fatal. Here the summary follows perfectly and both describe a
        run that generated nothing. Making it fatal blocked every regeneration
        over historical artifacts and would have pressured someone into
        deleting evidence.
        """
        from experiments.manifest import unanswered_arms, validate_stored_summary
        doc = {"summary": {"base": {"n": 3, "correct": 0, "accuracy": 0.0}},
               "rows": [{"arm": "base", "id": f"g{i}", "correct": False,
                         "predicted": None} for i in range(3)]}
        self.assertEqual(unanswered_arms(doc), {"base": 3})
        self.assertEqual(validate_stored_summary(doc), [],
                         "a dead run must not read as an unverifiable summary")

    def test_a_healthy_stored_artifact_is_not_flagged(self):
        from experiments.manifest import unanswered_arms, validate_stored_summary
        doc = {"summary": {"base": {"n": 2, "correct": 1, "accuracy": 0.5}},
               "rows": [{"arm": "base", "id": "g0", "correct": True,
                         "predicted": "ALICE"},
                        {"arm": "base", "id": "g1", "correct": False,
                         "predicted": "BOB"}]}
        self.assertEqual(validate_stored_summary(doc), [])
        self.assertEqual(unanswered_arms(doc), {})
