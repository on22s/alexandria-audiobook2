"""Malformed cached JSON must be rebuilt before summary aggregation."""
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf
from tests.test_voice_dataset_quality import quality


def get_corrupt_reports(report):
    for key, value in (("source", []), ("clip_count", "1"), ("clip_count", True),
                       ("clip_count", 20), ("warning_clip_count", None),
                       ("warning_clip_count", 20), ("clips", [0])):
        changed = copy.deepcopy(report)
        changed[key] = value
        yield key, changed
    for key, value in (("path", []), ("pcm_sha256", {}), ("warnings", "low_snr"),
                       ("warnings", [3]), ("metrics", [])):
        changed = copy.deepcopy(report)
        changed["clips"][0][key] = value
        yield key, changed
    yield "top-level list", []


class DatasetAuditCacheShapeTests(unittest.TestCase):
    def make_archive(self, root):
        narrator = root / "reader"
        narrator.mkdir()
        archive = narrator / "dataset.zip"
        audio = io.BytesIO()
        sf.write(audio, np.full(16000, .1, dtype=np.float32), 16000, format="WAV")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("train/clip.wav", audio.getvalue())
            handle.writestr("val/bad.wav", b"invalid audio")
        return archive

    def test_cache_shapes_are_rejected_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.make_archive(Path(directory))
            fingerprint = quality.get_file_fingerprint(archive)
            report = quality.audit_zip(archive, fingerprint)
            self.assertTrue(quality.is_reusable_report(report, fingerprint))
            for name, changed in get_corrupt_reports(report):
                with self.subTest(name=name):
                    original = copy.deepcopy(changed)
                    self.assertFalse(quality.is_reusable_report(changed, fingerprint))
                    self.assertEqual(changed, original)

    def test_cli_rebuilds_corrupt_cache_and_reuses_valid_warning_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.make_archive(root)
            original_zip = archive.read_bytes()
            fingerprint = quality.get_file_fingerprint(archive)
            valid = quality.audit_zip(archive, fingerprint)
            output = root / "_quality"
            output.mkdir()
            cache = output / (hashlib.sha256(b"reader/dataset.zip").hexdigest()[:16] + ".json")
            for name, corrupted in get_corrupt_reports(valid):
                with self.subTest(name=name):
                    cache.write_text(json.dumps(corrupted))
                    with patch.object(sys, "argv", ["audit_voice_datasets.py", "--zips2", str(root)]), \
                         patch.object(quality, "audit_zip", wraps=quality.audit_zip) as audit, \
                         redirect_stdout(io.StringIO()):
                        self.assertEqual(quality.main(), 0)
                        audit.assert_called_once()
                        rebuilt = json.loads(cache.read_text())
                        self.assertTrue(quality.is_reusable_report(rebuilt, fingerprint))
                        self.assertEqual(
                            [{k: v for k, v in clip.items() if k != "error"} for clip in rebuilt["clips"]],
                            [{k: v for k, v in clip.items() if k != "error"} for clip in valid["clips"]])
                        self.assertIn("Format not recognised", rebuilt["clips"][1]["error"])
                        summary = json.loads((output / "summary.json").read_text())
                        self.assertEqual(summary["clip_count"], 2)
                        self.assertEqual(summary["warning_clip_count"], valid["warning_clip_count"])
                        audit.reset_mock()
                        before = cache.read_bytes()
                        self.assertEqual(quality.main(), 0)
                        audit.assert_not_called()
                        self.assertEqual(cache.read_bytes(), before)
            self.assertEqual(archive.read_bytes(), original_zip)


if __name__ == "__main__":
    unittest.main()
