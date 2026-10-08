"""Native config reads tolerate malformed state without rewriting it."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import system


class ConfigStateShapeTests(unittest.TestCase):
    def test_invalid_top_level_and_input_path_shapes_keep_config_available_and_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root); config = directory / 'config.json'; state = directory / 'state.json'
            config.write_text('{}'); original_config = config.read_bytes()
            app = FastAPI(); app.include_router(system.router)
            with patch.object(system, 'DATA_DIR', root), patch.object(system, 'CONFIG_PATH', str(config)), TestClient(app, raise_server_exceptions=False) as client:
                for value in [[], None, 'synthetic', 3, True, {'input_file_path': []}, {'input_file_path': {}}, {'input_file_path': 1}, {'input_file_path': True}]:
                    with self.subTest(value=value):
                        state.write_text(json.dumps(value)); before = state.read_bytes()
                        response = client.get('/api/config')
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertIsNone(response.json()['current_file'])
                        self.assertEqual(state.read_bytes(), before)
                        self.assertEqual(config.read_bytes(), original_config)
                        self.assertFalse(list(directory.glob('*.corrupt*')))
                source = directory / 'selected.txt'; source.write_text('Synthetic source')
                state.write_text(json.dumps({'input_file_path': str(source)}))
                current = client.get('/api/config')
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.json()['current_file'], 'selected.txt')
                state.write_text(json.dumps({'input_file_path': str(directory / 'missing.txt')}))
                self.assertIsNone(client.get('/api/config').json()['current_file'])
