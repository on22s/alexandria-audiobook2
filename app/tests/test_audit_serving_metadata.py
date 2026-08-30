import json
import os
import tempfile
import unittest

from experiments.audit_serving_metadata import audit_artifact, summarize_base


class ServingMetadataAuditTests(unittest.TestCase):
    def write_artifact(self, directory, arms, notes="", decoding=None):
        path = os.path.join(directory, "artifact.json")
        rows = [{"arm": arm, "id": "book:1", "correct": arm == "base",
                 "predicted": "NAME"} for arm in arms]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"meta": {"model": "model", "notes": notes,
                                 "decoding": decoding or {}},
                       "rows": rows}, handle)
        return path

    def test_base_only_rejects_the_legacy_paired_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_artifact(
                directory, ["base"], "Arms differ only by the adapter scale",
                {"base_quant": "Q4_K_M", "lora": "f16"})
            result = audit_artifact(path)
        self.assertEqual(["base"], result["actual_arms"])
        self.assertEqual(["base_quant", "lora"],
                         result["unsupported_decoding_fields"])
        self.assertTrue(result["false_paired_claim"])
        self.assertFalse(result["measurement_changed"])

    def test_corrected_metadata_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_artifact(
                directory, ["base"], "Base-only serving evaluation",
                {"arms": ["base"]})
            result = audit_artifact(path)
        self.assertEqual([], result["unsupported_decoding_fields"])
        self.assertFalse(result["false_paired_claim"])

    def test_baseline_summary_keeps_book_and_unanswered_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "artifact.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"meta": {"model": "model"}, "rows": [
                    {"arm": "base", "id": "a:1", "correct": True,
                     "predicted": "A"},
                    {"arm": "base", "id": "a:2", "correct": False,
                     "predicted": None},
                    {"arm": "lora", "id": "a:1", "correct": False,
                     "predicted": None}]}, handle)
            result = summarize_base(path)
        self.assertEqual({"rows": 2, "correct": 1, "unanswered": 1},
                         result["pooled"])
        self.assertEqual(result["pooled"], result["per_book"]["a"])


if __name__ == "__main__":
    unittest.main()
