"""Cast publication uses real flat-JSON files and kernel locks."""
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import voice_library as routes
from utils import atomic_json_write, file_lock, secure_filename


class VoiceLibraryTransactionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as patches:
            root=Path(tmp);scripts=root/'scripts';scripts.mkdir()
            library=root/'library.json';config=root/'voices.json';script=root/'active.json'
            atomic_json_write({'shared':{},'favorites':[], 'casts':{'series':{'members':{
                'hero':{'name':'Hero','config':{'type':'custom','voice':'old'}}}}}},str(library))
            atomic_json_write({'Hero':{'type':'custom','voice':'active'}},str(config))
            atomic_json_write([{'speaker':'Hero','text':'One line.'}],str(script))
            atomic_json_write({'active_book_id':'active-book'},str(root/'state.json'))
            for name in ('one','two','__one'):
                atomic_json_write([{'speaker':'Hero','text':'One line.'}],str(scripts/f'{name}.json'))
                atomic_json_write({'Other':{'voice':'preserve'}},str(scripts/f'{name}.voice_config.json'))
            for module in (core,routes):
                for key,value in (('SCRIPTS_DIR',scripts),('VOICE_LIBRARY_PATH',library),
                                  ('VOICE_CONFIG_PATH',config),('SCRIPT_PATH',script)):
                    patches.enter_context(patch.object(module,key,str(value)))
                patches.enter_context(patch.object(module,'LORA_MODELS_DIR',str(root/'lora_models')))
            patches.enter_context(patch.object(core,'DATA_DIR',str(root)))
            counts=core._script_line_counts
            patches.enter_context(patch.object(routes,'_script_line_counts',side_effect=lambda path=None:counts(path or str(script))))
            patches.enter_context(patch.object(routes,'CHARACTER_ALIASES_PATH',str(root/'aliases.json')))
            api=FastAPI();api.include_router(routes.router)
            @api.get('/ping')
            async def ping():return {'alive':True}
            yield root,api

    def test_bulk_revalidates_deleted_cast_member_and_changed_voice_between_books(self):
        for change in ('cast','member','voice'):
            with self.subTest(change=change), self.fixture() as (root,api), TestClient(api) as client:
                second=root/'scripts/two.voice_config.json';original=second.read_bytes()
                def name_checked(name):
                    if name=='two':
                        def mutate(lib):
                            if change=='cast':del lib['casts']['series']
                            elif change=='member':del lib['casts']['series']['members']['hero']
                            else:lib['casts']['series']['members']['hero']['config']['voice']='new'
                        routes._mutate_voice_library(mutate)
                    return secure_filename(name)
                with patch.object(routes,'secure_filename',side_effect=name_checked):
                    response=client.post('/api/voice_library/apply_bulk',json={
                        'cast':'series','mapping':{'Hero':'hero'},'script_names':['one','two']})
                self.assertEqual(200,response.status_code,response.text)
                results=response.json()['results'];self.assertEqual(1,results[0]['count'])
                first=json.loads((root/'scripts/one.voice_config.json').read_bytes())
                self.assertEqual('old',first['Hero']['voice'])
                if change=='voice':
                    self.assertEqual('new',json.loads(second.read_bytes())['Hero']['voice'])
                else:
                    self.assertIn('error',results[1]);self.assertEqual(0,results[1]['count'])
                    self.assertEqual(original,second.read_bytes())

    def test_bulk_rejects_coerced_names_and_duplicate_writes(self):
        with self.fixture() as (root,api), TestClient(api) as client:
            coerced=root/'scripts/__one.voice_config.json';before=coerced.read_bytes()
            response=client.post('/api/voice_library/apply_bulk',json={
                'cast':'series','mapping':{'Hero':'hero'},'script_names':['../one','one','one']})
            self.assertEqual(200,response.status_code,response.text)
            rows=response.json()['results']
            self.assertEqual('Invalid script name',rows[0]['error'])
            self.assertEqual(1,rows[1]['count'])
            self.assertEqual('Duplicate script name',rows[2]['error'])
            self.assertEqual(before,coerced.read_bytes())

    def test_matching_rejects_coerced_names_and_deduplicates_line_counts(self):
        with self.fixture() as (_root,api),TestClient(api) as client:
            response=client.post('/api/voice_library/match_bulk',json={
                'name':'series','script_names':['../one']})
            self.assertEqual(400,response.status_code,response.text)
            valid=client.post('/api/voice_library/match_bulk',json={
                'name':'series','script_names':['one','one']})
            self.assertEqual(200,valid.status_code,valid.text)
            self.assertEqual(1,valid.json()['book_count'])
            self.assertEqual(1,valid.json()['proposals'][0]['line_count'])

    def test_bulk_keeps_library_kernel_lock_through_each_config_publication(self):
        with self.fixture() as (root,api),TestClient(api) as client:
            write=routes.atomic_json_write;seen=[]
            def checked_write(value,path):
                if str(path).endswith('.voice_config.json'):
                    probe=subprocess.run(['flock','-n',str(root/'library.json.lock'),'true'],timeout=3)
                    seen.append(probe.returncode)
                    self.assertEqual(1,probe.returncode,'cast was unlocked before book publication')
                return write(value,path)
            with patch.object(routes,'atomic_json_write',side_effect=checked_write):
                response=client.post('/api/voice_library/apply_bulk',json={
                    'cast':'series','mapping':{'Hero':'hero'},'script_names':['one','two']})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual([1,1],seen)

    def test_save_keeps_voice_config_kernel_lock_through_library_publication(self):
        with self.fixture() as (root,api), TestClient(api) as client:
            write=routes.atomic_json_write;seen=[]
            def checked_write(value,path):
                if path==str(root/'library.json'):
                    probe=subprocess.run(['flock','-n',str(root/'voices.json.lock'),'true'],timeout=3)
                    seen.append(probe.returncode)
                    self.assertEqual(1,probe.returncode,'voice snapshot was unlocked before library publication')
                return write(value,path)
            original=(root/'voices.json').read_bytes()
            with patch.object(routes,'atomic_json_write',side_effect=checked_write):
                response=client.post('/api/voice_library/save',json={'cast':'series','characters':['Hero']})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual([1],seen)
            self.assertEqual(original,(root/'voices.json').read_bytes())
            lib=json.loads((root/'library.json').read_bytes())
            self.assertEqual('active',lib['casts']['series']['members']['hero']['config']['voice'])

    def test_save_waits_for_config_writer_and_captures_its_completed_update(self):
        with self.fixture() as (root,api),TestClient(api) as client:
            entered=threading.Event();result=[];lock=routes.file_lock
            @contextlib.contextmanager
            def observed_lock(path,*args,**kwargs):
                if path==str(root/'voices.json'):entered.set()
                with lock(path,*args,**kwargs):yield
            with (root/'voices.json.lock').open('a') as holder:
                fcntl.flock(holder,fcntl.LOCK_EX)
                def save():
                    result.append(client.post('/api/voice_library/save',json={'cast':'series','characters':['Hero']}))
                worker=threading.Thread(target=save)
                with patch.object(routes,'file_lock',observed_lock):
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1),'save never entered the config-lock admission')
                        self.assertTrue(worker.is_alive(),'save finished while another writer owned config')
                        atomic_json_write({'Hero':{'type':'custom','voice':'newest'}},str(root/'voices.json'))
                    finally:
                        fcntl.flock(holder,fcntl.LOCK_UN);worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(200,result[0].status_code,result[0].text)
            lib=json.loads((root/'library.json').read_bytes())
            self.assertEqual('newest',lib['casts']['series']['members']['hero']['config']['voice'])

    def test_disk_reads_and_matching_leave_native_asgi_event_loop_responsive(self):
        async def run(api,path,body):
            started=threading.Event();release=threading.Event();finished=threading.Event();load=routes._load_voice_library
            proposals=routes._build_match_proposals;matching_threads=[];loop_thread=threading.get_ident()
            def observed_matching(*args,**kwargs):
                matching_threads.append(threading.get_ident());return proposals(*args,**kwargs)
            def held_load():
                started.set();release.wait(1);finished.set();return load()
            with patch.object(routes,'_load_voice_library',side_effect=held_load), \
                 patch.object(routes,'_build_match_proposals',side_effect=observed_matching):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://fixture') as client:
                    task=asyncio.create_task(client.get(path) if body is None else client.post(path,json=body))
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait,2))
                        self.assertFalse(finished.is_set(),'event loop could not run while the disk reader waited')
                        self.assertFalse(task.done(),'synchronous route blocked the event loop until its read completed')
                        self.assertEqual({'alive':True},(await client.get('/ping')).json())
                    finally:release.set()
                    result=await task;self.assertEqual(200,result.status_code,result.text)
                    if body is not None:
                        self.assertTrue(matching_threads)
                        self.assertNotIn(loop_thread,matching_threads)
        for path,body in (('/api/voice_library',None),('/api/voice_library/match',{'name':'series'}),
                          ('/api/voice_library/match_bulk',{'name':'series','script_names':['one']})):
            with self.subTest(path=path),self.fixture() as (_root,api):
                asyncio.run(run(api,path,body))
