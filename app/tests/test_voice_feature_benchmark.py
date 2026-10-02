import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

try:
    import torch  # noqa: F401 - availability controls the ROCm-specific test
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False


ROOT = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location(
    "voice_feature_benchmark",
    ROOT / "tools" / "experiments" / "voice_feature_benchmark.py")
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
            {"pyin", "rms", "centroid", "rolloff", "harmonic", "flatness", "onset"},
            set(timings),
        )
        self.assertTrue(all(seconds >= 0 for seconds in timings.values()))


if __name__ == "__main__":
    unittest.main()


class BenchmarkDeviceProvenanceTests(unittest.TestCase):
    def test_cli_reports_the_device_requested_for_the_measurements(self):
        import contextlib
        import io
        import json
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        names = {'cuda': 'current GPU', 'cuda:0': 'GPU zero', 'cuda:1': 'GPU one', 0: 'GPU zero'}
        for device in ('cpu', 'cuda', 'cuda:0', 'cuda:1'):
            for parity in (True, False):
                with self.subTest(device=device, parity=parity):
                    name = Mock(side_effect=lambda selected: names[selected])
                    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda:True, get_device_name=name))
                    output = io.StringIO()
                    with patch.dict(sys.modules, {'torch':torch}), \
                         patch.object(sys,'argv',['benchmark','clip.wav','--device',device,'--repeats','2','--json']), \
                         patch.object(benchmark,'benchmark_clip',return_value={'path':'clip.wav','parity_passed':parity}) as measure, \
                         contextlib.redirect_stdout(output):
                        code = benchmark.main()
                    report = json.loads(output.getvalue())
                    self.assertEqual(0 if parity else 2, code)
                    measure.assert_called_once_with('clip.wav',device,2)
                    self.assertEqual(device, report['device'])
                    self.assertEqual('CPU' if device=='cpu' else names[device], report['device_name'])
                    self.assertEqual(parity, report['parity_passed'])
                    if device=='cpu':
                        name.assert_not_called()
                    else:
                        name.assert_called_once_with(device)


class ProductionOperationParityTests(unittest.TestCase):
    def test_native_pitch_timing_uses_production_pyin_not_yin(self):
        from tests.test_unvoiced_profile import profiler
        import librosa
        self.assertIs(profiler.get_profiler_acoustic_operations,
                      benchmark.get_profiler_acoustic_operations)
        silence = np.zeros(22050, dtype=np.float32)
        original = librosa.pyin
        with patch.object(librosa, 'pyin', wraps=original) as measured, \
             patch.object(librosa, 'yin', side_effect=AssertionError('not the production pitch algorithm')):
            timings = benchmark.get_librosa_operation_times(silence, 22050, repeats=1)
        self.assertEqual(2, measured.call_count)  # warmup plus one timed production operation
        for call in measured.call_args_list:
            self.assertEqual({'fmin': 50, 'fmax': 400, 'sr': 22050}, call.kwargs)
            self.assertIs(silence, call.args[0])
        self.assertIn('pyin', timings)
        f0, voiced, _ = profiler.get_profiler_acoustic_operations(silence, 22050)['pyin']()
        self.assertFalse(voiced.any())
        self.assertTrue(np.isnan(f0).all())
        # The displaced algorithm invents pitch on this known rejection case.
        self.assertTrue(np.isfinite(librosa.yin(silence, fmin=50, fmax=400, sr=22050)).all())
