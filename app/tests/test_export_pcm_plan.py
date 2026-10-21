"""Compare source-frame plans with independently assembled, real PCM bytes."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
from pydub import AudioSegment

import audio_export_plan
from audio_pcm_producer import get_pcm_source_shape
from audio_streaming import write_combined_pcm, write_combined_wav
from tts import ExportCancelled, combine_audio_with_pauses


def clip(rate, frames, channels=1):
    return AudioSegment(data=b'\x00\x20' * frames * channels,
                        frame_rate=rate, channels=channels, sample_width=2)


class ExportPCMPlanTests(unittest.TestCase):
    def test_mixed_promotion_frame_counts_and_authored_gap_envelopes_match_native_bytes(self):
        cases = [([8000, 16000, 44100, 48000], [1, 1, 2, 2]),
                 ([48000, 8000, 24000, 44100], [2, 1, 1, 2]),
                 ([24000] * 4, [1] * 4)]
        for rates, channels in cases:
            with self.subTest(rates=rates):
                segments = [clip(rate, 2000 + index * 17, count)
                            for index, (rate, count) in enumerate(zip(rates, channels))]
                shapes = [get_pcm_source_shape(segment) for segment in segments]
                pauses = [17, 75, 2]
                before = copy.deepcopy((shapes, pauses))
                plan = audio_export_plan.get_combined_pcm_plan(shapes, pauses)
                native = combine_audio_with_pauses(segments, ['A'] * 4, pause_overrides=pauses)
                self.assertEqual(before, (shapes, pauses))
                self.assertEqual((native.channels, native.frame_rate, native.sample_width),
                    (plan['channels'], plan['frame_rate'], plan['sample_width']))
                self.assertEqual(len(native.raw_data) // native.frame_width, plan['frames'])
                signal = np.frombuffer(native.raw_data, dtype='<i2').reshape(-1, native.channels)
                silent = np.all(signal == 0, axis=1)
                starts = np.flatnonzero(silent & np.r_[True, ~silent[:-1]])
                ends = np.flatnonzero(silent & np.r_[~silent[1:], True]) + 1
                self.assertEqual(3, len(starts))
                self.assertEqual(3, len(plan['intentional_pause_frames']))
                source_starts = np.flatnonzero(~silent & np.r_[True, silent[:-1]])
                source_ends = np.flatnonzero(~silent & np.r_[silent[1:], True]) + 1
                self.assertEqual(4, len(plan['source_frame_ranges']))
                for actual_start, actual_end, (planned_start, planned_end) in zip(
                        source_starts, source_ends, plan['source_frame_ranges']):
                    # Count boundaries follow every prefix promotion; a ratecv
                    # interpolation sample may straddle the physical onset/edge.
                    self.assertAlmostEqual(actual_start, planned_start, delta=2)
                    self.assertAlmostEqual(actual_end, planned_end, delta=2)
                self.assertEqual(plan['frames'], plan['source_frame_ranges'][-1][1])
                for start, end, (planned_start, planned_end) in zip(starts, ends, plan['intentional_pause_frames']):
                    self.assertLessEqual(planned_start, start)
                    self.assertGreaterEqual(planned_end, end)
                    self.assertLessEqual(start - planned_start, native.frame_rate * .002)
                    self.assertLessEqual(planned_end - end, native.frame_rate * .002)

    def test_raw_and_both_wav_writer_paths_publish_source_plan_without_changing_return_contracts(self):
        for mode in ('pcm', 'wav_uniform', 'wav_mixed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                rates = [24000] * 3 if mode == 'wav_uniform' else [8000, 16000, 44100]
                segments = [clip(rate, 2000 + index * 17) for index, rate in enumerate(rates)]
                before = [segment.raw_data for segment in segments]
                path = Path(tmp) / ('combined.pcm' if mode == 'pcm' else 'combined.wav')
                plans = []
                writer = write_combined_pcm if mode == 'pcm' else write_combined_wav
                result = writer(segments, ['A', 'B', 'A'], path, 75, 2,
                    pause_overrides=[17, 75, None], pcm_plan_callback=plans.append)
                native = combine_audio_with_pauses(segments, ['A', 'B', 'A'], 75, 2, [17, 75, None])
                self.assertEqual(1, len(plans))
                plan = plans[0]
                if mode == 'pcm':
                    self.assertEqual({'sample_width', 'frame_rate', 'channels', 'frames'}, set(result))
                    data = path.read_bytes()
                else:
                    self.assertIsInstance(result, int)
                    with wave.open(str(path)) as stream:
                        self.assertEqual(plan['frames'], stream.getnframes())
                        self.assertEqual(plan['frame_rate'], stream.getframerate())
                        data = stream.readframes(stream.getnframes())
                self.assertEqual(native.raw_data, data)
                self.assertEqual(len(data) // native.frame_width, plan['frames'])
                self.assertEqual(before, [segment.raw_data for segment in segments])
                self.assertEqual([path.name], [item.name for item in Path(tmp).iterdir()])

    def test_many_authored_gaps_follow_fractional_pcm_clock_without_millisecond_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            segments = [clip(24000, 100)] * 300
            path = Path(tmp) / 'combined.pcm'
            plans = []
            settings = write_combined_pcm(segments, ['A'] * len(segments), path, 600, 600,
                                          pcm_plan_callback=plans.append)
            plan = plans[0]
            self.assertEqual(299, len(plan['intentional_pause_frames']))
            self.assertEqual(settings['frames'] * 2, path.stat().st_size)
            # These are actual converted gap bytes, not rounded timeline ms.
            with path.open('rb') as stream:
                for start, end in plan['intentional_pause_frames']:
                    self.assertEqual(14398, end - start)
                    stream.seek(start * 2)
                    self.assertEqual(b'\0' * ((end - start) * 2), stream.read((end - start) * 2))
                    self.assertEqual(b'\x00\x20', stream.read(2))
            self.assertEqual(300 * 100 + 299 * 14398, settings['frames'])
            self.assertNotEqual(round((300 * 100 / 24 + 299 * 600) * 24), settings['frames'])

    def test_callback_failure_and_late_cancel_leave_previous_pcm_and_wav_intact(self):
        for mode in ('pcm', 'wav_uniform', 'wav_mixed'):
            for fault in ('callback', 'cancel'):
                with self.subTest(mode=mode, fault=fault), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / ('delivery.pcm' if mode == 'pcm' else 'delivery.wav')
                    path.write_bytes(b'previous complete synthetic delivery')
                    segments = [clip(rate, 2000) for rate in ([24000, 24000] if mode == 'wav_uniform' else [8000, 44100])]
                    cancelled = False
                    def receive(plan):
                        nonlocal cancelled
                        self.assertGreater(plan['frames'], 0)
                        if fault == 'callback':
                            raise RuntimeError('synthetic plan callback failure')
                        cancelled = True
                    writer = write_combined_pcm if mode == 'pcm' else write_combined_wav
                    with self.assertRaises(RuntimeError if fault == 'callback' else ExportCancelled):
                        writer(segments, ['A', 'B'], path, 75, 2,
                               cancel_check=lambda: cancelled, pcm_plan_callback=receive)
                    self.assertEqual(b'previous complete synthetic delivery', path.read_bytes())
                    self.assertEqual([path.name], [item.name for item in Path(tmp).iterdir()])

    def test_bad_or_unbounded_plans_refuse_and_unknown_shapes_remain_explicit(self):
        shape = ((1, 24000, 2), 20)
        self.assertIsNone(audio_export_plan.get_combined_pcm_plan([], []))
        self.assertIsNone(audio_export_plan.get_combined_pcm_plan([None], []))
        for bad in [((1, 0, 2), 20), ((1, 24000, 3), 20), ((True, 24000, 2), 20), ((1, 24000, 2), -1)]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                audio_export_plan.get_combined_pcm_plan([bad], [])
        with self.assertRaises(ValueError):
            audio_export_plan.get_combined_pcm_plan([shape, shape], [])
        for duration in (float('nan'), float('inf'), 'invalid'):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                audio_export_plan.get_combined_pcm_plan([shape, shape], [duration])
        with patch.object(audio_export_plan, 'MAX_PLAN_REGIONS', 1), self.assertRaises(ValueError):
            audio_export_plan.get_combined_pcm_plan([shape] * 3, [1, 1])
        def cancel():
            raise ExportCancelled()
        with self.assertRaises(ExportCancelled):
            audio_export_plan.get_combined_pcm_plan([shape, shape], [1], cancel)
