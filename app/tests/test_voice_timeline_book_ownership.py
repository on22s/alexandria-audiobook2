"""Real version and timeline routes refuse stale book tokens before publication."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
import httpx
from routers import voices


class VoiceTimelineBookOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_version_save_timeline_save_and_clear_preserve_replacement_book(self):
        with tempfile.TemporaryDirectory() as root:
            script = Path(root, 'annotated_script.json'); config = Path(root, 'voice_config.json')
            script.write_text('[{"speaker":"Alice","text":"Book A"}]')
            config.write_text(json.dumps({'Alice': {'versions': {'adult': {'type': 'custom', 'voice': 'Ryan'}}, 'version_timeline': [{'from_index': 2, 'version_id': 'adult'}]}}))
            app = FastAPI(); app.include_router(voices.router)
            with patch.object(voices, 'SCRIPT_PATH', str(script)), patch.object(voices, 'VOICE_CONFIG_PATH', str(config)):
                token = voices._ensure_voice_snapshot()['book_token']
                script.write_text('[{"speaker":"Alice","text":"Book B"}]')
                before = config.read_bytes()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                    for method, path, body in [
                        ('POST', '/api/voices/Alice/versions', {'version_id': 'new', 'config': {'type': 'custom', 'voice': 'Serena'}, 'book_token': token}),
                        ('POST', '/api/voices/Alice/version_timeline', {'points': [{'from_index': 9, 'version_id': 'adult'}], 'book_token': token}),
                        ('DELETE', '/api/voices/Alice/version_timeline?book_token=' + token, None),
                    ]:
                        with self.subTest(path=path):
                            response = await client.request(method, path, json=body)
                            self.assertEqual(response.status_code, 409, response.text)
                            self.assertEqual(config.read_bytes(), before)
                    fresh = voices._ensure_voice_snapshot()['book_token']
                    saved = await client.post('/api/voices/Alice/version_timeline', json={'points': [{'from_index': 9, 'version_id': 'adult'}], 'book_token': fresh})
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertEqual(json.loads(config.read_text())['Alice']['version_timeline'], [{'from_index': 9, 'version_id': 'adult'}])
                    cleared = await client.delete('/api/voices/Alice/version_timeline?book_token=' + fresh)
                    self.assertEqual(cleared.status_code, 200, cleared.text)
                    self.assertNotIn('version_timeline', json.loads(config.read_text())['Alice'])
