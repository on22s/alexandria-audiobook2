import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from sound_design_decode import decode_sound_design_asset
import sound_design_decode as decoder
from audio_validation import GeneratedAudioError
from tts import ExportCancelled


class SoundDesignDecodeTests(unittest.TestCase):
    def decode(self, root, source, output='asset.pcm', **options):
        return decode_sound_design_asset(root, source, output, source_limit=1024**2,
                                         pcm_limit=options.pop('pcm_limit', 1024**2), **options)

    def test_integer_wav_and_flac_preserve_exact_samples_and_geometry(self):
        for format, subtype, shift in [('WAV', 'PCM_U8', 24), ('WAV', 'PCM_16', 16),
                ('WAV', 'PCM_24', 8), ('WAV', 'PCM_32', 0), ('FLAC', 'PCM_24', 8)]:
            for channels in (1, 2):
                with self.subTest(format=format, subtype=subtype, channels=channels), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    values = np.tile(np.array([-123, -1, 0, 1, 123], dtype=np.int64), 8001)[:24001]
                    expected = np.repeat((values * 2**shift).astype('<i4')[:, None], channels, axis=1)
                    sf.write(root/'source', expected, 24000, format=format, subtype=subtype)
                    result = self.decode(root, 'source')
                    raw = (root/'asset.pcm').read_bytes()
                    self.assertEqual(expected.tobytes(), raw)
                    self.assertEqual((24001, 24000, channels, 4),
                        tuple(result[k] for k in ('frames', 'frame_rate', 'channels', 'sample_width')))
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), result['sha256'])

    def test_mp3_matches_independent_single_read_without_block_boundary_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            signal = .1 * np.sin(np.arange(24001) * 2 * np.pi * 440 / 24000)
            sf.write(root/'source.mp3', signal, 24000, format='MP3', subtype='MPEG_LAYER_III')
            expected, rate = sf.read(root/'source.mp3', dtype='float64')
            result = self.decode(root, 'source.mp3')
            actual = np.frombuffer((root/'asset.pcm').read_bytes(), dtype='<i4') / 2147483648
            self.assertEqual((len(expected), rate), (result['frames'], result['frame_rate']))
            # Independent codecs may round differently; the old block decoder
            # differs by about 0.105 on this synthetic 0.1-amplitude control.
            np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)

    def test_nonfinite_or_overrange_float_sources_are_refused(self):
        for value in (float('nan'), float('inf'), 1.01, -1.01):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                sf.write(root/'source.wav', np.array([0., value, 0.]), 24000, subtype='FLOAT')
                before = (root/'source.wav').read_bytes()
                with self.assertRaises(ValueError):
                    self.decode(root, 'source.wav')
                self.assertEqual(before, (root/'source.wav').read_bytes())

    def test_excess_pcm_refuses_without_overwriting_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sf.write(root/'source.wav', np.zeros(24001), 24000)
            with self.assertRaises(ValueError):
                self.decode(root, 'source.wav', pcm_limit=128)
            self.assertLessEqual((root/'asset.pcm').stat().st_size, 128)
            (root/'retained.pcm').write_bytes(b'Prior owned data')
            with self.assertRaises(ValueError):
                self.decode(root, 'source.wav', 'retained.pcm')
            self.assertEqual(b'Prior owned data', (root/'retained.pcm').read_bytes())

    def test_ogg_and_mp3_preserve_stereo_rate_and_full_length(self):
        for format, subtype in [('OGG', 'VORBIS'), ('MP3', 'MPEG_LAYER_III')]:
            for rate in (24000, 48000):
                with self.subTest(format=format, rate=rate), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    signal = .1 * np.sin(np.arange(70001) * 2 * np.pi * 440 / rate)
                    sf.write(root/'source', np.column_stack([signal, -signal]), rate,
                             format=format, subtype=subtype)
                    expected, _ = sf.read(root/'source', dtype='float64', always_2d=True)
                    result = self.decode(root, 'source')
                    actual = np.frombuffer((root/'asset.pcm').read_bytes(), dtype='<i4').reshape(-1, 2) / 2147483648
                    self.assertEqual((len(expected), rate, 2),
                        tuple(result[k] for k in ('frames', 'frame_rate', 'channels')))
                    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)

    def test_truncated_wav_is_refused_before_starting_a_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sf.write(root/'source.wav', np.zeros(24001), 24000)
            original = (root/'source.wav').read_bytes()[:-1]
            (root/'source.wav').write_bytes(original)
            with patch.object(decoder, '_run_owned_check') as run, self.assertRaises(GeneratedAudioError):
                self.decode(root, 'source.wav')
            run.assert_not_called()
            self.assertEqual(original, (root/'source.wav').read_bytes())
            self.assertFalse((root/'asset.pcm').exists())

    def test_source_change_after_decode_refuses_success_and_retains_recovery_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sf.write(root/'source.wav', np.zeros(24001), 24000)
            run = decoder._run_owned_check
            def mutate(command, *args, **kwargs):
                result = run(command, *args, **kwargs)
                if command[0] == 'ffmpeg':
                    sf.write(root/'source.wav', np.full(24001, .1), 24000)
                return result
            with patch.object(decoder, '_run_owned_check', side_effect=mutate), self.assertRaisesRegex(ValueError, 'changed'):
                self.decode(root, 'source.wav')
            self.assertEqual(24001 * 4, (root/'asset.pcm').stat().st_size)

    def test_actual_ffmpeg_sink_cancellation_preserves_original_and_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sf.write(root/'source.wav', np.zeros(70001), 24000)
            original = (root/'source.wav').read_bytes()
            caller = threading.get_ident()
            observed = threading.Event()
            def cancel_in_reader():
                if threading.get_ident() != caller:
                    observed.set()
                    return True
                return False
            with self.assertRaises(ExportCancelled):
                self.decode(root, 'source.wav', cancel_check=cancel_in_reader)
            self.assertTrue(observed.is_set(), 'Cancellation never reached the real decoder sink')
            self.assertEqual(original, (root/'source.wav').read_bytes())
            self.assertTrue((root/'asset.pcm').exists())
            self.assertEqual(0, (root/'asset.pcm').stat().st_size)
