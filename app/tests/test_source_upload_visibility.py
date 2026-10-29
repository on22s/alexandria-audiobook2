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
