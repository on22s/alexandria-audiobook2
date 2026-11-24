"""Annotate-phase confidence values are validated before the quality filter."""
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import alexandria_preparer_rocm_compatible as preparer


class AnnotateConfidenceTests(unittest.TestCase):
    def run_annotation(self, confidence):
        words = [{"word": "Only.", "start": 0.0, "end": 1.2,
                  "confidence": confidence, "speaker": "ONLY"}]
        samples = np.sin(np.arange(28800) * .05).astype("float32") * .1
        llm = SimpleNamespace(create_chat_completion=lambda **request: {
            "choices": [{"message": {"content": "Only."}}]})
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(preparer, "ensure_run_manifest"), \
                        patch.object(preparer, "_load_llm", return_value=llm), \
                        patch.object(preparer, "log_gpu_stats"), \
                        patch.object(preparer, "_calculate_chunk_snr",
                                     return_value=80):
                    rows = preparer.annotate_chunks(
                        words, "mock.gguf", 2.0, samples,
                        min_chunk_duration=1, batch_size=1,
                        run_identity={"test": "fixture"})
                rejected_path = Path("dataset_temp/rejected_chunks.jsonl")
                rejected = ([json.loads(line) for line in
                             rejected_path.read_text().splitlines()]
                            if rejected_path.exists() else [])
                return rows, rejected
            finally:
                os.chdir(old)

    def test_invalid_confidence_drops_the_chunk_instead_of_crashing(self):
        for bad in (None, "high", 10**400, math.nan):
            with self.subTest(confidence=bad):
                rows, rejected = self.run_annotation(bad)
                self.assertEqual([], rows)
                self.assertEqual(1, len(rejected))
                self.assertIn("invalid_confidence", rejected[0]["reason"])

    def test_numeric_confidence_still_passes_the_quality_filter(self):
        rows, rejected = self.run_annotation(0.95)
        self.assertEqual(["Only."], [row["text"] for row in rows])
        self.assertEqual([], rejected)


if __name__ == "__main__":
    unittest.main()
