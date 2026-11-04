"""Legacy raw-map saves require identity captured with the originating book."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voices


class LegacyVoiceSaveIdentityTests(unittest.TestCase):
    def test_script_api_bound_save_through_http(self):
        from tests import test_api
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, 'voice_config.json')
            app = FastAPI()
            app.include_router(voices.router)
            with patch.object(voices, 'VOICE_CONFIG_PATH', str(config)), TestClient(app) as client, \
                 patch.object(test_api, 'get', client.get), patch.object(test_api, 'post', client.post):
                test_api.test_save_voice_config()
            saved = json.loads(config.read_text())[f'{test_api.TEST_PREFIX}voice']
            self.assertEqual('cheerful', saved['character_style'])
            self.assertEqual('42', saved['seed'])

    def test_rejected_saves_preserve_config_and_audio(self):
        for scenario in ('delayed_unbound', 'delayed_bound', 'absent', 'revision_only',
                         'token_only', 'stale_revision'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = root / 'annotated_script.json'
                config = root / 'voice_config.json'
                chunks = root / 'chunks.json'
                audio = root / 'chunk.wav'
                script.write_text('[{"speaker":"ALICE","text":"Book A synthetic line"}]')
                config.write_text('{"ALICE":{"voice":"Ryan","seed":"1"}}')
                chunks.write_text('[{"speaker":"ALICE","text":"Synthetic","audio_path":"chunk.wav","status":"done"}]')
                audio.write_bytes(b'synthetic audio sentinel')
                app = FastAPI()
                app.include_router(voices.router)
                with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                     patch.object(voices, 'VOICE_CONFIG_PATH', str(config)), TestClient(app) as client:
                    snapshot = client.get('/api/voice_config/snapshot').json()
                    params = {key: snapshot[key] for key in ('revision', 'book_token')}
                    if scenario.startswith('delayed'):
                        script.write_text('[{"speaker":"ALICE","text":"Book B synthetic line"}]')
                        if scenario == 'delayed_unbound':
                            config.write_text('{"ALICE":{"voice":"Jenny","seed":"2"}}')
                    if scenario in ('absent', 'delayed_unbound'):
                        params = {}
                    elif scenario == 'revision_only':
                        del params['book_token']
                    elif scenario == 'token_only':
                        del params['revision']
                    elif scenario == 'stale_revision':
                        config.write_text('{"ALICE":{"voice":"Jenny","seed":"1"}}')
                    before = {p: p.read_bytes() for p in (script, config, chunks, audio)}
                    response = client.post('/api/save_voice_config', params=params,
                                           json={'ALICE': {'voice': 'Aiden'}})
                    self.assertEqual(before, {p: p.read_bytes() for p in before})
                    self.assertEqual(409, response.status_code, response.text)
                    if scenario == 'delayed_bound':
                        self.assertIn('Active book changed', response.json()['detail'])
                    if not params or len(params) == 1:
                        self.assertIn('/api/voice_config/snapshot', response.json()['detail'])
                        self.assertIn('/api/voice_config/save', response.json()['detail'])
