"""Reject non-finite provider PCM before replacing an existing take (#1449)."""
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from audio_validation import GeneratedAudioError
from tts import TTSEngine


def tone(seconds=1, rate=24000):
    return (.2 * np.sin(2 * np.pi * 220 * np.arange(round(seconds * rate)) / rate)).astype('float32')


class ExternalFiniteSampleTests(unittest.TestCase):
    def test_nonfinite_samples_in_later_block_preserve_prior_take_and_clean_staging(self):
        engine = TTSEngine.__new__(TTSEngine)
        for bad in (np.nan, np.inf, -np.inf):
            for channels in (1, 2):
                with self.subTest(bad=bad, channels=channels), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source, output = root / 'source.wav', root / 'current.wav'
                    sf.write(output, tone(), 24000)
                    previous = output.read_bytes()
                    samples = np.tile(tone(4)[:, None], (1, channels))
                    samples[70000, -1] = bad
                    sf.write(source, samples, 24000, subtype='FLOAT')
                    provider_bytes = source.read_bytes()
                    with self.assertRaisesRegex(GeneratedAudioError, 'non-finite'):
                        engine._save_external_prediction((str(source),), str(output), None, 'Hello.')
                    self.assertEqual(previous, output.read_bytes())
                    self.assertEqual(provider_bytes, source.read_bytes())
                    self.assertEqual({'source.wav', 'current.wav'}, {path.name for path in root.iterdir()})

    def test_finite_float_response_is_published_without_reencoding(self):
        engine = TTSEngine.__new__(TTSEngine)
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'source.wav', Path(tmp) / 'current.wav'
            sf.write(source, tone(), 24000, subtype='FLOAT')
            self.assertTrue(engine._save_external_prediction((str(source),), str(output), None, 'Hello.'))
            self.assertEqual(source.read_bytes(), output.read_bytes())
            decoded, rate = sf.read(output)
            self.assertEqual(24000, rate)
            self.assertTrue(np.isfinite(decoded).all())


if __name__ == '__main__':
    unittest.main()
