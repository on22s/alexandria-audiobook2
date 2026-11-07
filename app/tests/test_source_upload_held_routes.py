"""HTTP route contracts for held upload publication (issue #1784).

Kept separate from the filesystem-only publication tests, which CI also runs
with site packages disabled (python -S).
"""
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from source_upload_stage import OwnedSourceUploadStage
from tests.test_source_upload_publication import _held_error

class UploadHeldRouteTests(unittest.IsolatedAsyncioTestCase):
    """Held-file route mapping (issue #1784): 503, honest wording, no traceback."""

    @asynccontextmanager
    async def environment(self):
        import httpx
        from fastapi import FastAPI
        from routers import script
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            app = FastAPI()
            app.include_router(script.router)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(script, 'DATA_DIR', str(root)):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                        base_url='http://fixture') as client:
                    yield root, uploads, client

    async def _upload(self, client, name, raw):
        return await client.post('/api/upload', params={'select_active': 'false'},
                                 files={'file': (name, raw, 'text/plain')})

    async def test_plain_upload_held_is_503_then_retry_succeeds(self):
        from routers import script
        payload = b'Complete synthetic source.\n' * 10
        async with self.environment() as (root, uploads, client):
            with patch.object(script, 'publish_completed_upload', side_effect=_held_error()):
                response = await self._upload(client, 'book.txt', payload)
            self.assertEqual(503, response.status_code, response.text)
            detail = response.json()['detail']
            self.assertIn('held by another program', detail)
            self.assertIn('nothing was published', detail)
            self.assertIn('may be held', detail)
            self.assertNotIn('Traceback', response.text)
            self.assertNotIn('WinError', response.text)
            self.assertEqual([], list(uploads.iterdir()))
            self.assertEqual(0, script.get_upload_storage_bytes())
            self.assertFalse((root / 'state.json').exists())
            retry = await self._upload(client, 'book.txt', payload)
            self.assertEqual(200, retry.status_code, retry.text)
            self.assertEqual(payload, (uploads / 'book.txt').read_bytes())
            self.assertEqual(1, (uploads / 'book.txt').stat().st_nlink)

    async def _epub_bytes(self):
        import io
        import zipfile
        container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfile full-path="book.opf"/></container>')
        package = ('<package xmlns="http://www.idpf.org/2007/opf" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<metadata><dc:title>Fixture title</dc:title>'
                   '<dc:creator>Fixture Author</dc:creator></metadata>'
                   '<manifest><item id="body" href="body.xhtml" '
                   'media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="body"/></spine></package>')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            for name, data in [('META-INF/container.xml', container), ('book.opf', package),
                               ('body.xhtml', '<html><body><p>Synthetic text.</p></body></html>')]:
                archive.writestr(zipfile.ZipInfo(name), data)
        return raw.getvalue()

    async def test_epub_retained_publish_held_is_503_and_publishes_nothing(self):
        from routers import script
        raw = await self._epub_bytes()
        async with self.environment() as (root, uploads, client):
            with patch.object(script, 'publish_retained_epub_upload', side_effect=_held_error()):
                response = await client.post(
                    '/api/upload', params={'select_active': 'false'},
                    files={'file': ('book.epub', raw, 'application/epub+zip')})
            self.assertEqual(503, response.status_code, response.text)
            detail = response.json()['detail']
            self.assertIn('held by another program', detail)
            self.assertIn('nothing was published', detail)
            self.assertNotIn('Traceback', response.text)
            self.assertEqual([], list(uploads.iterdir()))

    async def test_txt_publish_held_with_retained_visible_is_503_and_keeps_retained(self):
        from routers import script
        raw = await self._epub_bytes()
        async with self.environment() as (root, uploads, client):
            with patch.object(script, 'publish_completed_upload', side_effect=_held_error()):
                response = await client.post(
                    '/api/upload', params={'select_active': 'false'},
                    files={'file': ('book.epub', raw, 'application/epub+zip')})
            self.assertEqual(503, response.status_code, response.text)
            detail = response.json()['detail']
            self.assertIn('held by another program', detail)
            self.assertIn('book.source.epub', detail)
            self.assertIn('retained', detail)
            self.assertIn('Inspect it with GET /api/uploads/epub-retained', detail)
            self.assertIn('remove it manually before retrying', detail)
            self.assertNotIn('Traceback', response.text)
            self.assertEqual(raw, (uploads / 'book.source.epub').read_bytes())
            self.assertFalse((uploads / 'book.txt').exists())
            self.assertFalse((uploads / 'book.txt.publication.json').exists())
            self.assertEqual(len(raw), script.get_upload_storage_bytes())
            self.assertFalse((root / 'state.json').exists())

    async def _docx_upload(self, client, raw, **options):
        return await client.post('/api/upload', params={'select_active': 'false', **options},
                                 files={'file': ('book.docx', raw)})

    async def test_docx_publish_held_is_503_keeps_partial_group_and_retries(self):
        from routers import script
        from tests.test_docx_preview import archive
        raw = archive()
        async with self.environment() as (root, uploads, client):
            preview = await self._docx_upload(client, raw, docx_preview='true')
            self.assertEqual(200, preview.status_code, preview.text)
            digest = preview.json()['preview_digest']
            real_publish = OwnedSourceUploadStage.publish

            def held_receipt(stage, name):
                if name.endswith('.docx-receipt.json'):
                    raise _held_error()
                return real_publish(stage, name)

            with patch.object(OwnedSourceUploadStage, 'publish', held_receipt):
                response = await self._docx_upload(
                    client, raw, docx_confirmed='true', docx_preview_digest=digest)
            self.assertEqual(503, response.status_code, response.text)
            detail = response.json()['detail']
            self.assertIn('DOCX upload could not be completed', detail)
            self.assertIn('appears to be held', detail)
            self.assertIn('already published recovery material remains retained', detail)
            self.assertNotIn('Traceback', response.text)
            originals = list(uploads.glob('*.docx'))
            self.assertEqual(1, len(originals))
            self.assertEqual(raw, originals[0].read_bytes())
            self.assertFalse(list(uploads.glob('*.docx-receipt.json')))
            self.assertFalse(list(uploads.glob('*.txt')))
            self.assertFalse([path for path in uploads.iterdir() if path.name.startswith('.')])
            self.assertEqual(len(raw), script.get_upload_storage_bytes())
            self.assertFalse((root / 'state.json').exists())
            retry = await self._docx_upload(
                client, raw, docx_confirmed='true', docx_preview_digest=digest)
            self.assertEqual(200, retry.status_code, retry.text)
            for name in retry.json()['docx_source'].values():
                self.assertTrue((uploads / name).exists(), name)
