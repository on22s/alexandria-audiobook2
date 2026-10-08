"""Exercise compact polling through the actual HTTP router and stored chunks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from project import ProjectManager


class EditorPollDeltaTests(unittest.TestCase):
    def test_invalid_persisted_ids_are_consistently_refused_without_losing_progress_or_audio(self):
        import wave
        invalid = [None, True, '0', -1, 'missing', 'duplicate']
        for value in invalid:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); pm = ProjectManager(tmp)
                audio = root / 'voice.wav'
                with wave.open(str(audio), 'wb') as handle:
                    handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
                    handle.writeframes(b'\x01\x00' * 240)
                audio_bytes = audio.read_bytes()
                row = {'id': value, 'uid': 'saved-row', 'speaker': 'NARRATOR',
                       'text': 'Synthetic saved sentence.', 'status': 'done', 'audio_path': audio.name}
                if value == 'missing':
                    row.pop('id')
                if value == 'duplicate':
                    row['id'] = 0
                rows = [row, dict(row, uid='other-row')] if value == 'duplicate' else [row]
                raw = json.dumps(rows).encode(); (root / 'chunks.json').write_bytes(raw)
                app = FastAPI(); app.include_router(editor.router)
                with patch.object(editor, 'project_manager', pm), \
                        patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                        TestClient(app, raise_server_exceptions=False) as client:
                    responses = [client.get('/api/chunks'), client.get('/api/chunks/status')]
                    self.assertEqual([409, 409], [response.status_code for response in responses])
                    self.assertTrue(all('chunk' in response.json()['detail'].lower() for response in responses))
                    self.assertEqual(raw, (root / 'chunks.json').read_bytes())
                    self.assertEqual(audio_bytes, audio.read_bytes())
                    self.assertEqual([], list(root.glob('chunks.json.corrupt*')))
                    fixed = dict(row, id=0)
                    (root / 'chunks.json').write_text(json.dumps([fixed]))
                    self.assertEqual(200, client.get('/api/chunks').status_code)
                    snapshot = client.get('/api/chunks/status')
                    self.assertEqual(200, snapshot.status_code)
                    self.assertEqual([fixed], snapshot.json()['chunks'])

    def test_unchanged_poll_and_one_changed_row_do_not_transfer_book_text(self):
        with tempfile.TemporaryDirectory() as root:
            pm = ProjectManager(root)
            pm.save_chunks([{'id': i, 'uid': f'row-{i}', 'text': 'A long line. ' * 100,
                'speaker': 'Narrator', 'status': 'generating' if i == 7 else 'pending'} for i in range(500)])
            app = FastAPI(); app.include_router(editor.router)
            with patch.object(editor, 'project_manager', pm), patch.object(editor, 'SCRIPT_PATH', str(Path(root)/'annotated_script.json')), TestClient(app) as client:
                first = client.get('/api/chunks/status')
                self.assertEqual(200, first.status_code, first.text)
                data = first.json(); self.assertTrue(data['full']); self.assertEqual(500,len(data['chunks']))
                revision = data['revision']
                same = client.get('/api/chunks/status',params={'revision':revision}).json()
                self.assertFalse(same['full']); self.assertEqual([],same['chunks']); self.assertEqual([],same['changed_ids'])
                self.assertEqual(1,same['running_count']); self.assertLess(len(json.dumps(same)),500)
                pm._update_chunk_fields(7,status='done',audio_path='voicelines/7.wav')
                changed = client.get('/api/chunks/status',params={'revision':revision}).json()
                self.assertFalse(changed['full']);self.assertEqual([7],changed['changed_ids']);self.assertEqual(0,changed['running_count'])
                self.assertEqual('done',changed['chunks'][0]['status']);self.assertNotEqual(revision,changed['revision'])
                self.assertLess(len(json.dumps(changed)),len(first.content)//100)
                rows=pm.load_chunks();rows.reverse();pm.save_chunks(rows)
                reordered=client.get('/api/chunks/status',params={'revision':changed['revision']}).json()
                self.assertTrue(reordered['full']);self.assertEqual(499,reordered['chunks'][0]['id'])
                unknown=client.get('/api/chunks/status',params={'revision':'stale-client'}).json()
                self.assertTrue(unknown['full'])

    def test_native_journal_changes_invalidate_revision_without_snapshot_rewrite(self):
        from chunk_status_journal import ChunkStatusJournal
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); pm=ProjectManager(str(root))
            pm.save_chunks([{'id':0,'uid':'row','text':'Hello','status':'generating'}])
            app=FastAPI();app.include_router(editor.router)
            with patch.object(editor,'project_manager',pm),patch.object(editor,'SCRIPT_PATH',str(root/'annotated_script.json')),TestClient(app) as client:
                first=client.get('/api/chunks/status').json();before=(root/'chunks.json').read_bytes()
                ChunkStatusJournal(root/'chunks.json',{'active_book_id':None,'book_generation':None}).apply_update('row',{'status':'done','audio_path':'one.wav'})
                changed=client.get('/api/chunks/status',params={'revision':first['revision']}).json()
                self.assertEqual(before,(root/'chunks.json').read_bytes())
                self.assertEqual([0],changed['changed_ids']);self.assertEqual('one.wav',changed['chunks'][0]['audio_path'])
                self.assertNotEqual(first['revision'],changed['revision'])
                files={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
                same=client.get('/api/chunks/status',params={'revision':changed['revision']}).json()
                self.assertEqual([],same['chunks']);self.assertEqual(files,{p.name:p.read_bytes() for p in root.iterdir() if p.is_file()})

    def test_book_content_uid_deletion_and_eviction_force_full_resynchronization(self):
        import editor_poll_snapshot as snapshots
        state=snapshots.EditorPollSnapshots();rows=[{'id':0,'uid':'one','text':'hello','status':'pending'}]
        first=state.ensure_snapshot(rows,['book',1]);revision=first['revision']
        for changed in ([dict(rows[0],text='new text')],[dict(rows[0],uid='replacement')],[]):
            with self.subTest(rows=changed):
                report=state.ensure_snapshot(changed,['book',1],revision)
                self.assertTrue(report['full']);self.assertNotEqual(revision,report['revision'])
        other=state.ensure_snapshot(rows,['book',2],revision)
        self.assertTrue(other['full']);self.assertNotEqual(revision,other['revision'])
        state=snapshots.EditorPollSnapshots();old=state.ensure_snapshot(rows,['book',1])['revision']
        for i in range(12):state.ensure_snapshot([dict(rows[0],status=f'status-{i}')],['book',1])
        self.assertLessEqual(len(state._history),8)
        report=state.ensure_snapshot(rows,['book',1],old)
        self.assertTrue(report['full']);self.assertEqual(rows,report['chunks'])
