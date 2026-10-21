"""Actual AAC, chapters and authored metadata are checked before M4B publication."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pydub.generators import Sine

import export_quality as quality
import m4b_encode
import project


class M4BExportQualityTests(unittest.TestCase):
    def fixture(self, directory, rates=(24000, 24000, 24000)):
        manager = project.ProjectManager(directory)
        rows = []
        for index, rate in enumerate(rates):
            path = Path(directory) / f'source-{index}.wav'
            Sine(440 + 100 * index, sample_rate=rate).to_audio_segment(duration=1000).apply_gain(-24).export(
                path, format='wav').close()
            rows.append(dict(id=index, uid=f'line-{index}', speaker='NARRATOR', text=f'Chapter {index + 1}',
                audio_path=path.name, status='done', pause_after=600))
        manager.save_chunks(rows)
        return manager

    def assert_clean(self, directory):
        self.assertEqual([], [path.name for path in Path(directory).iterdir()
            if path.name.startswith(('.m4b-', '.source-pcm-', '.stream-wav-', '.spool-wav-', '.chapter-export-'))
            or '.pending.' in path.name])

    def test_native_legacy_and_mastered_aac_match_exact_source_clock_chapters_tags_and_one_decode(self):
        tags = dict(title='Synthetic = book; #1\nsecond line', author='Author \\ Example', narrator='Narrator',
                    year='2026', description='A bounded description.')
        for legacy in (False, True):
            for mastering in (None, {}):
                with self.subTest(legacy=legacy, mastering=mastering), tempfile.TemporaryDirectory() as tmp:
                    manager = self.fixture(tmp, (8000, 24000, 44100))
                    rows = copy.deepcopy(manager.load_chunks())
                    before = {row['audio_path']: (Path(tmp) / row['audio_path']).read_bytes() for row in rows}
                    reports = []
                    writer = project.write_combined_wav
                    with patch.object(project, 'write_combined_wav',
                            side_effect=(lambda *args, **kwargs: None) if legacy else writer), \
                            patch.object(quality, 'validate_finished_export', wraps=quality.validate_finished_export) as scan:
                        success, message = manager.merge_m4b(metadata=tags, mastering=mastering,
                            export_quality={'max_unexpected_silence_seconds': .2, 'reject_unexpected_silence': True},
                            export_quality_callback=reports.append)
                        self.assertTrue(success, message)
                        self.assertEqual(1, scan.call_count)
                    self.assertEqual(1, len(reports))
                    report = reports[0]
                    self.assertEqual('verified', report['status'])
                    self.assertEqual(3, report['chapters_checked'])
                    self.assertEqual(6, report['tags_checked'])
                    self.assertEqual(0, report['unexpected_silence_count'])
                    path = Path(tmp) / 'audiobook.m4b'
                    self.assertEqual(quality.get_export_file_identity(path)['sha256'], report['sha256'])
                    self.assertEqual(quality.get_export_file_version(path), report['file_version'])
                    actual = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_chapters',
                        '-show_format', '-of', 'json', str(path)], timeout=10))
                    self.assertEqual(['Chapter 1', 'Chapter 2', 'Chapter 3'],
                                     [item['tags']['title'] for item in actual['chapters']])
                    self.assertEqual(tags['title'].replace('\n', ' '), actual['format']['tags']['title'])
                    self.assertEqual(rows, manager.load_chunks())
                    self.assertEqual(before, {row['audio_path']: (Path(tmp) / row['audio_path']).read_bytes() for row in rows})
                    self.assert_clean(tmp)

    def test_successful_encoder_with_short_audio_missing_moved_or_renamed_chapter_and_wrong_tags_is_refused(self):
        for fault in ('short', 'missing', 'time', 'title', 'book-tag', 'corrupt'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                manager = self.fixture(tmp)
                output = Path(tmp) / 'audiobook.m4b'
                output.write_bytes(b'prior complete M4B')
                reports = []
                encode = m4b_encode.encode_m4b
                def faulty(command, *args, **kwargs):
                    if fault == 'corrupt':
                        Path(command[-1]).write_bytes(b'zero-exit corrupt output')
                        return 0, ''
                    metadata_path = Path(command[command.index('-map_metadata') - 1])
                    text = metadata_path.read_text()
                    if fault == 'missing':
                        text = text[:text.rfind('[CHAPTER]')]
                    elif fault == 'time':
                        text = text.replace('START=1600', 'START=1900')
                    elif fault == 'title':
                        text = text.replace('title=Chapter 2', 'title=Wrong title')
                    elif fault == 'book-tag':
                        text = text.replace('title=Authored book', 'title=Wrong book')
                    metadata_path.write_text(text)
                    if fault == 'short':
                        command = command[:-1] + ['-t', '0.25', command[-1]]
                    return encode(command, *args, **kwargs)
                with patch.object(m4b_encode, 'encode_m4b', side_effect=faulty):
                    with self.assertRaises(quality.ExportQualityError):
                        manager.merge_m4b(metadata={'title': 'Authored book'}, export_quality_callback=reports.append)
                self.assertEqual([], reports)
                self.assertEqual(b'prior complete M4B', output.read_bytes())
                self.assert_clean(tmp)

    def test_source_cover_mutation_and_late_cancel_preserve_prior_delivery(self):
        from PIL import Image
        for fault in ('source', 'cover', 'cancel'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                manager = self.fixture(tmp)
                output = Path(tmp) / 'audiobook.m4b'
                output.write_bytes(b'prior complete M4B')
                cover = Path(tmp) / 'cover.jpg'
                Image.new('RGB', (16, 16), 'red').save(cover)
                cancelled = False
                admit = quality.get_admitted_export_report
                def after_scan(*args, **kwargs):
                    nonlocal cancelled
                    report = admit(*args, **kwargs)
                    if fault == 'cancel':
                        cancelled = True
                    else:
                        path = cover if fault == 'cover' else Path(tmp) / 'source-0.wav'
                        info = path.stat()
                        raw = bytearray(path.read_bytes())
                        raw[-100] ^= 1
                        path.write_bytes(raw)
                        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                    return report
                with patch.object(quality, 'get_admitted_export_report', side_effect=after_scan):
                    if fault == 'cancel':
                        self.assertEqual((False, 'Export cancelled'), manager.merge_m4b(
                            metadata={'cover_path': str(cover)}, cancel_check=lambda: cancelled))
                    else:
                        with self.assertRaisesRegex(ValueError, 'source changed'):
                            manager.merge_m4b(metadata={'cover_path': str(cover)})
                self.assertEqual(b'prior complete M4B', output.read_bytes())
                self.assert_clean(tmp)

    def test_callback_exceptions_after_commit_do_not_erase_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.fixture(tmp)
            def broken(report):
                raise RuntimeError('observer failed after commit')
            with self.assertLogs('AlexandriaUI', level='WARNING'):
                result = manager.merge_m4b(mastering={}, mastering_callback=broken, export_quality_callback=broken)
            self.assertTrue(result[0], result[1])
            self.assertTrue((Path(tmp) / 'audiobook.m4b').is_file())
            self.assert_clean(tmp)
