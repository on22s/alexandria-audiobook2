import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("evaluate_lora", ROOT / "evaluate_lora.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


class LoraEvaluationTests(unittest.TestCase):
    def test_adapter_evaluation_generates_both_probes_and_scores_them(self):
        class FakeEngine:
            def generate_voice(self, output_path, **_kwargs):
                times = np.arange(8000, dtype=np.float32) / 16000
                sf.write(output_path, 0.1 * np.sin(2 * np.pi * 180 * times), 16000)
                return True

        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp, "voice")
            adapter_dir.mkdir()
            sf.write(adapter_dir / "ref_sample.wav", np.ones(8000) * 0.1, 16000)
            with patch.object(evaluation, "get_speaker_similarity", return_value=0.9):
                result = evaluation.evaluate_adapter(
                    {"id": "voice"}, tmp, FakeEngine(), object(), "cpu")

        self.assertEqual("pass", result["status"])
        self.assertEqual(["narration", "dialogue"],
                         [probe["id"] for probe in result["probes"]])
        self.assertTrue(all(probe["metrics"]["speaker_similarity"] == 0.9
                            for probe in result["probes"]))

    def test_audio_metrics_detect_silence_and_clipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "probe.wav"))
            audio = np.concatenate((np.zeros(8000), np.ones(4000), np.full(4000, 0.1)))
            sf.write(path, audio, 16000, subtype="FLOAT")

            metrics = evaluation.get_audio_metrics(path)

        self.assertAlmostEqual(1.0, metrics["duration_seconds"])
        self.assertAlmostEqual(0.5, metrics["silence_ratio"], places=3)
        self.assertAlmostEqual(0.25, metrics["clipping_ratio"], places=3)

    def test_warning_thresholds_are_warning_only(self):
        warnings = evaluation.get_warnings({
            "speaker_similarity": 0.2, "silence_ratio": 0.8, "clipping_ratio": 0.1,
        })
        self.assertEqual(["low_speaker_similarity", "excess_silence", "clipping"], warnings)

    def test_candidate_recommendation_prefers_quality_and_never_promotes(self):
        def result(similarity, warnings=None):
            return {"warnings": warnings or [], "probes": [{"metrics": {
                "speaker_similarity": similarity, "clipping_ratio": 0.0,
                "silence_ratio": 0.1,
            }}]}
        recommendation = evaluation.get_candidate_recommendation({
            "production": result(0.7, ["low_speaker_similarity"]),
            "epoch_001": result(0.8),
            "epoch_002": result(0.75),
        })
        self.assertEqual("epoch_001", recommendation["recommended"])
        self.assertTrue(recommendation["production_unchanged"])

    def test_candidate_cleanup_keeps_only_recommended_generated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "candidates")
            for name in ("epoch_001", "epoch_002"):
                Path(root, name).mkdir(parents=True)
            removed = evaluation.cleanup_candidates(tmp, "epoch_002")
            self.assertEqual(["epoch_001"], removed)
            self.assertFalse(Path(root, "epoch_001").exists())
            self.assertTrue(Path(root, "epoch_002").is_dir())

    def test_manifest_candidate_records_match_retained_recommendation(self):
        records = [{"id": "epoch_001"}, {"id": "epoch_002"}]

        self.assertEqual(
            [{"id": "epoch_002"}],
            evaluation.get_retained_candidate_records(records, "epoch_002"),
        )
        self.assertEqual(
            [], evaluation.get_retained_candidate_records(records, "production")
        )

    def test_resume_requires_version_probes_and_audio_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = {"version": evaluation.EVALUATION_VERSION, "probes": []}
            for probe_id, _text in evaluation.PROBES:
                filename = f"evaluation_{probe_id}.wav"
                Path(tmp, filename).write_bytes(b"audio")
                result["probes"].append({"id": probe_id, "audio_file": filename})
            self.assertTrue(evaluation.is_complete_evaluation(result, tmp))
            Path(tmp, "evaluation_dialogue.wav").unlink()
            self.assertFalse(evaluation.is_complete_evaluation(result, tmp))


if __name__ == "__main__":
    unittest.main()
