"""Real encoded-delivery bytes must be verified before chapter reuse."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pydub import AudioSegment
import soundfile as sf

import audio_mastering
from project import CHAPTER_EXPORT_DIR, ProjectManager


def tone(seconds=1, rate=24000, hz=220):
    return (.2 * np.sin(2 * np.pi * hz * np.arange(round(seconds * rate)) / rate)).astype('float32')


class RetainedChapterIntegrityTests(unittest.TestCase):
    def manager(self, root, fmt='wav', mastering=None, publish=True):
        manager = ProjectManager(str(root))
        rows = []
        for index in range(2):
            relative = f'voicelines/{index}.wav'
            sf.write(root / relative, tone(seconds=4.5 if mastering is not None else 1,
                                           hz=220 + index * 110), 24000)
            rows.append(dict(id=index, uid=f'chapter-{index}', speaker='NARRATOR',
                             text=f'Chapter {index + 1}', instruct='', status='done', audio_path=relative))
        manager.save_chunks(rows)
        out = root / CHAPTER_EXPORT_DIR
        if not publish:
            return manager, out, None
        result = manager.export_chapters(fmt=fmt, mastering=mastering)
        self.assertTrue(result[0], result)
        return manager, out, json.loads((out / 'manifest.json').read_text())

    def snapshot(self, out):
        return {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}

    def assert_hashes(self, out):
        for row in json.loads((out / 'manifest.json').read_text())['chapters']:
            data = (out / row['file']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row['output_sha256'])
            self.assertEqual(len(data), row['output_bytes'])

    def test_damaged_selected_wav_and_mp3_rebuild_even_with_restored_size_and_mtime(self):
        for fmt in ('wav', 'mp3'):
            for damage in ('empty', 'truncated', 'different'):
                with self.subTest(fmt=fmt, damage=damage), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    manager, out, manifest = self.manager(root, fmt)
                    chapter = out / manifest['chapters'][0]['file']
                    other = out / manifest['chapters'][1]['file']
                    original, other_bytes = chapter.read_bytes(), other.read_bytes()
                    stamp = chapter.stat()
                    if damage == 'empty':
                        chapter.write_bytes(b'')
                    elif damage == 'truncated':
                        chapter.write_bytes(original[:100])
                    else:
                        replacement = root / 'replacement.wav'
                        sf.write(replacement, tone(hz=880), 24000)
                        with replacement.open('rb') as stream:
                            replacement_audio = AudioSegment.from_wav(stream)
                        replacement_audio.export(
                            chapter, format=fmt, **({'bitrate': '128k'} if fmt == 'mp3' else {})).close()
                        self.assertEqual(len(original), chapter.stat().st_size)
                        self.assertNotEqual(original, chapter.read_bytes())
                        os.utime(chapter, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    result = manager.export_chapters(fmt=fmt, chapters=[0], changed_only=True)
                    self.assertTrue(result[0], result)
                    self.assertIn('1 chapter file(s) written', result[1])
                    self.assertEqual(original, chapter.read_bytes())
                    self.assertEqual(other_bytes, other.read_bytes())
                    self.assert_hashes(out)
                    self.assertIn('0 chapter file(s) written', manager.export_chapters(
                        fmt=fmt, changed_only=True)[1])

    def test_unselected_missing_or_damaged_delivery_refuses_after_staging_without_publication(self):
        for changed_only in (False, True):
            for damage in ('empty', 'missing'):
                with self.subTest(changed_only=changed_only, damage=damage), tempfile.TemporaryDirectory() as tmp:
                    manager, out, manifest = self.manager(Path(tmp))
                    chapter = out / manifest['chapters'][1]['file']
                    chapter.unlink() if damage == 'missing' else chapter.write_bytes(b'')
                    before = self.snapshot(out)
                    result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=changed_only)
                    self.assertFalse(result[0], result)
                    self.assertIn('select', result[1].lower())
                    self.assertEqual(before, self.snapshot(out))

    def test_legacy_selected_rebuild_unselected_refusal_and_export_all_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, out, manifest = self.manager(Path(tmp))
            for row in manifest['chapters']:
                row.pop('output_sha256', None)
                row.pop('output_bytes', None)
            (out / 'manifest.json').write_text(json.dumps(manifest))
            before = self.snapshot(out)
            result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=True)
            self.assertFalse(result[0], result)
            self.assertIn('select', result[1].lower())
            self.assertEqual(before, self.snapshot(out))
            result = manager.export_chapters(fmt='wav', changed_only=True)
            self.assertTrue(result[0], result)
            self.assertIn('2 chapter file(s) written', result[1])
            self.assert_hashes(out)
            self.assertIn('0 chapter file(s) written', manager.export_chapters(fmt='wav', changed_only=True)[1])

    def test_only_selected_legacy_row_rebuilds_while_verified_other_row_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, out, manifest = self.manager(Path(tmp))
            manifest['chapters'][0].pop('output_sha256', None)
            manifest['chapters'][0].pop('output_bytes', None)
            (out / 'manifest.json').write_text(json.dumps(manifest))
            prior = (out / manifest['chapters'][1]['file']).read_bytes()
            result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=True)
            self.assertTrue(result[0], result)
            self.assertIn('1 chapter file(s) written', result[1])
            self.assertEqual(prior, (out / manifest['chapters'][1]['file']).read_bytes())
            self.assert_hashes(out)

    def test_mastered_legacy_hash_migrates_with_one_scan_per_retained_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, out, manifest = self.manager(Path(tmp), mastering={})
            original = self.snapshot(out)
            for row in manifest['chapters']:
                row.pop('output_sha256', None)
                row.pop('output_bytes', None)
            (out / 'manifest.json').write_text(json.dumps(manifest))
            with patch('audio_mastering.get_audio_sha256', wraps=audio_mastering.get_audio_sha256) as digest:
                result = manager.export_chapters(fmt='wav', changed_only=True, mastering={})
            self.assertTrue(result[0], result)
            self.assertIn('0 chapter file(s) written', result[1])
            self.assertEqual(2, digest.call_count)
            for name, data in original.items():
                if name != 'manifest.json':
                    self.assertEqual(data, (out / name).read_bytes())
            self.assert_hashes(out)

    def test_conflicting_or_malformed_digest_is_not_silently_blessed(self):
        for bad in ('malformed', 'conflicting'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                manager, out, manifest = self.manager(Path(tmp))
                row = manifest['chapters'][1]
                if bad == 'malformed':
                    row['output_sha256'] = 'not-a-digest'
                else:
                    row['mastering_report'] = {'sha256': '0' * 64}
                (out / 'manifest.json').write_text(json.dumps(manifest))
                before = self.snapshot(out)
                result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=True)
                self.assertFalse(result[0], result)
                self.assertEqual(before, self.snapshot(out))

    def test_cancel_during_retained_hash_preserves_delivery_and_uses_shared_streaming_hasher(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, out, _ = self.manager(Path(tmp))
            before = self.snapshot(out)
            original = audio_mastering.get_audio_sha256
            def cancelled(path, cancel_check=None):
                return original(path, lambda: True)
            with patch('audio_mastering.get_audio_sha256', side_effect=cancelled):
                self.assertEqual((False, 'Export cancelled'),
                                 manager.export_chapters(fmt='wav', changed_only=True))
            self.assertEqual(before, self.snapshot(out))

    def test_changed_only_preview_is_metadata_only_and_export_still_verifies_bytes(self):
        for mastering in (None, {}):
            with self.subTest(mastering=mastering), tempfile.TemporaryDirectory() as tmp:
                manager, out, manifest = self.manager(Path(tmp), mastering=mastering)
                chapter = out / manifest['chapters'][0]['file']
                data = bytearray(chapter.read_bytes())
                data[-20] ^= 1
                chapter.write_bytes(data)
                with patch('audio_mastering.get_audio_sha256', side_effect=AssertionError('preview hashed delivery')), \
                     patch.object(manager, 'get_chunk_audio_segment', side_effect=AssertionError('preview decoded PCM')):
                    self.assertEqual([], manager.preview_chapter_filenames(
                        fmt='wav', changed_only=True, mastering=mastering))
                result = manager.export_chapters(fmt='wav', changed_only=True, mastering=mastering)
                self.assertTrue(result[0], result)
                self.assertIn('1 chapter file(s) written', result[1])
                self.assert_hashes(out)

    def test_first_partial_export_can_add_other_chapters_without_inventing_prior_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, out, _ = self.manager(Path(tmp), publish=False)
            self.assertTrue(manager.export_chapters(fmt='wav', chapters=[0])[0])
            self.assertEqual(1, len(json.loads((out / 'manifest.json').read_text())['chapters']))
            self.assertTrue(manager.export_chapters(fmt='wav', chapters=[1])[0])
            self.assertEqual(2, len(json.loads((out / 'manifest.json').read_text())['chapters']))
            self.assert_hashes(out)

    def test_unselected_unsafe_prior_filename_is_never_hashed_or_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, out, manifest = self.manager(root)
            outside = root / 'private.wav'
            outside.write_bytes(b'outside-delivery')
            manifest['chapters'][1]['file'] = '../private.wav'
            (out / 'manifest.json').write_text(json.dumps(manifest))
            before = self.snapshot(out)
            original = audio_mastering.get_audio_sha256
            def confined(path, cancel_check=None):
                self.assertNotEqual(outside.resolve(), Path(path).resolve())
                return original(path, cancel_check)
            with patch('audio_mastering.get_audio_sha256', side_effect=confined):
                result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=True)
            self.assertFalse(result[0], result)
            self.assertEqual(before, self.snapshot(out))
            self.assertEqual(b'outside-delivery', outside.read_bytes())


    def test_unselected_same_size_mastered_damage_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, out, manifest = self.manager(Path(tmp), mastering={})
            chapter = out / manifest['chapters'][1]['file']
            stamp = chapter.stat()
            data = bytearray(chapter.read_bytes())
            data[-20] ^= 1
            chapter.write_bytes(data)
            os.utime(chapter, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            before = self.snapshot(out)
            result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=True, mastering={})
            self.assertFalse(result[0], result)
            self.assertEqual(before, self.snapshot(out))

    def test_incremental_and_full_fallback_verify_the_same_retained_outputs(self):
        for changed_only in (False, True):
            with self.subTest(changed_only=changed_only), tempfile.TemporaryDirectory() as tmp:
                manager, out, manifest = self.manager(Path(tmp))
                chapter = out / manifest['chapters'][0]['file']
                original = chapter.read_bytes()
                other = out / manifest['chapters'][1]['file']
                other_bytes = other.read_bytes()
                chapter.write_bytes(b'')
                with patch('chapter_timing_cache.ensure_chapter_timing_facts', return_value=None):
                    result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=changed_only)
                self.assertTrue(result[0], result)
                self.assertIn('1 chapter file(s) written', result[1])
                self.assertEqual(original, chapter.read_bytes())
                self.assertEqual(other_bytes, other.read_bytes())
                self.assert_hashes(out)

    def test_malformed_prior_manifest_refuses_partial_export_and_full_export_repairs_it(self):
        for damage in ('filename', 'duplicate', 'json'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmp:
                manager, out, manifest = self.manager(Path(tmp))
                if damage == 'filename':
                    manifest['chapters'][1]['file'] = None
                elif damage == 'duplicate':
                    manifest['chapters'][1]['index'] = 0
                receipt = out / 'manifest.json'
                receipt.write_text('invalid json' if damage == 'json' else json.dumps(manifest))
                before = self.snapshot(out)
                result = manager.export_chapters(fmt='wav', chapters=[0], changed_only=True)
                self.assertFalse(result[0], result)
                self.assertIn('manifest', result[1].lower())
                self.assertEqual(before, self.snapshot(out))
                result = manager.export_chapters(fmt='wav')
                self.assertTrue(result[0], result)
                self.assertEqual(2, len(json.loads(receipt.read_text())['chapters']))
                self.assert_hashes(out)

    def test_verified_legacy_endpoints_supply_missing_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, out, manifest = self.manager(Path(tmp))
            prior = (out / manifest['chapters'][1]['file']).read_bytes()
            duration = manifest['chapters'][1].pop('duration_ms')
            (out / 'manifest.json').write_text(json.dumps(manifest))
            result = manager.export_chapters(fmt='wav', chapters=[0])
            self.assertTrue(result[0], result)
            current = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(duration, current['chapters'][1]['duration_ms'])
            self.assertEqual(prior, (out / manifest['chapters'][1]['file']).read_bytes())
            self.assert_hashes(out)

    def test_hash_read_failures_preserve_prior_delivery_and_clean_private_staging(self):
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as tmp:
                manager, out, manifest = self.manager(Path(tmp))
                retained = out / manifest['chapters'][1]['file']
                before = self.snapshot(out)
                original = audio_mastering.get_audio_sha256
                def unreadable(path, cancel_check=None):
                    target = Path(path)
                    if (staged and target.parent != out) or (not staged and target == retained):
                        raise OSError('synthetic hash read failure')
                    return original(path, cancel_check)
                with patch('audio_mastering.get_audio_sha256', side_effect=unreadable):
                    if staged:
                        with self.assertRaisesRegex(OSError, 'synthetic hash read failure'):
                            manager.export_chapters(fmt='wav', chapters=[0])
                    else:
                        result = manager.export_chapters(fmt='wav', chapters=[0])
                        self.assertFalse(result[0], result)
                self.assertEqual(before, self.snapshot(out))
                self.assertFalse(any(path.is_dir() and path.name.startswith('.chapter-export-')
                                     for path in out.iterdir()))

if __name__ == '__main__':
    unittest.main()
