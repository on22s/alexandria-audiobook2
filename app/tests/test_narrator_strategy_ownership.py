"""Actual narrator writes reject stale book generations before publication."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from routers import voices


class NarratorStrategyOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_token_saves_and_stale_token_preserves_native_config(self):
        with tempfile.TemporaryDirectory() as root:
            script = Path(root, 'annotated_script.json')
            config = Path(root, 'voice_config.json')
            script.write_text(json.dumps([{'speaker': 'NARRATOR', 'text': 'First book'}]))
            config.write_text(json.dumps({'NARRATOR': {'voice': 'Ryan'}, 'Other': {'voice': 'Serena'}}))
            app = FastAPI(); app.include_router(voices.router)
            with patch.object(voices, 'SCRIPT_PATH', str(script)), patch.object(voices, 'VOICE_CONFIG_PATH', str(config)):
                token = voices._ensure_voice_snapshot()['book_token']
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    response = await client.post('/api/narrator/strategy', json={'strategy': 'focus', 'book_token': token})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(json.loads(config.read_text())['NARRATOR']['narrator_strategy'], 'focus')
                    script.write_text(json.dumps([{'speaker': 'NARRATOR', 'text': 'Replacement book'}]))
                    before = config.read_bytes()
                    stale = await client.post('/api/narrator/strategy', json={'strategy': 'chapter', 'book_token': token})
                    self.assertEqual(stale.status_code, 409, stale.text)
                    self.assertEqual(config.read_bytes(), before)
                    fresh = voices._ensure_voice_snapshot()['book_token']
                    self.assertNotEqual(fresh, token)
                    accepted = await client.post('/api/narrator/strategy', json={'strategy': 'chapter', 'book_token': fresh})
                    self.assertEqual(accepted.status_code, 200, accepted.text)
                    self.assertEqual(accepted.json()['revision'], voices.get_voice_config_revision(json.loads(config.read_text())))
                    self.assertEqual(json.loads(config.read_text())['Other'], {'voice': 'Serena'})
