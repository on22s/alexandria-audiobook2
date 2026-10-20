"""A failed library read must never authorize replacing saved user data."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import voice_library as routes


class VoiceLibraryRecoveryTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / 'library.json'
            api = FastAPI()
            api.include_router(routes.router)
            with patch.object(core, 'VOICE_LIBRARY_PATH', str(library)), \
                 patch.object(routes, 'VOICE_LIBRARY_PATH', str(library)), \
                 TestClient(api) as client:
                yield library, client

    def test_damaged_library_refuses_cast_and_favorite_without_replacing_bytes(self):
        damaged = [b'{"casts":{"valuable":{"members":{}}},', b'[]',
                   b'{"casts":[],"shared":{"saved":{}},"favorites":["old"]}',
                   b'{"casts":{"valuable":{}},"shared":[],"favorites":[]}',
                   b'{"casts":{"valuable":{}},"favorites":{}}', b'\xff']
        for original in damaged:
            for endpoint, body in (('/api/voice_library/casts', {'name': 'New cast'}),
                                   ('/api/voice_library/favorites/voice', None)):
                with self.subTest(original=original, endpoint=endpoint), self.fixture() as (library, client):
                    library.write_bytes(original)
                    response = client.post(endpoint, json=body)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertIn('unchanged', response.json()['detail'])
                    self.assertEqual(original, library.read_bytes())

    def test_unreadable_existing_library_refuses_before_mutation(self):
        with self.fixture() as (library, client):
            original = b'{"casts":{"valuable":{"members":{}}}}'
            library.write_bytes(original)
            with patch('core.open', side_effect=PermissionError('read refused')):
                response = client.post('/api/voice_library/casts', json={'name': 'New cast'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(original, library.read_bytes())

    def test_missing_and_legacy_valid_libraries_remain_writable(self):
        for existing in (None, {'casts': {'valuable': {'members': {}}}, 'shared': {}}):
            with self.subTest(existing=existing), self.fixture() as (library, client):
                if existing is not None:
                    library.write_text(json.dumps(existing))
                response = client.post('/api/voice_library/casts', json={'name': 'New cast'})
                self.assertEqual(200, response.status_code, response.text)
                saved = json.loads(library.read_bytes())
                self.assertIn('New cast', saved['casts'])
                self.assertEqual([], saved['favorites'])
                if existing is not None:
                    self.assertEqual(existing['casts']['valuable'], saved['casts']['valuable'])

    def test_read_only_recovery_keeps_existing_fallback_without_changing_bytes(self):
        with self.fixture() as (library, _client):
            original = b'{"casts":{"valuable":'
            library.write_bytes(original)
            self.assertEqual({'shared': {}, 'casts': {}, 'favorites': []}, core._load_voice_library())
            self.assertEqual(original, library.read_bytes())
