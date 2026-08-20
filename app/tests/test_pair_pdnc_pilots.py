"""The scorer that decided three PDNC pilots had shown nothing.

Rule 21: hand-check a metric on cases whose answer is already known, INCLUDING
cases it must reject, before trusting it on 600 rows x 3 pilots.
"""
import json
import os
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.pair_pdnc_pilots import arms_of, score  # noqa: E402


def artifact(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"meta": {}, "summary": {}, "rows": rows}, handle)
    return path


def pilot(tmp, name, baseline, arm, arm_name="evidence"):
    rows = [{"arm": "baseline", "id": i, "correct": c}
            for i, c in enumerate(baseline)]
    rows += [{"arm": arm_name, "id": i, "correct": c} for i, c in enumerate(arm)]
    return artifact(os.path.join(tmp, name + ".json"), rows)


class PairedScoringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_an_arm_identical_to_its_baseline_shows_nothing(self):
        """No discordant pairs must read as p=1.0, not as a tie broken somehow."""
        same = [True, False, True, True, False] * 20
        result = score([pilot(self.tmp.name, "same", same, list(same))])
        row = result["pilots"][0]
        self.assertEqual(0, row["arm_only_wins"])
        self.assertEqual(0, row["baseline_only_wins"])
        self.assertEqual(1.0, row["p_value"])

    def test_the_same_accuracy_by_different_rows_is_still_no_evidence(self):
        """THE CASE THE HEADLINE PERCENTAGES CANNOT SEE. Both arms score 50%,
        but on disjoint halves - equal churn, and a between-arm comparison of
        totals would report a dead heat with no hint that 100 rows moved."""
        baseline = [True, False] * 50
        arm = [False, True] * 50
        row = score([pilot(self.tmp.name, "churn", baseline, arm)])["pilots"][0]
        self.assertEqual(row["baseline_accuracy"], row["arm_accuracy"])
        self.assertEqual(50, row["arm_only_wins"])
        self.assertEqual(50, row["baseline_only_wins"])
        self.assertGreater(row["p_value"], 0.5)

    def test_a_real_one_sided_improvement_is_detected(self):
        baseline = [False] * 100
        arm = [True] * 30 + [False] * 70
        row = score([pilot(self.tmp.name, "real", baseline, arm)])["pilots"][0]
        self.assertEqual(30, row["arm_only_wins"])
        self.assertEqual(0, row["baseline_only_wins"])
        self.assertLess(row["p_value"], 1e-6)

    def test_the_noise_floor_compares_baselines_not_arms(self):
        """Two runs of the identical condition that disagree on 10 rows must
        report 10 - this is the yardstick every p is read against, and reading
        an ARM here by mistake would make the floor look enormous."""
        base_a = [True] * 100
        base_b = [False] * 10 + [True] * 90
        paths = [pilot(self.tmp.name, "run_a", base_a, base_a),
                 pilot(self.tmp.name, "run_b", base_b, base_b)]
        floor = score(paths)["baseline_noise_floor"]
        self.assertEqual(1, len(floor))
        self.assertEqual(10, floor[0]["rows_disagreeing"])
        self.assertAlmostEqual(0.1, floor[0]["fraction"])

    def test_rows_are_matched_by_id_not_by_position(self):
        """A paired test that zipped two lists would silently compare unrelated
        lines whenever the arms are written in different orders."""
        rows = [{"arm": "baseline", "id": "a", "correct": True},
                {"arm": "baseline", "id": "b", "correct": False},
                {"arm": "evidence", "id": "b", "correct": False},
                {"arm": "evidence", "id": "a", "correct": True}]
        path = artifact(os.path.join(self.tmp.name, "order.json"), rows)
        row = score([path])["pilots"][0]
        self.assertEqual(0, row["arm_only_wins"] + row["baseline_only_wins"])

    def test_an_artifact_with_no_baseline_is_refused(self):
        rows = [{"arm": "evidence", "id": 1, "correct": True},
                {"arm": "sequence", "id": 1, "correct": False}]
        path = artifact(os.path.join(self.tmp.name, "nobase.json"), rows)
        with self.assertRaises(SystemExit):
            score([path])

    def test_a_third_arm_is_refused_rather_than_picked_from(self):
        """Choosing one of two candidate arms silently would make the result
        depend on dict ordering."""
        rows = [{"arm": "baseline", "id": 1, "correct": True},
                {"arm": "evidence", "id": 1, "correct": False},
                {"arm": "sequence", "id": 1, "correct": True}]
        path = artifact(os.path.join(self.tmp.name, "three.json"), rows)
        with self.assertRaises(SystemExit):
            score([path])

    def test_arms_of_reads_the_real_committed_pilot(self):
        """The fixtures above are synthetic; this one asserts the reader still
        matches the shape of the artifacts actually on disk."""
        real = os.path.join(os.path.dirname(APP), "ab_test_runtime",
                            "experiments",
                            "pdnc_sequence__pilot__local-llamacpp.json")
        if not os.path.exists(real):
            self.skipTest("pilot artifact not present")
        arms = arms_of(real)
        self.assertIn("baseline", arms)
        self.assertEqual(600, len(arms["baseline"]))


if __name__ == "__main__":
    unittest.main()
