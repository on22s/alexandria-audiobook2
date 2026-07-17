from pathlib import Path
import tempfile
import unittest

from train_lora import save_evaluation_candidate


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


if __name__ == "__main__":
    unittest.main()
