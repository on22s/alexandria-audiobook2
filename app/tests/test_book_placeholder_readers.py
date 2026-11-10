"""#1660 v4 prerequisite: readers that run without the book lock treat a
switch's 64-byte placeholder as "not there yet", never as content."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_placeholder
from book_placeholder import get_book_placeholder_bytes, is_book_placeholder, is_book_placeholder_bytes

PLACEHOLDER = get_book_placeholder_bytes('0123456789abcdef' * 2)


class PlaceholderPredicateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_only_the_exact_placeholder_matches(self):
        self.assertEqual(64, len(PLACEHOLDER))
        path = self.root / 'audiobook.mp3'
        path.write_bytes(PLACEHOLDER)
        self.assertTrue(is_book_placeholder(path))
        near_misses = (PLACEHOLDER[:-1], PLACEHOLDER + b'x', PLACEHOLDER.replace(b'v1', b'v2'),
                       PLACEHOLDER.upper(), PLACEHOLDER[:-1] + b' ', b'\0' * 64)
        for raw in near_misses:
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.assertFalse(is_book_placeholder(path))
                self.assertFalse(is_book_placeholder_bytes(raw))
        self.assertFalse(is_book_placeholder(self.root / 'missing'))
        self.assertFalse(is_book_placeholder(self.root))   # a directory

    @unittest.skipIf(os.name == 'nt', 'symlinks need privileges on Windows')
    def test_a_symlink_to_a_placeholder_is_not_one(self):
        real = self.root / 'real'
        real.write_bytes(PLACEHOLDER)
        (self.root / 'link').symlink_to(real)
        self.assertFalse(is_book_placeholder(self.root / 'link'))

    def test_the_nonce_must_be_32_lowercase_hex(self):
        for nonce in ('0' * 31, '0' * 33, 'A' * 32, 'g' * 32, None):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                get_book_placeholder_bytes(nonce)


class PlaceholderReaderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        app = FastAPI()
        app.include_router(editor.router)
        mp3, m4b = self.root / 'cloned_audiobook.mp3', self.root / 'audiobook.m4b'
        stack = [patch.object(editor, 'DATA_DIR', str(self.root)),
                 patch.object(editor, 'AUDIOBOOK_PATH', str(mp3)),
                 patch.object(editor, 'M4B_PATH', str(m4b))]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        return TestClient(app), mp3, m4b

    def test_downloads_refuse_a_placeholder_instead_of_serving_it(self):
        client, mp3, m4b = self.client()
        audacity = self.root / 'audacity_export.zip'
        for path, route in ((mp3, '/api/audiobook'), (m4b, '/api/audiobook_m4b'),
                            (audacity, '/api/export_audacity')):
            with self.subTest(route=route):
                self.assertEqual(404, client.get(route).status_code)   # missing, as before
                path.write_bytes(PLACEHOLDER)
                response = client.get(route)
                self.assertEqual(409, response.status_code)
                self.assertIn('book switch is in progress', response.json()['detail'])
                path.write_bytes(b'real export bytes')
                response = client.get(route)
                self.assertEqual((200, b'real export bytes'), (response.status_code, response.content))
                path.unlink()

    def test_the_export_zip_leaves_a_placeholder_out(self):
        import io
        import zipfile
        client, mp3, m4b = self.client()
        mp3.write_bytes(PLACEHOLDER)
        self.assertEqual(404, client.get('/api/export_zip').status_code)
        m4b.write_bytes(b'real m4b')
        response = client.get('/api/export_zip')
        self.assertEqual(200, response.status_code)
        self.assertEqual(['audiobook.m4b'], zipfile.ZipFile(io.BytesIO(response.content)).namelist())

    def test_chunks_are_refused_not_renamed_aside_or_regenerated(self):
        from project import ProjectManager
        from chunk_status_journal import get_chunk_status_journal_path
        manager = ProjectManager(str(self.root))
        (self.root / 'annotated_script.json').write_text(json.dumps([{'speaker': 'A', 'text': 'Hi.'}]))
        for name in ('chunks.json', Path(get_chunk_status_journal_path(str(self.root / 'chunks.json'))).name):
            with self.subTest(name=name):
                (self.root / name).write_bytes(PLACEHOLDER)
                with self.assertRaisesRegex(ValueError, 'book switch is in progress'):
                    manager.load_chunks()
                self.assertEqual(PLACEHOLDER, (self.root / name).read_bytes())
                self.assertEqual([], [p.name for p in self.root.iterdir() if '.corrupt' in p.name])
                (self.root / name).unlink()

    def test_a_placeholder_state_file_has_no_export_inventory_yet(self):
        from project_scope_exports import is_scope_export_historical
        (self.root / 'state.json').write_bytes(PLACEHOLDER)
        (self.root / 'audiobook.m4b').write_bytes(b'export')
        self.assertFalse(is_scope_export_historical(self.root, self.root / 'audiobook.m4b'))


if __name__ == '__main__':
    unittest.main()
