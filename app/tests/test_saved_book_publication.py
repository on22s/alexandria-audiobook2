"""Saved-book operations publish recoverable artifact sets, without inference."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import book_state_transaction as books
import core
from routers import scripts_library as routes


class SavedBookPublicationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, voices=True):
        with tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
            root=Path(tmp);saved=root/'scripts';saved.mkdir();uploads=root/'uploads';uploads.mkdir()
            script=root/'annotated_script.json';config=root/'voice_config.json'
            script.write_text('[{"speaker":"Alice","text":"Alice arrived.","instruct":"Read"}]')
            if voices:config.write_text('{"Alice":{"voice":"new voice"}}')
            (root/'state.json').write_text('{"active_book_id":"new-book"}')
            (uploads/'source.txt').write_text('Alice arrived.')
            (saved/'book.json').write_text('[{"speaker":"Old","text":"Old book."}]')
            for path in routes._get_saved_book_companions(str(saved/'book.json')):
                Path(path).write_bytes(b'old companion bytes')
            for module in (routes,core):
                for key,value in (('DATA_DIR',root),('SCRIPTS_DIR',saved),('SCRIPT_PATH',script),
                                  ('VOICE_CONFIG_PATH',config),('UPLOADS_DIR',uploads)):
                    stack.enter_context(patch.object(module,key,str(value)))
            stack.enter_context(patch.object(routes,'process_state',{}))
            api=FastAPI();api.include_router(routes.router)
            @api.get('/ping')
            async def ping():return {'alive':True}
            yield root,api

    def artifacts(self,root):
        return {p.name:p.read_bytes() for p in (root/'scripts').iterdir()
                if p.is_file() and not p.name.endswith('.lock')}

    def test_save_replaces_complete_family_and_removes_stale_voice_and_checkpoints(self):
        for voices in (False,True):
            with self.subTest(voices=voices),self.fixture(voices) as (root,api),TestClient(api) as client:
                active=(root/'annotated_script.json').read_bytes()
                response=client.post('/api/scripts/save',json={'name':'book'})
                self.assertEqual(200,response.status_code,response.text)
                expected={'book.json':active,'book.meta.json':b'{"book_id": "new-book"}'}
                if voices:expected['book.voice_config.json']=(root/'voice_config.json').read_bytes()
                self.assertEqual(expected,self.artifacts(root))
                self.assertEqual(active,(root/'annotated_script.json').read_bytes())
                self.assertEqual(['book'],[r['name'] for r in client.get('/api/scripts').json()])

    def test_real_metadata_publication_failure_restores_every_original_saved_artifact(self):
        with self.fixture() as (root,api),TestClient(api,raise_server_exceptions=False) as client:
            before=self.artifacts(root);replace=os.replace;failed=False
            def fail_metadata(source,destination,*args,**kwargs):
                nonlocal failed
                if not failed and str(destination)==str(root/'scripts/book.meta.json'):
                    failed=True;raise OSError('fixture metadata publication disk full')
                return replace(source,destination,*args,**kwargs)
            with patch('os.replace',side_effect=fail_metadata):
                response=client.post('/api/scripts/save',json={'name':'book'})
            self.assertTrue(failed)
            self.assertEqual(500,response.status_code)
            self.assertEqual(before,self.artifacts(root))
            self.assertFalse((root/'scripts'/books.JOURNAL).exists())

    def test_delete_removes_every_known_companion_and_busy_jobs_refuse_without_changes(self):
        for task in ('batch_review','batch_script',None):
            with self.subTest(task=task),self.fixture() as (root,api),TestClient(api) as client:
                before=self.artifacts(root)
                states={task:{'running':True}} if task else {}
                with patch.object(routes,'process_state',states):
                    response=client.delete('/api/scripts/book')
                self.assertEqual(409 if task else 200,response.status_code,response.text)
                self.assertEqual(before if task else {},self.artifacts(root))

    def test_reserved_companion_names_are_rejected_before_writing(self):
        names=('book.voice_config','book.meta','book.review_checkpoint','book.generation_checkpoint',
               'book.generation_quality','book.threepass_checkpoint','book.threepass_manifest',
               'book.json.review_completed')
        with self.fixture() as (root,api),TestClient(api) as client:
            before=self.artifacts(root)
            for name in names:
                with self.subTest(name=name):
                    response=client.post('/api/scripts/save',json={'name':name})
                    self.assertEqual(400,response.status_code,response.text)
                    self.assertEqual(before,self.artifacts(root))

    def test_preflight_and_all_preview_analysis_leave_asgi_event_loop_responsive(self):
        async def run(root,api,path,body,helper):
            started=threading.Event();release=threading.Event();finished=threading.Event()
            original=getattr(routes,helper)
            def held(*args,**kwargs):
                started.set();release.wait(1);finished.set();return original(*args,**kwargs)
            with patch.object(routes,helper,side_effect=held):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://fixture') as client:
                    task=asyncio.create_task(client.get(path) if body is None else client.post(path,json=body))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait,2))
                        self.assertFalse(finished.is_set(),'analysis blocked the event loop')
                        self.assertEqual({'alive':True},(await client.get('/ping')).json())
                    finally:release.set()
                    response=await task;self.assertEqual(200,response.status_code,response.text)
        cases=(('/api/scripts/book/preflight',{},'audit_script'),
               ('/api/scripts/book/repair/deterministic/preview',{'source_filename':'source.txt'},'build_deterministic_repair'),
               ('/api/scripts/book/repair/speakers/preview',None,'build_speaker_review'),
               ('/api/scripts/book/repair/content/preview',None,'build_content_review'))
        for path,body,helper in cases:
            with self.subTest(path=path),self.fixture() as (root,api):
                (root/'scripts/book.json').write_bytes((root/'annotated_script.json').read_bytes())
                asyncio.run(run(root,api,path,body,helper))

    def test_delete_move_failure_rolls_back_complete_family(self):
        with self.fixture() as (root,api),TestClient(api,raise_server_exceptions=False) as client:
            before=self.artifacts(root);move=books._move;count=0
            def fail_once(source,destination):
                nonlocal count
                count+=1
                if count==3:raise OSError('fixture removal disk full')
                return move(source,destination)
            with patch.object(books,'_move',side_effect=fail_once):
                response=client.delete('/api/scripts/book')
            self.assertEqual(500,response.status_code)
            self.assertEqual(before,self.artifacts(root))

    def test_process_death_during_save_recovers_complete_previous_family_on_next_read(self):
        worker='''import os,sys
from pathlib import Path
import core,book_state_transaction as books
from routers import scripts_library as routes
root=Path(sys.argv[1])
for module in (routes,core):
 module.DATA_DIR=str(root);module.SCRIPTS_DIR=str(root/'scripts')
 module.SCRIPT_PATH=str(root/'annotated_script.json');module.VOICE_CONFIG_PATH=str(root/'voice_config.json')
move=books._move;count=0
def die(source,destination):
 global count
 result=move(source,destination);count+=1
 if count==3:os._exit(77)
 return result
books._move=die
routes._save_script_sync(routes.ScriptSaveRequest(name='book'))
'''
        with self.fixture() as (root,api),TestClient(api) as client:
            before=self.artifacts(root)
            result=subprocess.run([sys.executable,'-c',worker,str(root)],capture_output=True,text=True,timeout=30)
            self.assertEqual(77,result.returncode,result.stdout+result.stderr)
            self.assertTrue((root/'scripts'/books.JOURNAL).exists())
            response=client.get('/api/scripts')
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual(before,self.artifacts(root))
            self.assertFalse((root/'scripts'/books.JOURNAL).exists())
            self.assertEqual(response.json(),client.get('/api/scripts').json())
