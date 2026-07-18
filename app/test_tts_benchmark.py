import tempfile
import unittest
import wave
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tts_benchmark import (measure_wav, run_clone_voice_case,
                           run_custom_voice_case, run_design_voice_case)
import tts_vram_benchmark


class TTSBenchmarkTests(unittest.TestCase):
    def test_measure_wav_reports_duration_throughput_and_audio_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "tone.wav")
            sample_rate = 24000
            times = np.arange(sample_rate, dtype=np.float32) / sample_rate
            samples = (0.25 * np.sin(2 * np.pi * 440 * times) * 32767).astype("<i2")
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                output.writeframes(samples.tobytes())
            result = measure_wav(str(path), 0.5)
        self.assertEqual(1.0, result["duration_seconds"])
        self.assertEqual(2.0, result["audio_seconds_per_second"])
        self.assertEqual(24000, result["sample_rate"])
        self.assertGreater(result["rms"], 0.1)
        self.assertEqual(0.0, result["clipping_ratio"])

    def test_measure_wav_rejects_non_pcm16_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "eight-bit.wav")
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(1)
                output.setframerate(8000)
                output.writeframes(bytes([128]) * 100)
            with self.assertRaisesRegex(ValueError, "16-bit PCM"):
                measure_wav(str(path), 1.0)

    def test_custom_voice_case_uses_production_engine_call(self):
        class FakeEngine:
            def __init__(self):
                self.loaded = 0
                self.call = None

            def _init_local_custom(self):
                self.loaded += 1

            def generate_custom_voice(self, text, instruct, speaker, config, path):
                self.call = (text, instruct, speaker, config)
                with wave.open(path, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return True

        engine = FakeEngine()
        fixture = {"text": "Hello.", "instruct": "Warmly.", "speaker": "N",
                   "voice": "Ryan", "seed": 3}
        with tempfile.TemporaryDirectory() as tmp:
            metrics = run_custom_voice_case(
                engine, fixture, str(Path(tmp, "out.wav")), load_model=True)
        self.assertEqual(1, engine.loaded)
        self.assertEqual("Hello.", engine.call[0])
        self.assertEqual(0.1, metrics["duration_seconds"])

    def test_vram_sweep_uses_peak_across_all_sub_batches(self):
        class FakeCuda:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def reset_peak_memory_stats():
                pass

        class FakeEngine:
            def set_sub_batch_size(self, size):
                pass

            def run_benchmark_batch(self, chunks, voice_config, output_dir, batch_seed=-1):
                self.batch_seed = batch_seed
                return {"completed": [], "failed": [], "peak_vram_gb": 13.83}

            def run_clone_benchmark_batch(self, chunks, voice_config, output_dir,
                                          batch_seed=-1):
                self.clone_batch_seed = batch_seed
                return {"completed": [], "failed": [], "peak_vram_gb": 14.25}

            def _clear_gpu_cache(self):
                pass

        fake_torch = type("FakeTorch", (), {"cuda": FakeCuda})
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(sys.modules, {"torch": fake_torch}), \
             patch.object(tts_vram_benchmark, "vram_state", return_value={}):
            results = tts_vram_benchmark.run_sweep(
                engine, {}, [16], tmp, n_chunks_per_run=1)
        self.assertEqual(13.83, results[0]["peak_vram_gb"])
        self.assertEqual(42, engine.batch_seed)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(sys.modules, {"torch": fake_torch}), \
             patch.object(tts_vram_benchmark, "vram_state", return_value={}):
            clone_results = tts_vram_benchmark.run_sweep(
                engine, {}, [8], tmp, n_chunks_per_run=1, voice_type="clone")
        self.assertEqual(14.25, clone_results[0]["peak_vram_gb"])
        self.assertEqual(42, engine.clone_batch_seed)

    def test_vram_results_create_nested_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "new", "nested", "results.json")
            tts_vram_benchmark.save_benchmark_results({"ok": True}, str(path))
            self.assertTrue(path.is_file())

    def test_clone_case_separates_prompt_and_generation(self):
        class FakeEngine:
            def _init_local_clone(self):
                pass

            def _get_clone_prompt(self, speaker, config):
                return "prompt"

            def generate_clone_voice(self, text, speaker, config, path):
                with wave.open(path, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return True

        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp, "ref.wav")
            ref.write_bytes(b"reference")
            fixture = {"text": "Hello.", "speaker": "CLONE", "seed": 2,
                       "ref_audio": "ref.wav", "ref_text": "Reference.",
                       "ref_audio_sha256": hashlib.sha256(b"reference").hexdigest()}
            metrics = run_clone_voice_case(
                FakeEngine(), fixture, str(Path(tmp, "out.wav")), tmp,
                load_model=True)
        self.assertIn("prompt_build_seconds", metrics)
        self.assertEqual(0.1, metrics["duration_seconds"])

    def test_design_case_moves_preview_into_benchmark_output(self):
        class FakeEngine:
            def _init_local_design(self):
                pass

            def generate_voice_design(self, description, sample_text, seed):
                preview = Path(tmp, "preview.wav")
                with wave.open(str(preview), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(np.ones(2400, dtype="<i2").tobytes())
                return str(preview), 24000

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp, "results", "design.wav")
            metrics = run_design_voice_case(FakeEngine(), {
                "text": "Hello.", "description": "Warm voice.", "seed": 5},
                str(output_path), load_model=True)
            self.assertTrue(output_path.is_file())
            self.assertFalse(Path(tmp, "preview.wav").exists())
        self.assertEqual(0.1, metrics["duration_seconds"])


if __name__ == "__main__":
    unittest.main()
