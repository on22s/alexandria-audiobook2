"""Real lossy mastering keeps source PCM distinct from its output layout."""
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
import soundfile as sf
from pydub.generators import Sine

from audio_encoding_layout import get_audio_encoding_layout
from audio_export import export_audio_segment
from audio_mastering import get_audio_sha256
from project import ProjectManager


class MasteringOutputLayoutTests(unittest.TestCase):
    def assert_encoded_media(self, path, rate, channels, codec_frames):
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
            'stream=codec_type,sample_rate,channels', '-of', 'json', str(path)], timeout=10))
        audio = [row for row in probe['streams'] if row['codec_type'] == 'audio']
        self.assertEqual(1, len(audio))
        self.assertEqual(rate, int(audio[0]['sample_rate']))
        self.assertEqual(channels, audio[0]['channels'])
        decoded = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
            '-map', '0:a:0', '-vn', '-sn', '-dn', '-c:a', 'pcm_f32le', '-f', 'f32le', '-'],
            capture_output=True, check=True, timeout=10)
        self.assertFalse(decoded.stderr)
        samples = np.frombuffer(decoded.stdout, dtype='<f4').reshape(-1, channels)
        self.assertTrue(np.isfinite(samples).all())
        self.assertGreater(float(np.max(np.abs(samples))), 1e-5)
        self.assertLessEqual(abs(len(samples) - rate), codec_frames)

    def test_real_mastered_mp3_supports_high_nonstandard_rates_and_multichannel_sources(self):
        cases = [(24000, 1, 24000, 1), (44100, 2, 44100, 2), (30000, 1, 32000, 1),
                 (96000, 1, 48000, 1), (192000, 2, 48000, 2), (24000, 6, 24000, 2)]
        for source_rate, source_channels, rate, channels in cases:
            with self.subTest(rate=source_rate, channels=source_channels), tempfile.TemporaryDirectory() as root:
                source = Sine(440, sample_rate=source_rate).to_audio_segment(duration=1000).apply_gain(-24).set_channels(source_channels)
                before = source.raw_data
                path = Path(root) / 'delivery.mp3'
                path.write_bytes(b'previous synthetic delivery')
                report = export_audio_segment(source, path, 'mp3', lambda: False,
                                               mastering={'loudness': -18, 'true_peak': -2})
                self.assertEqual(before, source.raw_data)
                self.assertEqual(get_audio_sha256(path), report['sha256'])
                self.assertTrue(math.isfinite(report['after']['integrated_lufs']))
                self.assertAlmostEqual(-18, report['after']['integrated_lufs'], delta=1)
                self.assert_encoded_media(path, rate, channels, 2304)
                self.assertEqual(['delivery.mp3'], [item.name for item in Path(root).iterdir()])

    def test_real_mastered_m4b_supports_192k_source_and_retains_aac_multichannel_layout(self):
        for source_rate, channels, rate in [(24000, 1, 24000), (192000, 1, 96000), (192000, 6, 96000)]:
            with self.subTest(source_rate=source_rate, channels=channels), tempfile.TemporaryDirectory() as root:
                source = Sine(440, sample_rate=source_rate).to_audio_segment(duration=1000).apply_gain(-24).set_channels(channels)
                before = source.raw_data
                manager = ProjectManager(root)
                manager._load_pause_defaults = lambda: (0, 0)
                manager._load_chunks_with_audio = lambda **kwargs: ([
                    ({'speaker': 'NARRATOR', 'text': 'Synthetic tone control'}, kwargs['segment_transform'](source))], 0)
                reports = []
                path = Path(root) / 'audiobook.m4b'
                path.write_bytes(b'previous synthetic delivery')
                success, message = manager.merge_m4b(mastering={'loudness': -18, 'true_peak': -2},
                                                     mastering_callback=reports.append)
                self.assertTrue(success, message)
                self.assertEqual(before, source.raw_data)
                self.assertEqual(1, len(reports))
                self.assertEqual(get_audio_sha256(path), reports[0]['sha256'])
                self.assert_encoded_media(path, rate, channels, 2048)
                self.assertEqual([], list(Path(root).glob('.m4b-*')))
                self.assertEqual([], list(Path(root).glob('*.pending.*')))

    def test_real_mastered_lossless_wav_retains_source_rate_and_frame_count(self):
        with tempfile.TemporaryDirectory() as root:
            source = Sine(440, sample_rate=192000).to_audio_segment(duration=1000).apply_gain(-24)
            path = Path(root) / 'delivery.wav'
            report = export_audio_segment(source, path, 'wav', lambda: False,
                                           mastering={'loudness': -18, 'true_peak': -2})
            info = sf.info(path)
            self.assertEqual((192000, 1, 192000), (info.samplerate, info.channels, info.frames))
            self.assertEqual(get_audio_sha256(path), report['sha256'])

    def test_bad_layout_values_refuse_instead_of_reinterpreting_source_pcm(self):
        for rate, channels in [(0, 1), (-1, 1), (24000, 0), (float('nan'), 1), (True, 1), (24000, 1.5)]:
            with self.subTest(rate=rate, channels=channels), self.assertRaises(ValueError):
                get_audio_encoding_layout('mp3', rate, channels)
        with self.assertRaises(ValueError):
            get_audio_encoding_layout('unknown', 24000, 1)
