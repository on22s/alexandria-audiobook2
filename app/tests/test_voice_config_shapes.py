import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voices
from tts import active_character_style


class VoiceConfigShapeTests(unittest.TestCase):
    def test_bad_shapes_rejected_by_both_save_routes_without_persisting(self):
        malformed = [{'narrator_strategy': 'invented'}]
        malformed += [{'style_timeline': [{'from_index': value}]} for value in
                      ('bad', -1, True, 1.5, None, {}, [])]
        malformed += [{'style_timeline': [42]},
                      {'style_timeline': [{'character_style': 42}]}]
        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'annotated_script.json'), Path(root, 'voice_config.json')
            script.write_text('[{"speaker":"Hero","text":"Hello"}]')
            config.write_text('{"Hero":{"voice":"Ryan"}}')
            app = FastAPI(); app.include_router(voices.router)
            with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices, 'VOICE_CONFIG_PATH', str(config)), TestClient(app) as client:
                snapshot = client.get('/api/voice_config/snapshot').json()
                before = config.read_bytes()
                for fields in malformed:
                    for route in ('/api/save_voice_config', '/api/voice_config/save'):
                        with self.subTest(fields=fields, route=route):
                            config.write_bytes(before)
                            payload = {'Hero': fields}
                            if route.endswith('/save'):
                                payload = {'voices': payload, 'revision': snapshot['revision'],
                                           'book_token': snapshot['book_token']}
                            response = client.post(route, json=payload)
                            self.assertEqual(422, response.status_code, response.text)
                            self.assertEqual(before, config.read_bytes())

    def test_valid_saved_points_feed_real_tts_and_style_mutations(self):
        with tempfile.TemporaryDirectory() as root:
            script, config = Path(root, 'annotated_script.json'), Path(root, 'voice_config.json')
            script.write_text('[{"speaker":"Hero","text":"Hello"}]')
            config.write_text('{"Hero":{"server_metadata":{"keep":true}}}')
            app = FastAPI(); app.include_router(voices.router)
            with patch.object(voices, 'SCRIPT_PATH', str(script)), \
                 patch.object(voices, 'VOICE_CONFIG_PATH', str(config)), TestClient(app) as client:
                payload = {'Hero': {'character_style': 'base', 'narrator_strategy': 'global',
                    'style_timeline': [{'character_style': 'first', 'note': 'preserved'},
                                       {'from_index': '2', 'character_style': None},
                                       {'from_index': 4, 'character_style': 'older'}]}}
                response = client.post('/api/save_voice_config', json=payload)
                self.assertEqual(200, response.status_code, response.text)
                saved = json.loads(config.read_text())['Hero']
                self.assertEqual({'keep': True}, saved['server_metadata'])
                self.assertEqual('preserved', saved['style_timeline'][0]['note'])
                self.assertEqual([0, 2, 4], [p['from_index'] for p in saved['style_timeline']])
                self.assertEqual(['first', '', 'older'],
                                 [active_character_style(saved, index) for index in (0, 2, 4)])
                response = client.post('/api/voices/Hero/style_timeline',
                                       json={'from_index': 3, 'character_style': 'middle'})
                self.assertEqual(200, response.status_code, response.text)
                response = client.delete('/api/voices/Hero/style_timeline/2')
                self.assertEqual(200, response.status_code, response.text)
                saved = json.loads(config.read_text())['Hero']
                self.assertEqual([0, 3, 4], [p['from_index'] for p in saved['style_timeline']])
                self.assertEqual('middle', active_character_style(saved, 3))

    def test_all_supported_strategies_share_the_same_request_contract(self):
        from typing import get_args
        from pydantic import ValidationError
        for strategy in get_args(voices.NarratorStrategy):
            self.assertEqual(strategy, voices.VoiceConfigItem(narrator_strategy=strategy).narrator_strategy)
            self.assertEqual(strategy, voices.NarratorStrategyRequest(strategy=strategy).strategy)
            self.assertEqual(strategy, voices.NarratorPreviewRequest(strategy=strategy).strategy)
        for model in (voices.NarratorStrategyRequest, voices.NarratorPreviewRequest):
            with self.assertRaises(ValidationError):
                model(strategy='invented')
        self.assertIsNone(voices.VoiceConfigItem(narrator_strategy=None).narrator_strategy)
