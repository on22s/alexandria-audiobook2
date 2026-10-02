"""Reject oversized request collections/content before work or publication."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script


class ScriptRequestSizeTests(unittest.TestCase):
    def test_oversized_batch_models_stop_before_gpu_admission_or_preflight(self):
        limit = getattr(script, 'MAX_SCRIPT_BATCH_ITEMS', 1000)
        tasks = [{'filename': 'source.txt'}] * (limit + 1)
        cases = [('/api/review_script/batch/start', {'script_names': ['book'] * (limit + 1)}),
                 ('/api/generate_script/batch/start', {'tasks': tasks}),
                 ('/api/generate_script/batch/preflight', {'tasks': tasks})]
        app = FastAPI()
        app.include_router(script.router)
        with patch.object(script, 'check_global_gpu_lock', side_effect=AssertionError('oversized batch reached GPU admission')), \
             patch.object(script, '_resolve_batch_script_input', side_effect=AssertionError('oversized batch reached preflight')), \
             TestClient(app) as client:
            for url, payload in cases:
                with self.subTest(url=url):
                    response = client.post(url, json=payload)
                    self.assertEqual(422, response.status_code, response.text)
                    self.assertEqual('too_long', response.json()['detail'][0]['type'])

    def test_exact_batch_boundary_preserves_order_duplicates_and_task_settings(self):
        count = getattr(script, 'MAX_SCRIPT_BATCH_ITEMS', 1000)
        names = [f'book{i % 3}' for i in range(count)]
        tasks = [{'filename': f'{i}.txt', 'first_person_narrator': 'ELENA'} for i in range(count)]
        self.assertEqual(names, script.BatchReviewRequest(script_names=names).script_names)
        request = script.BatchScriptRequest(tasks=tasks, collision_policy='version', strip_front_matter=False)
        self.assertEqual(tasks, [task.model_dump() for task in request.tasks])
        self.assertEqual('version', request.collision_policy)
        self.assertFalse(request.strip_front_matter)

    def test_manual_content_above_boundary_never_overwrites_response_file(self):
        count = getattr(script, 'MAX_MANUAL_REPLY_CHARACTERS', 2 * 1024 * 1024)
        app = FastAPI()
        app.include_router(script.router)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(script.manual_llm_dir(tmp))
            root.mkdir(parents=True)
            (root / 'pending.json').write_text(json.dumps({'id': 'pending', 'sequence': 1}))
            response_file = root / 'response.json'
            response_file.write_bytes(b'original recovery bytes')
            with patch.object(script, 'DATA_DIR', tmp), TestClient(app) as client:
                response = client.post('/api/manual_llm/response', json={'id': 'pending', 'content': 'x' * (count + 1)})
                self.assertEqual(422, response.status_code, response.text[:200])
                self.assertEqual(b'original recovery bytes', response_file.read_bytes())
                # Unicode is counted as characters, not accidentally truncated bytes.
                content = 'あ' * count
                response = client.post('/api/manual_llm/response', json={'id': 'pending', 'content': content})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual({'accepted': True, 'sequence': 1}, response.json())
                self.assertEqual(content, json.loads(response_file.read_text())['content'])
