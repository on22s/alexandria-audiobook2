"""Actual upload/read/selection boundaries using synthetic source bytes."""
import asyncio
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from routers import script
from tests import test_sound_design_upload_selection as selection_tests
from tests.test_upload_publication_source import epub
from source_upload_stage import OwnedSourceUploadStage


class SourceUploadVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_vanished_reusable_upload_is_skipped(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (_, uploads, client):
            vanished = uploads / 'vanished.txt'
            vanished.write_bytes(b'Transient source.')
            original = script.os.lstat
            with patch.object(script.os, 'lstat', side_effect=lambda path: (
                    (_ for _ in ()).throw(FileNotFoundError(path))
                    if os.path.normcase(str(path)) == os.path.normcase(str(vanished))
                    else original(path))):
                response = await client.get('/api/uploads')
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([], response.json())

    async def test_multiply_linked_upload_is_not_reusable_or_selectable(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (_, uploads, client):
            source = uploads / 'linked.txt'
            source.write_bytes(b'Externally aliased source.')
            os.link(source, uploads / 'external-alias')
            listed = await client.get('/api/uploads')
            selected = await client.post('/api/uploads/select', json={'filename': 'linked.txt'})
            self.assertEqual(200, listed.status_code, listed.text)
            self.assertEqual([], listed.json())
            self.assertEqual(404, selected.status_code, selected.text)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                script._resolve_batch_script_input('linked.txt')

    async def test_dedupe_storage_replacement_is_a_governed_conflict(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (_, uploads, client):
            with patch.object(script, '_reuse_duplicate_upload',
                              side_effect=ValueError('Upload storage changed during publication')):
                response = await client.post('/api/upload', params={'select_active': 'false'},
                    files={'file': ('new.txt', b'New source.', 'text/plain')})
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('storage changed', response.json()['detail'].lower())
            self.assertEqual([], list(uploads.iterdir()))

    async def test_busy_book_selection_is_a_retryable_conflict(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (_, uploads, client):
            (uploads / 'existing.txt').write_bytes(b'Existing reusable source.')
            with patch.object(script, 'apply_book_input_selection',
                              side_effect=TimeoutError('controlled busy lock')):
                selected = await client.post('/api/uploads/select', json={'filename': 'existing.txt'})
                uploaded = await client.post('/api/upload', params={'select_active': 'true'},
                    files={'file': ('new.txt', b'New source.', 'text/plain')})
            self.assertEqual(409, selected.status_code, selected.text)
            self.assertEqual(409, uploaded.status_code, uploaded.text)
            self.assertIn('busy', selected.json()['detail'].lower())
            self.assertIn('busy', uploaded.json()['detail'].lower())
            self.assertEqual(b'Existing reusable source.', (uploads / 'existing.txt').read_bytes())

    async def test_epub_stage_cleanup_leaves_event_loop_free_and_waits_on_cancel(self):
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, uploads, client):
            entered, release = threading.Event(), threading.Event()
            original, loop_thread = OwnedSourceUploadStage.close, threading.get_ident()
            worker_threads = []
            def paused_close(stage):
                if stage._stream is not None and stage.published_path is None and not entered.is_set():
                    worker_threads.append(threading.get_ident())
                    entered.set()
                    if not release.wait(4):
                        raise TimeoutError('archive close worker was not released')
                return original(stage)
            upload = UploadFile(filename='book.epub', file=io.BytesIO(epub()))
            with patch.object(OwnedSourceUploadStage, 'close', paused_close):
                task = asyncio.create_task(script.upload_file(upload, select_active=False))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    self.assertNotEqual(loop_thread, worker_threads[0])
                    self.assertEqual([], (await client.get('/api/uploads')).json())
                    task.cancel()
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 3)
                    await upload.close()
            self.assertEqual([], list(uploads.iterdir()))
            with script.file_lock(str(uploads) + '.quota', timeout=.1):
                pass

    async def test_writer_close_failure_retains_unknown_source_and_provenance(self):
        for name, data in (('book.txt', b'Complete synthetic text.'), ('book.epub', epub())):
            with self.subTest(name=name):
                async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, uploads, client):
                    state = root / 'state.json'
                    before = state.read_bytes() if state.exists() else None
                    original = OwnedSourceUploadStage._close_stream
                    failed = []
                    def close_then_fail(stage):
                        original(stage)
                        if stage.published_path is not None and not failed:
                            failed.append(True)
                            raise OSError('controlled writer close failure')
                    upload = UploadFile(filename=name, file=io.BytesIO(data))
                    try:
                        with patch.object(OwnedSourceUploadStage, '_close_stream', close_then_fail):
                            with self.assertRaisesRegex(OSError, 'controlled writer close failure'):
                                await script.upload_file(upload, select_active=False)
                    finally:
                        await upload.close()
                    expected = (script.extract_epub_text(name, archive_bytes=data).encode('utf-8')
                                if name.endswith('.epub') else data)
                    self.assertEqual([True], failed)
                    self.assertEqual(expected, (uploads / 'book.txt').read_bytes())
                    if name.endswith('.epub'):
                        import hashlib
                        companion = json.loads((uploads / 'book.txt.publication.json').read_bytes())
                        self.assertEqual(hashlib.sha256(expected).hexdigest(), companion['binding']['sha256'])
                    self.assertEqual(before, state.read_bytes() if state.exists() else None)
                    self.assertFalse(list(uploads.glob('.source-upload-*')))

    async def test_partial_writer_is_private_until_success_and_cancel_removes_stage(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, uploads, client):
                    entered, release = asyncio.Event(), asyncio.Event()
                    payload = b'A' * (1024 * 1024) + b'Complete synthetic ending.'
                    class PausedUpload(UploadFile):
                        calls = 0
                        async def read(self, size=-1):
                            self.calls += 1
                            if self.calls == 2:
                                entered.set()
                                await release.wait()
                            return await super().read(size)
                    upload = PausedUpload(filename='book.txt', file=io.BytesIO(payload))
                    task = asyncio.create_task(script.upload_file(upload, select_active=False))
                    try:
                        await asyncio.wait_for(entered.wait(), 3)
                        self.assertEqual([], (await client.get('/api/uploads')).json())
                        response = await client.post('/api/uploads/select', json={'filename': 'book.txt'})
                        self.assertEqual(404, response.status_code, response.text)
                        stages = list(uploads.iterdir())
                        self.assertEqual(1 if os.name == 'nt' else 0, len(stages))
                        self.assertEqual(1024 * 1024, script.get_upload_storage_bytes())
                        for stage in stages:
                            with self.assertRaises(ValueError):
                                script._resolve_batch_script_input(stage.name)
                        if cancel:
                            task.cancel()
                            await asyncio.sleep(.02)
                            self.assertFalse(task.done())
                    finally:
                        release.set()
                        if cancel:
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(task, 3)
                        else:
                            await asyncio.wait_for(task, 3)
                        await upload.close()
                    if cancel:
                        self.assertEqual([], list(uploads.iterdir()))
                    else:
                        self.assertEqual(payload, (uploads / 'book.txt').read_bytes())
                        self.assertEqual(['book.txt'], [p.name for p in uploads.iterdir()])

    async def test_foreign_selection_survives_original_upload_cancel_with_provenance(self):
        for name, data in (('book.txt', b'Complete synthetic text.'), ('book.epub', epub())):
            with self.subTest(name=name):
                async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, uploads, client):
                    entered, release = threading.Event(), threading.Event()
                    publish = script.publish_completed_upload
                    def paused(*args, **kwargs):
                        result = publish(*args, **kwargs)
                        entered.set()
                        if not release.wait(4):
                            raise TimeoutError('publication worker was not released')
                        return result
                    upload = UploadFile(filename=name, file=io.BytesIO(data))
                    with patch.object(script, 'publish_completed_upload', side_effect=paused):
                        task = asyncio.create_task(script.upload_file(upload, select_active=False))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                            before = {p.name: p.read_bytes() for p in uploads.iterdir()}
                            self.assertIn('book.txt', before)
                            if name.endswith('.epub'):
                                self.assertIn('book.txt.publication.json', before)
                            response = await client.post('/api/uploads/select', json={'filename': 'book.txt'})
                            self.assertEqual(200, response.status_code, response.text)
                            task.cancel()
                            await asyncio.sleep(.02)
                            self.assertFalse(task.done())
                        finally:
                            release.set()
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(task, 3)
                            await upload.close()
                    self.assertEqual(before, {p.name: p.read_bytes() for p in uploads.iterdir()})
                    state = json.loads((root / 'state.json').read_bytes())
                    self.assertEqual(str(uploads / 'book.txt'), state['input_file_path'])
                    self.assertTrue(Path(state['input_file_path']).is_file())
