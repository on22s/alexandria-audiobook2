"""Synthetic discrimination only, not real-speech qualification."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

import numpy as np

import chunk_outliers as acoustics


def make_pcm(pitch=120, nuclei=2, amplitude=0.5, voiced=1.0):
    times = np.arange(64000) / 16000
    envelope = (0.05 + 0.95 * np.sin(np.pi * nuclei * times) ** 4)
    carrier = np.sin(2 * np.pi * pitch * times)
    noise = np.random.default_rng(17).normal(0, 0.4, times.size)
    carrier[times >= 4 * voiced] = noise[times >= 4 * voiced]
    return amplitude * envelope * carrier


class AcousticTests(unittest.TestCase):
    def test_pitch_and_amplitude_discriminate(self):
        baseline = acoustics.measure_take_acoustics(make_pcm(), 16000)
        high = acoustics.measure_take_acoustics(make_pcm(pitch=220), 16000)
        quiet = acoustics.measure_take_acoustics(make_pcm(amplitude=0.25), 16000)
        self.assertIsNone(baseline['error'])
        self.assertAlmostEqual(120, baseline['median_pitch_hz'], delta=5)
        self.assertAlmostEqual(220, high['median_pitch_hz'], delta=8)
        self.assertAlmostEqual(-6.0206, quiet['loudness_db'] - baseline['loudness_db'], delta=0.1)
        self.assertEqual(baseline['median_pitch_hz'], quiet['median_pitch_hz'])

    def test_nuclei_and_voiced_ratios_discriminate(self):
        slow = acoustics.measure_take_acoustics(make_pcm(), 16000)
        fast = acoustics.measure_take_acoustics(make_pcm(nuclei=4), 16000)
        mixed = acoustics.measure_take_acoustics(make_pcm(voiced=0.5), 16000)
        noise = acoustics.measure_take_acoustics(make_pcm(voiced=0), 16000)
        self.assertAlmostEqual(2, slow['syllable_rate'], delta=0.25)
        self.assertAlmostEqual(2 * slow['syllable_rate'], fast['syllable_rate'], delta=0.25)
        self.assertGreater(slow['voiced_ratio'], 0.9)
        self.assertTrue(0.35 < mixed['voiced_ratio'] < 0.65)
        self.assertEqual(0, noise['voiced_ratio'])
        self.assertIsNone(noise['median_pitch_hz'])
        self.assertIsNotNone(noise['loudness_db'])
        self.assertTrue(noise['error'])

    def test_silence_dc_and_padding(self):
        for pcm in (np.zeros(16000), np.ones(16000) * 0.1):
            result = acoustics.measure_take_acoustics(pcm, 16000)
            self.assertTrue(result['error'])
            self.assertTrue(all(result[key] is None for key in acoustics.SPREAD_FLOORS))
        baseline = acoustics.measure_take_acoustics(make_pcm(), 16000)
        padded = acoustics.measure_take_acoustics(np.r_[np.zeros(16000), make_pcm(), np.zeros(16000)], 16000)
        self.assertAlmostEqual(baseline['loudness_db'], padded['loudness_db'], delta=0.1)
        self.assertGreater(padded['voiced_ratio'], 0.95)

    def test_invalid_inputs_are_unavailable(self):
        cases = [(np.zeros(0), 16000), (np.zeros((2, 100)), 16000),
                 (np.array([np.nan] * 1600), 16000), (np.ones(1600) * 2, 16000),
                 (np.zeros(1600), 96000), (np.zeros(1600), True),
                 (np.zeros(1600, dtype=np.uint8), 16000),
                 (np.zeros(8000 * 61), 8000), (np.zeros(50), 16000)]
        for pcm, rate in cases:
            with self.subTest(rate=rate, shape=pcm.shape):
                self.assertTrue(acoustics.measure_take_acoustics(pcm, rate)['error'])
        integer = (make_pcm() * 32767).astype(np.int16)
        self.assertAlmostEqual(120, acoustics.measure_take_acoustics(integer, 16000)['median_pitch_hz'], delta=5)

    def test_flat_mad_floor_alignment_purity_and_outlier(self):
        base = acoustics.measure_take_acoustics(make_pcm(), 16000)
        values = [dict(base) for _ in range(8)] + [{**base, 'median_pitch_hz': 220.0}]
        original = copy.deepcopy(values)
        scores = acoustics.get_outlier_scores(values)
        self.assertEqual(original, values)
        self.assertEqual(9, len(scores))
        self.assertFalse(any(item['flagged'] for item in scores[:8]))
        self.assertTrue(scores[8]['flagged'])
        self.assertEqual(25, scores[8]['metrics']['median_pitch_hz']['spread'])
        self.assertAlmostEqual((220 - base['median_pitch_hz']) / 25,
                               scores[8]['metrics']['median_pitch_hz']['z'])
        for metric, floor in acoustics.SPREAD_FLOORS.items():
            self.assertEqual(floor, scores[0]['metrics'][metric]['spread'])

    def test_expressive_consistent_group_must_not_flag(self):
        measurements = [acoustics.measure_take_acoustics(make_pcm(
            pitch=120 + index * 3, nuclei=2 + index / 10,
            amplitude=0.4 + index / 100, voiced=0.8 + index / 100), 16000)
            for index in range(8)]
        scores = acoustics.get_outlier_scores(measurements)
        self.assertTrue(all(item['error'] is None for item in scores))
        self.assertFalse(any(item['flagged'] for item in scores))

    def test_missing_and_small_groups_never_score_clean(self):
        base = dict(loudness_db=-20, syllable_rate=3, median_pitch_hz=120, voiced_ratio=0.9)
        for count in range(1, 8):
            for item in acoustics.get_outlier_scores([base] * count):
                self.assertTrue(item['error'])
                self.assertTrue(all(record['z'] is None and record['flagged'] is None
                                    for record in item['metrics'].values()))
        values = [base] * 7 + [{**base, 'median_pitch_hz': float('nan'), 'error': 'pitch failed'}]
        scores = acoustics.get_outlier_scores(values)
        self.assertIn('pitch failed', scores[-1]['error'])
        self.assertIsNone(scores[0]['metrics']['median_pitch_hz']['z'])
        self.assertIsNotNone(scores[0]['metrics']['loudness_db']['z'])
        for sigma in (0, -1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                acoustics.get_outlier_scores([], sigma)


class DecodeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'synthetic.wav'
        self.path.touch()
        resolver = patch.object(acoustics.shutil, 'which', side_effect=lambda name: name)
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_decode_contract_and_limits(self):
        pcm = (make_pcm() * 32767).astype('<i2').tobytes()
        with patch.object(acoustics, 'run_owned_media_check', side_effect=[b'4.0', pcm]) as runner:
            result = acoustics.decode_take_acoustics(self.path)
        self.assertAlmostEqual(120, result['median_pitch_hz'], delta=5)
        probe, decoder = runner.call_args_list
        self.assertEqual(1024, probe.kwargs['output_limit'])
        self.assertEqual(1920000, decoder.kwargs['output_limit'])
        self.assertEqual(20, decoder.args[2])
        self.assertIn('16000', decoder.args[0])
        self.assertNotIn('-t', decoder.args[0])
        self.assertIn('-protocol_whitelist', decoder.args[0])

    def test_probe_and_decode_failures_never_accept(self):
        for duration in (b'61', b'NaN', b'N/A', b'0', b'-1'):
            with patch.object(acoustics, 'run_owned_media_check', return_value=duration) as runner:
                self.assertTrue(acoustics.decode_take_acoustics(self.path)['error'])
                self.assertEqual(1, runner.call_count)
        for failure in (ValueError('output cap'), subprocess.TimeoutExpired('ffmpeg', 20), OSError('missing')):
            with patch.object(acoustics, 'run_owned_media_check', side_effect=[b'4', failure]):
                self.assertTrue(acoustics.decode_take_acoustics(self.path)['error'])
        for output in (b'', b'x', bytes(1920002)):
            with patch.object(acoustics, 'run_owned_media_check', side_effect=[b'4', output]):
                self.assertTrue(acoustics.decode_take_acoustics(self.path)['error'])
        with patch.object(acoustics.shutil, 'which', return_value=None):
            self.assertTrue(acoustics.decode_take_acoustics(self.path)['error'])

    def test_real_owned_runner_caps_output_and_times_out(self):
        runner = acoustics.run_owned_media_check
        with self.assertRaisesRegex(ValueError, 'bounded output'):
            runner([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(bytes(200000))'],
                   lambda: None, 5, output_limit=1024)
        with self.assertRaises(subprocess.TimeoutExpired):
            runner([sys.executable, '-c', 'import time;time.sleep(10)'],
                   lambda: None, 0.2, output_limit=1024)


class RealCodecTests(unittest.TestCase):
    def test_pcm_codec_matches_controls_and_refuses_overlong_without_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'synthetic.wav'
            for options in ({}, {'pitch': 220}, {'amplitude': .25}):
                pcm = (make_pcm(**options) * 32767).astype('<i2')
                with wave.open(str(source), 'wb') as output:
                    output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    output.writeframes(pcm.tobytes())
                measured = acoustics.measure_take_acoustics(pcm, 16000)
                decoded = acoustics.decode_take_acoustics(source)
                self.assertIsNone(decoded['error'])
                for metric in acoustics.SPREAD_FLOORS:
                    self.assertAlmostEqual(measured[metric], decoded[metric], places=6)
            with wave.open(str(source), 'wb') as output:
                output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                output.writeframes(bytes(61 * 16000 * 2))
            with patch.object(acoustics, 'run_owned_media_check',
                              wraps=acoustics.run_owned_media_check) as runner:
                self.assertIn('duration limit', acoustics.decode_take_acoustics(source)['error'])
            self.assertEqual(1, runner.call_count)
            self.assertIn('ffprobe', Path(runner.call_args.args[0][0]).name)

    def test_cancellation_callback_reaches_running_process_and_reaps_it(self):
        from chunk_outlier_check import OutlierCheckCancelled
        import media_process
        processes = []
        original = media_process.start_owned_subprocess
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        def cancel():
            if processes:
                self.assertIsNone(processes[0].poll())
                raise OutlierCheckCancelled('synthetic cancellation')
        with patch.object(media_process, 'start_owned_subprocess', side_effect=launch):
            with self.assertRaises(OutlierCheckCancelled):
                acoustics.run_owned_media_check([acoustics.shutil.which('ffmpeg'),
                    '-nostdin', '-v', 'error', '-re', '-f', 'lavfi', '-i',
                    'sine=frequency=120:sample_rate=16000', '-f', 'null', '-'],
                    cancel, 5, output_limit=1024)
        self.assertEqual(1, len(processes))
        self.assertIsNotNone(processes[0].poll())


if __name__ == '__main__':
    unittest.main()
