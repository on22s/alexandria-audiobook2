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
from tests.test_saved_book_publication import SavedBookPublicationTests


class SpeakerRepairPublicationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, entries=None, voices=True):
        with SavedBookPublicationTests().fixture() as (root,api):
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
        return SavedBookPublicationTests().artifacts(root)

    def test_full_rename_split_merge_and_narration_correction_back_up_and_clear_voices(self):
        cases=([self.select(1,'Alice','Alice Smith'),self.select(2,'Alice','Alice Smith')],
               [self.select(1,'Alice','Alice Smith')],
               [self.select(1,'Alice','Bob'),self.select(2,'Alice','Bob')],
               [self.select(1,'Alice','NARRATOR')])
        for selections in cases:
            with self.subTest(selections=selections),self.fixture() as (root,api),TestClient(api) as client:
                path=root/'scripts/book.json';voice=root/'scripts/book.voice_config.json'
                before_script=path.read_bytes();before_voice=voice.read_bytes();metadata=(root/'scripts/book.meta.json').read_bytes()
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
                for companion in routes._get_saved_book_companions(str(path)):
                    if companion!=str(root/'scripts/book.meta.json'):self.assertFalse(Path(companion).exists())
                self.assertFalse(client.get('/api/scripts').json()[0]['has_voice_config'])

    def test_missing_companion_is_reported_without_creating_a_voice_backup(self):
        with self.fixture(voices=False) as (root,api),TestClient(api) as client:
            response=client.post('/api/scripts/book/repair/speakers/apply',json=self.request(root,[self.select(1,'Alice','NARRATOR')]))
            self.assertEqual(200,response.status_code,response.text)
            self.assertIs(False,response.json().get('voice_config_invalidated'))
            self.assertIsNone(response.json()['voice_config_backup'])

    def test_dangling_voice_symlink_is_refused_before_script_publication(self):
        with self.fixture() as (root,api),TestClient(api) as client:
            voice=root/'scripts/book.voice_config.json';voice.unlink()
            voice.symlink_to(root/'missing-voice.json')
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
            before=self.snapshot(root);replace=os.replace;failed=False
            def fail_once(source,destination,*args,**kwargs):
                nonlocal failed
                if str(source)==str(root/'scripts/book.voice_config.json') and not failed:
                    failed=True;raise OSError('fixture voice invalidation disk full')
                return replace(source,destination,*args,**kwargs)
            body=self.request(root,[self.select(1,'Alice','NARRATOR')])
            with patch('os.replace',side_effect=fail_once):
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
def die(source,destination):
 global count
 result=move(source,destination);count+=1
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
            self.assertEqual(200,client.get('/api/scripts').status_code)
            self.assertEqual(before,self.snapshot(root))
            self.assertFalse((root/'scripts'/books.JOURNAL).exists())
