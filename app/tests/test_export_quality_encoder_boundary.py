"""Finished-byte guards run before encoder publication, including mastering."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pydub.generators import Sine

import audio_export
import audio_ffmpeg
import audio_mastering
import export_quality
from tts import ExportCancelled


class ExportQualityEncoderBoundaryTests(unittest.TestCase):
    def source(self, rate=24000, channels=1):
        return Sine(440, sample_rate=rate).to_audio_segment(duration=1000).apply_gain(-24).set_channels(channels)

    def plan(self, source, fmt):
        return export_quality.get_export_encoding_plan(len(source.raw_data) // source.frame_width,
            source.frame_rate, source.channels, format_name=fmt)

    def settings(self, source):
        return dict(frames=len(source.raw_data) // source.frame_width, frame_rate=source.frame_rate,
            channels=source.channels, sample_width=source.sample_width)

    def assert_receipt(self, path, plan, reports):
        self.assertEqual(1, len(reports))
        report = reports[0]
        self.assertEqual('verified', report['status'])
        self.assertEqual(export_quality.get_export_quality_plan_digest(plan), report['plan_sha256'])
        self.assertEqual(audio_mastering.get_audio_sha256(path), report['sha256'])
        tolerance = 2304 if path.suffix == '.mp3' else 0
        self.assertLessEqual(abs(plan['frames'] - report['decoded_frames']), tolerance)

    def test_segment_encoding_transports_actual_receipt_and_preserves_mastering_return(self):
        for fmt in ('mp3', 'wav'):
            for mastering in (None, {}):
                with self.subTest(fmt=fmt, mastering=mastering), tempfile.TemporaryDirectory() as tmp:
                    source = self.source(30000, 6)
                    before = source.raw_data
                    path = Path(tmp) / ('delivery.' + fmt)
                    plan = self.plan(source, fmt)
                    captured = copy.deepcopy(plan)
                    reports = []
                    result = audio_export.export_audio_segment(source, path, fmt, lambda: False,
                        mastering=mastering, quality_plan=plan, quality_callback=reports.append)
                    self.assertEqual(captured, plan)
                    self.assertEqual(before, source.raw_data)
                    self.assert_receipt(path, plan, reports)
                    if mastering is None:
                        self.assertIsNone(result)
                    else:
                        self.assertEqual(reports[0]['sha256'], result['sha256'])
                    self.assertEqual([path.name], [item.name for item in Path(tmp).iterdir()])

    def test_spooled_pcm_encoding_uses_same_output_clock_and_receipt(self):
        for mastering in (None, {}):
            with self.subTest(mastering=mastering), tempfile.TemporaryDirectory() as tmp:
                source = self.source(96000, 6)
                raw, path = Path(tmp) / 'input.pcm', Path(tmp) / 'delivery.mp3'
                raw.write_bytes(source.raw_data)
                settings = self.settings(source)
                before = copy.deepcopy(settings)
                plan, reports = self.plan(source, 'mp3'), []
                result = audio_export.export_pcm_mp3(raw, path, settings, lambda: False,
                    mastering=mastering, quality_plan=plan, quality_callback=reports.append)
                self.assertEqual(before, settings)
                self.assertEqual(source.raw_data, raw.read_bytes())
                self.assert_receipt(path, plan, reports)
                self.assertEqual((48000, 2), (reports[0]['sample_rate'], reports[0]['channels']))
                if mastering is not None:
                    self.assertEqual(result['sha256'], reports[0]['sha256'])
                self.assertEqual([], list(Path(tmp).glob('.pcm-encode-*')))

    def test_zero_exit_short_encoded_audio_cannot_replace_previous_delivery(self):
        for mode in ('segment', 'pcm'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                source = self.source()
                path = Path(tmp) / 'delivery.mp3'
                path.write_bytes(b'previous verified delivery')
                plan = self.plan(source, 'mp3')
                if mode == 'segment':
                    launch = audio_export.start_owned_subprocess
                    def shortened(command, **kwargs):
                        raw = Path(command[2])
                        raw.write_bytes(raw.read_bytes()[:len(source.raw_data) // 2])
                        return launch(command, **kwargs)
                    with patch.object(audio_export, 'start_owned_subprocess', side_effect=shortened):
                        with self.assertRaises(export_quality.ExportQualityError):
                            audio_export.export_audio_segment(source, path, 'mp3', lambda: False,
                                quality_plan=plan)
                else:
                    raw = Path(tmp) / 'input.pcm'
                    raw.write_bytes(source.raw_data)
                    encode = audio_ffmpeg.run_audio_ffmpeg
                    def shortened(arguments, *args, **kwargs):
                        return encode([*arguments[:-1], '-t', '0.25', arguments[-1]], *args, **kwargs)
                    with patch.object(audio_ffmpeg, 'run_audio_ffmpeg', side_effect=shortened):
                        with self.assertRaises(export_quality.ExportQualityError):
                            audio_export.export_pcm_mp3(raw, path, self.settings(source), lambda: False,
                                quality_plan=plan)
                    self.assertEqual(source.raw_data, raw.read_bytes())
                self.assertEqual(b'previous verified delivery', path.read_bytes())
                self.assertEqual([], list(Path(tmp).glob('.chapter-export-*')) + list(Path(tmp).glob('.pcm-encode-*')))

    def test_mastering_report_does_not_exempt_short_finished_bytes_from_validation(self):
        for mode in ('segment', 'pcm'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                source = self.source()
                path = Path(tmp) / 'delivery.mp3'
                path.write_bytes(b'previous verified delivery')
                original = audio_mastering.export_mastered_pcm
                def replace_after_report(*args, **kwargs):
                    report = original(*args, **kwargs)
                    output = Path(args[1])
                    short = output.with_name('short.mp3')
                    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output),
                        '-t', '0.25', '-c:a', 'libmp3lame', str(short)], check=True, timeout=10)
                    os.replace(short, output)
                    return report
                with patch.object(audio_mastering, 'export_mastered_pcm', side_effect=replace_after_report) as render:
                    with self.assertRaises(export_quality.ExportQualityError):
                        if mode == 'segment':
                            audio_export.export_audio_segment(source, path, 'mp3', lambda: False,
                                mastering={}, quality_plan=self.plan(source, 'mp3'))
                        else:
                            raw = Path(tmp) / 'input.pcm'
                            raw.write_bytes(source.raw_data)
                            audio_export.export_pcm_mp3(raw, path, self.settings(source), lambda: False,
                                mastering={}, quality_plan=self.plan(source, 'mp3'))
                render.assert_called_once()
                self.assertEqual(b'previous verified delivery', path.read_bytes())
                self.assertEqual([], list(Path(tmp).glob('.chapter-export-*')) + list(Path(tmp).glob('.pcm-encode-*')))

    def test_cancel_or_changed_bytes_after_receipt_transport_keep_prior_output(self):
        for mode in ('cancel', 'changed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                source = self.source()
                path = Path(tmp) / 'delivery.wav'
                path.write_bytes(b'previous verified delivery')
                stages, cancelled = [], False
                validate = export_quality.validate_finished_export
                def capture_stage(stage, *args, **kwargs):
                    result = validate(stage, *args, **kwargs)
                    stages.append(Path(stage))
                    return result
                def receive(report):
                    nonlocal cancelled
                    if mode == 'cancel':
                        cancelled = True
                    else:
                        stage = stages[-1]
                        stat = stage.stat()
                        data = stage.read_bytes()
                        stage.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
                        os.utime(stage, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                        # A callback cannot forge the original admitted receipt.
                        report['sha256'] = audio_mastering.get_audio_sha256(stage)
                with patch.object(export_quality, 'validate_finished_export', side_effect=capture_stage):
                    with self.assertRaises(ExportCancelled if mode == 'cancel' else export_quality.ExportQualityError):
                        audio_export.export_audio_segment(source, path, 'wav', lambda: cancelled,
                            quality_plan=self.plan(source, 'wav'), quality_callback=receive)
                self.assertEqual(1, len(stages))
                self.assertEqual(b'previous verified delivery', path.read_bytes())
                self.assertEqual([path.name], [item.name for item in Path(tmp).iterdir()])

    def test_ordinary_chunk_exports_do_not_add_a_finished_export_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.source()
            raw = Path(tmp) / 'input.pcm'
            raw.write_bytes(source.raw_data)
            with patch.object(export_quality, 'validate_finished_export', side_effect=AssertionError('unexpected scan')) as scan:
                audio_export.export_audio_segment(source, Path(tmp) / 'chunk.wav', 'wav', lambda: False)
                audio_export.export_pcm_mp3(raw, Path(tmp) / 'chunk.mp3', self.settings(source), lambda: False)
            scan.assert_not_called()
            self.assertGreater((Path(tmp) / 'chunk.wav').stat().st_size, 44)
            self.assertGreater((Path(tmp) / 'chunk.mp3').stat().st_size, 44)
