"""Native file/HTTP checks for chapter export membership and containment."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor


class ChapterDownloadBoundaryTests(unittest.TestCase):
    def test_invalid_manifest_paths_never_become_zip_members_or_available_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / editor.CHAPTER_EXPORT_DIR
            out.mkdir()
            outside = root / 'secret.wav'
            outside.write_bytes(b'outside secret')
            (out / 'good.wav').write_bytes(b'valid chapter')
            (out / 'escape.wav').symlink_to(outside)
            for name in ('../secret.wav', str(outside), 'escape.wav', 'manifest.json', 'missing.wav'):
                with self.subTest(name=name):
                    (out / 'manifest.json').write_text(json.dumps({'chapters': [
                        {'index': 0, 'file': name}, {'index': 1, 'file': 'good.wav'}]}))
                    app = FastAPI(); app.include_router(editor.router)
                    with patch.object(editor, 'DATA_DIR', str(root)), TestClient(app) as client:
                        listing = client.get('/api/chapter_exports').json()
                        self.assertFalse(listing['chapters'][0]['exists'])
                        self.assertEqual(0, listing['chapters'][0]['bytes'])
                        response = client.get('/api/chapter_exports/zip')
                        self.assertEqual(200, response.status_code)
                        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                            self.assertEqual(['good.wav'], archive.namelist())
                            self.assertEqual(b'valid chapter', archive.read('good.wav'))
                    self.assertEqual(b'outside secret', outside.read_bytes())
                    self.assertFalse(list(root.glob('.alexandria-export-*.zip')))

    def test_download_requires_listed_audio_and_correct_mime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / editor.CHAPTER_EXPORT_DIR; out.mkdir()
            (root / 'outside.wav').write_bytes(b'secret')
            (out / 'escape.wav').symlink_to(root / 'outside.wav')
            for name in ('chapter.WAV', 'chapter.MP3', 'stray.wav', 'notes.txt'):
                (out / name).write_bytes(name.encode())
            (out / 'manifest.json').write_text(json.dumps({'chapters': [
                {'index': i, 'file': n} for i, n in enumerate(
                    ('chapter.WAV', 'chapter.MP3', 'escape.wav', 'notes.txt'))]}))
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'DATA_DIR', str(root)), TestClient(app) as client:
                for name in ('manifest.json', 'stray.wav', 'escape.wav', 'notes.txt'):
                    with self.subTest(name=name):
                        self.assertEqual(404, client.get('/api/chapter_exports/file/' + name).status_code)
                for name, mime in (('chapter.WAV', 'audio/wav'), ('chapter.MP3', 'audio/mpeg')):
                    response = client.get('/api/chapter_exports/file/' + name)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual(mime, response.headers['content-type'])
                    self.assertEqual(name.encode(), response.content)
