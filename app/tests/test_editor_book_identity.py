"""Synthetic API regressions preserve bytes when a captured target changes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from project import ProjectManager
from routers import editor
from tests import test_voice_library_transactions as transaction_tests


class EditorBookIdentityTests(unittest.TestCase):
    def test_stale_draft_after_delete_or_book_switch_preserves_all_chunks(self):
        for change in ('delete', 'book', 'generation'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pm = ProjectManager(tmp)
                pm.save_chunks([{'id':i, 'text':f'Synthetic row {i}', 'speaker':'Hero',
                                 'status':'pending'} for i in range(3)])
                state = root / 'state.json'
                state.write_text(json.dumps({'active_book_id':'A', 'book_generation':'one'}))
                api = FastAPI()
                api.include_router(editor.router)
                with patch.object(editor, 'project_manager', pm), patch.object(editor, 'SCRIPT_PATH',
                        str(root / 'annotated_script.json')), patch.object(editor, 'check_global_gpu_lock'), TestClient(api) as client:
                    snapshot = client.get('/api/chunks/status').json()
                    identity = {'expected_book_token':snapshot['book_token'],
                                'expected_uid':snapshot['chunks'][1]['uid']}
                    if change == 'delete':
                        self.assertEqual(200, client.delete('/api/chunks/0').status_code)
                    else:
                        state.write_text(json.dumps({'active_book_id':'B' if change == 'book' else 'A',
                                                     'book_generation':'two'}))
                    before = (root / 'chunks.json').read_bytes()
                    response = client.post('/api/chunks/1', params=identity, json={'text':'Stale draft'})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(before, (root / 'chunks.json').read_bytes())
                    fresh = client.get('/api/chunks/status').json()
                    identity = {'expected_book_token':fresh['book_token'], 'expected_uid':fresh['chunks'][1]['uid']}
                    response = client.post('/api/chunks/1', params=identity, json={'text':'Current draft'})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual('Current draft', pm.load_chunks()[1]['text'])
                    before = (root / 'chunks.json').read_bytes()
                    self.assertEqual(422, client.post('/api/chunks/1', json={'text':'Unbound draft'}).status_code)
                    self.assertEqual(before, (root / 'chunks.json').read_bytes())

    def test_cast_match_book_generation_and_source_fence_current_apply(self):
        for change in ('book', 'generation', 'source'):
            with self.subTest(change=change), transaction_tests.VoiceLibraryTransactionTests().fixture() as (root, api), TestClient(api) as client:
                matched = client.post('/api/voice_library/match', json={'name':'series'}).json()
                payload = {'cast':'series', 'mapping':{'Hero':'hero'}, 'book_token':matched['book_token']}
                if change == 'source':
                    (root / 'annotated_script.json').write_text(json.dumps([{'speaker':'Hero','text':'Changed source.'}]))
                else:
                    (root / 'state.json').write_text(json.dumps({'active_book_id':
                        'B' if change == 'book' else 'active-book', 'book_generation':'new'}))
                (root / 'voices.json').write_text(json.dumps({'Hero':{'type':'custom','voice':'B-approved'}}))
                before = (root / 'voices.json').read_bytes()
                library_before = (root / 'library.json').read_bytes()
                response = client.post('/api/voice_library/apply', json=payload)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, (root / 'voices.json').read_bytes())
                self.assertEqual(library_before, (root / 'library.json').read_bytes())
                payload.pop('book_token')
                self.assertEqual(422, client.post('/api/voice_library/apply', json=payload).status_code)
                payload['book_token'] = client.post('/api/voice_library/match', json={'name':'series'}).json()['book_token']
                self.assertEqual(200, client.post('/api/voice_library/apply', json=payload).status_code)
                self.assertEqual('old', json.loads((root / 'voices.json').read_text())['Hero']['voice'])
