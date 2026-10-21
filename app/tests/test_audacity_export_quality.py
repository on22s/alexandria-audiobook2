"""Real Audacity ZIP bytes must match independent per-track frame plans."""
from contextlib import ExitStack
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pydub import AudioSegment

import audacity_export_quality as quality
import export_quality
import project


class AudacityExportQualityTests(unittest.TestCase):
    def fixture(self, root, rates=(24000, 8000, 44100), widths=(2, 1, 4), channels=(1, 1, 2)):
        manager = project.ProjectManager(root)
        rows = []
        for index, (rate, width, channel_count) in enumerate(zip(rates, widths, channels)):
            path = Path(root) / f'source-{index}.wav'
            sample = (10 if width == 1 else 2000).to_bytes(width, 'little', signed=True)
            AudioSegment(sample * channel_count * (1000 + index * 13), sample_width=width,
                frame_rate=rate, channels=channel_count).export(path, format='wav').close()
            rows.append(dict(id=index, uid=f'voice-{index}', status='done',
                speaker='Alice' if index % 2 == 0 else 'Bob',
                text=f'Synthetic sentence {index}.', audio_path=path.name, pause_after=41))
        manager.save_chunks(rows)
        manager._load_pause_defaults = lambda: (41, 11)
        return manager

    def assert_clean(self, root):
        self.assertFalse(list(Path(root).glob('*.pending.*')))
        self.assertFalse(list(Path(root).glob('.source-pcm-*')))
        self.assertFalse(list(Path(root).glob('.pcm-*')))

    def test_native_mixed_and_legacy_tracks_match_independent_headers_without_extra_pcm_decode(self):
        for mode in ('native', 'forced-legacy', 'custom-source'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root, ExitStack() as stack:
                manager = self.fixture(root)
                rows = copy.deepcopy(manager.load_chunks())
                originals = {row['audio_path']: (Path(root) / row['audio_path']).read_bytes() for row in rows}
                if mode == 'forced-legacy':
                    stack.enter_context(patch.object(project, 'get_track_wav_plan', return_value=None))
                elif mode == 'custom-source':
                    load = manager.get_chunk_audio_segment
                    class Custom(AudioSegment):
                        pass
                    def custom(row):
                        audio = load(row)
                        return Custom(audio.raw_data, sample_width=audio.sample_width,
                                      frame_rate=audio.frame_rate, channels=audio.channels)
                    stack.enter_context(patch.object(manager, 'get_chunk_audio_segment', side_effect=custom))
                else:
                    stack.enter_context(patch.object(project, 'AudioSegmentBuilder',
                        side_effect=AssertionError('materialized native track')))
                    stack.enter_context(patch.object(AudioSegment, 'export',
                        side_effect=AssertionError('materialized native WAV')))
                reports = []
                with patch.object(export_quality, 'validate_finished_export',
                        side_effect=AssertionError('archive tracks require no extra FFmpeg decode')):
                    result = manager.export_audacity(export_quality_callback=reports.append)
                self.assertTrue(result[0], result)
                self.assertEqual(1, len(reports))
                report = reports[0]
                self.assertEqual('audacity_structure_and_frames', report['verification_scope'])
                self.assertEqual(('verified', 2, 4),
                    (report['status'], report['tracks_checked'], report['members_checked']))
                archive_path = Path(root) / 'audacity_export.zip'
                self.assertEqual(export_quality.get_export_file_identity(archive_path)['sha256'], report['sha256'])
                self.assertEqual(export_quality.get_export_file_version(archive_path), report['file_version'])
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertEqual({'alice.wav', 'bob.wav', 'project.lof', 'labels.txt'}, set(archive.namelist()))
                    for track in report['track_summaries']:
                        raw = archive.read(track['file'])
                        self.assertEqual(44 + track['frames'] * track['channels'] * track['sample_width'], len(raw))
                        self.assertEqual(track['sample_rate'], struct.unpack_from('<L', raw, 24)[0])
                    self.assertEqual(3, len(archive.read('labels.txt').splitlines()))
                self.assertEqual(rows, manager.load_chunks())
                for name, content in originals.items():
                    self.assertEqual(content, (Path(root) / name).read_bytes())
                self.assert_clean(root)

    def test_successful_writer_with_corrupt_or_wrong_members_never_replaces_prior_archive(self):
        faults = ('empty', 'truncated', 'missing', 'duplicate', 'unexpected', 'symlink',
                  'short-pcm', 'long-pcm', 'wrong-rate', 'float-header', 'lof', 'labels', 'crc')
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                manager = self.fixture(root)
                output = Path(root) / 'audacity_export.zip'
                output.write_bytes(b'previous complete archive')
                validate = quality.validate_audacity_export
                def damage(path, *args, **kwargs):
                    path = Path(path)
                    if fault == 'empty':
                        path.write_bytes(b'')
                    elif fault == 'truncated':
                        path.write_bytes(path.read_bytes()[:-30])
                    else:
                        with zipfile.ZipFile(path) as archive:
                            content = {name: archive.read(name) for name in archive.namelist()}
                        first = next(name for name in content if name.endswith('.wav'))
                        if fault == 'missing':
                            content.pop(first)
                        elif fault == 'unexpected':
                            content['unplanned.wav'] = content[first]
                        elif fault in ('short-pcm', 'long-pcm'):
                            content[first] = content[first][:-2] if fault == 'short-pcm' else content[first] + b'xx'
                        elif fault == 'wrong-rate':
                            raw = bytearray(content[first]); struct.pack_into('<L', raw, 24, 12345)
                            content[first] = bytes(raw)
                        elif fault == 'float-header':
                            raw = bytearray(content[first]); struct.pack_into('<H', raw, 20, 3)
                            content[first] = bytes(raw)
                        elif fault in ('lof', 'labels'):
                            name = 'project.lof' if fault == 'lof' else 'labels.txt'
                            content[name] = bytes(byte ^ 1 for byte in content[name])
                        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
                            for name, payload in content.items():
                                if fault == 'symlink' and name == first:
                                    info = zipfile.ZipInfo(name)
                                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                                    archive.writestr(info, payload)
                                else:
                                    archive.writestr(name, payload)
                            if fault == 'duplicate':
                                with self.assertWarns(UserWarning):
                                    archive.writestr(first, content[first])
                        if fault == 'crc':
                            with zipfile.ZipFile(path) as archive:
                                info = archive.getinfo(first)
                                offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
                            raw = bytearray(path.read_bytes()); raw[offset + 50] ^= 1
                            path.write_bytes(raw)
                    return validate(path, *args, **kwargs)
                reports = []
                with patch.object(quality, 'validate_audacity_export', side_effect=damage):
                    with self.assertRaises(export_quality.ExportQualityError) as error:
                        manager.export_audacity(export_quality_callback=reports.append)
                self.assertEqual('failed', error.exception.report['status'])
                self.assertEqual([], reports)
                self.assertEqual(b'previous complete archive', output.read_bytes())
                self.assert_clean(root)

    def test_source_and_editor_changes_after_archive_validation_refuse_publication(self):
        for change in ('source-bytes', 'uid', 'book', 'pause'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                manager = self.fixture(root)
                output = Path(root) / 'audacity_export.zip'
                output.write_bytes(b'prior archive')
                validate = quality.validate_audacity_export
                def edit(path, *args, **kwargs):
                    report = validate(path, *args, **kwargs)
                    if change == 'source-bytes':
                        source = Path(root) / manager.load_chunks()[0]['audio_path']
                        stamp = source.stat(); raw = bytearray(source.read_bytes()); raw[-1] ^= 1
                        source.write_bytes(raw)
                        import os
                        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    elif change == 'uid':
                        rows = manager.load_chunks(); rows[0]['uid'] = 'changed-editor-uid'; manager.save_chunks(rows)
                    elif change == 'book':
                        (Path(root) / 'state.json').write_text(json.dumps({'active_book_id': 'other', 'book_generation': 2}))
                    else:
                        Path(manager.config_path).write_text('{}')
                    return report
                with patch.object(quality, 'validate_audacity_export', side_effect=edit):
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        manager.export_audacity()
                self.assertEqual(b'prior archive', output.read_bytes())
                self.assert_clean(root)

    def simple_archive(self, root, *, extra_members=0):
        path = Path(root) / 'private.zip'
        shapes = {'a.wav': ((1, 24000, 2), 2000)}
        texts = {'project.lof': b'file "a.wav"\n', 'labels.txt': b'0\t0.083333\tSynthetic\n'}
        with io.BytesIO() as output:
            AudioSegment(b'\x01\x00' * 2000, sample_width=2, frame_rate=24000,
                         channels=1).export(output, format='wav')
            raw = output.getvalue()
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
            archive.writestr('a.wav', raw)
            for name, content in texts.items():
                archive.writestr(name, content)
            for index in range(extra_members):
                archive.writestr(f'extra-{index}', b'')
        return path, shapes, texts

    def test_actual_directory_records_are_bounded_before_zipinfo_allocation(self):
        with tempfile.TemporaryDirectory() as root:
            path, shapes, texts = self.simple_archive(root, extra_members=100)
            raw = bytearray(path.read_bytes())
            offset = raw.rfind(b'PK\x05\x06')
            struct.pack_into('<HH', raw, offset + 8, 3, 3)
            path.write_bytes(raw)
            with patch.object(quality.zipfile, 'ZipFile', side_effect=AssertionError('unbounded ZipInfo parse')):
                with self.assertRaisesRegex(export_quality.ExportQualityError, 'central-directory|unexpected'):
                    quality.validate_audacity_export(path, shapes, texts)

    def test_checked_directory_is_pinned_across_callbacks_and_actual_bytes_still_fenced(self):
        with tempfile.TemporaryDirectory() as root:
            path, shapes, texts = self.simple_archive(root)
            checked = False
            check = quality._check_directory
            def admitted(*args):
                nonlocal checked
                check(*args)
                checked = True
            changed = False
            def rewrite_after_check():
                nonlocal changed
                if checked and not changed:
                    raw = bytearray(path.read_bytes())
                    offset = raw.rfind(b'PK\x05\x06')
                    struct.pack_into('<L', raw, offset + 12, 10 * quality.MAX_PROBE_BYTES)
                    path.write_bytes(raw)
                    changed = True
                return False
            with patch.object(quality, '_check_directory', side_effect=admitted):
                with self.assertRaisesRegex(export_quality.ExportQualityError, 'changed'):
                    quality.validate_audacity_export(path, shapes, texts, rewrite_after_check)
            self.assertTrue(changed)

    def test_zip64_and_cancellable_member_reads_preserve_bounded_behavior(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(zipfile, 'ZIP64_LIMIT', 64):
                path, shapes, texts = self.simple_archive(root)
            self.assertEqual('verified', quality.validate_audacity_export(path, shapes, texts)['status'])
            read = zipfile.ZipExtFile.read
            cancelled, read_sizes = False, []
            def cancel_after_wav_bytes(member, size=-1):
                nonlocal cancelled
                self.assertGreaterEqual(size, 0)
                self.assertLessEqual(size, quality.READ_BYTES)
                read_sizes.append(size)
                result = read(member, size)
                if member.name.endswith('.wav') and size > 44:
                    cancelled = True
                return result
            with patch.object(zipfile.ZipExtFile, 'read', cancel_after_wav_bytes):
                with self.assertRaises(project.ExportCancelled):
                    quality.validate_audacity_export(path, shapes, texts, lambda: cancelled)
            self.assertTrue(cancelled)
            self.assertTrue(read_sizes)

    def test_zip64_old_and_new_python_end_locations_with_and_without_prefix(self):
        for zip64 in (False, True):
            for prefix in (b'', b'synthetic archive prefix'):
                for legacy_location in (False, True):
                    with self.subTest(zip64=zip64, prefix=bool(prefix), legacy=legacy_location):
                        with tempfile.TemporaryDirectory() as root:
                            with patch.object(zipfile, 'ZIP64_LIMIT', 64 if zip64 else 2 ** 31 - 1):
                                path, shapes, texts = self.simple_archive(root)
                            raw = path.read_bytes()
                            path.write_bytes(prefix + raw)
                            read_end = zipfile._EndRecData
                            calls = 0
                            def old_location(source):
                                nonlocal calls
                                end = read_end(source)
                                calls += 1
                                # Only the checker's first parse emulates the old
                                # API. The host ZipFile keeps its own convention.
                                if legacy_location and calls == 1:
                                    end[zipfile._ECD_LOCATION] = len(prefix) + raw.rfind(b'PK\x05\x06')
                                return end
                            with patch.object(zipfile, '_EndRecData', side_effect=old_location):
                                report = quality.validate_audacity_export(path, shapes, texts)
                            self.assertEqual('verified', report['status'])
                            self.assertEqual(3, report['members_checked'])

    def test_late_cancel_and_postcommit_observer_keep_publication_result_correct(self):
        with tempfile.TemporaryDirectory() as root:
            manager = self.fixture(root)
            output = Path(root) / 'audacity_export.zip'
            output.write_bytes(b'prior archive')
            cancelled = False
            validate = quality.validate_audacity_export
            def cancel(path, *args, **kwargs):
                nonlocal cancelled
                result = validate(path, *args, **kwargs)
                cancelled = True
                return result
            with patch.object(quality, 'validate_audacity_export', side_effect=cancel):
                self.assertEqual((False, 'Export cancelled'), manager.export_audacity(cancel_check=lambda: cancelled))
            self.assertEqual(b'prior archive', output.read_bytes())
            self.assert_clean(root)
            def broken(report):
                raise RuntimeError('observer failure')
            with self.assertLogs('AlexandriaUI', 'WARNING'):
                result = manager.export_audacity(export_quality_callback=broken)
            self.assertTrue(result[0], result)
            self.assertTrue(zipfile.is_zipfile(output))


if __name__ == '__main__':
    unittest.main()
