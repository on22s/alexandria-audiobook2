import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lora_training_benchmark import execute_fixture


class LoraTrainingBenchmarkTests(unittest.TestCase):
    def _fixture(self, root):
        dataset = Path(root, "dataset")
        dataset.mkdir()
        audio = Path(dataset, "sample.wav")
        audio.write_bytes(b"audio")
        metadata = Path(dataset, "metadata.jsonl")
        metadata.write_text(
            '{"audio_filepath":"sample.wav","text":"Words."}\n', encoding="utf-8")
        return {"id": "calibration", "root_dir": root, "dataset_path": "dataset",
                "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
                "sample_count": 1,
                "audio_sha256": {"sample.wav": hashlib.sha256(b"audio").hexdigest()},
                "epochs": 1, "seed": 42, "lr": 1e-6, "lora_r": 8,
                "lora_alpha": 16, "grad_accum": 1, "language": "english"}

    def test_execute_fixture_rejects_changed_audio_before_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            Path(tmp, "dataset", "sample.wav").write_bytes(b"changed")
            with patch("lora_training_benchmark.subprocess.run") as run, \
                 self.assertRaisesRegex(ValueError, "audio hash changed"):
                execute_fixture(fixture, "python", "train.py", str(Path(tmp, "out")))
        run.assert_not_called()

    def test_execute_fixture_verifies_produced_adapter_and_reports_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            output_root = Path(tmp, "output")

            def fake_run(command, **kwargs):
                output_dir = Path(command[command.index("--output_dir") + 1])
                output_dir.mkdir(parents=True)
                adapter = Path(output_dir, "adapter_model.safetensors")
                adapter.write_bytes(b"adapter")
                Path(output_dir, "training_meta.json").write_text(json.dumps({
                    "training_time_seconds": 2.0, "num_samples": 1, "epochs": 1,
                    "final_loss": 4.8, "best_loss": 4.8, "oom_skips": 0,
                    "checkpoint_sha256": hashlib.sha256(b"adapter").hexdigest()}))
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("lora_training_benchmark.subprocess.run", side_effect=fake_run):
                metrics = execute_fixture(fixture, "python", "train.py", str(output_root))
        self.assertEqual(0.5, metrics["samples_per_second"])
        self.assertEqual(0, metrics["oom_skips"])


if __name__ == "__main__":
    unittest.main()
