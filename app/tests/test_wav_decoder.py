"""Exercise real FFmpeg fallback decoding for floating-point WAV chunks."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from project import ProjectManager


class WavDecoderTests(unittest.TestCase):
    def test_float_wav_uses_container_format_without_invalid_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            samples = (0.25 * np.sin(np.arange(4000) * 2 * np.pi * 440 / 16000)).astype('float32')
            audio_path = Path(tmp, 'line.wav')
            sf.write(audio_path, samples, 16000, subtype='FLOAT')
            manager = ProjectManager(tmp)
            with patch.object(manager, 'load_chunks', return_value=[{'audio_path': 'line.wav'}]), \
                 contextlib.redirect_stdout(io.StringIO()):
                result, skipped = manager._load_chunks_with_audio()
            self.assertEqual(1, len(result))
            self.assertEqual(0, skipped)
            segment = result[0][1]
            self.assertEqual(16000, segment.frame_rate)
            self.assertEqual(1, segment.channels)
            decoded = np.asarray(segment.get_array_of_samples(), dtype='float64')
            decoded /= 2 ** (8 * segment.sample_width - 1)
            np.testing.assert_allclose(samples, decoded, atol=1e-5)


if __name__ == '__main__':
    unittest.main()
