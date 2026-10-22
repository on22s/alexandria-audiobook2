import asyncio
from contextlib import asynccontextmanager, nullcontext
import hashlib
import io
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI, UploadFile
import httpx
from routers import script

TEXT = b'Shareable source paragraph.'


def epub(title='Fixture title', *, cover=None, metadata=True, text=TEXT):
    container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfile full-path="book.opf"/></container>')
    meta = ('<metadata><dc:title>' + title + '</dc:title>'
            '<dc:creator>Fixture Author</dc:creator></metadata>') if metadata else ''
    cover_item = '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>' if cover else ''
    package = ('<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
               + meta + '<manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>'
               + cover_item + '</manifest><spine><itemref idref="body"/></spine></package>')
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        for name, data in [('META-INF/container.xml', container), ('book.opf', package),
                           ('body.xhtml', '<html><body><p>' + text.decode() + '</p></body></html>')]:
            archive.writestr(name, data)
        if cover:
            archive.writestr('cover.png', cover)
    return raw.getvalue()


class UploadPublicationSourceTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); uploads = root / 'uploads'; uploads.mkdir()
            app = FastAPI(); app.include_router(script.router)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', str(root)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                                             base_url='http://fixture') as client:
                    yield root, uploads, client

    async def upload(self, client, raw, name='book.epub', select=False):
        return await client.post('/api/upload', params={'select_active': str(select).lower()},
                                 files={'file': (name, raw, 'application/epub+zip')})

    async def test_capture_preserves_exact_origin_and_distinct_deduplicated_editions(self):
        async with self.environment() as (root, uploads, client):
            first = epub(); second = epub('Other edition')
            for raw, name in ((first, 'first.epub'), (second, 'second.epub'), (first, 'repeat.epub')):
                response = await self.upload(client, raw, name)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual('available', response.json()['publication_status'])
            source = uploads / 'first.txt'
            self.assertEqual(TEXT, source.read_bytes())
            record = json.loads(Path(str(source) + '.publication.json').read_bytes())
            self.assertEqual(hashlib.sha256(TEXT).hexdigest(), record['binding']['sha256'])
            self.assertEqual([hashlib.sha256(raw).hexdigest() for raw in (first, second)],
                             [row['origin']['sha256'] for row in record['candidates']])
            self.assertEqual([source, Path(str(source) + '.publication.json')], sorted(uploads.iterdir()))
            self.assertFalse((root / 'state.json').exists())

    async def test_origin_cap_refusal_preserves_prior_companion_and_source(self):
        async with self.environment() as (_, uploads, client):
            for index in range(4):
                response = await self.upload(client, epub(str(index)))
                self.assertEqual(200, response.status_code, response.text)
            before = {p.name: p.read_bytes() for p in uploads.iterdir()}
            response = await self.upload(client, epub('Fifth origin'))
            self.assertEqual(413, response.status_code, response.text)
            self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})

    async def test_changed_capture_of_same_origin_requires_review_without_overwriting_it(self):
        async with self.environment() as (_, uploads, client):
            self.assertEqual(200, (await self.upload(client, epub())).status_code)
            companion = uploads / 'book.txt.publication.json'
            document = json.loads(companion.read_bytes())
            document['candidates'][0]['fields']['title'] = ['Edited prior capture']
            companion.write_text(json.dumps(document))
            before = {p.name: p.read_bytes() for p in uploads.iterdir()}
            response = await self.upload(client, epub())
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})

    async def test_text_and_metadata_use_same_captured_bytes_even_if_stage_is_replaced(self):
        async with self.environment() as (_, uploads, client):
            original = epub(); replacement = epub('Replacement title', text=b'Replacement source words.')
            extract = script.extract_epub_text
            def replaced(path, **kwargs):
                Path(path).write_bytes(replacement)
                return extract(path, **kwargs)
            with patch.object(script, 'extract_epub_text', side_effect=replaced):
                response = await self.upload(client, original)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(TEXT, (uploads / 'book.txt').read_bytes())
            record = json.loads((uploads / 'book.txt.publication.json').read_bytes())
            self.assertEqual(hashlib.sha256(original).hexdigest(), record['candidates'][0]['origin']['sha256'])
            self.assertEqual(['Fixture title'], record['candidates'][0]['fields']['title'])

    async def test_deep_corrupt_companion_refuses_reimport_without_changing_existing_source(self):
        async with self.environment() as (_, uploads, client):
            self.assertEqual(200, (await self.upload(client, epub())).status_code)
            companion = uploads / 'book.txt.publication.json'
            companion.write_bytes(b'[' * 2000 + b']' * 2000)
            before = {p.name: p.read_bytes() for p in uploads.iterdir()}
            response = await self.upload(client, epub())
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})

    async def test_missing_metadata_retains_readable_text_and_explicit_unavailable_origin(self):
        async with self.environment() as (_, uploads, client):
            response = await self.upload(client, epub(metadata=False))
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('unavailable', response.json()['publication_status'])
            record = json.loads((uploads / 'book.txt.publication.json').read_bytes())
            self.assertEqual({'title': [], 'author': []}, record['candidates'][0]['fields'])
            self.assertTrue(record['candidates'][0]['warnings'])

    async def test_companion_staging_counts_against_actual_upload_quota(self):
        from PIL import Image
        image = io.BytesIO()
        with Image.frombytes('RGB', (64, 64), random.Random(17).randbytes(64 * 64 * 3)) as pixels:
            pixels.save(image, format='PNG')
        raw = epub(cover=image.getvalue())
        async with self.environment() as (_, uploads, client):
            existing = uploads / 'existing.txt'; existing.write_bytes(TEXT)
            with patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', len(raw) + len(TEXT) * 2 + 10):
                response = await self.upload(client, raw)
            self.assertEqual(413, response.status_code, response.text)
            self.assertEqual([existing], list(uploads.iterdir()))
            self.assertEqual(TEXT, existing.read_bytes())

    async def test_failure_after_publication_and_failed_selection_restore_previous_bytes(self):
        async with self.environment() as (_, uploads, client):
            self.assertEqual(200, (await self.upload(client, epub())).status_code)
            before = {p.name: p.read_bytes() for p in uploads.iterdir()}
            apply = script.apply_publication_source_update
            def fail_after_replace(update):
                apply(update)
                raise OSError('fixture after publication')
            with patch.object(script, 'apply_publication_source_update', side_effect=fail_after_replace):
                self.assertEqual(500, (await self.upload(client, epub('Different'))).status_code)
            self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})
            with patch.object(script, 'ensure_book_switch_allowed', return_value=nullcontext()), \
                 patch.object(script, 'apply_book_input_selection', side_effect=ValueError('fixture selection refusal')):
                self.assertEqual(500, (await self.upload(client, epub('Different'), select=True)).status_code)
            self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})

    async def test_cancelled_publication_waits_for_owned_worker_then_restores_prior_companion(self):
        async with self.environment() as (_, uploads, client):
            self.assertEqual(200, (await self.upload(client, epub())).status_code)
            before = {p.name: p.read_bytes() for p in uploads.iterdir()}
            entered, release = threading.Event(), threading.Event()
            apply = script.apply_publication_source_update
            def paused(update):
                apply(update); entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture publication worker not released')
            upload = UploadFile(filename='cancelled.epub', file=io.BytesIO(epub('Cancelled edition')))
            with patch.object(script, 'apply_publication_source_update', side_effect=paused):
                task = asyncio.create_task(script.upload_file(upload, select_active=False))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel(); await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            await upload.close()
            self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})
