import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from tts_benchmark import measure_wav, run_custom_voice_case


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


if __name__ == "__main__":
    unittest.main()
