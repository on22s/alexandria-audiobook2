import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from routers import voices
from utils import file_lock


class VoiceMutationEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_entry_mutations_yield_while_a_real_file_lock_is_held(self):
        cases = [
            ('POST', '/api/voices/Hero/versions', {'version_id': 'adult', 'config': {'type': 'custom', 'voice': 'Dylan'}}),
            ('POST', '/api/voices/Hero/versions/teen/select', None),
            ('POST', '/api/voices/Hero/candidates', {'candidate_id': 'new', 'config': {'type': 'custom', 'voice': 'Dylan'}}),
            ('POST', '/api/voices/Hero/candidates/old/select', None),
            ('DELETE', '/api/voices/Hero/candidates/old', None),
            ('POST', '/api/voices/Hero/candidates/old/favorite', {'favorite': True}),
            ('POST', '/api/narrator/strategy', {'strategy': 'focus'}),
            ('POST', '/api/voices/Hero/style_timeline', {'from_index': 7, 'character_style': 'Older and steady'}),
            ('DELETE', '/api/voices/Hero/style_timeline/3', None),
            ('POST', '/api/voices/Hero/approval', {'persona_status': 'reviewed'}),
            ('POST', '/api/voices/Hero/persona-voice-audit', {'suggestion_reason': 'Reviewed source'}),
        ]
        for method, endpoint, payload in cases:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as root:
                script, config = Path(root, 'script.json'), Path(root, 'voice_config.json')
                script.write_text('[{"speaker":"Hero"},{"speaker":"NARRATOR"}]')
                initial = {'Hero': {'type': 'custom', 'voice': 'Ryan',
                           'versions': {'teen': {'type': 'custom', 'voice': 'Serena', 'age_group': 'teen'}},
                           'candidates': [{'candidate_id': 'old', 'type': 'custom', 'voice': 'Serena'}],
                           'style_timeline': [{'from_index': 3, 'character_style': 'Young'}]},
                           'NARRATOR': {'type': 'custom', 'voice': 'Ryan'},
                           'Unrelated': {'type': 'custom', 'voice': 'Serena'}}
                config.write_text(json.dumps(initial))
                old_script = script.read_bytes()
                held, attempted, release = threading.Event(), threading.Event(), threading.Event()
                worker_ids = []

                def holder():
                    with file_lock(config):
                        held.set()
                        release.wait(5)

                def watchdog():
                    attempted.wait(3)
                    release.wait(1)
                    release.set()

                @contextlib.contextmanager
                def blocked_lock(path, *args, **kwargs):
                    worker_ids.append(threading.get_ident())
                    attempted.set()
                    with file_lock(path, *args, **kwargs):
                        yield

                peer, guard = threading.Thread(target=holder), threading.Thread(target=watchdog)
                peer.start(); guard.start()
                app = FastAPI(); app.include_router(voices.router)
                @app.get('/fixture/ping')
                async def ping():
                    return {'ok': True}
                task = None
                try:
                    self.assertTrue(await asyncio.to_thread(held.wait, 3))
                    with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                         patch.object(voices, 'VOICE_CONFIG_PATH', str(config)), \
                         patch.object(voices, 'file_lock', blocked_lock):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                     base_url='http://fixture') as client:
                            task = asyncio.create_task(client.request(method, endpoint, json=payload))
                            self.assertTrue(await asyncio.to_thread(attempted.wait, 3))
                            self.assertFalse(task.done(), 'lock waiting blocked the event loop')
                            self.assertEqual({'ok': True}, (await client.get('/fixture/ping')).json())
                            self.assertFalse(release.is_set(), 'ping only ran after the lock was released')
                            self.assertNotEqual(threading.get_ident(), worker_ids[0])
                            release.set()
                            response = await task
                            self.assertEqual(200, response.status_code, response.text)
                    saved = json.loads(config.read_text())
                    self.assertEqual(initial['Unrelated'], saved['Unrelated'])
                    self.assertNotEqual(initial, saved)
                    self.assertEqual(old_script, script.read_bytes())
                    if 'config' in response.json():
                        self.assertEqual(saved['Hero'], response.json()['config'])
                finally:
                    release.set()
                    if task is not None and not task.done():
                        await task
                    peer.join(5); guard.join(5)
                    self.assertFalse(peer.is_alive()); self.assertFalse(guard.is_alive())

    async def test_speaker_validation_file_read_also_runs_off_the_event_loop(self):
        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'script.json'), Path(root, 'voice_config.json')
            script.write_text('[{"speaker":"Hero"}]'); config.write_text('{}')
            attempted, release = threading.Event(), threading.Event()
            ids, original = [], voices._require_script_speaker

            def read(speaker):
                ids.append(threading.get_ident()); attempted.set()
                if not release.wait(2):
                    raise TimeoutError('validation paused')
                return original(speaker)

            def watchdog():
                attempted.wait(3); release.wait(1); release.set()

            guard = threading.Thread(target=watchdog); guard.start()
            task = None
            try:
                with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                     patch.object(voices, 'VOICE_CONFIG_PATH', str(config)), \
                     patch.object(voices, '_require_script_speaker', side_effect=read):
                    task = asyncio.create_task(voices.set_voice_approval(
                        'Hero', voices.VoiceApprovalRequest(persona_status='reviewed')))
                    self.assertTrue(await asyncio.to_thread(attempted.wait, 3))
                    self.assertFalse(task.done())
                    self.assertNotEqual(threading.get_ident(), ids[0])
                    release.set()
                    self.assertEqual('reviewed', (await task)['persona_status'])
            finally:
                release.set()
                if task is not None and not task.done():
                    await task
                guard.join(5)

    async def test_worker_validation_errors_keep_http_status_and_prior_config_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'script.json'), Path(root, 'voice_config.json')
            script.write_text('[{"speaker":"Hero"}]')
            config.write_text('{"Hero":{"type":"custom","voice":"Ryan"}}')
            before = config.read_bytes()
            app = FastAPI(); app.include_router(voices.router)
            cases = [('POST', '/api/voices/Missing/approval', {'persona_status': 'reviewed'}, 404),
                     ('POST', '/api/voices/Hero/versions/missing/select', None, 404),
                     ('POST', '/api/voices/Hero/candidates/missing/select', None, 404),
                     ('DELETE', '/api/voices/Hero/candidates/missing', None, 404),
                     ('POST', '/api/voices/Hero/approval', {}, 422),
                     ('POST', '/api/voices/Hero/versions', {'version_id': 'bad', 'config': {'type': 'invalid'}}, 422),
                     ('POST', '/api/voices/Hero/persona-voice-audit', {}, 422)]
            with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices, 'VOICE_CONFIG_PATH', str(config)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url='http://fixture') as client:
                    for method, endpoint, payload, status in cases:
                        response = await client.request(method, endpoint, json=payload)
                        self.assertEqual(status, response.status_code, response.text)
                        self.assertEqual(before, config.read_bytes())
