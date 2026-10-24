import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from routers import editor
from epub_publication import get_epub_publication_candidate
from publication_source import (get_publication_source_path, get_upload_publication_update,
                                apply_publication_source_update)
import publication_preview
from tests import test_saved_publication_source as fixtures
from tests.test_m4b_cover_admission import image_bytes
from tests.test_upload_publication_source import epub, TEXT


class PublicationPreviewAPITests(unittest.TestCase):
    @contextmanager
    def fixture(self, cover=True):
        with fixtures.SavedPublicationSourceTests().fixture() as (root, _):
            source = root / 'uploads/source.txt'
            if cover:
                candidate = get_epub_publication_candidate(epub(cover=image_bytes('PNG')),
                    filename='origin.epub', text_sha256=hashlib.sha256(TEXT).hexdigest())
                Path(get_publication_source_path(source)).write_text(json.dumps({
                    'version': 1, 'binding': {'kind': 'text', 'sha256': hashlib.sha256(TEXT).hexdigest()},
                    'candidates': [candidate]}))
            api = FastAPI(); api.include_router(editor.router)
            @api.get('/ping')
            async def ping():
                return {'ok': True}
            with patch.object(editor, 'DATA_DIR', str(root)), patch.object(
                    editor.project_manager, 'get_engine', side_effect=AssertionError('Must not construct an engine')):
                yield root, api

    def payloads(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
                if p.is_file() and not p.name.endswith('.lock')}

    def choice(self, preview, **options):
        return {'expected_book_token': preview['book_token'], 'expected_revision': preview['revision'],
                'origin_id': preview['candidates'][0]['id'], **options}

    def uploaded_preview_params(self, client):
        preview = client.get('/api/m4b_publication').json()
        return {'expected_book_token': preview['book_token'],
                'sha256': preview['uploaded_cover']['sha256']}

    def test_uploaded_image_preview_returns_exact_bytes_without_mutation(self):
        for fmt, media_type in (('PNG', 'image/png'), ('JPEG', 'image/jpeg')):
            with self.subTest(fmt=fmt), self.fixture() as (root, api), TestClient(api) as client:
                raw = image_bytes(fmt)
                (root / 'm4b_cover.jpg').write_bytes(raw)
                params = self.uploaded_preview_params(client)
                before = self.payloads(root)
                result = client.get('/api/m4b_cover/preview', params={**params,
                    'path': 'uploads/source.txt'})
                self.assertEqual(200, result.status_code, result.text)
                self.assertEqual(raw, result.content)
                self.assertEqual(params['sha256'], hashlib.sha256(result.content).hexdigest())
                self.assertEqual(media_type, result.headers['content-type'])
                self.assertEqual('no-store', result.headers['cache-control'])
                self.assertEqual('nosniff', result.headers['x-content-type-options'])
                self.assertEqual(before, self.payloads(root))
                uploaded = client.get('/api/m4b_publication').json()['uploaded_cover']
                self.assertEqual({'present': True, 'sha256': params['sha256'],
                    'validation': 'not_rechecked'}, uploaded)

    def test_uploaded_image_preview_rejects_invalid_or_stale_authority(self):
        for fault in ('missing', 'empty', 'corrupt', 'oversized', 'book', 'cover', 'symlink'):
            with self.subTest(fault=fault), self.fixture() as (root, api), TestClient(api) as client:
                cover = root / 'm4b_cover.jpg'; cover.write_bytes(image_bytes('PNG'))
                params = self.uploaded_preview_params(client)
                if fault == 'missing':
                    cover.unlink()
                elif fault == 'empty':
                    cover.write_bytes(b''); params['sha256'] = hashlib.sha256(b'').hexdigest()
                elif fault == 'corrupt':
                    cover.write_bytes(b'bad image'); params['sha256'] = hashlib.sha256(b'bad image').hexdigest()
                elif fault == 'oversized':
                    with cover.open('wb') as stream:
                        stream.truncate(25 * 1024**2 + 1)
                elif fault == 'book':
                    state = json.loads((root / 'state.json').read_bytes())
                    state['book_generation'] = 'new-book'; (root / 'state.json').write_text(json.dumps(state))
                elif fault == 'cover':
                    cover.write_bytes(image_bytes('JPEG'))
                else:
                    cover.unlink()
                    try:
                        cover.symlink_to(root / 'uploads/source.txt')
                    except (OSError, NotImplementedError):
                        self.skipTest('Host does not permit symbolic links')
                result = client.get('/api/m4b_cover/preview', params=params)
                self.assertEqual(409, result.status_code, result.text)
                self.assertNotIn(str(root), result.text)
                self.assertEqual('no-store', result.headers['cache-control'])
                self.assertEqual('nosniff', result.headers['x-content-type-options'])
                self.assertEqual('Selected cover is unavailable or changed. Refresh cover options.',
                    result.json()['detail'])

    def test_uploaded_preview_pixel_and_recovery_failures_are_generic_and_readonly(self):
        from PIL import Image
        for fault in ('pixels', 'pending', 'state'):
            with self.subTest(fault=fault), self.fixture() as (root, api), TestClient(api) as client:
                (root/'m4b_cover.jpg').write_bytes(image_bytes('PNG'))
                params = self.uploaded_preview_params(client)
                if fault == 'pending':
                    (root/'.active_book_transaction.json').write_bytes(b'broken transaction')
                elif fault == 'state':
                    (root/'state.json').write_bytes(b'broken state')
                before = self.payloads(root)
                with patch.object(Image, 'MAX_IMAGE_PIXELS', 400 if fault == 'pixels' else Image.MAX_IMAGE_PIXELS):
                    response = client.get('/api/m4b_cover/preview', params=params)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual('Selected cover is unavailable or changed. Refresh cover options.', response.json()['detail'])
                self.assertEqual(before, self.payloads(root))

    def test_uploaded_preview_oversized_matching_digest_refuses_before_pixel_decode(self):
        import m4b_cover
        with self.fixture() as (root, api), TestClient(api) as client:
            cover = root/'m4b_cover.jpg'; cover.write_bytes(image_bytes('PNG'))
            params = self.uploaded_preview_params(client)
            with cover.open('wb') as stream:
                stream.truncate(m4b_cover.MAX_COVER_BYTES + 1)
            digest = hashlib.sha256()
            with cover.open('rb') as stream:
                for block in iter(lambda: stream.read(65536), b''):
                    digest.update(block)
            params['sha256'] = digest.hexdigest()
            with patch.object(m4b_cover, 'get_validated_cover_bytes',
                    side_effect=AssertionError('Oversized cover reached pixel validation')):
                result = client.get('/api/m4b_cover/preview', params=params)
            self.assertEqual(409, result.status_code, result.text)
            self.assertEqual(m4b_cover.MAX_COVER_BYTES + 1, cover.stat().st_size)

    def test_uploaded_preview_query_is_validated_before_worker(self):
        with self.fixture() as (_, api), TestClient(api) as client, patch.object(
                editor, '_get_uploaded_cover_preview_sync', side_effect=AssertionError('Invalid query reached worker')):
            valid = {'expected_book_token': 'a'*64, 'sha256': 'b'*64}
            for params in ({}, {'sha256': 'b'*64}, {**valid, 'sha256': '../cover'},
                    {**valid, 'expected_book_token': 'A'*64}, {**valid, 'sha256': 'b'*65}):
                self.assertEqual(422, client.get('/api/m4b_cover/preview', params=params).status_code)

    def test_uploaded_preview_rechecks_cover_and_book_after_pixel_validation(self):
        import m4b_cover
        for change in ('cover', 'book'):
            with self.subTest(change=change), self.fixture() as (root, api), TestClient(api) as client:
                cover = root / 'm4b_cover.jpg'; cover.write_bytes(image_bytes('PNG'))
                params = self.uploaded_preview_params(client)
                original = m4b_cover.get_validated_cover_bytes
                def changed(raw, media_type):
                    result = original(raw, media_type)
                    if change == 'cover':
                        cover.write_bytes(image_bytes('JPEG'))
                    else:
                        state = json.loads((root / 'state.json').read_bytes())
                        state['book_generation'] = 'changed'; (root / 'state.json').write_text(json.dumps(state))
                    return result
                with patch.object(m4b_cover, 'get_validated_cover_bytes', side_effect=changed):
                    result = client.get('/api/m4b_cover/preview', params=params)
                self.assertEqual(409, result.status_code, result.text)

    def test_uploaded_preview_validation_runs_off_the_event_loop(self):
        import m4b_cover
        with self.fixture() as (root, api), TestClient(api) as sync_client:
            (root / 'm4b_cover.jpg').write_bytes(image_bytes('PNG'))
            params = self.uploaded_preview_params(sync_client)
            entered, release = threading.Event(), threading.Event()
            original = m4b_cover.get_validated_cover_bytes
            def paused(*args):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('Fixture validator not released')
                return original(*args)
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(api), base_url='http://fixture') as client:
                    task = asyncio.create_task(client.get('/api/m4b_cover/preview', params=params))
                    watchdog = threading.Timer(1, release.set); watchdog.start()
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        self.assertEqual({'ok': True}, (await client.get('/ping')).json())
                        self.assertFalse(release.is_set()); self.assertFalse(task.done())
                    finally:
                        release.set(); watchdog.cancel(); result = await task
                    self.assertEqual(200, result.status_code, result.text)
            with patch.object(m4b_cover, 'get_validated_cover_bytes', side_effect=paused):
                asyncio.run(run())

    def test_preview_and_explicit_choices_are_readonly_and_expose_origins_not_local_paths(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            before = self.payloads(root)
            response = client.get('/api/m4b_publication'); self.assertEqual(200, response.status_code, response.text)
            preview = response.json(); self.assertNotIn(str(root), response.text)
            self.assertEqual('current', preview['binding_status'])
            self.assertEqual('origin.epub', preview['candidates'][0]['origin']['filename'])
            self.assertTrue(preview['candidates'][0]['cover']['preview_data_url'].startswith('data:image/png;base64,'))
            selected = client.post('/api/m4b_publication/resolve', json=self.choice(
                preview, title_index=0, author_index=0, select_cover=True))
            self.assertEqual(200, selected.status_code, selected.text)
            self.assertEqual({'title': 'Fixture title', 'author': 'Fixture Author'}, selected.json()['fields'])
            self.assertIsNotNone(selected.json()['cover_selection'])
            self.assertEqual(before, self.payloads(root))

    def test_every_changed_book_source_companion_or_cover_refuses_old_preview(self):
        for change in ('book', 'source', 'companion', 'cover'):
            with self.subTest(change=change), self.fixture() as (root, api), TestClient(api) as client:
                preview = client.get('/api/m4b_publication').json()
                if change == 'book':
                    state = json.loads((root / 'state.json').read_bytes()); state['book_generation'] = 'other'
                    (root / 'state.json').write_text(json.dumps(state))
                elif change == 'source':
                    (root / 'uploads/source.txt').write_bytes(TEXT + b' Changed.')
                elif change == 'companion':
                    path = Path(get_publication_source_path(root / 'uploads/source.txt'))
                    doc = json.loads(path.read_bytes()); doc['candidates'][0]['fields']['title'] = ['Changed title']
                    path.write_text(json.dumps(doc))
                else:
                    (root / 'm4b_cover.jpg').write_bytes(image_bytes('JPEG'))
                before = self.payloads(root)
                result = client.post('/api/m4b_publication/resolve', json=self.choice(
                    preview, title_index=0, reviewed=True))
                self.assertEqual(409, result.status_code, result.text)
                self.assertEqual(before, self.payloads(root))

    def test_stale_or_multiple_origins_require_explicit_review_of_current_preview(self):
        with self.fixture(cover=False) as (root, api), TestClient(api) as client:
            source = root / 'uploads/source.txt'
            candidate = get_epub_publication_candidate(epub('Other edition'), filename='other.epub',
                                                       text_sha256=hashlib.sha256(TEXT).hexdigest())
            apply_publication_source_update(get_upload_publication_update(
                source, candidate, 48 * 1024**2, source_limit=512 * 1024**2))
            for stale in (False, True):
                if stale:
                    source.write_bytes(TEXT + b' Edited.')
                preview = client.get('/api/m4b_publication').json()
                self.assertEqual('stale' if stale else 'current', preview['binding_status'])
                choice = self.choice(preview, title_index=0)
                choice['origin_id'] = preview['candidates'][1]['id']
                self.assertEqual(409, client.post('/api/m4b_publication/resolve', json=choice).status_code)
                choice['reviewed'] = True
                result = client.post('/api/m4b_publication/resolve', json=choice)
                self.assertEqual(200, result.status_code, result.text)
                self.assertEqual({'title': 'Other edition'}, result.json()['fields'])

    def test_uploaded_cover_precedence_keeps_metadata_choices_and_requires_fresh_source_cover_choice(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            cover = root / 'm4b_cover.jpg'; cover.write_bytes(image_bytes('JPEG'))
            preview = client.get('/api/m4b_publication').json()
            self.assertTrue(preview['uploaded_cover']['present'])
            self.assertEqual(409, client.post('/api/m4b_publication/resolve', json=self.choice(
                preview, select_cover=True)).status_code)
            self.assertEqual(200, client.post('/api/m4b_publication/resolve', json=self.choice(
                preview, title_index=0)).status_code)
            cover.unlink()
            self.assertEqual(409, client.post('/api/m4b_publication/resolve', json=self.choice(
                preview, select_cover=True)).status_code)
            preview = client.get('/api/m4b_publication').json()
            self.assertEqual(200, client.post('/api/m4b_publication/resolve', json=self.choice(
                preview, select_cover=True)).status_code)
            self.assertFalse(cover.exists())

    def test_invalid_indices_types_empty_choices_or_unknown_origin_cannot_resolve(self):
        with self.fixture() as (_, api), TestClient(api) as client:
            preview = client.get('/api/m4b_publication').json()
            for options, code in (({'title_index': True}, 422), ({'title_index': '0'}, 422),
                    ({'title_index': 31}, 409), ({'origin_id': '0' * 64, 'title_index': 0}, 409),
                    ({'cover_path': '/not/allowed'}, 422), ({}, 409)):
                with self.subTest(options=options):
                    result = client.post('/api/m4b_publication/resolve', json=self.choice(preview, **options))
                    self.assertEqual(code, result.status_code, result.text)

    def test_pending_recovery_and_corrupt_provenance_refuse_without_repair(self):
        for mode in ('journal', 'companion'):
            with self.subTest(mode=mode), self.fixture() as (root, api), TestClient(api) as client:
                path = (root / '.active_book_transaction.json' if mode == 'journal' else
                        Path(get_publication_source_path(root / 'uploads/source.txt')))
                path.write_bytes(b'broken metadata'); before = self.payloads(root)
                self.assertEqual(409, client.get('/api/m4b_publication').status_code)
                self.assertEqual(before, self.payloads(root))

    def test_same_event_loop_remains_responsive_during_bounded_source_read(self):
        with self.fixture() as (_, api):
            entered, release = threading.Event(), threading.Event()
            original = editor.get_publication_review
            def paused(*args):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture publication reader not released')
                return original(*args)
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(api), base_url='http://fixture') as client:
                    task = asyncio.create_task(client.get('/api/m4b_publication'))
                    watchdog = threading.Timer(.5, release.set); watchdog.start()
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        self.assertEqual({'ok': True}, (await client.get('/ping')).json())
                        self.assertFalse(release.is_set()); self.assertFalse(task.done())
                    finally:
                        release.set(); watchdog.cancel(); response = await task
                    self.assertEqual(200, response.status_code, response.text)
            with patch.object(editor, 'get_publication_review', side_effect=paused):
                asyncio.run(run())

    def test_companion_changed_during_resolution_refuses_the_partially_read_snapshot(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            preview = client.get('/api/m4b_publication').json()
            original = publication_preview.get_publication_source_capture
            def changed(path):
                captured = original(path)
                candidate = get_epub_publication_candidate(epub('Concurrent edition'), filename='other.epub',
                                                           text_sha256=hashlib.sha256(TEXT).hexdigest())
                apply_publication_source_update(get_upload_publication_update(
                    path, candidate, 48 * 1024**2, source_limit=512 * 1024**2))
                return captured
            with patch.object(publication_preview, 'get_publication_source_capture', side_effect=changed):
                result = client.post('/api/m4b_publication/resolve', json=self.choice(preview, title_index=0))
            self.assertEqual(409, result.status_code, result.text)
