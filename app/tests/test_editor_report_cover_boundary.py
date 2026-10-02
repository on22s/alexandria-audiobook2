import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from tests.test_m4b_cover_admission import image_bytes


class EditorReportCoverBoundaryTests(unittest.TestCase):
    def test_cover_response_uses_leaf_and_published_image_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            app = FastAPI(); app.include_router(editor.router)
            payload = image_bytes('PNG')
            with patch.object(editor, 'DATA_DIR', root), TestClient(app) as client:
                response = client.post('/api/m4b_cover', files={'file': ('cover.png', payload, 'image/png')})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({'status': 'uploaded', 'path': 'm4b_cover.jpg'}, response.json())
            self.assertEqual(payload, (Path(root) / 'm4b_cover.jpg').read_bytes())
            self.assertFalse(list(Path(root).glob('*.upload.*')))

    def test_listing_and_download_share_realpath_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); reports = root / 'reports'; reports.mkdir()
            payload = '# Résumé\n\nReal Markdown.\n'
            (reports / 'review.md').write_text(payload, encoding='utf-8')
            (reports / 'inside.md').symlink_to(reports / 'review.md')
            outside = root / 'reports-other'; outside.mkdir()
            secret = outside / 'secret.md'; secret.write_text('outside private bytes')
            (reports / 'outside.md').symlink_to(secret)
            (reports / 'broken.md').symlink_to(reports / 'missing.md')
            (reports / 'directory.md').mkdir()
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'REPORTS_DIR', str(reports)), TestClient(app) as client:
                response = client.get('/api/reports')
                self.assertEqual(200, response.status_code)
                rows = response.json()
                self.assertEqual({'review.md', 'inside.md'}, {row['filename'] for row in rows})
                self.assertEqual(sorted((row['mtime'] for row in rows), reverse=True), [row['mtime'] for row in rows])
                for filename in ('review.md', 'inside.md'):
                    downloaded = client.get('/api/reports/' + filename)
                    self.assertEqual(200, downloaded.status_code)
                    self.assertEqual(payload, downloaded.text)
                    self.assertTrue(downloaded.headers['content-type'].startswith('text/markdown'))
                self.assertEqual(400, client.get('/api/reports/outside.md').status_code)
                for filename in ('broken.md', 'directory.md', 'missing.md'):
                    self.assertEqual(404, client.get('/api/reports/' + filename).status_code)
            self.assertEqual('outside private bytes', secret.read_text())

    def test_portable_invalid_names_are_not_listed_or_downloaded(self):
        with tempfile.TemporaryDirectory() as root:
            reports = Path(root)
            (reports / 'bad\\name.md').write_text('bad leaf')
            (reports / 'other.txt').write_text('not Markdown')
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'REPORTS_DIR', root), TestClient(app) as client:
                self.assertEqual([], client.get('/api/reports').json())
                self.assertEqual(400, client.get('/api/reports/bad%5Cname.md').status_code)
                self.assertEqual(400, client.get('/api/reports/other.txt').status_code)
