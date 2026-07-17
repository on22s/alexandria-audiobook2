from pathlib import Path
import tempfile
import unittest

from train_lora import deduplicate_evaluation_candidates, save_evaluation_candidate


class LoraCandidateRetentionTests(unittest.TestCase):
    def test_candidate_save_is_bounded_and_does_not_mutate_records_input(self):
        class FakeModel:
            def __init__(self):
                self.saved = []

            def save_pretrained(self, path):
                Path(path).mkdir(parents=True)
                Path(path, "adapter_model.safetensors").write_bytes(b"weights")
                self.saved.append(path)

        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel()
            original = []
            records, first = save_evaluation_candidate(model, tmp, original, 2, 1, 4.8)
            records, second = save_evaluation_candidate(model, tmp, records, 2, 2, 4.5)
            records, third = save_evaluation_candidate(model, tmp, records, 2, 3, 4.2)

        self.assertEqual([], original)
        self.assertEqual(["epoch_001", "epoch_002"], [record["id"] for record in records])
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertEqual(2, len(model.saved))

    def test_final_dedup_removes_production_and_candidate_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            output.joinpath("adapter_model.safetensors").write_bytes(b"production")
            records = []
            for candidate_id, weights in (
                    ("epoch_001", b"distinct"),
                    ("epoch_002", b"production"),
                    ("epoch_003", b"distinct")):
                path = output / "candidates" / candidate_id
                path.mkdir(parents=True)
                path.joinpath("adapter_model.safetensors").write_bytes(weights)
                records.append({"id": candidate_id, "epoch": 1, "loss": 4.5,
                                "path": str(path)})

            retained, skipped, production_hash = deduplicate_evaluation_candidates(
                str(output), records)

            self.assertEqual(["epoch_001"], [record["id"] for record in retained])
            self.assertEqual(
                [("epoch_002", "production"), ("epoch_003", "epoch_001")],
                [(record["id"], record["duplicate_of"]) for record in skipped],
            )
            self.assertEqual(64, len(production_hash))
            self.assertTrue(Path(retained[0]["path"]).is_dir())
            self.assertFalse((output / "candidates" / "epoch_002").exists())
            self.assertFalse((output / "candidates" / "epoch_003").exists())


if __name__ == "__main__":
    unittest.main()
