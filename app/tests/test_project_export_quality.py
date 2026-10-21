"""Mandatory assembled-delivery checks survive missing encoder transport."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine

import audio_export
import export_quality as quality
import project


class ProjectExportQualityTests(unittest.TestCase):
    def fixture(self, directory, interior_silence=False):
        root = Path(directory)
        manager = project.ProjectManager(directory)
        tone = Sine(440, sample_rate=24000).to_audio_segment(duration=1000).apply_gain(-24)
        if interior_silence:
            tone += AudioSegment.silent(duration=2000, frame_rate=24000) + tone
        source = root / 'source.wav'
        tone.export(source, format='wav').close()
        manager.save_chunks([dict(id=index, uid='line-' + str(index), speaker='A',
            text='Synthetic line.', status='done', audio_path=source.name, pause_after=1500)
            for index in range(2)])
        return manager, source

    def assert_clean(self, directory):
        self.assertEqual([], [path.name for path in Path(directory).iterdir()
            if path.name.startswith(('.mp3-merge-', '.pcm-encode-', '.stream-pcm-', '.chapter-export-'))
            or '.pending.' in path.name])

    def test_native_legacy_and_mastered_mp3_have_one_actual_decode_and_current_evidence(self):
        for legacy in (False, True):
            for mastering in (None, {}):
                with self.subTest(legacy=legacy, mastering=mastering), tempfile.TemporaryDirectory() as tmp:
                    manager, source = self.fixture(tmp)
                    original, rows = source.read_bytes(), copy.deepcopy(manager.load_chunks())
                    reports = []
                    writer = project.write_combined_pcm
                    with patch.object(project, 'write_combined_pcm',
                            side_effect=(lambda *args, **kwargs: None) if legacy else writer), \
                            patch.object(quality, 'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                        success, message = manager.merge_audio(mastering=mastering,
                            export_quality={'max_unexpected_silence_seconds': .2, 'reject_unexpected_silence': True},
                            export_quality_callback=reports.append)
                        self.assertTrue(success, message)
                        self.assertEqual(1, scan.call_count)
                    self.assertEqual(1, len(reports))
                    report = reports[0]
                    path = Path(tmp) / 'cloned_audiobook.mp3'
                    self.assertEqual('verified', report['status'])
                    self.assertEqual(0, report['unexpected_silence_count'])
                    self.assertEqual('cloned_audiobook.mp3', report['file'])
                    self.assertEqual(quality.get_export_file_identity(path)['sha256'], report['sha256'])
                    self.assertEqual(quality.get_export_file_version(path), report['file_version'])
                    self.assertEqual(rows, manager.load_chunks())
                    self.assertEqual(original, source.read_bytes())
                    self.assert_clean(tmp)

    def test_zero_exit_short_output_without_encoder_receipt_is_refused_by_project(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                manager, source = self.fixture(tmp)
                prior = Path(tmp) / 'cloned_audiobook.mp3'
                prior.write_bytes(b'previous complete delivery')
                original = source.read_bytes()
                reports = []
                short = Sine(440, sample_rate=24000).to_audio_segment(duration=250).apply_gain(-24)
                def successful_but_short(*args, **kwargs):
                    audio_export.export_audio_segment(short, args[1], 'mp3', lambda: False)
                target = project if legacy else audio_export
                name = '_export_audio_segment' if legacy else 'export_pcm_mp3'
                with patch.object(project, 'write_combined_pcm',
                        side_effect=(lambda *args, **kwargs: None) if legacy else project.write_combined_pcm), \
                        patch.object(target, name, side_effect=successful_but_short):
                    with self.assertRaises(quality.ExportQualityError):
                        manager.merge_audio(export_quality_callback=reports.append)
                self.assertEqual([], reports)
                self.assertEqual(b'previous complete delivery', prior.read_bytes())
                self.assertEqual(original, source.read_bytes())
                self.assert_clean(tmp)

    def test_missing_transport_checks_valid_bytes_and_unplanned_gap_policy_preserves_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, source = self.fixture(tmp, interior_silence=True)
            path = Path(tmp) / 'cloned_audiobook.mp3'
            path.write_bytes(b'prior delivery')
            original = source.read_bytes()
            reports = []
            encode = audio_export.export_pcm_mp3
            def no_transport(*args, **kwargs):
                kwargs.pop('quality_plan')
                kwargs.pop('quality_callback')
                return encode(*args, **kwargs)
            with patch.object(audio_export, 'export_pcm_mp3', side_effect=no_transport):
                with self.assertRaises(quality.ExportQualityError):
                    manager.merge_audio(export_quality={'max_unexpected_silence_seconds': .5,
                        'reject_unexpected_silence': True}, export_quality_callback=reports.append)
                self.assertEqual(b'prior delivery', path.read_bytes())
                self.assertEqual([], reports)
                result = manager.merge_audio(export_quality={'max_unexpected_silence_seconds': .5},
                    export_quality_callback=reports.append)
                self.assertTrue(result[0], result[1])
            self.assertEqual('review', reports[0]['status'])
            self.assertEqual(2, reports[0]['unexpected_silence_count'])
            self.assertEqual(original, source.read_bytes())
            self.assert_clean(tmp)

    def test_late_cancel_keeps_previous_delivery_and_postcommit_reporter_cannot_fail_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self.fixture(tmp)
            path = Path(tmp) / 'cloned_audiobook.mp3'
            path.write_bytes(b'prior delivery')
            cancelled = False
            admit = quality.get_admitted_export_report
            def cancel_after_admission(*args, **kwargs):
                nonlocal cancelled
                report = admit(*args, **kwargs)
                cancelled = True
                return report
            with patch.object(quality, 'get_admitted_export_report', side_effect=cancel_after_admission):
                self.assertEqual((False, 'Merge cancelled'), manager.merge_audio(cancel_check=lambda: cancelled))
            self.assertEqual(b'prior delivery', path.read_bytes())
            self.assert_clean(tmp)
            def broken_reporter(report):
                raise RuntimeError('report observer failed')
            with self.assertLogs('AlexandriaUI', level='WARNING'):
                success, message = manager.merge_audio(mastering={}, mastering_callback=broken_reporter,
                    export_quality_callback=broken_reporter)
            self.assertTrue(success, message)
            self.assertGreater(len(AudioSegment.from_mp3(path)), 3000)
            self.assert_clean(tmp)


if __name__ == '__main__':
    unittest.main()
