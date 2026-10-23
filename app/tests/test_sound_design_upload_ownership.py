"""Real upload leases, event-loop responsiveness and cancellation boundaries."""
import asyncio
from contextlib import asynccontextmanager
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, UploadFile
import httpx
import numpy as np
import soundfile as sf

from routers import sound_design as routes
from sound_design_assets import SoundDesignAssetStore
import sound_design_decode
import sound_design_uploads as uploads
from utils import file_lock


class SoundDesignUploadOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_while_waiting_for_staging_lease_preserves_holder(self):
        async with self.fixture() as (root, raw, _client):
            staging = root / uploads.STAGING_DIRECTORY
            staging.mkdir()
            retained = staging / '.source-upload-retained.part'
            retained.write_bytes(b'owned by another import')
            waiting = threading.Event()
            from sound_design_assets import ensure_sound_asset_lease
            def acquire(*args, **kwargs):
                waiting.set()
                return ensure_sound_asset_lease(*args, **kwargs)
            request = AsyncMock(); request.is_disconnected.return_value = False
            stream = io.BytesIO(raw)
            with file_lock(str(staging / 'quota')), patch.object(uploads, 'ensure_sound_asset_lease', side_effect=acquire):
                task = asyncio.create_task(routes.import_sound_asset(request,
                    UploadFile(filename='effect.wav', file=stream)))
                try:
                    self.assertTrue(await asyncio.to_thread(waiting.wait, 2))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(task), 1)
                    self.assertTrue(task.done(), 'Cancelled waiter retained its worker')
                    self.assertEqual(0, stream.tell())
                    self.assertEqual(b'owned by another import', retained.read_bytes())
                    with self.assertRaises(TimeoutError), file_lock(str(staging / 'quota'), timeout=0):
                        self.fail('Waiter released another import lease')
                finally:
                    # Keep the holder until assertions finish, then allow cleanup
                    # if a regression leaves the worker waiting for the lease.
                    task.cancel()
            if not task.done():
                with self.assertRaises(asyncio.CancelledError):
                    await task

    @asynccontextmanager
    async def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = io.BytesIO()
            sf.write(source, np.zeros(24001), 24000, format='WAV', subtype='PCM_16')
            raw = source.getvalue()
            api = FastAPI(); api.include_router(routes.router)
            @api.get('/fixture/ping')
            async def ping():
                return {'alive': True}
            with patch.object(routes, 'DATA_DIR', str(root)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(api), base_url='http://fixture') as client:
                    yield root, raw, client

    async def test_slow_real_import_keeps_event_loop_free_and_staging_lease_owned(self):
        async with self.fixture() as (root, raw, client):
            entered, release = threading.Event(), threading.Event()
            decode = sound_design_decode.decode_sound_design_asset
            worker_threads = []
            def paused(*args, **kwargs):
                worker_threads.append(threading.get_ident()); entered.set()
                if not release.wait(5):
                    raise AssertionError('Import worker not released')
                return decode(*args, **kwargs)
            with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=paused):
                task = asyncio.create_task(client.post('/api/sound_design/assets/import',
                    files={'file': ('effect.wav', raw, 'audio/wav')}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    response = await asyncio.wait_for(client.get('/fixture/ping'), 1)
                    self.assertEqual({'alive': True}, response.json())
                    with self.assertRaises(TimeoutError), file_lock(str(root/uploads.STAGING_DIRECTORY/'quota'), timeout=0):
                        self.fail('Live staging lease was released')
                finally:
                    release.set()
                    result = await task
            self.assertEqual(200, result.status_code, result.text)
            self.assertTrue(all(value != threading.get_ident() for value in worker_threads))

    async def test_repeated_cancel_waits_for_decoder_then_retains_incomplete_record(self):
        async with self.fixture() as (root, raw, _client):
            entered, release = threading.Event(), threading.Event()
            decode = sound_design_decode.decode_sound_design_asset
            def paused(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Import worker not released')
                return decode(*args, **kwargs)
            request = AsyncMock(); request.is_disconnected.return_value = False
            file = UploadFile(filename='effect.wav', file=io.BytesIO(raw))
            with patch.object(sound_design_decode, 'decode_sound_design_asset', side_effect=paused):
                task = asyncio.create_task(routes.import_sound_asset(request, file))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    for _ in range(2):
                        task.cancel(); await asyncio.sleep(.03)
                        self.assertFalse(task.done(), 'Cancellation abandoned the import worker')
                    self.assertEqual(1, len(list((root/uploads.STAGING_DIRECTORY).glob('*.part'))))
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            store = SoundDesignAssetStore(root)
            row = store.list_assets()['assets'][0]
            self.assertEqual('incomplete', row['state'])
            self.assertEqual(raw, (store.directory/(row['asset_id']+'.source')).read_bytes())
            self.assertFalse(list((root/uploads.STAGING_DIRECTORY).glob('*.part')))
            self.assertFalse(file.file.closed, 'Worker closed the request-owned input')

    async def test_cancel_after_ready_publication_retains_complete_reusable_asset(self):
        async with self.fixture() as (root, raw, _client):
            entered, release = threading.Event(), threading.Event()
            save = SoundDesignAssetStore._save
            def paused(store, document):
                save(store, document)
                if document['assets'][-1]['state'] == 'ready':
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Publication worker not released')
            request = AsyncMock(); request.is_disconnected.return_value = False
            with patch.object(SoundDesignAssetStore, '_save', paused):
                task = asyncio.create_task(routes.import_sound_asset(request,
                    UploadFile(filename='effect.wav', file=io.BytesIO(raw))))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    task.cancel(); await asyncio.sleep(.03)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            store = SoundDesignAssetStore(root)
            row = store.list_assets()['assets'][0]
            with store.open_canonical(row['asset_id']) as (stream, _):
                self.assertEqual(24001 * 4, len(stream.read()))
            self.assertFalse(list((root/uploads.STAGING_DIRECTORY).glob('*.part')))


class SoundDesignUploadQuotaTests(unittest.TestCase):
    def test_crash_stages_count_toward_budget_and_failed_copy_preserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); staging = root/uploads.STAGING_DIRECTORY; staging.mkdir()
            retained = staging/'.source-upload-retained.part'; retained.write_bytes(b'x'*32)
            with patch.object(uploads, 'MAX_STAGING_BYTES', 64), patch.object(SoundDesignAssetStore, 'import_asset') as admit:
                with self.assertRaisesRegex(ValueError, 'allowance'):
                    uploads.import_sound_asset_upload(root, io.BytesIO(b'y'*33))
                admit.assert_not_called()
            self.assertEqual(b'x'*32, retained.read_bytes())
            self.assertEqual([retained], list(staging.glob('*.part')))

    def test_full_or_count_exhausted_staging_refuses_before_reading_upload(self):
        for kind in ('bytes', 'count'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); staging = root/uploads.STAGING_DIRECTORY; staging.mkdir()
                (staging/'.source-upload-retained.part').write_bytes(b'x'*64)
                stream = io.BytesIO(b'new')
                with patch.object(uploads, 'MAX_STAGING_BYTES', 64 if kind == 'bytes' else 128), \
                        patch.object(uploads, 'MAX_STAGES', 1 if kind == 'count' else 16):
                    with self.assertRaisesRegex(ValueError, 'full'):
                        uploads.import_sound_asset_upload(root, stream)
                self.assertEqual(0, stream.tell())
