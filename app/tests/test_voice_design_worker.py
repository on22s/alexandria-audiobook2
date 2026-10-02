import asyncio
import copy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
import core
from routers import voice_design
from tts import UnsupportedVoiceBackendError
from tests import test_chapter_export as fixtures


class VoiceDesignWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = copy.deepcopy(core.process_state)
        for row in self.state.values(): row['running'] = False
        for context in (patch.object(core, 'process_state', self.state),
                        patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}),
                        patch.object(core, 'DATA_DIR', str(self.root)),
                        patch.object(core, 'acquire_gpu_lock', return_value=None),
                        patch.object(core, 'llm_is_on_this_gpu', return_value=True)):
            context.start(); self.addCleanup(context.stop)
        self.addCleanup(self.release_claims)

    def release_claims(self):
        for name, owner in list(core._task_claims.items()): core.release_gpu_task_claim(name, owner['id'])

    def request(self):
        return voice_design.VoiceDesignPreviewRequest(description='warm and measured', sample_text='Hello there.')

    def test_initialization_and_generation_are_owned_worker_operations(self):
        for stage in ('initialization', 'generation'):
            with self.subTest(stage=stage):
                arrived, release = threading.Event(), threading.Event()
                thread_ids, claims = [], []
                output = self.root / f'{stage}.wav'
                def pause():
                    thread_ids.append(threading.get_ident())
                    owner = core._task_claims.get('voice_design')
                    claims.append({key: owner[key] for key in ('id', 'phase')} if owner else None)
                    arrived.set()
                    if not release.wait(3): raise TimeoutError('provider fixture not released')
                def generate(**kwargs):
                    self.assertEqual('warm and measured', kwargs['description'])
                    if stage == 'generation': pause()
                    fixtures._tone(str(output), 0.1)
                    return str(output), 24000
                engine = SimpleNamespace(generate_voice_design=generate)
                def initialize():
                    if stage == 'initialization': pause()
                    return engine
                app = FastAPI(); app.include_router(voice_design.router)
                @app.get('/ping')
                async def ping(): return {'ok': True}
                async def run():
                    loop_id = threading.get_ident()
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                        task = asyncio.create_task(client.post('/api/voice_design/preview', json=self.request().model_dump()))
                        watchdog = threading.Timer(0.5, release.set); watchdog.start()
                        try:
                            self.assertTrue(await asyncio.to_thread(arrived.wait, 2))
                            self.assertEqual(200, (await client.get('/ping')).status_code)
                            self.assertFalse(release.is_set())
                            self.assertFalse(task.done())
                            self.assertTrue(all(identity != loop_id for identity in thread_ids))
                            self.assertEqual('started', claims[0]['phase'])
                            with self.assertRaises(HTTPException): core.claim_gpu_task('audio')
                        finally:
                            release.set(); watchdog.cancel(); response = await task
                        self.assertEqual(200, response.status_code, response.text)
                        self.assertEqual('/designed_voices/previews/' + output.name, response.json()['audio_url'])
                with patch.object(voice_design.project_manager, 'get_engine', side_effect=initialize): asyncio.run(run())
                with wave.open(str(output), 'rb') as clip:
                    self.assertEqual(2400, clip.getnframes())
                    self.assertEqual(24000, clip.getframerate())
                self.assertFalse(self.state['voice_design']['running'])
                self.assertNotIn('voice_design', core._task_claims)

    def test_cancelled_request_keeps_started_provider_claim_until_real_output_finishes(self):
        arrived, release, finished = threading.Event(), threading.Event(), threading.Event()
        output = self.root / 'cancelled.wav'
        def generate(**kwargs):
            arrived.set()
            if not release.wait(3): raise TimeoutError('provider fixture not released')
            fixtures._tone(str(output), 0.1); finished.set()
            return str(output), 24000
        async def run():
            task = asyncio.create_task(voice_design.voice_design_preview(self.request()))
            watchdog = threading.Timer(0.5, release.set); watchdog.start()
            try:
                self.assertTrue(await asyncio.to_thread(arrived.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                self.assertEqual('started', core._task_claims['voice_design']['phase'])
                with self.assertRaises(HTTPException): core.claim_gpu_task('audio')
            finally:
                release.set(); watchdog.cancel()
                await asyncio.to_thread(finished.wait, 2)
                deadline = time.monotonic() + 2
                while 'voice_design' in core._task_claims and time.monotonic() < deadline:
                    await asyncio.sleep(0.001)
            self.assertNotIn('voice_design', core._task_claims)
            self.assertFalse(self.state['voice_design']['running'])
        with patch.object(voice_design.project_manager, 'get_engine', return_value=SimpleNamespace(generate_voice_design=generate)):
            asyncio.run(run())
        with wave.open(str(output), 'rb') as clip:
            self.assertEqual(2400, clip.getnframes())
            self.assertEqual(24000, clip.getframerate())

    def test_failure_translation_and_conflict_preserve_claim_lifetime(self):
        for failure, expected in ((UnsupportedVoiceBackendError('fixture backend unsupported'), 400),
                                  (RuntimeError('fixture provider failed'), 500)):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(voice_design.project_manager, 'get_engine', return_value=SimpleNamespace(
                     generate_voice_design=lambda **kwargs: (_ for _ in ()).throw(failure))):
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(voice_design.voice_design_preview(self.request()))
                self.assertEqual(expected, error.exception.status_code)
                self.assertNotIn('voice_design', core._task_claims)
                self.assertFalse(self.state['voice_design']['running'])
        owner = core.claim_gpu_task('audio')
        try:
            with patch.object(voice_design.project_manager, 'get_engine') as initialize:
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(voice_design.voice_design_preview(self.request()))
                self.assertEqual(400, error.exception.status_code)
                initialize.assert_not_called()
                self.assertEqual(owner, core._task_claims['audio']['id'])
        finally:
            core.release_gpu_task_claim('audio', owner)
