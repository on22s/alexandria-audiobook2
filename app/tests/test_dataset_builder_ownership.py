"""Project ownership isolates status/edit/cancel while retaining the global task gate."""
import asyncio
import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import dataset_builder as routes


class DatasetBuilderOwnershipTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        from tests.test_dataset_builder_regressions import DatasetReferenceIndexTests
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);builder,work,output=DatasetReferenceIndexTests()._prepare(root)
            (builder/'other').mkdir();(builder/'other/state.json').write_text(json.dumps({'samples':[{'text':'Other','status':'pending'}]}))
            states={'dataset_builder':{'running':True,'logs':['other job log'],'cancel':False,'dataset_name':'other'}}
            api=FastAPI();api.include_router(routes.router)
            @api.get('/ping')
            async def ping():return {'ok':True}
            with patch.object(routes,'DATASET_BUILDER_DIR',str(builder)),patch.object(routes,'LORA_DATASETS_DIR',str(output)),patch.object(routes,'process_state',states),patch.object(core,'_task_claims',{}):
                yield root,builder,work,output,states,api

    def test_other_project_status_edits_save_and_named_cancel_do_not_touch_the_active_job(self):
        with self.fixture() as (root,builder,work,output,states,api),TestClient(api) as client:
            before=(builder/'other/state.json').read_bytes()
            idle=client.get('/api/dataset_builder/status/voice').json()
            self.assertFalse(idle['running']);self.assertEqual([],idle['logs']);self.assertEqual('other',idle['active_dataset_name'])
            active=client.get('/api/dataset_builder/status/other').json()
            self.assertTrue(active['running']);self.assertEqual(['other job log'],active['logs'])
            response=client.post('/api/dataset_builder/update_meta',json={'name':'voice','description':'New description'})
            self.assertEqual(200,response.status_code,response.text)
            response=client.post('/api/dataset_builder/update_rows',json={'name':'voice','rows':[{'text':'First reference'},{'text':'Second reference'}]})
            self.assertEqual(200,response.status_code,response.text)
            for route,body in (('update_meta',{'name':'other','description':'Blocked'}),('update_rows',{'name':'other','rows':[]})):
                self.assertEqual(409,client.post('/api/dataset_builder/'+route,json=body).status_code)
            # Reapply completed state for CPU save while another project owns generation.
            saved=json.loads((work/'state.json').read_text())
            for sample in saved['samples']:sample['status']='done'
            (work/'state.json').write_text(json.dumps(saved))
            response=client.post('/api/dataset_builder/save',json={'name':'voice','ref_index':0})
            self.assertEqual(200,response.status_code,response.text);self.assertTrue((output/'voice/metadata.jsonl').is_file())
            response=client.delete('/api/dataset_builder/other');self.assertEqual(409,response.status_code,response.text)
            self.assertEqual(before,(builder/'other/state.json').read_bytes())
            self.assertEqual('not_running',client.post('/api/dataset_builder/cancel?name=voice').json()['status']);self.assertFalse(states['dataset_builder']['cancel'])
            self.assertEqual('cancelling',client.post('/api/dataset_builder/cancel?name=other').json()['status']);self.assertTrue(states['dataset_builder']['cancel'])
            response=client.delete('/api/dataset_builder/voice');self.assertEqual(200,response.status_code,response.text)
            self.assertFalse(work.exists());self.assertTrue((builder/'other').is_dir())

    def test_pending_claim_protects_its_project_and_unknown_legacy_owner_conservatively_blocks_delete(self):
        with self.fixture() as (root,builder,work,output,states,api),TestClient(api) as client:
            states['dataset_builder']['running']=False
            with patch.object(core,'_task_claims',{'dataset_builder':{'phase':'pending','id':'fixture'}}):
                status=client.get('/api/dataset_builder/status/other').json();self.assertTrue(status['running'])
                self.assertEqual(409,client.delete('/api/dataset_builder/other').status_code)
                self.assertEqual(409,client.post('/api/dataset_builder/update_meta',json={'name':'other'}).status_code)
                self.assertEqual(200,client.post('/api/dataset_builder/update_meta',json={'name':'voice'}).status_code)
            states['dataset_builder']['running']=True;states['dataset_builder'].pop('dataset_name')
            self.assertEqual(409,client.delete('/api/dataset_builder/voice').status_code)
            self.assertTrue(work.is_dir())

    def test_held_reference_analysis_keeps_asgi_responsive_and_preserves_selected_ref_pcm(self):
        with self.fixture() as (root,builder,work,output,states,api):
            states['dataset_builder']['running']=False
            state=json.loads((work/'state.json').read_text());state['samples'].append({'text':'Third reference','status':'done'})
            (work/'state.json').write_text(json.dumps(state));(work/'sample_002.wav').write_bytes((work/'sample_000.wav').read_bytes())
            before={p.name:p.read_bytes() for p in work.iterdir()};entered=threading.Event();release=threading.Event();threads=[]
            def select(paths, *, dataset_root):
                self.assertEqual(work.resolve(), Path(dataset_root).resolve())
                self.assertEqual([str(work / f'sample_{i:03d}.wav') for i in range(3)], paths)
                threads.append(threading.get_ident());entered.set();self.assertTrue(release.wait(2));return 1,.9
            timer=threading.Timer(.5,release.set);timer.start()
            async def exercise():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://fixture') as client:
                    task=asyncio.create_task(client.post('/api/dataset_builder/save',json={'name':'voice'}))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait,2))
                        response=await asyncio.wait_for(client.get('/ping'),.2);self.assertEqual(200,response.status_code)
                        self.assertFalse(task.done(),'reference analysis blocked the API until completion')
                    finally:release.set();response=await task
                    return response
            try:
                with patch('voice_reference.select_reference_sample',side_effect=select):response=asyncio.run(exercise())
            finally:release.set();timer.cancel();timer.join()
            self.assertEqual(200,response.status_code,response.text);self.assertNotEqual(threading.get_ident(),threads[0])
            self.assertEqual(before['sample_001.wav'],(output/'voice/ref.wav').read_bytes());self.assertEqual('Second reference',(output/'voice/ref_text.txt').read_text())
            self.assertEqual(before,{p.name:p.read_bytes() for p in work.iterdir()});self.assertEqual(3,len((output/'voice/metadata.jsonl').read_text().splitlines()))

    def test_active_project_delete_refuses_and_keeps_its_complete_working_state(self):
        with self.fixture() as (root,builder,work,output,states,api),TestClient(api) as client:
            path=builder/'other/state.json';before=path.read_bytes()
            response=client.delete('/api/dataset_builder/other')
            self.assertEqual(409,response.status_code,response.text);self.assertEqual(before,path.read_bytes())

    def test_inactive_project_metadata_is_editable_while_another_project_generates(self):
        with self.fixture() as (root,builder,work,output,states,api),TestClient(api) as client:
            response=client.post('/api/dataset_builder/update_meta',json={'name':'voice','description':'Independent edit'})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual('Independent edit',json.loads((work/'state.json').read_text())['description'])
            self.assertTrue(states['dataset_builder']['running']);self.assertEqual('other',states['dataset_builder']['dataset_name'])

    def test_batch_setup_failure_releases_only_the_unstarted_reservation(self):
        with self.fixture() as (root,builder,work,output,states,api),TestClient(api,raise_server_exceptions=False) as client:
            states['dataset_builder']['running']=False
            def reserve(task):
                states[task]['running']=True;return 'pending-fixture'
            with patch.object(routes,'check_global_gpu_lock'),patch.object(routes,'reserve_background_task',side_effect=reserve),patch.object(routes.os,'makedirs',side_effect=OSError('fixture setup disk failure')),patch.object(routes,'release_gpu_task_claim') as release,patch.object(routes,'start_claimed_task_thread') as start:
                response=client.post('/api/dataset_builder/generate_batch',json={'name':'voice','description':'Warm','samples':[{'text':'Known'}]})
            self.assertEqual(500,response.status_code);start.assert_not_called()
            release.assert_called_once_with('dataset_builder','pending-fixture',pending_only=True)
            self.assertEqual('voice',states['dataset_builder']['dataset_name'])
