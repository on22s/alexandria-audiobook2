"""Real finished media, deliberate corruption and owned validation failures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

import export_quality as quality
from tts import ExportCancelled


class ExportQualityTests(unittest.TestCase):
    def source(self, root, *, seconds=2, channels=1, values=None, subtype='PCM_16'):
        if values is None:
            values = .2 * np.sin(np.arange(round(seconds * 24000)) * .1)
            if channels == 2:
                values = np.column_stack([values, -values])
        path = Path(root) / 'source.wav'
        sf.write(path, values, 24000, subtype=subtype)
        return path, quality.get_export_quality_plan(len(values), 24000, channels, format_name='wav')

    def encode(self, source, suffix, extra=()):
        path = source.with_name('finished.' + suffix)
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source), *extra]
        command += (['-c:a', 'aac', '-f', 'mp4'] if suffix == 'm4b' else []) + [str(path)]
        subprocess.run(command, check=True, capture_output=True, timeout=10)
        return path

    def test_real_wav_mp3_aac_and_opposite_phase_stereo_match_independent_frame_plan(self):
        for channels in (1, 2):
            with self.subTest(channels=channels), tempfile.TemporaryDirectory() as root:
                source, plan = self.source(root, channels=channels)
                for fmt in ('wav', 'mp3', 'm4b'):
                    with self.subTest(format=fmt):
                        path = source if fmt == 'wav' else self.encode(source, fmt)
                        before = path.read_bytes()
                        current_plan = {**plan, 'format': fmt}
                        result = quality.validate_finished_export(path, current_plan)
                        self.assertEqual('verified', result['status'])
                        self.assertEqual(0, result['unexpected_silence_count'])
                        self.assertEqual(channels, result['channels'])
                        self.assertLessEqual(result['duration_delta_seconds'], result['duration_tolerance_seconds'])
                        self.assertEqual(before, path.read_bytes())
                        self.assertEqual(quality.get_export_quality_plan_digest(current_plan), result['plan_sha256'])

    def test_valid_pcm_bytes_cannot_be_certified_as_requested_mp3(self):
        with tempfile.TemporaryDirectory() as root:
            source, plan = self.source(root)
            mislabeled = Path(root) / 'delivery.mp3'
            mislabeled.write_bytes(source.read_bytes())
            plan['format'] = 'mp3'
            with self.assertRaisesRegex(quality.ExportQualityError, 'requested export format'):
                quality.validate_finished_export(mislabeled, plan)

    def test_high_rate_encoding_uses_independently_planned_pinned_output_clock(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'high-rate.wav'
            for source_rate in (30000, 96000, 192000):
                frames = source_rate + 137
                sf.write(source, .2 * np.sin(np.arange(frames) * .1), source_rate)
                for fmt in ('mp3', 'm4b'):
                    with self.subTest(source_rate=source_rate, format=fmt):
                        plan = quality.get_export_encoding_plan(frames, source_rate, 1, format_name=fmt)
                        encoded = self.encode(source, fmt, ['-ar', str(plan['sample_rate']), '-ac', str(plan['channels'])])
                        result = quality.validate_finished_export(encoded, plan)
                        self.assertEqual('verified', result['status'])
                        self.assertEqual(source_rate, result['source_shape']['sample_rate'])
                        self.assertLessEqual(result['duration_delta_seconds'], result['duration_tolerance_seconds'])

    def test_empty_truncated_and_valid_wrong_duration_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            source, plan = self.source(root)
            for content in (b'', b'not an encoded audiobook', source.read_bytes()[:1000]):
                path = Path(root) / 'broken.wav'
                path.write_bytes(content)
                with self.subTest(size=len(content)), self.assertRaises(quality.ExportQualityError):
                    quality.validate_finished_export(path, plan)
                self.assertEqual(content, path.read_bytes())
            wrong = quality.get_export_quality_plan(24000, 24000, 1, format_name='wav')
            with self.assertRaisesRegex(quality.ExportQualityError, 'duration'):
                quality.validate_finished_export(source, wrong)

    def test_nan_and_infinity_are_rejected_even_when_native_decoder_exits_successfully(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                samples = np.column_stack([np.zeros(48000), np.zeros(48000)])
                samples[12345, 1] = value
                source, plan = self.source(root, channels=2, values=samples, subtype='FLOAT')
                with self.assertRaisesRegex(quality.ExportQualityError, 'non-finite'):
                    quality.validate_finished_export(source, plan)

    def test_inserted_gap_warns_by_default_and_explicit_policy_refuses_it(self):
        with tempfile.TemporaryDirectory() as root:
            samples = .2 * np.sin(np.arange(48000) * .1)
            samples[12000:36000] = 0
            source, _ = self.source(root, values=samples)
            for reject in (False, True):
                plan = quality.get_export_quality_plan(48000, 24000, 1, format_name='wav', options={
                    'max_unexpected_silence_seconds': .5, 'reject_unexpected_silence': reject})
                if reject:
                    with self.assertRaises(quality.ExportQualityError) as caught:
                        quality.validate_finished_export(source, plan)
                    result = caught.exception.report
                else:
                    result = quality.validate_finished_export(source, plan)
                    self.assertEqual('review', result['status'])
                self.assertEqual(1, result['unexpected_silence_count'])
                self.assertAlmostEqual(.5, result['unexpected_silence_ranges'][0]['start_seconds'], delta=.001)
                self.assertAlmostEqual(1.5, result['unexpected_silence_ranges'][0]['end_seconds'], delta=.001)

    def test_authored_pause_is_exempt_but_extra_silence_outside_it_is_not(self):
        with tempfile.TemporaryDirectory() as root:
            samples = np.zeros(96000)
            samples[:12000] = .2 * np.sin(np.arange(12000) * .1)
            samples[84000:] = samples[:12000]
            source, _ = self.source(root, values=samples)
            options = dict(max_unexpected_silence_seconds=.5, reject_unexpected_silence=True)
            plan = quality.get_export_quality_plan(96000, 24000, 1, format_name='wav',
                intentional_pauses=[(.5, 3.5)], options=options)
            self.assertEqual('verified', quality.validate_finished_export(source, plan)['status'])
            plan = quality.get_export_quality_plan(96000, 24000, 1, format_name='wav',
                intentional_pauses=[(1.5, 2.5)], options=options)
            with self.assertRaises(quality.ExportQualityError) as caught:
                quality.validate_finished_export(source, plan)
            self.assertEqual(2, caught.exception.report['unexpected_silence_count'])

    def test_diagnostics_keep_complete_counts_and_bound_excerpts(self):
        with tempfile.TemporaryDirectory() as root:
            tone = .2 * np.sin(np.arange(2400) * .1)
            samples = np.concatenate([np.concatenate([tone, np.zeros(4800)]) for _ in range(40)] + [tone])
            source, _ = self.source(root, values=samples)
            plan = quality.get_export_quality_plan(len(samples), 24000, 1, format_name='wav',
                                                  options={'max_unexpected_silence_seconds': .1})
            result = quality.validate_finished_export(source, plan)
            self.assertEqual(40, result['unexpected_silence_count'])
            self.assertEqual(quality.MAX_DIAGNOSTICS, len(result['unexpected_silence_ranges']))
            self.assertTrue(result['diagnostics_abbreviated'])
            self.assertLess(len(json.dumps(result)), 10000)

    def test_largest_late_gap_survives_excerpt_limit_and_malformed_event_is_refused(self):
        plan = quality.get_export_quality_plan(240000, 24000, 1, format_name='wav',
                                              options={'max_unexpected_silence_seconds': .1})
        evidence = quality._DecodeEvidence(plan)
        for index in range(20):
            evidence.consume(f'[silencedetect @ fixture] silence_start: {index * .3}')
            evidence.consume(f'[silencedetect @ fixture] silence_end: {index * .3 + .2} | silence_duration: .2')
        evidence.consume('[silencedetect @ fixture] silence_start: 7')
        evidence.consume('[silencedetect @ fixture] silence_end: 9 | silence_duration: 2')
        self.assertEqual(21, evidence.unexpected_count)
        self.assertEqual(16, len(evidence.ranges))
        self.assertEqual(dict(start_seconds=7, end_seconds=9), evidence.longest_range)
        for line in ['silence_start: invalid', 'silence_start: nan', 'silence_start broken']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                evidence.consume('[silencedetect @ fixture] ' + line)

    def test_real_chapter_metadata_requires_exact_count_times_and_titles(self):
        with tempfile.TemporaryDirectory() as root:
            source, _ = self.source(root)
            metadata = Path(root) / 'chapters.txt'
            metadata.write_text(';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=First\n'
                                '[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Second\n')
            encoded = self.encode(source, 'm4b', ['-f', 'ffmetadata', '-i', str(metadata), '-map_metadata', '1'])
            chapters = [('First', 0, 1000), ('Second', 1000, 2000)]
            plan = quality.get_export_quality_plan(48000, 24000, 1, format_name='m4b', chapters=chapters)
            self.assertEqual(2, quality.validate_finished_export(encoded, plan)['chapters_checked'])
            for changed in [chapters[:1], [('First', 0, 800), ('Second', 1200, 2000)],
                            [('Incorrect', 0, 1000), chapters[1]]]:
                plan = quality.get_export_quality_plan(48000, 24000, 1, format_name='m4b', chapters=changed)
                with self.subTest(chapters=changed), self.assertRaisesRegex(quality.ExportQualityError, 'chapter'):
                    quality.validate_finished_export(encoded, plan)

    def test_late_bytes_with_restored_mtime_invalidate_the_measured_stage(self):
        with tempfile.TemporaryDirectory() as root:
            source, plan = self.source(root)
            original = quality._run_owned_check
            def change_after_decode(command, *args, **kwargs):
                result = original(command, *args, **kwargs)
                if command[0] == 'ffmpeg':
                    before = source.stat()
                    with source.open('r+b') as stream:
                        stream.seek(-1, 2)
                        stream.write(b'X')
                    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                return result
            with patch.object(quality, '_run_owned_check', side_effect=change_after_decode):
                with self.assertRaisesRegex(quality.ExportQualityError, 'changed after validation'):
                    quality.validate_finished_export(source, plan)

    def test_mp4_implicit_chapter_ends_preserve_intentional_interchapter_pauses(self):
        with tempfile.TemporaryDirectory() as root:
            source, _ = self.source(root)
            metadata = Path(root) / 'chapters.txt'
            metadata.write_text(';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=800\ntitle=First\n'
                                '[CHAPTER]\nTIMEBASE=1/1000\nSTART=1200\nEND=2000\ntitle=Second\n')
            encoded = self.encode(source, 'm4b', ['-f', 'ffmetadata', '-i', str(metadata), '-map_metadata', '1'])
            plan = quality.get_export_quality_plan(48000, 24000, 1, format_name='m4b',
                chapters=[('First', 0, 800), ('Second', 1200, 2000)])
            self.assertEqual(2, quality.validate_finished_export(encoded, plan)['chapters_checked'])
            wrong = quality.get_export_quality_plan(48000, 24000, 1, format_name='m4b',
                chapters=[('First', 0, 800), ('Second', 1100, 2000)])
            with self.assertRaisesRegex(quality.ExportQualityError, 'chapter'):
                quality.validate_finished_export(encoded, wrong)

    def test_finished_hash_fences_bytes_when_metadata_clock_does_not_change(self):
        with tempfile.TemporaryDirectory() as root:
            source, plan = self.source(root)
            result = quality.validate_finished_export(source, plan)
            with source.open('r+b') as stream:
                stream.seek(-1, 2)
                stream.write(b'X')
            descriptor_stat = os.fstat
            def coarse_clock(descriptor):
                info = descriptor_stat(descriptor)
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode,
                    st_size=info.st_size, st_mtime_ns=result['file_version'][3], st_ctime_ns=result['file_version'][4])
            with patch.object(quality, 'get_export_file_version', return_value=result['file_version']), \
                    patch.object(quality.os, 'fstat', side_effect=coarse_clock):
                # Both metadata views appear unchanged; the actual changed bytes
                # are still read and hashed by the publication guard.
                with self.assertRaises(quality.ExportQualityError):
                    quality.ensure_export_stage_current(source, result)

    def test_nonregular_files_are_refused_before_any_blocking_read(self):
        with tempfile.TemporaryDirectory() as root:
            plan = quality.get_export_quality_plan(48000, 24000, 1, format_name='wav')
            with self.assertRaisesRegex(quality.ExportQualityError, 'regular private file'):
                quality.validate_finished_export(root, plan)
            if hasattr(os, 'mkfifo'):
                fifo = Path(root) / 'pipe.wav'
                os.mkfifo(fifo)
                with self.assertRaisesRegex(quality.ExportQualityError, 'regular private file'):
                    quality.validate_finished_export(fifo, plan)

    def test_private_file_open_failure_and_fifo_swap_close_descriptors_with_a_hard_bound(self):
        code = r'''
import os,tempfile
from pathlib import Path
from unittest.mock import patch
import export_quality as quality
with tempfile.TemporaryDirectory() as root:
    source = Path(root) / 'private.zip'
    source.write_bytes(b'private file')
    original, descriptors = os.open, []
    def tracked(path, *args, **kwargs):
        fd = original(path, *args, **kwargs)
        descriptors.append(fd)
        return fd
    with patch.object(quality.os, 'open', side_effect=tracked), \
            patch.object(quality.os, 'fdopen', side_effect=OSError('failed fdopen')):
        try:quality.get_export_file_identity(source)
        except OSError:pass
        else:raise AssertionError('fdopen failure was hidden')
    assert len(descriptors) == 1
    try:os.fstat(descriptors[0])
    except OSError:pass
    else:raise AssertionError('failed fdopen leaked its descriptor')
    if hasattr(os, 'mkfifo'):
        descriptors.clear()
        def fifo_before_open(path, *args, **kwargs):
            source.unlink();os.mkfifo(source)
            return tracked(path, *args, **kwargs)
        with patch.object(quality.os, 'open', side_effect=fifo_before_open):
            try:quality.get_export_file_identity(source)
            except ValueError:pass
            else:raise AssertionError('nonregular private file was admitted')
        assert len(descriptors) == 1
        try:os.fstat(descriptors[0])
        except OSError:pass
        else:raise AssertionError('FIFO race leaked its descriptor')
print('private open checks complete')
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('private open checks complete', result.stdout)

    def test_probe_output_bound_and_cancel_stop_the_owned_check(self):
        with self.assertRaisesRegex(ValueError, 'bounded output'):
            quality._run_owned_check([sys.executable, '-c', 'import sys;sys.stdout.write("x"*1000000)'],
                                     None, 5, output_limit=128)
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'started'
            command = [sys.executable, '-c',
                       'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");time.sleep(60)', str(marker)]
            with self.assertRaises(ExportCancelled):
                quality._run_owned_check(command, marker.exists, 10)

    def test_options_are_finite_and_invalid_plans_never_enter_a_tool_command(self):
        for value in ({'silence_dbfs': float('nan')}, {'max_unexpected_silence_seconds': 0},
                      {'unknown': True}, [], ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quality.get_export_quality_options(value)
        plan = quality.get_export_quality_plan(48000, 24000, 1, format_name='wav')
        plan['intentional_pauses'] = [(0, float('inf'))]
        with patch.object(quality, '_run_owned_check') as run:
            with self.assertRaises(ValueError):
                quality.validate_finished_export('unused', plan)
            run.assert_not_called()
