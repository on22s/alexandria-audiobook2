"""Malformed versions containers + canonical-null entry: clear 4xx refusals
at every shared mutation seam (#1724 v28). Actual HTTP, synthetic temp
book only; refusals must write nothing and invalidate nothing."""
from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import voices as v


class VersionRouteRefusalTests(unittest.TestCase):
    def _client(self, root, stack):
        script = root / 'annotated_script.json'
        config = root / 'voice_config.json'
        state = root / 'state.json'
        script.write_text(json.dumps([{'speaker': 'Hero', 'text': 'Synthetic test.'}]))
        state.write_text(json.dumps({'active_book_id': 'book', 'book_generation': 'one'}))
        stack.enter_context(patch.object(v, 'SCRIPT_PATH', str(script)))
        stack.enter_context(patch.object(v, 'VOICE_CONFIG_PATH', str(config)))
        app = FastAPI()
        app.include_router(v.router)
        return stack.enter_context(TestClient(app, raise_server_exceptions=False)), script, config, state

    def _snapshot_guards(self, client):
        snap = client.get('/api/voice_config/snapshot')
        assert snap.status_code == 200, snap.text
        return snap.json()

    def test_malformed_versions_routes_refuse_400_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, script, config, state = self._client(Path(tmp), stack)
            for versions in ([], ['bad']):
                with self.subTest(versions=versions):
                    config.write_text(json.dumps(
                        {'Hero': {'type': 'custom', 'voice': 'Serena', 'versions': versions}}))
                    guards = self._snapshot_guards(client)
                    before = {p: p.read_bytes() for p in (script, config, state)}
                    calls = [
                        ('timeline', lambda: client.post(
                            '/api/voices/Hero/version_timeline',
                            json={'points': [], 'book_token': guards['book_token']})),
                        ('create', lambda: client.post(
                            '/api/voices/Hero/versions',
                            json={'version_id': 'v1', 'age_group': 'adult',
                                  'book_token': guards['book_token']})),
                        ('select', lambda: client.post(
                            '/api/voices/Hero/versions/v1/select',
                            json={'book_token': guards['book_token']})),
                        ('remove', lambda: client.request(
                            'DELETE', '/api/voices/Hero/versions/v1',
                            params={'book_token': guards['book_token']})),
                    ]
                    for label, call in calls:
                        with self.subTest(route=label):
                            response = call()
                            self.assertEqual(400, response.status_code, response.text)
                            self.assertIn('malformed voice versions', response.json()['detail'])
                            self.assertEqual(before, {p: p.read_bytes() for p in (script, config, state)},
                                             f'{label} refusal must not write')

    def test_valid_versions_controls_still_work(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, script, config, state = self._client(Path(tmp), stack)
            for versions in (None, {}):
                with self.subTest(versions=versions):
                    entry = {'type': 'custom', 'voice': 'Serena'}
                    if versions is not None:
                        entry['versions'] = versions
                    config.write_text(json.dumps({'Hero': entry}))
                    guards = self._snapshot_guards(client)
                    created = client.post('/api/voices/Hero/versions', json={
                        'version_id': 'v1', 'age_group': 'adult',
                        'book_token': guards['book_token']})
                    self.assertEqual(200, created.status_code, created.text)
                    selected = client.post('/api/voices/Hero/versions/v1/select', json={
                        'book_token': guards['book_token']})
                    # Select uses the pre-create revision, so accept the
                    # canonical CAS refusal OR a fresh-guard success.
                    self.assertIn(selected.status_code, (200, 409), selected.text)
                    guards = self._snapshot_guards(client)
                    timeline = client.post('/api/voices/Hero/version_timeline', json={
                        'points': [], 'book_token': guards['book_token']})
                    self.assertEqual(200, timeline.status_code, timeline.text)

    def test_valid_dict_history_preserved_through_timeline(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, script, config, state = self._client(Path(tmp), stack)
            history = {'v1': {'type': 'custom', 'voice': 'Jenny', 'seed': '1'}}
            config.write_text(json.dumps(
                {'Hero': {'type': 'custom', 'voice': 'Serena', 'versions': history}}))
            guards = self._snapshot_guards(client)
            response = client.post('/api/voices/Hero/version_timeline', json={
                'points': [{'from_index': 0, 'version_id': 'v1'}],
                'book_token': guards['book_token']})
            self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(config.read_bytes())
            self.assertEqual(history, saved['Hero']['versions'])

    def test_null_entry_mutations_refuse_422_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, script, config, state = self._client(Path(tmp), stack)
            config.write_text(json.dumps({'Hero': None}))
            guards = self._snapshot_guards(client)
            before = {p: p.read_bytes() for p in (script, config, state)}
            calls = [
                ('approval', lambda: client.post('/api/voices/Hero/approval', json={
                    'persona_status': 'approved', 'book_token': guards['book_token']})),
                ('create', lambda: client.post('/api/voices/Hero/versions', json={
                    'version_id': 'v1', 'age_group': 'adult',
                    'book_token': guards['book_token']})),
                ('timeline', lambda: client.post('/api/voices/Hero/version_timeline', json={
                    'points': [], 'book_token': guards['book_token']})),
            ]
            for label, call in calls:
                with self.subTest(route=label):
                    response = call()
                    self.assertEqual(422, response.status_code, response.text)
                    self.assertIn('no voice configured', response.json()['detail'])
                    self.assertEqual(before, {p: p.read_bytes() for p in (script, config, state)},
                                     f'{label} refusal must not write/fabricate')

    def test_absent_entry_approval_and_explicit_save_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, script, config, state = self._client(Path(tmp), stack)
            config.write_text('{}')
            guards = self._snapshot_guards(client)
            approved = client.post('/api/voices/Hero/approval', json={
                'persona_status': 'approved', 'book_token': guards['book_token']})
            self.assertEqual(200, approved.status_code, approved.text)
            saved = json.loads(config.read_bytes())
            self.assertEqual('approved', saved['Hero']['persona_status'])
            # Explicit ordinary assignment on a null entry still works.
            config.write_text(json.dumps({'Hero': None}))
            guards = self._snapshot_guards(client)
            assigned = client.post('/api/voice_config/save', json={
                'voices': {'Hero': {'type': 'custom', 'voice': 'Serena'}},
                'revision': guards['revision'], 'book_token': guards['book_token']})
            self.assertEqual(200, assigned.status_code, assigned.text)
            saved = json.loads(config.read_bytes())
            self.assertEqual('Serena', saved['Hero']['voice'])


if __name__ == '__main__':
    unittest.main()
