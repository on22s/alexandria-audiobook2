"""Real positive-LUFS audio uses a complete, truthful normalization fallback."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pydub.generators import Sine

import audio_mastering as mastering
from audio_export import export_audio_segment
from project import ProjectManager
from tts import ExportCancelled


class MasteringMeasurementRangeTests(unittest.TestCase):
    def test_measured_bundle_uses_all_inclusive_domains_or_omits_every_field(self):
        ordinary = dict(input_i='-20', input_tp='-3', input_lra='2', input_thresh='-30', target_offset='0')
        domains = [('input_i', -99, 0), ('input_tp', -99, 99), ('input_lra', 0, 99),
                   ('input_thresh', -99, 0), ('target_offset', -99, 99)]
        for key, low, high in domains:
            for value in (low, high, low - .01, high + .01):
                with self.subTest(key=key, value=value):
                    record = {**ordinary, key: str(value)}
                    before = dict(record)
                    result = mastering.get_mastering_filter({}, record, output_channels=2)
                    self.assertEqual(before, record)
                    self.assertTrue(result.startswith('aformat=channel_layouts=stereo,loudnorm='))
                    if low <= value <= high:
                        self.assertEqual(4, result.count('measured_'))
                        self.assertIn(':offset=', result)
                        self.assertIn(':linear=true', result)
                    else:
                        self.assertNotIn('measured_', result)
                        self.assertNotIn(':offset=', result)
                        self.assertNotIn(':linear=', result)
                        self.assertEqual(mastering.get_mastering_filter({}, output_channels=2), result)

    def test_malformed_nonfinite_and_missing_measurements_still_refuse(self):
        ordinary = dict(input_i='-20', input_tp='-3', input_lra='2', input_thresh='-30', target_offset='0')
        for key in ordinary:
            for value in ('nan', 'inf', '-inf', 'invalid', None):
                with self.subTest(key=key, value=value), self.assertRaises((ValueError, TypeError)):
                    mastering.get_mastering_filter({}, {**ordinary, key: value})
            record = dict(ordinary)
            del record[key]
            with self.subTest(missing=key), self.assertRaises(KeyError):
                mastering.get_mastering_filter({}, record)

    def assert_positive_media(self, path, report, seconds, channels):
        self.assertGreater(report['before']['integrated_lufs'], 0)
        self.assertAlmostEqual(-18, report['after']['integrated_lufs'], delta=1)
        self.assertLessEqual(report['after']['true_peak_dbtp'], -1.5)
        self.assertIn(report['mode'], ('linear', 'dynamic'))
        self.assertEqual(mastering.get_audio_sha256(path), report['sha256'])
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
            '-show_chapters', '-of', 'json', str(path)], timeout=10))
        streams = [row for row in probe['streams'] if row['codec_type'] == 'audio']
        self.assertEqual(1, len(streams))
        self.assertEqual((24000, channels), (int(streams[0]['sample_rate']), streams[0]['channels']))
        decoded = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(path),
            '-map', '0:a:0', '-c:a', 'pcm_f32le', '-f', 'f32le', '-'],
            capture_output=True, check=True, timeout=10)
        self.assertFalse(decoded.stderr)
        audio = np.frombuffer(decoded.stdout, dtype='<f4').reshape(-1, channels)
        self.assertTrue(np.isfinite(audio).all())
        self.assertGreater(float(np.max(np.abs(audio))), 1e-5)
        self.assertLessEqual(abs(len(audio) - seconds * 24000), 0 if path.suffix == '.wav' else 2304)
        # A distinct meter independently checks the actual encoded loudness.
        meter = subprocess.run(['ffmpeg', '-nostdin', '-v', 'info', '-i', str(path),
            '-af', 'ebur128=peak=true', '-f', 'null', '-'], capture_output=True,
            text=True, check=True, timeout=10)
        import re
        values = re.findall(r'I:\s*(-?[\d.]+) LUFS', meter.stderr)
        self.assertTrue(values)
        self.assertAlmostEqual(report['after']['integrated_lufs'], float(values[-1]), delta=.2)
        return probe

    def test_real_positive_pcm_exports_keep_actual_measurements_and_encoded_loudness(self):
        for seconds in (2, 5):
            for fmt, channels in [('mp3', 2), ('wav', 6)]:
                with self.subTest(seconds=seconds, fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                    source = Sine(440, sample_rate=24000).to_audio_segment(duration=seconds * 1000).set_channels(6)
                    before = source.raw_data
                    path = Path(tmp) / ('delivery.' + fmt)
                    path.write_bytes(b'previous synthetic output')
                    report = export_audio_segment(source, path, fmt, lambda: False, mastering={})
                    self.assert_positive_media(path, report, seconds, channels)
                    self.assertEqual(before, source.raw_data)
                    self.assertEqual([path.name], [item.name for item in Path(tmp).iterdir()])

    def test_real_positive_m4b_preserves_source_chapters_and_previous_file_on_late_cancel(self):
        for seconds in (2, 5):
            with self.subTest(seconds=seconds), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'tone.wav'
                Sine(440, sample_rate=24000).to_audio_segment(duration=seconds * 1000).set_channels(6).export(source, format='wav').close()
                manager = ProjectManager(tmp)
                manager.save_chunks([dict(id=0, uid='positive-control', speaker='NARRATOR', text='Chapter 1',
                    status='done', audio_path='tone.wav', pause_after=0)])
                captured = {path: path.read_bytes() for path in [source, Path(manager.chunks_path)]}
                path = root / 'audiobook.m4b'
                path.write_bytes(b'previous synthetic output')
                reports = []
                success, message = manager.merge_m4b(mastering={}, mastering_callback=reports.append)
                self.assertTrue(success, message)
                self.assertEqual(1, len(reports))
                probe = self.assert_positive_media(path, reports[0], seconds, 6)
                self.assertEqual(['Chapter 1'], [row['tags']['title'] for row in probe['chapters']])
                previous = path.read_bytes()
                cancelled = False
                original = mastering.get_mastering_report
                def report_then_cancel(*args, **kwargs):
                    nonlocal cancelled
                    report = original(*args, **kwargs)
                    cancelled = True
                    return report
                with patch.object(mastering, 'get_mastering_report', side_effect=report_then_cancel):
                    result = manager.merge_m4b(mastering={}, cancel_check=lambda: cancelled)
                self.assertEqual((False, 'Export cancelled'), result)
                self.assertEqual(previous, path.read_bytes())
                self.assertEqual(captured, {item: item.read_bytes() for item in captured})
                self.assertEqual([], list(root.glob('.m4b-*')) + list(root.glob('*.pending.*')))

    def test_positive_fallback_report_failure_or_late_cancel_keeps_previous_pcm_export(self):
        source = Sine(440, sample_rate=24000).to_audio_segment(duration=2000).set_channels(6)
        for failure in ('report', 'cancel'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'delivery.mp3'
                path.write_bytes(b'previous synthetic output')
                cancelled = False
                original = mastering.get_mastering_report
                def fail_after_render(*args, **kwargs):
                    nonlocal cancelled
                    report = original(*args, **kwargs)
                    if failure == 'report':
                        raise ValueError('synthetic final-report failure')
                    cancelled = True
                    return report
                with patch.object(mastering, 'get_mastering_report', side_effect=fail_after_render) as report_call:
                    with self.assertRaises(ValueError if failure == 'report' else ExportCancelled):
                        export_audio_segment(source, path, 'mp3', lambda: cancelled, mastering={})
                report_call.assert_called_once()
                self.assertEqual(b'previous synthetic output', path.read_bytes())
                self.assertEqual([path.name], [item.name for item in Path(tmp).iterdir()])
