"""The direct combined-WAV writer stays gated to 16/32-bit PCM layouts."""
import tempfile
from pathlib import Path
import unittest
import wave

from pydub import AudioSegment

import audio_streaming


class DirectCombinedWavLayoutTests(unittest.TestCase):
    def test_eight_bit_pcm_skips_the_direct_writer_and_keeps_pydub_bias(self):
        # Internal signed 8-bit zero samples become unsigned 0x80 on export.
        segment = AudioSegment(data=b'\x00' * 2000, sample_width=1,
                               frame_rate=24000, channels=1)
        self.assertIsNone(audio_streaming.get_streamable_layout([segment]))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'combined.wav'
            audio_streaming.write_combined_wav([segment], ['A'], output, 0, 0)
            with wave.open(str(output)) as wav:
                self.assertEqual(1, wav.getsampwidth())
                self.assertEqual(2000, wav.getnframes())
                self.assertEqual(b'\x80' * 2000,
                                 wav.readframes(wav.getnframes()))


if __name__ == '__main__':
    unittest.main()
