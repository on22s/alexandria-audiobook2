import asyncio
import hashlib
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from routers import script
import docx_publication
from source_upload_stage import OwnedSourceUploadStage
from tests import test_upload_publication_source as fixtures
from tests.test_docx_preview import archive


class DocxUploadTests(unittest.IsolatedAsyncioTestCase):
    environment = fixtures.UploadPublicationSourceTests.environment

    async def test_unicode_title_remains_safe_and_selectable(self):
        async with self.environment() as (_, uploads, client):
            filename = '日本語' * 14 + '.docx'
            raw = archive()
            preview = await client.post('/api/upload', params={
                'select_active': 'false', 'docx_preview': 'true'},
                files={'file': (filename, raw)})
            self.assertEqual(200, preview.status_code, preview.text)
            accepted = await client.post('/api/upload', params={
                'select_active': 'false', 'docx_confirmed': 'true',
                'docx_preview_digest': preview.json()['preview_digest']},
                files={'file': (filename, raw)})
            self.assertEqual(200, accepted.status_code, accepted.text)
            name = accepted.json()['stored_filename']
            self.assertTrue(name.startswith('日本語'))
            for paired_name in accepted.json()['docx_source'].values():
                self.assertEqual(paired_name, script.secure_filename(paired_name))
                self.assertTrue((uploads / paired_name).exists())
            selected = await client.post('/api/uploads/select', json={'filename': name})
            self.assertEqual(200, selected.status_code, selected.text)

    async def upload(self, client, raw, **options):
        return await client.post('/api/upload', params={'select_active': 'false', **options},
                                 files={'file': ('book.docx', raw)})

    async def accept(self, client, raw):
        preview = await self.upload(client, raw, docx_preview='true')
        self.assertEqual(200, preview.status_code, preview.text)
        return await self.upload(client, raw, docx_confirmed='true', docx_preview_digest=preview.json()['preview_digest'])

    async def test_preview_cancel_bypass_and_generation_leave_previous_state(self):
        async with self.environment() as (root, uploads, client):
            state = root / 'state.json'
            state.write_text('{"active_book_id":"prior"}')
            prior = state.read_bytes()
            with patch.object(script, 'start_script_generation', side_effect=AssertionError('inference')):
                response = await self.upload(client, archive(), docx_preview='true')
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([], list(uploads.iterdir()))
                self.assertEqual(prior, state.read_bytes())
                self.assertEqual([], (await client.get('/api/uploads')).json())
                self.assertEqual(404, (await client.post('/api/uploads/select', json={'filename': 'book.docx'})).status_code)
                self.assertEqual(400, (await client.post('/api/generate_script')).status_code)
                for options in ({}, {'docx_confirmed': 'true'}, {'docx_preview_digest': 'a'*64},
                                {'docx_confirmed': 'true', 'docx_preview_digest': 'a'*64},
                                {'docx_preview': 'true', 'select_active': 'true'}):
                    refused = await self.upload(client, archive(), **options)
                    self.assertIn(refused.status_code, (400, 409), refused.text)
                self.assertEqual(prior, state.read_bytes())
                self.assertEqual([], list(uploads.iterdir()))

    async def test_preview_and_accept_do_not_dispatch_or_replace_selected_source(self):
        async with self.environment() as (root, uploads, client):
            (uploads / 'prior.txt').write_text('An already selected book.')
            selected = await client.post('/api/uploads/select', json={'filename': 'prior.txt'})
            self.assertEqual(200, selected.status_code, selected.text)
            prior = (root / 'state.json').read_bytes()
            with patch.object(script, 'start_script_generation', side_effect=AssertionError('inference')) as start:
                accepted = await self.accept(client, archive())
                self.assertEqual(200, accepted.status_code, accepted.text)
                start.assert_not_called()
            self.assertEqual(prior, (root / 'state.json').read_bytes())

    async def test_exact_original_derived_receipt_quota_and_duplicate_origins(self):
        async with self.environment() as (root, uploads, client):
            raw = archive()
            different = archive(comment=b'distinct original archive')
            for original in (raw, different):
                result = await self.accept(client, original)
                self.assertEqual(200, result.status_code, result.text)
                names = result.json()['docx_source']
                self.assertTrue(names['text'].startswith('book-'))
                self.assertEqual(original, (uploads / names['original']).read_bytes())
                receipt = json.loads((uploads / names['receipt']).read_bytes())
                self.assertTrue(receipt['confirmed'])
                self.assertEqual(names, receipt['filenames'])
                self.assertEqual(hashlib.sha256(original).hexdigest(), receipt['original_sha256'])
                text = (uploads / names['text']).read_bytes()
                self.assertEqual('Synthetic “book” 123 café é.'.encode(), text)
                self.assertEqual(hashlib.sha256(text).hexdigest(), receipt['text_sha256'])
            self.assertEqual(6, len(list(uploads.iterdir())))
            self.assertEqual(2, len((await client.get('/api/uploads')).json()))
            self.assertEqual(sum(p.stat().st_size for p in uploads.iterdir()), script.get_upload_storage_bytes())
            self.assertFalse((root / 'state.json').exists())

    async def test_changed_preview_and_insufficient_pair_quota_do_not_publish(self):
        async with self.environment() as (_, uploads, client):
            raw = archive()
            preview = (await self.upload(client, raw, docx_preview='true')).json()
            result = await self.upload(client, archive(comment=b'changed'), docx_confirmed='true', docx_preview_digest=preview['preview_digest'])
            self.assertEqual(409, result.status_code)
            with patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', len(raw) + 1):
                result = await self.upload(client, raw, docx_confirmed='true', docx_preview_digest=preview['preview_digest'])
            self.assertEqual(413, result.status_code, result.text)
            self.assertEqual([], list(uploads.iterdir()))

    async def test_publication_failures_preserve_prior_book_and_recovery_material(self):
        for boundary in ('original', 'receipt', 'text'):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    async with self.environment() as (root, uploads, client):
                        state = root / 'state.json'; state.write_bytes(b'{"active_book_id":"prior"}')
                        old = uploads / 'prior.txt'; old.write_bytes(b'Prior source')
                        original = OwnedSourceUploadStage.publish
                        def injected(stage, name):
                            kind = 'original' if name.endswith('.docx') else 'receipt' if name.endswith('.json') else 'text'
                            if kind == boundary and not after:
                                raise OSError('controlled publication failure')
                            result = original(stage, name)
                            if kind == boundary and after:
                                raise OSError('controlled unknown publication outcome')
                            return result
                        with patch.object(OwnedSourceUploadStage, 'publish', injected):
                            response = await self.accept(client, archive())
                        self.assertEqual(503, response.status_code)
                        self.assertEqual(b'{"active_book_id":"prior"}', state.read_bytes())
                        self.assertEqual(b'Prior source', old.read_bytes())
                        files = [p for p in uploads.iterdir() if p != old]
                        self.assertFalse(any(p.name.startswith('.') for p in files))
                        expected = {'original': 0, 'receipt': 1, 'text': 2}[boundary] + int(after)
                        self.assertEqual(expected, len(files))
                        for text in (p for p in files if p.suffix == '.txt'):
                            self.assertTrue(text.with_suffix('.docx').exists())
                            self.assertTrue(text.with_suffix('.docx-receipt.json').exists())

    async def test_cancellation_waits_for_owned_extraction_then_cleans_stage(self):
        async with self.environment() as (root, uploads, _):
            state = root / 'state.json'; state.write_bytes(b'prior')
            entered, release = threading.Event(), threading.Event()
            original = script.extract_docx_preview
            def delayed(raw):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('worker was not released')
                return original(raw)
            upload = UploadFile(filename='book.docx', file=io.BytesIO(archive()))
            with patch.object(script, 'extract_docx_preview', delayed):
                task = asyncio.create_task(script.upload_file(upload, select_active=False, docx_preview=True))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel(); release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            await upload.close()
            self.assertEqual([], list(uploads.iterdir()))
            self.assertEqual(b'prior', state.read_bytes())

    async def test_malformed_extraction_preserves_previous_state(self):
        async with self.environment() as (root, uploads, client):
            state = root / 'state.json'; state.write_bytes(b'prior')
            response = await self.upload(client, archive('<w:tbl/>'), docx_preview='true')
            self.assertEqual(400, response.status_code)
            self.assertIn('tbl', response.json()['detail'])
            self.assertEqual(b'prior', state.read_bytes())
            self.assertEqual([], list(uploads.iterdir()))

    async def test_publication_cancellation_retains_confirmed_group_without_selecting(self):
        async with self.environment() as (root, uploads, _):
            state = root / 'state.json'; state.write_bytes(b'prior')
            raw = archive()
            preview = script.extract_docx_preview(raw)
            entered, release = threading.Event(), threading.Event()
            original = OwnedSourceUploadStage.publish
            def delayed(stage, name):
                result = original(stage, name)
                if name.endswith('.docx'):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('worker was not released')
                return result
            upload = UploadFile(filename='book.docx', file=io.BytesIO(raw))
            with patch.object(OwnedSourceUploadStage, 'publish', delayed):
                task = asyncio.create_task(script.upload_file(upload, select_active=False, docx_confirmed=True,
                                                               docx_preview_digest=preview['preview_digest']))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel(); release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            await upload.close()
            self.assertEqual(3, len(list(uploads.iterdir())))
            self.assertEqual(raw, next(uploads.glob('*.docx')).read_bytes())
            self.assertEqual(b'prior', state.read_bytes())

    async def test_capture_error_and_cleanup_error_preserve_primary_error(self):
        async with self.environment() as (root, uploads, client):
            state = root / 'state.json'; state.write_bytes(b'prior')
            close = OwnedSourceUploadStage.close
            def bad_close(stage):
                close(stage)
                raise OSError('secondary cleanup failure')
            with patch.object(script, '_save_upload_limited', side_effect=script.HTTPException(status_code=418, detail='capture failure')), \
                    patch.object(OwnedSourceUploadStage, 'close', bad_close):
                response = await self.upload(client, archive(), docx_preview='true')
            self.assertEqual(418, response.status_code)
            self.assertEqual('capture failure', response.json()['detail'])
            self.assertEqual(b'prior', state.read_bytes())
            self.assertEqual([], list(uploads.iterdir()))

    async def test_collision_never_overwrites_another_owner(self):
        for suffix in ('.docx', '.docx-receipt.json', '.txt'):
            with self.subTest(suffix=suffix):
                async with self.environment() as (_, uploads, client):
                    foreign = uploads / ('book-aa2docx-fixed' + suffix); foreign.write_bytes(b'foreign owner')
                    with patch.object(docx_publication.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixed')):
                        response = await self.accept(client, archive())
                    self.assertEqual(503, response.status_code)
                    self.assertEqual(b'foreign owner', foreign.read_bytes())
                    self.assertFalse(any(p.name.startswith('.') for p in uploads.iterdir()))

    async def test_sync_failure_prevents_later_publication_and_preserves_book(self):
        for fail_on in (1, 2, 3):
            with self.subTest(fail_on=fail_on):
                async with self.environment() as (root, uploads, client):
                    state = root / 'state.json'; state.write_bytes(b'prior')
                    original = OwnedSourceUploadStage.sync_directory
                    calls = []
                    def failing(stage):
                        calls.append(stage.published_path)
                        if len(calls) == fail_on:
                            raise OSError('controlled sync failure')
                        return original(stage)
                    with patch.object(OwnedSourceUploadStage, 'sync_directory', failing):
                        response = await self.accept(client, archive())
                    self.assertEqual(503, response.status_code)
                    self.assertEqual(fail_on, len(list(uploads.iterdir())))
                    self.assertEqual(b'prior', state.read_bytes())

    async def test_confirmed_text_selects_through_existing_transaction_and_selection_error_preserves_it(self):
        async with self.environment() as (root, uploads, client):
            result = await self.accept(client, archive())
            self.assertEqual(200, result.status_code, result.text)
            name = result.json()['stored_filename']
            before = {p.name: p.read_bytes() for p in uploads.iterdir()}
            state = root / 'state.json'; state.write_text('{"active_book_id":"prior"}')
            previous = state.read_bytes()
            with patch.object(script, 'apply_book_input_selection', side_effect=ValueError('controlled refusal')):
                response = await client.post('/api/uploads/select', json={'filename': name})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(previous, state.read_bytes())
            self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})
            response = await client.post('/api/uploads/select', json={'filename': name})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(str(uploads / name), json.loads(state.read_bytes())['input_file_path'])
