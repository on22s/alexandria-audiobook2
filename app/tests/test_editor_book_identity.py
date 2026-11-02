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
    def test_gap_suggestion_requires_current_context_and_preserves_takes(self):
        from project import get_playback_pause_plan
        for change in ('none', 'text', 'pause', 'book', 'uid', 'busy', 'metadata', 'boundary'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager = ProjectManager(tmp)
                manager.save_chunks([{'id':0, 'uid':'heading', 'speaker':'NARRATOR', 'text':'Chapter 1'}] + [dict(id=index + 1, uid=f'row-{index}', speaker=speaker,
                    text=text, instruct='', take=3, status='done', audio_path=f'{index}.wav',
                    drift={'score': 0.9}) for index, (speaker, text) in enumerate([
                        ('Anna', 'I will go,'), ('NARRATOR', 'Anna said, looking out at the long road while the rain kept falling beyond the open window,'),
                        ('Anna', 'when the rain stops.')])] + [
                            {'id':4, 'uid':'next-heading', 'speaker':'NARRATOR', 'text':'Chapter 2'}])
                api = FastAPI(); api.include_router(editor.router)
                with patch.object(editor, 'project_manager', manager), patch.object(editor,
                        'SCRIPT_PATH', str(root / 'annotated_script.json')), patch.object(editor,
                        'check_global_gpu_lock') as gate, TestClient(api) as client:
                    snapshot = client.get('/api/chunks/status').json()
                    params = dict(expected_book_token=snapshot['book_token'], expected_uid='row-0')
                    url = '/api/chunks/1/gap_suggestion'
                    before = (root / 'chunks.json').read_bytes()
                    proposal = client.get(url, params=params)
                    self.assertEqual(200, proposal.status_code, proposal.text)
                    self.assertEqual(120, proposal.json()['pause_after'])
                    self.assertEqual(before, (root / 'chunks.json').read_bytes())
                    rows = manager.load_chunks()
                    if change == 'text':
                        rows[2]['text'] = 'Unrelated narration.'
                    elif change == 'pause':
                        rows[1]['pause_after'] = 0
                    elif change == 'uid':
                        rows[1]['uid'] = 'replacement'
                    elif change == 'metadata':
                        rows[1]['review_note'] = 'Unrelated metadata'
                    elif change == 'boundary':
                        rows[4]['spoken'] = True
                    elif change == 'book':
                        (root / 'state.json').write_text(json.dumps({'active_book_id':'new'}))
                    elif change == 'busy':
                        from fastapi import HTTPException
                        gate.side_effect = HTTPException(status_code=409, detail='Audio owner busy')
                    manager.save_chunks(rows)
                    before = (root / 'chunks.json').read_bytes()
                    fingerprint = manager._chapter_fingerprint(rows, (500, 500))
                    response = client.post(url, params=params,
                        json={'expected_revision':proposal.json()['revision']})
                    if change not in ('none', 'metadata'):
                        self.assertEqual(409, response.status_code, response.text)
                        self.assertEqual(before, (root / 'chunks.json').read_bytes())
                        continue
                    self.assertEqual(200, response.status_code, response.text)
                    expected = json.loads(before); expected[1]['pause_after'] = 120
                    reloaded = ProjectManager(tmp).load_chunks()
                    self.assertEqual(expected, reloaded)
                    self.assertNotEqual(fingerprint, manager._chapter_fingerprint(reloaded, (500, 500)))
                    self.assertEqual(120, get_playback_pause_plan(reloaded, 500, 500)[1])
                    replay = client.post(url, params=params,
                        json={'expected_revision':proposal.json()['revision']})
                    self.assertEqual(409, replay.status_code)

    def test_gap_suggestion_protects_explicit_pauses_and_chapter_boundaries(self):
        from dialogue_continuity import get_dialogue_gap_suggestion
        for explicit in (0, None, 173):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                manager = ProjectManager(tmp)
                manager.save_chunks([{'id':0, 'uid':'a', 'speaker':'Anna', 'text':'I will go,',
                    'pause_after':explicit}, {'id':1, 'uid':'n', 'speaker':'NARRATOR', 'text':'Anna said,'}])
                proposal = manager.get_gap_suggestion(0)
                self.assertIsNone(proposal['pause_after'])
                before = Path(manager.chunks_path).read_bytes()
                with self.assertRaisesRegex(ValueError, 'explicit pauses'):
                    manager.apply_gap_suggestion(0, 'a', proposal['revision'])
                self.assertEqual(before, Path(manager.chunks_path).read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            manager.save_chunks([{'id':0, 'uid':'a', 'speaker':'Anna', 'text':'I will go,'},
                {'id':1, 'uid':'n', 'speaker':'NARRATOR', 'text':'Anna said,'}])
            self.assertIsNone(manager.get_gap_suggestion(0)['pause_after'])
        rows = [{'uid':'a', 'speaker':'Anna', 'text':'I will go,'},
                {'uid':'n', 'speaker':'NARRATOR', 'text':'Anna said,'}]
        self.assertIsNone(get_dialogue_gap_suggestion(rows[:1], 0)['pause_after'])
        self.assertEqual(120, get_dialogue_gap_suggestion(rows, 0)['pause_after'])
        rows[1]['speaker'] = 'NARRATOR_CHARACTER'
        self.assertIsNone(get_dialogue_gap_suggestion(rows, 0)['pause_after'])

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
