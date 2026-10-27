"""Timeline annotations and independent tracks follow the published PCM clock."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import zipfile

import numpy as np
from pydub import AudioSegment
from audio_streaming import write_combined_wav
from project import ProjectManager
import tts


def clip(frames=2000, rate=24000):
    return AudioSegment(data=b'\x00\x20' * frames, sample_width=2, frame_rate=rate, channels=1)


class ExportFrameTimelineTests(unittest.TestCase):
    def test_thousand_clips_keep_native_labels_chapters_and_speaker_tracks_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment = clip()
            rows = [({'speaker': 'A' if i % 2 == 0 else 'B', 'text': str(i)}, segment)
                    for i in range(1000)]
            manager = ProjectManager(tmp)
            manager._load_chunks_with_audio = lambda **kwargs: (rows, 0)
            manager._load_pause_defaults = lambda: (0, 0)
            timeline = tts.compute_timeline(iter(rows), 0, 0)
            for index, (_, _, start) in enumerate(timeline):
                self.assertAlmostEqual(index * 2000, start * 24, delta=1e-6)
            chapters = manager._build_m4b_chapters(timeline, True)
            self.assertAlmostEqual(2000000 / 24, chapters[-1][2], delta=1)
            self.assertTrue(manager.export_audacity()[0])
            with zipfile.ZipFile(Path(tmp) / 'audacity_export.zip') as archive:
                labels = [line.split('\t') for line in archive.read('labels.txt').decode().splitlines()]
                self.assertEqual(1000, len(labels))
                for index, (start, end, _) in enumerate(labels):
                    self.assertAlmostEqual(index * 2000 / 24000, float(start), delta=.000001)
                    self.assertAlmostEqual((index + 1) * 2000 / 24000, float(end), delta=.000001)
                for parity, speaker in enumerate(('a', 'b')):
                    with wave.open(io.BytesIO(archive.read(speaker + '.wav'))) as wav:
                        self.assertEqual(24000, wav.getframerate())
                        self.assertEqual(2000000, wav.getnframes())
                        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').reshape(1000, 2000)
                    expected = np.zeros((1000, 2000), dtype='<i2')
                    expected[parity::2] = 8192
                    np.testing.assert_array_equal(expected, samples)
            from m4b_encode import encode_m4b
            def encode(cmd, *args, **kwargs):
                with wave.open(cmd[cmd.index('-i') + 1]) as wav:
                    self.assertEqual(2000000, wav.getnframes())
                metadata = Path(cmd[cmd.index('-i') + 3]).read_text()
                starts = [int(line[6:]) for line in metadata.splitlines() if line.startswith('START=')]
                ends = [int(line[4:]) for line in metadata.splitlines() if line.startswith('END=')]
                self.assertEqual(1000, len(starts))
                for index, (start, end) in enumerate(zip(starts, ends)):
                    self.assertAlmostEqual(index * 2000 / 24, start, delta=1)
                    self.assertAlmostEqual((index + 1) * 2000 / 24, end, delta=1)
                return encode_m4b(cmd, *args, **kwargs)
            with patch('m4b_encode.encode_m4b', side_effect=encode):
                self.assertTrue(manager.merge_m4b(per_chunk_chapters=True)[0])

    def test_pause_rounding_and_layout_promotion_follow_actual_pcm(self):
        for rate in (24000, 44100, 8000):
            with self.subTest(rate=rate), tempfile.TemporaryDirectory() as tmp:
                segments = [clip(2000, rate) for _ in range(30)]
                chunks = [{'speaker': 'A' if i % 3 else 'B', 'pause_after': (17 if i % 4 == 0 else None)}
                          for i in range(30)]
                timeline = tts.compute_timeline(zip(chunks, segments), 75, 2)
                combined = tts.combine_audio_with_pauses(segments, [row['speaker'] for row in chunks],
                    75, 2, [row['pause_after'] for row in chunks])
                data = np.frombuffer(combined.raw_data, dtype='<i2')
                starts = np.flatnonzero((data != 0) & np.r_[True, data[:-1] == 0])
                self.assertEqual(30, len(starts))
                for (_, _, actual), frame in zip(timeline, starts):
                    self.assertAlmostEqual(frame * 1000 / combined.frame_rate, actual, delta=1)
                output = Path(tmp) / 'combined.wav'
                write_combined_wav(segments, [row['speaker'] for row in chunks], output, 75, 2,
                                   [row['pause_after'] for row in chunks])
                with wave.open(str(output)) as wav:
                    self.assertEqual(combined.raw_data, wav.readframes(wav.getnframes()))
                manager = ProjectManager(tmp)
                manager._load_chunks_with_audio = lambda **kwargs: (list(zip(chunks, segments)), 0)
                manager._load_pause_defaults = lambda: (75, 2)
                self.assertTrue(manager.export_audacity()[0])
                with zipfile.ZipFile(Path(tmp) / 'audacity_export.zip') as archive:
                    for speaker in ('A', 'B'):
                        with wave.open(io.BytesIO(archive.read(speaker.lower() + '.wav'))) as wav:
                            track_rate = wav.getframerate()
                            track = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
                        onsets = np.flatnonzero((track != 0) & np.r_[True, track[:-1] == 0])
                        expected = [frame for chunk, frame in zip(chunks, starts) if chunk['speaker'] == speaker]
                        self.assertEqual(len(expected), len(onsets))
                        for actual, frame in zip(onsets, expected):
                            self.assertAlmostEqual(frame * 1000 / combined.frame_rate,
                                                   actual * 1000 / track_rate, delta=1)
                        self.assertAlmostEqual(len(data) / combined.frame_rate, len(track) / track_rate, delta=.001)

    def test_mixed_rate_timeline_preserves_incremental_resampling(self):
        segments = [clip(2000, rate) for rate in (8000, 16000, 24000, 44100, 48000) * 20]
        chunks = [{'speaker': 'A', 'pause_after': 17} for _ in segments]
        combined = tts.combine_audio_with_pauses(segments, ['A'] * len(segments), pause_overrides=[17] * len(segments))
        data = np.frombuffer(combined.raw_data, dtype='<i2')
        starts = np.flatnonzero((data != 0) & np.r_[True, data[:-1] == 0])
        timeline = tts.compute_timeline(zip(chunks, segments))
        self.assertEqual(len(segments), len(starts))
        for (_, _, start), frame in zip(timeline, starts):
            self.assertAlmostEqual(frame * 1000 / combined.frame_rate, start, delta=1)
