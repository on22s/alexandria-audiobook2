import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from dedup_benchmark import _hash_dataset_content, execute_fixture


class DedupBenchmarkTests(unittest.TestCase):
    def test_changed_audio_is_rejected_before_dedup_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp, "dataset")
            dataset.mkdir()
            Path(dataset, "metadata.jsonl").write_text(
                '{"audio_filepath":"one.wav","text":"One"}\n'
                '{"audio_filepath":"two.wav","text":"Two"}\n')
            Path(dataset, "one.wav").write_bytes(b"changed")
            Path(dataset, "two.wav").write_bytes(b"two")
            fixture = {"root_dir": tmp, "dataset_path": "dataset",
                       "metadata_sha256": hashlib.sha256(
                           Path(dataset, "metadata.jsonl").read_bytes()).hexdigest(),
                       "samples_per_volume": 1,
                       "audio_sha256": {"one.wav": hashlib.sha256(b"one").hexdigest(),
                                        "two.wav": hashlib.sha256(b"two").hexdigest()},
                       "seed": 42}
            with patch("dedup_benchmark.subprocess.run") as run, \
                 self.assertRaisesRegex(ValueError, "audio hash changed"):
                execute_fixture(fixture, "python", "voice_analysis.py")
        run.assert_not_called()

    def test_content_hash_ignores_archive_packaging(self):
        with tempfile.TemporaryDirectory() as tmp:
            hashes = []
            for index in range(2):
                path = Path(tmp, f"{index}.zip")
                with zipfile.ZipFile(path, "w", compression=(
                        zipfile.ZIP_STORED if index == 0 else zipfile.ZIP_DEFLATED)) as archive:
                    archive.writestr("train/sample.wav", b"audio")
                    archive.writestr("metadata.jsonl", json.dumps({
                        "audio_filepath": "train/sample.wav", "text": "Words"}) + "\n")
                hashes.append(_hash_dataset_content(path)[0])
        self.assertEqual(hashes[0], hashes[1])


if __name__ == "__main__":
    unittest.main()
