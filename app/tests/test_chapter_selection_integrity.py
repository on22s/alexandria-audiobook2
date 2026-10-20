"""Synthetic real-media chapter identity checks; run from app/."""
import contextlib
import io
from itertools import product
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from project import ProjectManager, CHAPTER_EXPORT_DIR
from chapter_timing_cache import CACHE_FILENAME


class ChapterSelectionIntegrityTests(unittest.TestCase):
    def create_project(self, root):
        manager = ProjectManager(str(root))
        rows = []
        for index in range(3):
            relative = f'voicelines/chapter-{index}.flac'
            samples = .2 * np.sin(2 * np.pi * (220 + index * 220) * np.arange(8000) / 16000)
            sf.write(root / relative, samples, 16000)
            rows.append(dict(id=index, uid=f'chapter-{index}', speaker='NARRATOR',
                             text=f'Chapter {index + 1}', status='done', audio_path=relative))
        manager.save_chunks(rows)
        return manager, rows

    def get_outputs(self, root):
        output = root / CHAPTER_EXPORT_DIR
        return {str(path.relative_to(output)): path.read_bytes()
                for path in output.rglob('*') if path.is_file()} if output.exists() else {}

    def test_decoder_rejected_selection_preserves_prior_delivery_cold_and_warm(self):
        for fmt, per_chunk, changed, fallback in product(('wav', 'mp3'), (False, True),
                                                         (False, True), (False, True)):
            with self.subTest(fmt=fmt, per_chunk=per_chunk, changed=changed, fallback=fallback), \
                    tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                root = Path(tmp)
                manager, rows = self.create_project(root)
                self.assertTrue(manager.export_chapters(fmt=fmt, per_chunk_chapters=per_chunk)[0])
                before = self.get_outputs(root)
                bad = root / rows[1]['audio_path']
                bad.write_bytes(bad.read_bytes()[:42])
                self.assertTrue(manager.is_chunk_audio_available(rows[1]))
                self.assertIsNone(manager.get_chunk_audio_segment(rows[1]))
                (root / CACHE_FILENAME).unlink(missing_ok=True)
                preview = manager.preview_chapter_filenames(fmt=fmt, per_chunk_chapters=per_chunk,
                                                           chapters=[1], changed_only=changed)
                if preview:
                    self.assertIn('Chapter 2', preview[0]['title'])
                scope = (patch.object(manager, '_get_chapter_decoder_codes', return_value=(None,))
                         if fallback else contextlib.nullcontext())
                with scope:
                    for attempt in range(2):
                        result = manager.export_chapters(fmt=fmt, per_chunk_chapters=per_chunk,
                                                         chapters=[1], changed_only=changed)
                        self.assertFalse(result[0], result)
                        self.assertIn('decode', result[1].lower())
                        self.assertEqual(before, self.get_outputs(root))
                        self.assertEqual([], list(root.glob('.source-pcm-*')))
                        self.assertEqual([], list((root / CHAPTER_EXPORT_DIR).glob('.chapter-export-*')))

    def test_first_selected_export_never_substitutes_the_following_chapter(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager, rows = self.create_project(root)
            bad = root / rows[1]['audio_path']
            bad.write_bytes(bad.read_bytes()[:42])
            self.assertEqual('Chapter 2', manager.preview_chapter_filenames(fmt='wav', chapters=[1])[0]['title'])
            result = manager.export_chapters(fmt='wav', chapters=[1])
            self.assertFalse(result[0], result)
            self.assertEqual({}, self.get_outputs(root))

    def test_fallback_keeps_row_multiplicity_without_uids_when_one_shared_clip_decode_fails(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager, rows = self.create_project(root)
            shared = rows[0]['audio_path']
            for row in rows:
                row.pop('uid')
                row['audio_path'] = shared
            reader = manager.get_chunk_audio_segment
            def decode(row):
                return None if row['text'] == 'Chapter 2' else reader(row)
            with patch.object(manager, 'load_chunks', return_value=rows), \
                    patch.object(manager, '_get_chapter_decoder_codes', return_value=(None,)), \
                    patch.object(manager, 'get_chunk_audio_segment', side_effect=decode):
                preview = manager.preview_chapter_filenames(fmt='wav', chapters=[1])
                self.assertEqual('Chapter 2', preview[0]['title'])
                result = manager.export_chapters(fmt='wav', chapters=[1])
            self.assertFalse(result[0], result)
            self.assertEqual({}, self.get_outputs(root))

    def test_matching_metadata_and_decoder_omissions_keep_previewed_selection(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as tmp, \
                    contextlib.redirect_stdout(io.StringIO()):
                root = Path(tmp)
                manager, rows = self.create_project(root)
                (root / rows[1]['audio_path']).write_bytes(b'not media')
                self.assertFalse(manager.is_chunk_audio_available(rows[1]))
                self.assertEqual('Chapter 3', manager.preview_chapter_filenames(fmt='wav', chapters=[1])[0]['title'])
                scope = (patch.object(manager, '_get_chapter_decoder_codes', return_value=(None,))
                         if fallback else contextlib.nullcontext())
                with scope:
                    result = manager.export_chapters(fmt='wav', chapters=[1])
                self.assertTrue(result[0], result)
                manifest = json.loads((root / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())
                self.assertEqual(['Chapter 3'], [row['title'] for row in manifest['chapters']])

    def test_changed_only_shortcut_cannot_rebind_selection_when_metadata_excludes_a_row(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager, rows = self.create_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav')[0])
            bad = root / rows[1]['audio_path']
            stat = bad.stat()
            bad.write_bytes(b'x' * stat.st_size)
            os.utime(bad, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertFalse(manager.is_chunk_audio_available(rows[1]))
            preview = manager.preview_chapter_filenames(fmt='wav', chapters=[1], changed_only=True)
            self.assertEqual('Chapter 3', preview[0]['title'])
            with patch.object(manager, '_get_chapter_decoder_codes', return_value=(None,)):
                result = manager.export_chapters(fmt='wav', chapters=[1], changed_only=True)
            self.assertTrue(result[0], result)
            manifest = json.loads((root / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())
            selected = next(row for row in manifest['chapters'] if row['index'] == 1)
            self.assertEqual('Chapter 3', selected['title'])
            self.assertEqual(preview[0]['file'], selected['file'])

    def test_all_chapter_export_keeps_its_partial_export_warning(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager, rows = self.create_project(root)
            bad = root / rows[1]['audio_path']
            bad.write_bytes(bad.read_bytes()[:42])
            result = manager.export_chapters(fmt='wav')
            self.assertTrue(result[0], result)
            self.assertIn('1 chunk(s) skipped', result[1])
            manifest = json.loads((root / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())
            self.assertEqual(['Chapter 1', 'Chapter 3'], [row['title'] for row in manifest['chapters']])

    def test_fallback_uses_one_source_snapshot_if_live_rows_change(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager, rows = self.create_project(root)
            reordered = [rows[0], rows[2], rows[1]]
            with patch.object(manager, 'load_chunks', side_effect=[rows, reordered]) as load, \
                    patch.object(manager, '_get_chapter_decoder_codes', return_value=(None,)):
                result = manager.export_chapters(fmt='wav', chapters=[1])
            self.assertTrue(result[0], result)
            self.assertEqual(1, load.call_count)
            manifest = json.loads((root / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())
            self.assertEqual(['Chapter 2'], [row['title'] for row in manifest['chapters']])


if __name__ == '__main__':
    unittest.main()
