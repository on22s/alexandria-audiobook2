"""A private encoder receipt is reusable only for the current bytes and plan."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydub.generators import Sine

import audio_export
import export_quality as quality
from tts import ExportCancelled


class ExportQualityAdmissionTests(unittest.TestCase):
    def encoded(self, directory, fmt='wav'):
        source = Sine(440, sample_rate=24000).to_audio_segment(duration=1000).apply_gain(-24)
        plan = quality.get_pcm_export_encoding_plan(dict(frames=24000, frame_rate=24000,
            channels=1, sample_width=2, intentional_pause_frames=[]), format_name=fmt)
        path, reports = Path(directory) / ('delivery.' + fmt), []
        audio_export.export_audio_segment(source, path, fmt, lambda: False,
            quality_plan=plan, quality_callback=reports.append)
        return path, plan, reports[0]

    def test_real_encoder_receipt_rebinds_after_rename_without_second_decode(self):
        for fmt in ('wav', 'mp3'):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                path, plan, report = self.encoded(tmp, fmt)
                before, plan_before = copy.deepcopy(report), copy.deepcopy(plan)
                renamed = path.with_name('pending.' + fmt)
                os.replace(path, renamed)
                with patch.object(quality, 'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                    result = quality.get_admitted_export_report(renamed, plan, report)
                    scan.assert_not_called()
                self.assertEqual(before, report)
                self.assertEqual(plan_before, plan)
                self.assertIsNot(result, report)
                self.assertEqual(quality.get_export_file_version(renamed), result['file_version'])
                self.assertEqual(report['sha256'], result['sha256'])
                result['diagnostics'].append({'independent': True})
                self.assertEqual([], report['diagnostics'])

    def test_missing_stale_or_incomplete_receipt_runs_real_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, plan, report = self.encoded(tmp)
            invalid = [None, True, {}, {**report, 'stage': 'probe'}, {**report, 'status': 'failed'},
                {**report, 'version': 0}, {**report, 'plan_sha256': '0' * 64},
                {**report, 'sha256': '0' * 64}, {**report, 'expected_frames': 1},
                {**report, 'source_shape': None}, {**report, 'options': {}},
                {**report, 'decoded_frames': True}, {**report, 'output_bytes': True},
                {**report, 'decoded_frames': 1}, {**report, 'duration_delta_seconds': float('nan')},
                {**report, 'codec': None}, {**report, 'reason': 'x' * 100000},
                {**report, 'unexpected_silence_ranges': [None]}, {**report, 'seconds': float('inf')},
                {**report, 'status': 'verified', 'unexpected_silence_count': 1}]
            for candidate in invalid:
                with self.subTest(candidate=candidate), patch.object(quality,
                        'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                    admitted = quality.get_admitted_export_report(path, plan, candidate)
                    scan.assert_called_once()
                    self.assertEqual('verified', admitted['status'])

    def test_changed_options_and_chapter_expectation_cannot_reuse_prior_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, plan, report = self.encoded(tmp)
            changed = copy.deepcopy(plan)
            changed['options']['silence_dbfs'] = -40
            with patch.object(quality, 'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                result = quality.get_admitted_export_report(path, changed, report)
                scan.assert_called_once()
                self.assertNotEqual(report['plan_sha256'], result['plan_sha256'])
            changed['chapters'] = [['Missing chapter', 0, 1]]
            with self.assertRaisesRegex(quality.ExportQualityError, 'chapter count'):
                quality.get_admitted_export_report(path, changed, report)

    def test_modified_bytes_with_restored_timestamp_are_rechecked_and_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, plan, report = self.encoded(tmp)
            prior = path.stat()
            raw = path.read_bytes()
            path.write_bytes(b'broken!!' + raw[8:])
            os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
            with self.assertRaises(quality.ExportQualityError):
                quality.get_admitted_export_report(path, plan, report)
            self.assertEqual(b'broken!!', path.read_bytes()[:8])

    def test_cancelled_receipt_admission_stops_before_hash_or_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, plan, report = self.encoded(tmp)
            with patch.object(quality, 'get_export_file_identity') as identity, \
                    patch.object(quality, 'validate_finished_export') as scan:
                with self.assertRaises(ExportCancelled):
                    quality.get_admitted_export_report(path, plan, report, lambda: True)
                identity.assert_not_called()
                scan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
