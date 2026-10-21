"""Real Editor endpoints snapshot audio without changing selected metadata."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient
from book_state_transaction import get_book_operation_token
from project import ProjectManager
from routers import editor


class EditorTakeHistoryTests(unittest.TestCase):
    @contextmanager
    def fixture(self, journal=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager = ProjectManager(tmp)
            path = root / 'voicelines/line.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\x01\x10' * 2400)
            manager.save_chunks([dict(id=0, uid='line', speaker='NARRATOR', text='Synthetic words.',
                instruct='', take=3, status='done', audio_path='voicelines/line.wav', audio_revision='revision1')])
            if journal: manager._get_status_journal_locked().ensure_writable()
            api = FastAPI(); api.include_router(editor.router)
            with patch.object(editor, 'project_manager', manager), patch.object(editor, 'SCRIPT_PATH', str(root/'annotated_script.json')), \
                    patch.object(manager, 'get_engine', side_effect=AssertionError('Snapshot loaded a model')), TestClient(api) as client:
                yield root, manager, client, dict(expected_book_token=get_book_operation_token(tmp), expected_uid='line', expected_audio_revision='revision1')

    def test_real_roundtrip_both_backends_keeps_current_revision_source_and_audio(self):
        for journal in (False, True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, client, request):
                rows = copy.deepcopy(manager.load_chunks()); raw = (root/'voicelines/line.wav').read_bytes()
                response = client.post('/api/chunks/0/take_snapshots', json=request)
                self.assertEqual(response.status_code, 200, response.text); row = response.json()
                listing = client.get('/api/take_snapshots', params={'expected_book_token':request['expected_book_token'], 'uid':'line'})
                self.assertEqual(listing.status_code, 200, listing.text); self.assertEqual(listing.json()['total'], 1)
                params = dict(expected_book_token=request['expected_book_token'], sha256=row['sha256'], source_book_token=row['book_token'], expected_uid=row['uid'])
                response = client.get(f"/api/take_snapshots/{row['version']}/audio", params=params)
                self.assertEqual(response.status_code, 200, response.text); self.assertEqual(response.content, raw)
                self.assertEqual(int(response.headers['content-length']), len(raw))
                action = dict(expected_book_token=request['expected_book_token'], version=row['version'], sha256=row['sha256'], source_book_token=row['book_token'], expected_uid=row['uid'])
                for verb in ('protect', 'unprotect', 'delete', 'undo', 'delete', 'purge'):
                    response = client.post('/api/take_snapshots/action', json=dict(action, action=verb))
                    self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(manager.load_chunks(), rows); self.assertEqual((root/'voicelines/line.wav').read_bytes(), raw)

    def test_stale_book_uid_revision_and_edited_line_refuse_before_copy(self):
        with self.fixture() as (root, manager, client, request):
            for changed in ({'expected_book_token':'b'*64}, {'expected_uid':'other'}, {'expected_audio_revision':'old'}):
                response = client.post('/api/chunks/0/take_snapshots', json={**request, **changed})
                self.assertEqual(response.status_code, 409, response.text)
            manager.update_chunk(0, {'text':'New authored words.'})
            self.assertEqual(client.post('/api/chunks/0/take_snapshots', json=request).status_code, 409)
            self.assertFalse((root/'take_history').exists())

    def test_old_book_archive_is_discoverable_but_never_attached_to_new_rows(self):
        with self.fixture() as (root, manager, client, request):
            row = client.post('/api/chunks/0/take_snapshots', json=request).json()
            (root/'state.json').write_text(json.dumps({'active_book_id':'new', 'book_generation':'next'}))
            token = get_book_operation_token(str(root))
            self.assertEqual(client.get('/api/take_snapshots', params={'expected_book_token':request['expected_book_token']}).status_code, 409)
            self.assertEqual(client.get('/api/take_snapshots', params={'expected_book_token':token, 'uid':'line'}).json()['total'], 0)
            all_rows = client.get('/api/take_snapshots', params={'expected_book_token':token, 'all_books':True}).json()
            self.assertEqual(all_rows['records'][0]['book_token'], request['expected_book_token'])
            self.assertEqual(client.get(f"/api/take_snapshots/{row['version']}/audio", params={'expected_book_token':token, 'sha256':row['sha256'], 'source_book_token':row['book_token'], 'expected_uid':row['uid']}).status_code, 200)
            self.assertEqual(manager.load_chunks()[0]['audio_revision'], 'revision1')

    def test_selected_archive_reference_and_alias_source_cannot_be_deleted_or_captured(self):
        with self.fixture(True) as (root, manager, client, request):
            row = client.post('/api/chunks/0/take_snapshots', json=request).json()
            manager._update_chunk_fields_by_uid('line', audio_path=f"take_history/{row['version']}.wav")
            response = client.post('/api/take_snapshots/action', json=dict(expected_book_token=request['expected_book_token'], version=row['version'], sha256=row['sha256'], source_book_token=row['book_token'], expected_uid=row['uid'], action='delete'))
            self.assertEqual(response.status_code, 409, response.text)
            alias = root/'voicelines/alias.wav'; alias.symlink_to(root/'voicelines/line.wav')
            manager._update_chunk_fields_by_uid('line', audio_path='voicelines/alias.wav')
            response = client.post('/api/chunks/0/take_snapshots', json=request)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn('aliases', response.text)

    def test_corrupt_audio_and_invalid_route_action_boundaries(self):
        with self.fixture() as (root, manager, client, request):
            row = client.post('/api/chunks/0/take_snapshots', json=request).json()
            (root/f"take_history/{row['version']}.wav").write_bytes(b'corrupt bytes')
            response = client.get(f"/api/take_snapshots/{row['version']}/audio", params={'expected_book_token':request['expected_book_token'], 'sha256':row['sha256'], 'source_book_token':row['book_token'], 'expected_uid':row['uid']})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(client.get('/api/take_snapshots', params={'expected_book_token':request['expected_book_token'], 'offset':-1}).status_code, 422)
            self.assertEqual(client.post('/api/take_snapshots/action', json=dict(expected_book_token=request['expected_book_token'], version=row['version'], sha256=row['sha256'], source_book_token=row['book_token'], expected_uid=row['uid'], action='promote')).status_code, 422)

    def test_undecodable_selected_audio_returns_conflict_and_preserves_pending_charge(self):
        with self.fixture() as (root, manager, client, request):
            (root/'voicelines/line.wav').write_bytes(b'not a real audio container')
            response = client.post('/api/chunks/0/take_snapshots', json=request)
            self.assertEqual(response.status_code, 409, response.text)
            rows = client.get('/api/take_snapshots', params={'expected_book_token':request['expected_book_token']}).json()
            self.assertEqual(rows['records'][0]['state'], 'pending')
            self.assertEqual(rows['usage']['bytes'], len(b'not a real audio container'))

    def test_damaged_missing_and_legacy_uid_state_is_not_repaired_by_snapshot_refusal(self):
        for state in ('damaged', 'missing', 'legacy'):
            with self.subTest(state=state), self.fixture() as (root, manager, client, request):
                chunks = root/'chunks.json'
                if state == 'damaged': chunks.write_bytes(b'not valid JSON')
                elif state == 'missing': chunks.unlink()
                else:
                    rows = json.loads(chunks.read_bytes()); rows[0].pop('uid'); chunks.write_text(json.dumps(rows))
                before = chunks.read_bytes() if chunks.exists() else None
                response = client.post('/api/chunks/0/take_snapshots', json=request)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(chunks.read_bytes() if chunks.exists() else None, before)
                self.assertFalse((root/'take_history').exists())
                self.assertEqual(list(root.glob('chunks.json.corrupt*')), [])

    def test_archive_actions_and_audition_refuse_wrong_source_book_or_uid(self):
        with self.fixture() as (root, manager, client, request):
            row = client.post('/api/chunks/0/take_snapshots', json=request).json()
            target = dict(expected_book_token=request['expected_book_token'], source_book_token=row['book_token'],
                          expected_uid=row['uid'], version=row['version'], sha256=row['sha256'])
            before = (root/'take_history/manifest.json').read_bytes()
            for changed in ({'source_book_token':'b'*64}, {'expected_uid':'wrong-line'}):
                response = client.post('/api/take_snapshots/action', json={**target, **changed, 'action':'delete'})
                self.assertEqual(response.status_code, 409, response.text)
                response = client.get(f"/api/take_snapshots/{row['version']}/audio", params={**target, **changed})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual((root/'take_history/manifest.json').read_bytes(), before)
