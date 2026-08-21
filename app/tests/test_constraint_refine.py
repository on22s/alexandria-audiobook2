"""The alternation repair, and the scorer that nearly misreported it.

DiLA (KDD '26) proposes: the LLM makes an initial assignment, a constraint step
repairs it. Attribution looked like the right shape - the gold speaker is in the
roster for 100% of 2,494 rows and the model still answers something else on 857
of them, so every remaining error is a pick the roster already contained.

Tested with one constraint, alternation, and it LOSES: 190 fixed against 472
broken, McNemar p=1.3e-28. Kept as a test because the negative is the result.

THE SCORER HAD TO BE VALIDATED FIRST. Two runs reported a 52.8% baseline where
the artifact's own `correct` field says 65.6% - a 13-point gap that was entirely
alias groups, which live in the FIXTURE and not in the artifact's flat roster.
The paired verdict happened to survive it, but a headline that disagrees with
the artifact it came from is not one to publish.
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class ScorerAgreementTest(unittest.TestCase):
    """The experiment's scorer must agree with the artifact it reads."""

    def test_it_uses_the_harness_scorer_not_its_own(self):
        src = (REPO / "app" / "experiments" / "constraint_refine.py").read_text(
            encoding="utf-8")
        self.assertIn("from experiments.scoring import", src)
        self.assertNotIn("def same_speaker(", src,
                         "a second implementation of the comparison will drift "
                         "from the one that produced the artifact")

    def test_aliases_are_loaded_from_the_fixture_not_the_artifact(self):
        """The artifact's roster is flat names - `LIZZY` and `ELIZABETH` appear
        as separate entries with nothing linking them. Building an alias map
        from it silently produced no aliases at all."""
        src = (REPO / "app" / "experiments" / "constraint_refine.py").read_text(
            encoding="utf-8")
        self.assertIn("--fixtures", src)
        self.assertIn("alias_groups", src)


class RefinementTest(unittest.TestCase):
    def setUp(self):
        from experiments import constraint_refine as m
        self.m = m

    def _rows(self, preds, book="b"):
        return [{"id": f"{book}:Book-{i:05d}", "predicted": p,
                 "expected": p, "candidates": sorted(set(preds))}
                for i, p in enumerate(preds)]

    def test_a_repeated_speaker_is_reassigned_to_the_previous_distinct_one(self):
        rows = self._rows(["ANNE", "BEN", "BEN"])
        repaired, changed = self.m.refine(rows)
        self.assertEqual(1, changed)
        self.assertEqual("ANNE", repaired["b:Book-00002"])

    def test_it_never_invents_a_speaker_outside_the_roster(self):
        """The repair may only move to a name the roster offers, or it
        manufactures a character - the failure the roster check prevents."""
        rows = self._rows(["ANNE", "BEN", "BEN"])
        rows[2]["candidates"] = ["BEN"]          # ANNE not offered here
        repaired, changed = self.m.refine(rows)
        self.assertEqual(0, changed)
        self.assertEqual("BEN", repaired["b:Book-00002"])

    def test_alternation_leaves_a_non_repeating_run_alone(self):
        rows = self._rows(["ANNE", "BEN", "ANNE", "BEN"])
        _repaired, changed = self.m.refine(rows)
        self.assertEqual(0, changed)

    def test_books_are_refined_independently(self):
        """A run must not alternate across a book boundary, where the previous
        speaker is a character from another novel."""
        rows = self._rows(["ANNE", "BEN"], book="one") + \
               self._rows(["BEN"], book="two")
        repaired, changed = self.m.refine(rows)
        self.assertEqual(0, changed)
        self.assertEqual("BEN", repaired["two:Book-00000"])


class RecordedResultTest(unittest.TestCase):
    """The negative, pinned so it is not quietly re-tried."""

    def test_the_recorded_run_shows_the_repair_losing(self):
        path = REPO / "ab_test_runtime" / "experiments" / "constraint_refine.json"
        if not path.exists():
            self.skipTest("artifact not present in this checkout")
        d = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(0.656, round(d["baseline_accuracy"], 3),
                         "baseline must match the artifact's own 65.6%")
        c = d["constraints"]
        self.assertGreater(c["alternation"]["broke"], c["alternation"]["fixed"])
        for name in ("adjacency", "adjacency_120", "adjacency_400"):
            with self.subTest(constraint=name):
                self.assertGreater(c[name]["broke"], c[name]["fixed"],
                                   "every proximity variant loses to the model")
        self.assertEqual(0, c["roster"]["broke"],
                         "roster repair may be small but must never harm")

    def test_the_best_proximity_baseline_is_recorded(self):
        """The model beating nearest-mention by 15.7 points is the finding
        worth keeping; it is evidence FOR the arm, found while trying to
        improve it."""
        path = REPO / "ab_test_runtime" / "experiments" / "constraint_refine.json"
        if not path.exists():
            self.skipTest("artifact not present")
        d = json.loads(path.read_text(encoding="utf-8"))
        best = max(d["constraints"][k]["refined_accuracy"]
                   for k in ("adjacency", "adjacency_120", "adjacency_400"))
        self.assertLess(best, d["baseline_accuracy"])


if __name__ == "__main__":
    unittest.main()
