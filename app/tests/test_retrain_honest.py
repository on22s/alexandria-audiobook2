import json
import os
import tempfile
import unittest

from experiments.retrain_honest import load_resumed_results


class RetrainResumeTests(unittest.TestCase):
    def test_resume_restores_completed_adapter_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "result.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"seed": 1234, "reference_rank": 1,
                           "results": [{"adapter": "done"}]}, handle)
            self.assertEqual([{"adapter": "done"}],
                             load_resumed_results(path, True, 1234, 1))

    def test_resume_refuses_a_different_reference_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "result.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"seed": 1234, "reference_rank": 0,
                           "results": []}, handle)
            with self.assertRaisesRegex(ValueError, "settings do not match"):
                load_resumed_results(path, True, 1234, 1)


if __name__ == "__main__":
    unittest.main()
