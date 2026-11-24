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


def word(text, start, end, confidence):
    return {"word": text, "start": start, "end": end,
            "confidence": confidence, "speaker": "ONLY"}


class AnnotateConfidenceTests(unittest.TestCase):
    def make_llm(self, calls=None):
        def complete(**request):
            prompt = request["messages"][-1]["content"]
            text = prompt.split("Annotate this segment:\n", 1)[-1]
            if calls is not None:
                calls.append(text)
            return {"choices": [{"message": {"content": text}}]}
        return SimpleNamespace(create_chat_completion=complete)

    def run_annotation(self, words, *, workdir, resume=False, llm=None):
        samples = np.sin(np.arange(57600) * .05).astype("float32") * .1
        llm = llm or self.make_llm()
        old = Path.cwd()
        os.chdir(workdir)
        try:
            with patch.object(preparer, "ensure_run_manifest"), \
                    patch.object(preparer, "_load_llm", return_value=llm) as load, \
                    patch.object(preparer, "log_gpu_stats"), \
                    patch.object(preparer, "_calculate_chunk_snr", return_value=80), \
                    self.assertLogs("alexandria", level="INFO") as logs:
                rows = preparer.annotate_chunks(
                    words, "mock.gguf", 1.0, samples, resume=resume,
                    min_chunk_duration=1, batch_size=1,
                    run_identity={"test": "fixture"})
            rejected_path = Path("dataset_temp/rejected_chunks.jsonl")
            rejected = ([json.loads(line) for line in
                         rejected_path.read_text().splitlines()]
                        if rejected_path.exists() else [])
            return rows, rejected, "\n".join(logs.output), load
        finally:
            os.chdir(old)

    def test_invalid_confidence_drops_the_chunk_instead_of_crashing(self):
        for bad in (None, "high", True, 10**400, math.nan):
            with self.subTest(confidence=bad), \
                    tempfile.TemporaryDirectory() as tmp:
                rows, rejected, logs, _ = self.run_annotation(
                    [word("Only.", 0.0, 1.2, bad)], workdir=tmp)
                self.assertEqual([], rows)
                self.assertEqual(1, len(rejected))
                self.assertTrue(rejected[0]["reason"].startswith(
                    "invalid_confidence"), rejected[0]["reason"])
                self.assertIn("Dropped chunks (before LLM):    1", logs)

    def test_numeric_confidence_still_passes_the_quality_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, rejected, _, _ = self.run_annotation(
                [word("Only.", 0.0, 1.2, 0.95)], workdir=tmp)
        self.assertEqual(["Only."], [row["text"] for row in rows])
        self.assertEqual([], rejected)

    def test_boolean_confidence_is_rejected_as_non_numeric(self):
        # bool is a Real: without the explicit guard True/False would average
        # as 1.0/0.0 and silently stand in for a quality measurement.
        for bad in (True, False):
            with self.subTest(confidence=bad), self.assertRaisesRegex(
                    ValueError, "non-numeric confidence"):
                preparer.get_average_word_confidence(
                    [{"word": "Only.", "confidence": bad}])

    def test_invalid_confidence_is_counted_like_a_low_confidence_drop(self):
        for confidence, reason in ((math.nan, "invalid_confidence"),
                                   (0.2, "low_confidence")):
            with self.subTest(confidence=confidence), \
                    tempfile.TemporaryDirectory() as tmp:
                rows, rejected, logs, _ = self.run_annotation(
                    [word("Only.", 0.0, 1.2, confidence)], workdir=tmp)
                self.assertEqual([], rows)
                self.assertEqual(1, len(rejected))
                self.assertTrue(rejected[0]["reason"].startswith(reason),
                                rejected[0]["reason"])
                self.assertIn("Dropped chunks (before LLM):    1", logs)

    def test_resume_does_not_redo_or_duplicate_the_rejected_chunk(self):
        bad = word("Bad.", 0.0, 1.2, math.nan)
        good = word("Good.", 1.2, 2.4, 0.95)
        with tempfile.TemporaryDirectory() as tmp:
            rows, rejected, logs, load = self.run_annotation(
                [bad, good], workdir=tmp)
            self.assertEqual(["Good."], [row["text"] for row in rows])
            self.assertEqual(1, len(rejected))
            self.assertTrue(rejected[0]["reason"].startswith("invalid_confidence"))
            metadata = Path(tmp, "dataset_temp", "metadata.jsonl").read_text()
            samples = sorted(p.name for p in
                             Path(tmp, "dataset_temp").glob("sample_*.wav"))
            self.assertEqual(["sample_0000.wav"], samples)
            load.assert_called_once()

            calls = []
            rows, rejected, logs, load = self.run_annotation(
                [bad, good], workdir=tmp, resume=True,
                llm=self.make_llm(calls))
            self.assertEqual(["Good."], [row["text"] for row in rows])
            self.assertEqual(1, len(rejected))
            self.assertTrue(rejected[0]["reason"].startswith("invalid_confidence"))
            self.assertEqual(
                metadata, Path(tmp, "dataset_temp", "metadata.jsonl").read_text())
            self.assertEqual(
                ["sample_0000.wav"],
                sorted(p.name for p in
                       Path(tmp, "dataset_temp").glob("sample_*.wav")))
            load.assert_not_called()
            self.assertEqual([], calls)
            self.assertIn("No annotation words remain", logs)


if __name__ == "__main__":
    unittest.main()
