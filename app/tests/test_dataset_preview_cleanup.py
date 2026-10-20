"""Dataset generation retires only its returned preview after atomic copying."""
import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import dataset_builder as routes


class DatasetPreviewCleanupTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            previews = root / 'previews'
            previews.mkdir()
            bystander = previews / 'saved-preview.wav'
            bystander.write_bytes(b'another owner')
            work = root / 'builder/voice'
            work.mkdir(parents=True)
            (work / 'state.json').write_text(json.dumps({'samples': [
                {'text': 'Original line.', 'status': 'pending'}]}))
            generated = []
            def render(**kwargs):
                path = previews / f'preview_{len(generated)}.wav'
                with wave.open(str(path), 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(b'\0\x10' * 240)
                generated.append((path, path.read_bytes(), kwargs))
                return str(path), 24000
            states = {'dataset_builder': {'running': False, 'cancel': False, 'logs': []}}
            def claim(name, *, resources):
                states[name]['running'] = True
                return 'owned-fixture'
            def release(name, token, **kwargs):
                self.assertEqual('owned-fixture', token)
                states[name]['running'] = False
            def start(name, token, worker):
                self.assertEqual('owned-fixture', token)
                try:
                    worker()
                finally:
                    release(name, token)
            for name, value in (('DATASET_BUILDER_DIR', str(work.parent)), ('process_state', states),
                    ('check_global_gpu_lock', lambda name: None), ('claim_gpu_task', claim),
                    ('reserve_background_task', claim), ('release_gpu_task_claim', release),
                    ('start_claimed_task_thread', start)):
                stack.enter_context(patch.object(routes, name, value))
            stack.enter_context(patch.object(routes.project_manager, 'get_engine',
                return_value=SimpleNamespace(generate_voice_design=render)))
            api = FastAPI()
            api.include_router(routes.router)
            with TestClient(api) as client:
                yield root, work, bystander, generated, states, client

    def generate(self, client, mode):
        if mode == 'single':
            return client.post('/api/dataset_builder/generate_sample', json={
                'dataset_name': 'voice', 'sample_index': 0,
                'description': 'Warm', 'text': 'New line.', 'seed': 42})
        return client.post('/api/dataset_builder/generate_batch', json={
            'name': 'voice', 'description': 'Warm', 'samples': [{'text': 'New line.'}], 'global_seed': 42})

    def test_single_and_batch_regeneration_retire_previews_and_delete_keeps_other_owners(self):
        for mode in ('single', 'batch'):
            with self.subTest(mode=mode), self.fixture() as (_root, work, bystander, generated, states, client):
                for _ in range(2):
                    response = self.generate(client, mode)
                    self.assertEqual(200, response.status_code, response.text)
                    path, audio, kwargs = generated[-1]
                    self.assertFalse(path.exists())
                    self.assertEqual(audio, (work / 'sample_000.wav').read_bytes())
                    self.assertEqual(42, kwargs['seed'])
                    self.assertEqual('done', routes._load_builder_state('voice')['samples'][0]['status'])
                    self.assertFalse(states['dataset_builder']['running'])
                self.assertEqual(200, client.delete('/api/dataset_builder/voice').status_code)
                self.assertFalse(work.exists())
                self.assertEqual(b'another owner', bystander.read_bytes())
                self.assertEqual([bystander], list(bystander.parent.iterdir()))

    def test_copy_failure_keeps_prior_sample_and_retires_only_returned_preview(self):
        for mode in ('single', 'batch'):
            with self.subTest(mode=mode), self.fixture() as (_root, work, bystander, generated, states, client):
                destination = work / 'sample_000.wav'
                destination.write_bytes(b'previous complete audio')
                with patch.object(routes.shutil, 'copy2', side_effect=OSError('fixture copy failed')):
                    response = self.generate(client, mode)
                self.assertEqual(500 if mode == 'single' else 200, response.status_code, response.text)
                self.assertFalse(generated[-1][0].exists())
                self.assertEqual(b'previous complete audio', destination.read_bytes())
                self.assertEqual('error', routes._load_builder_state('voice')['samples'][0]['status'])
                self.assertFalse(states['dataset_builder']['running'])
                self.assertEqual(b'another owner', bystander.read_bytes())
                self.assertFalse(any(path.name.startswith('.sample_') for path in work.iterdir()))

    def test_state_write_failure_still_retires_preview_after_successful_audio_publication(self):
        with self.fixture() as (_root, work, bystander, generated, states, client):
            original = routes._save_builder_state
            failed = False
            def fail_once(name, state):
                nonlocal failed
                if not failed and state['samples'][0]['status'] == 'done':
                    failed = True
                    raise OSError('fixture state publication failed')
                return original(name, state)
            with patch.object(routes, '_save_builder_state', side_effect=fail_once):
                response = self.generate(client, 'single')
            self.assertEqual(500, response.status_code)
            self.assertTrue(failed)
            self.assertFalse(generated[0][0].exists())
            self.assertEqual(generated[0][1], (work / 'sample_000.wav').read_bytes())
            self.assertFalse(states['dataset_builder']['running'])
            self.assertEqual(b'another owner', bystander.read_bytes())

    def test_retirement_failure_warns_without_deleting_good_sample(self):
        with self.fixture() as (_root, work, _bystander, generated, states, client):
            unlink = routes.os.unlink
            def refuse_preview(path, *args, **kwargs):
                if str(path).endswith('preview_0.wav'):
                    raise PermissionError('fixture preview locked')
                return unlink(path, *args, **kwargs)
            with patch.object(routes.os, 'unlink', side_effect=refuse_preview), self.assertLogs(routes.logger, level='WARNING') as logs:
                response = self.generate(client, 'single')
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(generated[0][0].exists())
            self.assertEqual(generated[0][1], (work / 'sample_000.wav').read_bytes())
            self.assertTrue(any('cleanup' in message.lower() for message in logs.output))
            self.assertFalse(states['dataset_builder']['running'])

    def test_engine_returning_destination_never_retires_the_good_sample(self):
        with self.fixture() as (_root, work, _bystander, _generated, _states, client):
            self.assertEqual(200, self.generate(client, 'single').status_code)
            destination = work / 'sample_000.wav'
            before = destination.read_bytes()
            engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(destination), 24000))
            routes.generate_sample_audio(engine, str(destination), description='Warm',
                                          sample_text='New line.', seed=42)
            self.assertEqual(before, destination.read_bytes())
            with patch.object(routes.shutil, 'copy2', side_effect=OSError('fixture copy failed')):
                with self.assertRaisesRegex(OSError, 'fixture copy failed'):
                    routes.generate_sample_audio(engine, str(destination), description='Warm',
                                                  sample_text='New line.', seed=42)
            self.assertEqual(before, destination.read_bytes())
            self.assertFalse(any(path.name.startswith('.sample_') for path in work.iterdir()))
