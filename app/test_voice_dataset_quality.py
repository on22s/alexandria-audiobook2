import importlib.util
import io
from pathlib import Path
import unittest

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("audit_voice_datasets", ROOT / "audit_voice_datasets.py")
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


class VoiceDatasetQualityTests(unittest.TestCase):
    def make_wav(self, audio, sample_rate=16000):
        output = io.BytesIO()
        sf.write(output, audio, sample_rate, format="WAV", subtype="FLOAT")
        return output.getvalue()

    def test_metrics_and_warnings_detect_real_failure_modes(self):
        audio = np.concatenate((np.zeros(12000), np.ones(4000))).astype(np.float32)
        metrics, digest = quality.get_clip_metrics(self.make_wav(audio))
        warnings = quality.get_clip_warnings(metrics)
        self.assertEqual(64, len(digest))
        self.assertIn("excess_silence", warnings)
        self.assertIn("clipping", warnings)

    def test_reuse_requires_matching_source_and_thresholds(self):
        fingerprint = {"size": 1, "mtime_ns": 2, "edge_sha256": "x"}
        report = {"version": quality.REPORT_VERSION, "source_fingerprint": fingerprint,
                  "thresholds": quality.THRESHOLDS, "clips": []}
        self.assertTrue(quality.is_reusable_report(report, fingerprint))
        changed = dict(fingerprint, size=2)
        self.assertFalse(quality.is_reusable_report(report, changed))

    def test_stationary_noise_is_flagged_as_low_snr(self):
        rng = np.random.default_rng(4)
        metrics, _digest = quality.get_clip_metrics(
            self.make_wav(rng.normal(0, 0.05, 32000).astype(np.float32)))
        self.assertIn("low_snr", quality.get_clip_warnings(metrics))

    def test_summary_reports_cross_archive_pcm_duplicates(self):
        clip = {"path": "train/a.wav", "pcm_sha256": "same", "warnings": []}
        reports = [
            {"source": "one.zip", "clip_count": 1, "warning_clip_count": 0, "clips": [clip]},
            {"source": "two.zip", "clip_count": 1, "warning_clip_count": 0, "clips": [clip]},
        ]
        summary = quality.build_summary(reports)
        self.assertEqual(1, len(summary["exact_duplicate_groups"]))


if __name__ == "__main__":
    unittest.main()
