import json
import os
import tempfile
import unittest

from experiments.attribution_hybrid import (
    choose_prediction, evaluate, load_rows, select_policy, summarise,
)


def row(row_id, expected, predicted, **extra):
    return {"id": row_id, "expected": expected, "predicted": predicted, **extra}


class AttributionHybridTest(unittest.TestCase):

    def test_evaluate_reports_missing_rows_instead_of_dropping_silently(self):
        rows, coverage = evaluate(
            [row("a", "ALICE", "ALICE"), row("b", "BOB", "BOB")],
            [row("a", "ALICE", "BOB", confidence=.9)], {"kind": "qwen"})
        self.assertEqual(2, len(rows))
        self.assertEqual("BOB", rows[1]["predicted"])
        self.assertEqual({"qwen_rows": 2, "modern_rows": 1, "shared_rows": 1,
                          "missing_from_qwen": 0, "missing_from_modern": 1},
                         coverage)

    def test_evaluate_rejects_duplicate_ids_and_gold_disagreement(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            evaluate([row("a", "A", "A"), row("a", "A", "A")],
                     [row("a", "A", "A")], {"kind": "qwen"})
        with self.assertRaisesRegex(ValueError, "gold disagreement"):
            evaluate([row("a", "A", "A")], [row("a", "B", "B")],
                     {"kind": "qwen"})

    def test_confidence_policy_falls_back_on_abstention_or_low_confidence(self):
        policy = {"kind": "modern_confidence", "threshold": .7}
        self.assertEqual(("Q", "qwen"), choose_prediction(
            row("a", "A", "Q"), row("a", "A", None, confidence=.99), policy))
        self.assertEqual(("Q", "qwen"), choose_prediction(
            row("a", "A", "Q"), row("a", "A", "M", confidence=.69), policy))
        self.assertEqual(("M", "modern"), choose_prediction(
            row("a", "A", "Q"), row("a", "A", "M", confidence=.7), policy))

    def test_select_policy_uses_pilot_labels_and_returns_reusable_policy(self):
        qwen = [row("a", "A", "A"), row("b", "B", "A"), row("c", "C", "A")]
        modern = [row("a", "A", "B", confidence=.1, quote_type="Explicit"),
                  row("b", "B", "B", confidence=.9, quote_type="Implicit"),
                  row("c", "C", "C", confidence=.9, quote_type="Anaphoric")]
        policy, rows, _ = select_policy(qwen, modern)
        self.assertEqual(1.0, summarise(rows)["hybrid_accuracy"])
        applied, _ = evaluate(qwen, modern, policy)
        self.assertEqual(1.0, summarise(applied)["hybrid_accuracy"])

    def test_cli_policy_file_shape_is_json_serializable(self):
        policy, _, _ = select_policy([row("a", "A", "A")],
                                     [row("a", "A", "A", confidence=.5)])
        restored = json.loads(json.dumps({"selected_policy": policy}))
        self.assertTrue(restored["selected_policy"])

    def test_preserves_source_alias_aware_correctness(self):
        qwen = [row("a", "RACHEL LYNDE", "MRS. LYNDE", correct=True)]
        modern = [row("a", "RACHEL LYNDE", "RACHEL LYNDE", correct=True)]
        rows, _ = evaluate(qwen, modern, {"kind": "qwen"})
        self.assertIs(True, rows[0]["qwen_correct"])
        self.assertIs(True, rows[0]["correct"])

    def test_narrator_aware_policy_protects_known_first_person_books(self):
        policy = {"kind": "narrator_aware"}
        qwen = row("a", "JAKE", "JAKE")
        modern = row("a", "JAKE", "BRETT", narrator="JAKE")
        self.assertEqual(("JAKE", "qwen"),
                         choose_prediction(qwen, modern, policy))
        modern["narrator"] = None
        self.assertEqual(("BRETT", "modern"),
                         choose_prediction(qwen, modern, policy))

    def test_load_rows_supports_pdnc_eval_book_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pdnc.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"Book": {"base": {"rows": [
                    {"id": "Book-00001", "expected": "A", "predicted": "A",
                     "correct": True}]}}}, handle)
            self.assertEqual(
                [{"id": "Book:Book-00001", "expected": "A", "predicted": "A",
                  "correct": True}], load_rows(path))


if __name__ == "__main__":
    unittest.main()
