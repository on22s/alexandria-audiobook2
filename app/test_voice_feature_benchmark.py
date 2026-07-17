import importlib.util
from pathlib import Path
import unittest

import numpy as np

try:
    import torch  # noqa: F401 - availability controls the ROCm-specific test
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "voice_feature_benchmark", ROOT / "voice_feature_benchmark.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class VoiceFeatureBenchmarkTests(unittest.TestCase):
    def test_torch_features_match_librosa_on_voiced_and_noisy_audio(self):
        sr = 22050
        seconds = 2
        times = np.arange(sr * seconds, dtype=np.float32) / sr
        if not HAS_TORCH:
            with self.assertRaisesRegex(ImportError, "torch"):
                benchmark.get_torch_spectral_features(times, sr, "cpu")
            return
        rng = np.random.default_rng(7)
        signals = [
            (0.35 * np.sin(2 * np.pi * 137 * times)
             + 0.12 * np.sin(2 * np.pi * 931 * times)).astype(np.float32),
            (rng.normal(0, 0.08, len(times)) * np.hanning(len(times))).astype(np.float32),
        ]

        for signal in signals:
            with self.subTest(signal_std=float(signal.std())):
                reference = benchmark.get_librosa_spectral_features(signal, sr)
                candidate = benchmark.get_torch_spectral_features(signal, sr, "cpu")
                comparisons = benchmark.compare_features(reference, candidate)
                self.assertTrue(all(item["passed"] for item in comparisons.values()),
                                comparisons)

    def test_comparison_reports_feature_drift(self):
        reference = {name: 1.0 for name in benchmark.FEATURE_TOLERANCES}
        candidate = dict(reference)
        candidate["mean_rolloff"] = 2.0

        comparisons = benchmark.compare_features(reference, candidate)

        self.assertFalse(comparisons["mean_rolloff"]["passed"])
        self.assertTrue(comparisons["mean_rms"]["passed"])

    def test_operation_timing_covers_every_profiler_acoustic_operation(self):
        sr = 22050
        times = np.arange(sr, dtype=np.float32) / sr
        signal = (0.2 * np.sin(2 * np.pi * 160 * times)).astype(np.float32)

        timings = benchmark.get_librosa_operation_times(signal, sr, repeats=1)

        self.assertEqual(
            {"yin", "rms", "centroid", "rolloff", "harmonic", "flatness", "onset"},
            set(timings),
        )
        self.assertTrue(all(seconds >= 0 for seconds in timings.values()))


if __name__ == "__main__":
    unittest.main()
