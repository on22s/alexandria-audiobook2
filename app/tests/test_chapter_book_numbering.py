"""Chapter export numbers chapters by the whole book (#1921). Synthetic audio; run from app/.

What these verify, each against real files written by the real exporter:
- with only later chapters generated, they export under their book numbers (file
  names, manifest index/number, preview), not renumbered from 1;
- selecting a chapter that has no audio is refused by its book number in preview,
  export and the HTTP route, and nothing is written;
- a chapter with some lines still missing audio is exported and named in the
  result message, so the gap is not silent;
- the numbers match the Editor's chapter numbers (/api/chunks/repair_options).
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from project import CHAPTER_EXPORT_DIR, ProjectManager, build_chapter_filenames, get_chapter_selection


class ChapterBookNumberingTests(unittest.TestCase):
    def make_project(self, root, with_audio):
        """Three headed chapters of two lines each; `with_audio` lists line ids that have audio."""
        manager = ProjectManager(str(root))
        Path(manager.voicelines_dir).mkdir(exist_ok=True)
        rows = []
        for chapter in range(3):
            for part, (speaker, text) in enumerate((('NARRATOR', f'Chapter {chapter + 1}'),
                                                    ('Hero', f'Line {chapter + 1}.'))):
                index = chapter * 2 + part
                row = dict(id=index, uid=f'row{index}', speaker=speaker, text=text)
                if index in with_audio:
                    relative = f'voicelines/line{index}.wav'
                    samples = .2 * np.sin(2 * np.pi * (220 + 40 * index) * np.arange(4000) / 16000)
                    sf.write(root / relative, samples, 16000)
                    row.update(status='done', audio_path=relative)
                rows.append(row)
        manager.save_chunks(rows)
        return manager

    def manifest(self, root):
        return json.loads((root / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())

    def test_later_chapters_keep_their_book_numbers(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.make_project(root, with_audio={4, 5})
            preview = manager.preview_chapter_filenames(fmt='wav')
            self.assertEqual([(3, 'Chapter 3', '03 - Chapter 3.wav')],
                             [(row['number'], row['title'], row['file']) for row in preview])
            ok, message = manager.export_chapters(fmt='wav')
            self.assertTrue(ok, message)
            rows = self.manifest(root)['chapters']
            self.assertEqual([(2, 3, 'Chapter 3', '03 - Chapter 3.wav')],
                             [(row['index'], row['number'], row['title'], row['file']) for row in rows])
            self.assertTrue((root / CHAPTER_EXPORT_DIR / '03 - Chapter 3.wav').is_file())
            self.assertEqual(2, self.manifest(root)['chapter_plan_version'])

    def test_a_chapter_without_audio_is_refused_by_its_number_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.make_project(root, with_audio={4, 5})
            with self.assertRaisesRegex(ValueError, r'No generated audio for chapter\(s\) 1, 2\.'):
                manager.preview_chapter_filenames(fmt='wav', chapters=[0, 1, 2])
            ok, message = manager.export_chapters(fmt='wav', chapters=[1])
            self.assertFalse(ok, message)
            self.assertIn('No generated audio for chapter(s) 2.', message)
            self.assertFalse((root / CHAPTER_EXPORT_DIR / 'manifest.json').exists())
            ok, message = manager.export_chapters(fmt='wav', chapters=[2])
            self.assertTrue(ok, message)
            self.assertEqual([3], [row['number'] for row in self.manifest(root)['chapters']])

    def test_partly_generated_chapter_is_exported_and_named(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            # Chapter 1 complete, chapter 2 has its heading but not its line, chapter 3 none.
            manager = self.make_project(root, with_audio={0, 1, 2})
            ok, message = manager.export_chapters(fmt='wav')
            self.assertTrue(ok, message)
            self.assertIn('Chapter(s) 2 still have lines with no generated audio', message)
            self.assertEqual([1, 2], [row['number'] for row in self.manifest(root)['chapters']])
            ok, message = manager.export_chapters(fmt='wav', chapters=[0])
            self.assertTrue(ok, message)
            self.assertNotIn('no generated audio', message)

    def test_front_matter_counts_as_chapter_one_in_export_and_editor(self):
        """Lines before the first heading form "Introduction", chapter 1, in both
        the Editor's list and the export, so the heading "Chapter 1" is number 2."""
        from routers import editor
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.make_project(root, with_audio={0, 1, 2, 3, 4, 5})
            rows = manager.load_chunks()
            relative = 'voicelines/front.wav'
            sf.write(root / relative, .1 * np.ones(2000), 16000)
            front = dict(id=0, uid='front', speaker='Hero', text='Before it all began.',
                         status='done', audio_path=relative)
            manager.save_chunks([front] + [dict(row, id=row['id'] + 1) for row in rows])
            titles = [(c['index'] + 1, c['title']) for c in
                      editor.get_audio_repair_options(manager.load_chunks())['chapters']]
            exported = [(row['number'], row['title']) for row in manager.preview_chapter_filenames(fmt='wav')]
            self.assertEqual(titles, exported)
            self.assertEqual([(1, 'Introduction'), (2, 'Chapter 1')], exported[:2])

    def test_numbers_match_the_editor_chapter_list(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        from routers import editor
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.make_project(root, with_audio={4, 5})
            editor_chapters = editor.get_audio_repair_options(manager.load_chunks())['chapters']
            exported = manager.preview_chapter_filenames(fmt='wav')
            self.assertEqual([(c['index'] + 1, c['title']) for c in editor_chapters if c['index'] == 2],
                             [(row['number'], row['title']) for row in exported])
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'project_manager', manager), \
                    patch.object(editor, 'validate_export_readiness', return_value=None), \
                    patch.object(editor, 'schedule_claimed_background_task'):
                client = TestClient(app)
                refused = client.post('/api/export_chapters', json={'format': 'wav', 'chapters': [1]})
                self.assertEqual(422, refused.status_code, refused.text)
                self.assertIn('No generated audio for chapter(s) 2.', refused.text)
                accepted = client.post('/api/export_chapters', json={'format': 'wav', 'chapters': [2]})
                self.assertEqual(200, accepted.status_code, accepted.text)

    def test_selection_and_filename_helpers(self):
        self.assertEqual([2, 4], get_chapter_selection([4, 2, 4], {2, 4}))
        for bad in ([], [-1], [True], ['1']):
            with self.assertRaisesRegex(ValueError, 'Select at least one|zero-based'):
                get_chapter_selection(bad, {0})
        with self.assertRaisesRegex(ValueError, r'chapter\(s\) 1, 2, 3'):
            get_chapter_selection([0, 1, 2], set())
        groups = [('Alpha', 0, 0), ('Beta', 1, 1)]
        self.assertEqual(['01 - Alpha.wav', '02 - Beta.wav'],
                         build_chapter_filenames(groups, '{chapter_number} - {chapter_name}', 'wav'))
        self.assertEqual(['499 - Alpha.wav', '500 - Beta.wav'],
                         build_chapter_filenames(groups, '{chapter_number} - {chapter_name}', 'wav',
                                                 numbers=[499, 500]))


if __name__ == '__main__':
    unittest.main()
