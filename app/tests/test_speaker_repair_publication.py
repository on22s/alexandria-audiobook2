"""Attribution repairs invalidate saved voice approvals without losing their bytes."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import book_state_transaction as books
from routers import scripts_library as routes
from tests import test_saved_book_publication as saved_book_fixture


class SpeakerRepairPublicationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, entries=None, voices=True):
        with saved_book_fixture.SavedBookPublicationTests().fixture() as (root,api):
            path=root/'scripts/book.json';voice=root/'scripts/book.voice_config.json'
            path.write_text(json.dumps(entries or [
                {'speaker':'Alice','text':'Alice arrived.'},
                {'speaker':'Alice','text':'Hello!'},
                {'speaker':'Bob','text':'Goodbye.'}]))
            if voices:voice.write_text('{\n "Alice": {"voice":"Ryan","ready":true}, "Bob":{"voice":"Aiden","ready":true}\n}')
            else:voice.unlink()
            yield root,api

    def request(self, root, selections):
        path=root/'scripts/book.json'
        return {'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'selections':selections}

    def select(self, number, old, new):
        return {'entry_number':number,'expected_speaker':old,'new_speaker':new}

    def snapshot(self, root):
        return saved_book_fixture.SavedBookPublicationTests().artifacts(root)

    def test_full_rename_split_merge_and_narration_correction_back_up_and_clear_voices(self):
        cases=([self.select(1,'Alice','Alice Smith'),self.select(2,'Alice','Alice Smith')],
               [self.select(1,'Alice','Alice Smith')],
               [self.select(1,'Alice','Bob'),self.select(2,'Alice','Bob')],
               [self.select(1,'Alice','NARRATOR')])
        for selections in cases:
            with self.subTest(selections=selections),self.fixture() as (root,api),TestClient(api) as client:
                path=root/'scripts/book.json';voice=root/'scripts/book.voice_config.json'
                before_script=path.read_bytes();before_voice=voice.read_bytes();metadata=(root/'scripts/book.meta.json').read_bytes()
                publication=Path(str(path)+'.publication.json');before_publication=publication.read_bytes()
                from sound_design_registry import get_saved_sound_design_path
                cue_path=Path(get_saved_sound_design_path(path));before_cues=cue_path.read_bytes()
                from sound_design_renditions import get_saved_sound_renditions_path
                rendition_path=Path(get_saved_sound_renditions_path(path));before_renditions=rendition_path.read_bytes()
                response=client.post('/api/scripts/book/repair/speakers/apply',json=self.request(root,selections))
                self.assertEqual(200,response.status_code,response.text)
                self.assertFalse(voice.exists(),'obsolete assignments survived attribution repair')
                result=response.json();self.assertTrue(result['voice_config_invalidated'])
                self.assertEqual(before_script,(root/'scripts'/result['backup']).read_bytes())
                self.assertEqual(before_voice,(root/'scripts'/result['voice_config_backup']).read_bytes())
                self.assertIn('assign and approve',result['message'])
                entries=json.loads(path.read_bytes())
                for item in selections:self.assertEqual(item['new_speaker'],entries[item['entry_number']-1]['speaker'])
                self.assertEqual(metadata,(root/'scripts/book.meta.json').read_bytes())
                self.assertEqual(before_publication,publication.read_bytes())
                self.assertEqual(before_cues,cue_path.read_bytes())
                self.assertEqual(before_renditions,rendition_path.read_bytes())
                for companion in routes._get_saved_book_companions(str(path)):
                    if companion not in (str(root/'scripts/book.meta.json'),str(publication),str(cue_path),str(rendition_path)):
                        self.assertFalse(Path(companion).exists())
                self.assertFalse(client.get('/api/scripts').json()[0]['has_voice_config'])

    def test_missing_companion_is_reported_without_creating_a_voice_backup(self):
        with self.fixture(voices=False) as (root,api),TestClient(api) as client:
            response=client.post('/api/scripts/book/repair/speakers/apply',json=self.request(root,[self.select(1,'Alice','NARRATOR')]))
            self.assertEqual(200,response.status_code,response.text)
            self.assertIs(False,response.json().get('voice_config_invalidated'))
            self.assertIsNone(response.json()['voice_config_backup'])

    def test_active_saved_speaker_repair_requires_switching_books(self):
        with self.fixture() as (root,api),TestClient(api) as client:
            saved = root/'scripts/book.json'; saved_voice = root/'scripts/book.voice_config.json'
            active = root/'annotated_script.json'; active_voice = root/'voice_config.json'
            active.write_bytes(saved.read_bytes()); active_voice.write_bytes(b'{"active":"new assignment"}')
            before = {path:path.read_bytes() for path in (saved, saved_voice, active, active_voice)}
            (root/'chunks.json').write_text('[{"speaker":"Alice","text":"stale"}]')
            (root/'chunks.json.status.jsonl').write_text('{"stale":true}\n')
            (root/'state.json').write_text(json.dumps({
                'active_book_id':'different-id', 'book_generation':'generation',
                'input_file_path':str(saved)}))
            request = self.request(root,[self.select(1,'Alice','NARRATOR')])
            with patch.object(routes, 'get_repair_script_snapshot',
                              side_effect=AssertionError('active saved book must be refused before parsing')):
                response=client.post('/api/scripts/book/repair/speakers/apply',json=request)
            self.assertEqual(409,response.status_code,response.text)
            self.assertIn('switch books', response.json()['detail'])
            self.assertEqual(before, {path:path.read_bytes() for path in before})
            self.assertTrue((root/'chunks.json').exists())
            self.assertTrue((root/'chunks.json.status.jsonl').exists())

    def test_active_speaker_repair_preserves_active_binding_when_saved_binding_missing(self):
        from script_selection_binding import BINDING_NAME
        with self.fixture() as (root,api), TestClient(api) as client:
            saved = root/'scripts/book.json'
            active = root/'annotated_script.json'
            active.write_bytes(saved.read_bytes())
            (root/'state.json').write_text(json.dumps({
                'active_book_id':'book', 'book_generation':'generation',
                'input_file_path':str(saved), 'script_binding_required': True}))
            binding = root/BINDING_NAME
            binding.write_bytes(b'active identity must remain')
            before = saved.read_bytes()
            response = client.post('/api/scripts/book/repair/speakers/apply',
                                   json=self.request(root,[self.select(1,'Alice','NARRATOR')]))
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('switch books', response.json()['detail'])
            self.assertEqual(before, saved.read_bytes())
            self.assertEqual(before, active.read_bytes())
            self.assertEqual(b'active identity must remain', binding.read_bytes())

    def test_dangling_voice_symlink_is_refused_before_script_publication(self):
        with self.fixture() as (root,api),TestClient(api) as client:
            voice=root/'scripts/book.voice_config.json';voice.unlink()
            try:
                voice.symlink_to(root/'missing-voice.json')
            except OSError as error:
                if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                    self.skipTest('Windows symlink creation requires privilege or developer mode')
                raise
            script=(root/'scripts/book.json').read_bytes()
            response=client.post('/api/scripts/book/repair/speakers/apply',json=self.request(root,[self.select(1,'Alice','NARRATOR')]))
            self.assertEqual(409,response.status_code,response.text)
            self.assertEqual(script,(root/'scripts/book.json').read_bytes())
            self.assertTrue(voice.is_symlink())
            self.assertFalse(list((root/'scripts').glob('*.bak-*')))

    def test_stale_preview_and_unchanged_selection_preserve_complete_family(self):
        with self.fixture() as (root,api),TestClient(api) as client:
            before=self.snapshot(root)
            body=self.request(root,[self.select(1,'Alice','NARRATOR')]);body['expected_sha256']='0'*64
            self.assertEqual(409,client.post('/api/scripts/book/repair/speakers/apply',json=body).status_code)
            body=self.request(root,[self.select(1,'Alice','Alice')])
            response=client.post('/api/scripts/book/repair/speakers/apply',json=body)
            self.assertEqual('unchanged',response.json()['status'])
            self.assertEqual(before,self.snapshot(root))

    def test_voice_removal_failure_restores_script_voices_checkpoints_and_backup_ownership(self):
        with self.fixture() as (root,api),TestClient(api,raise_server_exceptions=False) as client:
            from source_upload_publication import move_source_upload_exclusively
            before=self.snapshot(root);failed=False
            def fail_once(source,destination,**kwargs):
                nonlocal failed
                if Path(source).name=='book.voice_config.json' and not failed:
                    failed=True;raise OSError('fixture voice invalidation disk full')
                return move_source_upload_exclusively(source,destination,**kwargs)
            body=self.request(root,[self.select(1,'Alice','NARRATOR')])
            with patch('source_upload_publication.move_source_upload_exclusively',side_effect=fail_once):
                response=client.post('/api/scripts/book/repair/speakers/apply',json=body)
            self.assertTrue(failed);self.assertEqual(500,response.status_code)
            self.assertEqual(before,self.snapshot(root))
            self.assertFalse((root/'scripts'/books.JOURNAL).exists())

    def test_backup_collision_preserves_existing_files_and_refuses_the_repair(self):
        with self.fixture() as (root,api),TestClient(api) as client:
            backup=root/'scripts/book.json.bak-existing';backup.write_bytes(b'owned older backup')
            before=self.snapshot(root)
            with patch.object(routes,'get_timestamped_backup_path',return_value=str(backup)):
                response=client.post('/api/scripts/book/repair/speakers/apply',json=self.request(root,[self.select(1,'Alice','NARRATOR')]))
            self.assertEqual(409,response.status_code,response.text)
            self.assertEqual(before,self.snapshot(root))

    @unittest.skipIf(os.name == 'nt', 'POSIX flock fixture; Windows locking needs a native fixture')
    def test_current_voice_bytes_are_captured_after_a_competing_writer_releases_its_kernel_lock(self):
        import fcntl
        with self.fixture() as (root,api),TestClient(api) as client:
            body=self.request(root,[self.select(1,'Alice','NARRATOR')]);voice=root/'scripts/book.voice_config.json'
            entered=threading.Event();results=[];lock=routes.file_lock
            @contextlib.contextmanager
            def observed(path,*args,**kwargs):
                if path==str(voice):entered.set()
                with lock(path,*args,**kwargs):yield
            with Path(str(voice)+'.lock').open('a') as holder:
                fcntl.flock(holder,fcntl.LOCK_EX)
                worker=threading.Thread(target=lambda:results.append(client.post('/api/scripts/book/repair/speakers/apply',json=body)))
                with patch.object(routes,'file_lock',observed):
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1));self.assertTrue(worker.is_alive())
                        newest=b'{"Alice":{"voice":"newest assignment","ready":true}}'
                        voice.write_bytes(newest)
                    finally:fcntl.flock(holder,fcntl.LOCK_UN);worker.join(timeout=3)
            self.assertFalse(worker.is_alive());self.assertEqual(200,results[0].status_code,results[0].text)
            self.assertEqual(newest,(root/'scripts'/results[0].json()['voice_config_backup']).read_bytes())

    def test_process_death_after_voice_removal_recovers_old_script_and_assignments_without_orphan_backups(self):
        worker='''import hashlib,os,sys
from pathlib import Path
import book_state_transaction as books
from routers import scripts_library as routes
root=Path(sys.argv[1]);routes.SCRIPTS_DIR=str(root/'scripts');routes.process_state={}
move=books._move;count=0
def die(source,destination, **kwargs):
 global count
 result=move(source,destination, **kwargs);count+=1
 if count==5:os._exit(77)
 return result
books._move=die
request=routes.SpeakerRepairRequest(expected_sha256=hashlib.sha256((root/'scripts/book.json').read_bytes()).hexdigest(),selections=[routes.SpeakerSelection(entry_number=1,expected_speaker='Alice',new_speaker='NARRATOR')])
routes._apply_speaker_repair_sync('book',request)
'''
        with self.fixture() as (root,api),TestClient(api) as client:
            before=self.snapshot(root)
            process=subprocess.run([sys.executable,'-c',worker,str(root)],capture_output=True,text=True,timeout=30)
            self.assertEqual(77,process.returncode,process.stdout+process.stderr)
            self.assertTrue((root/'scripts'/books.JOURNAL).exists())
            interrupted={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*')
                         if p.is_file() and not p.name.endswith('.lock')}
            self.assertEqual(409,client.get('/api/scripts').status_code)
            self.assertEqual(interrupted,{str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*')
                                         if p.is_file() and not p.name.endswith('.lock')})
            with books.ensure_book_state(str(root/'scripts')):
                pass
            self.assertEqual(200,client.get('/api/scripts').status_code)
            self.assertEqual(before,self.snapshot(root))
            self.assertFalse((root/'scripts'/books.JOURNAL).exists())
