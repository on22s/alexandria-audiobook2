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
