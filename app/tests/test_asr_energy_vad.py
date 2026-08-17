import os
import tempfile
import unittest

import numpy as np
import soundfile as sf

from experiments.asr_backends import run_energy_vad, run_silero_whisper_cpp


class EnergyVadTests(unittest.TestCase):
    def test_splits_long_silence_but_keeps_short_pause(self):
        rate = 16000
        tone = np.sin(np.arange(rate // 2) * 2 * np.pi * 220 / rate) * 0.2
        audio = np.concatenate((tone, np.zeros(rate // 10), tone,
                                np.zeros(rate // 2), tone))
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "probe.wav")
            sf.write(path, audio, rate)
            _text, segments = run_energy_vad(path)
        self.assertEqual(2, len(segments))
        self.assertAlmostEqual(0, segments[0][0], places=2)
        self.assertAlmostEqual(1.6, segments[1][0], places=2)


class SileroWhisperWindowTests(unittest.TestCase):
    def test_transcribes_every_vad_window_without_coalescing_boundaries(self):
        rate = 16000
        audio = np.zeros(rate * 3, dtype=np.float32)
        calls = []

        def vad(_path):
            return "", [(0.1, 0.9, ""), (1.1, 1.4, ""),
                        (1.7, 2.8, "")]

        def transcribe(path, model, binary, language="en"):
            calls.append((sf.info(path).duration, model, binary, language))
            return f"part{len(calls)}", [(0.0, sf.info(path).duration, "x")]

        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "probe.wav")
            sf.write(path, audio, rate)
            text, segments = run_silero_whisper_cpp(
                path, "large-v3", "whisper-cli", language="ja",
                vad_runner=vad, transcriber=transcribe)

        self.assertEqual("part1 part2 part3", text)
        self.assertEqual([(0.1, 0.9), (1.1, 1.4), (1.7, 2.8)],
                         [(start, end) for start, end, _text in segments])
        self.assertEqual(["ja", "ja", "ja"], [call[3] for call in calls])


if __name__ == "__main__":
    unittest.main()
