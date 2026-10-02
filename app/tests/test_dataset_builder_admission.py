"""Builder request admission and native ASGI responsiveness during sample work."""
import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import httpx
import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import dataset_builder as routes


class DatasetBuilderAdmissionTests(unittest.TestCase):
    def api(self):
        api=FastAPI();api.include_router(routes.router)
        @api.get('/ping')
        def ping():return {'ok':True}
        return api

    def test_empty_duplicate_indices_and_out_of_range_seeds_refuse_before_dispatch_or_directory_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);samples=[{'text':'One'},{'text':'Two'}];base={'name':'uncreated','description':'Warm','samples':samples}
            requests=[dict(base,indices=[]),dict(base,indices=[0,0]),dict(base,global_seed=-2),dict(base,seeds=[-1,-2])]
            with patch.object(routes,'DATASET_BUILDER_DIR',str(root)),patch.object(routes,'check_global_gpu_lock'),patch.object(routes,'reserve_background_task') as claim,patch.object(routes,'start_claimed_task_thread') as start,TestClient(self.api()) as client:
                for body in requests:
                    with self.subTest(body=body):
                        before=copy.deepcopy(body);response=client.post('/api/dataset_builder/generate_batch',json=body)
                        self.assertIn(response.status_code,(400,422),response.text)
                        self.assertFalse((root/'uncreated').exists());self.assertEqual(before,body)
                claim.assert_not_called();start.assert_not_called()

    def test_single_seed_bound_and_valid_batch_seed_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'voice';work.mkdir();(work/'state.json').write_text(json.dumps({'samples':[{'text':'Known','status':'pending'}]}))
            with patch.object(routes,'DATASET_BUILDER_DIR',str(root)),patch.object(routes,'check_global_gpu_lock'),patch.object(routes,'claim_gpu_task') as single_claim,patch.object(routes,'reserve_background_task',return_value='fixture') as batch_claim,patch.object(routes,'start_claimed_task_thread') as start,patch.object(routes.project_manager,'get_engine',return_value=None),TestClient(self.api(),raise_server_exceptions=False) as client:
                for seed in (-2,-999):
                    response=client.post('/api/dataset_builder/generate_sample',json={'dataset_name':'voice','sample_index':0,'description':'Warm','text':'Known','seed':seed})
                    self.assertEqual(422,response.status_code,response.text)
                single_claim.assert_not_called()
                for seed in (-1,0,42):
                    body={'name':'voice','description':'Warm','samples':[{'text':'One'},{'text':'Two'}],'indices':[1,0],'global_seed':seed,'seeds':[-1,0]}
                    response=client.post('/api/dataset_builder/generate_batch',json=body)
                    self.assertEqual(200,response.status_code,response.text);self.assertEqual(2,response.json()['total'])
                self.assertEqual(3,batch_claim.call_count);self.assertEqual(3,start.call_count)

    def test_actual_asgi_can_ping_while_single_sample_worker_is_held_and_publishes_exact_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'voice';work.mkdir();(work/'state.json').write_text(json.dumps({'samples':[{'text':'Old','emotion':'calm','status':'pending'}]}))
            source=root/'source.wav';sf.write(source,np.full(240,.125),24000)
            entered=threading.Event();release=threading.Event();states={'dataset_builder':{'running':False,'logs':[]}};threads=[]
            def claim(task):states[task]['running']=True;threads.append(('claim',threading.get_ident()));return 'fixture'
            def finish(task,claim_id):states[task]['running']=False;threads.append(('release',threading.get_ident()))
            def render(**kwargs):
                threads.append(('render',threading.get_ident()));entered.set();self.assertTrue(release.wait(2));return str(source),24000
            timer=threading.Timer(.5,release.set);timer.start()
            async def exercise():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api()),base_url='http://fixture') as client:
                    task=asyncio.create_task(client.post('/api/dataset_builder/generate_sample',json={'dataset_name':'voice','sample_index':0,'description':'Warm','text':'Known','seed':0}))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait,2))
                        response=await asyncio.wait_for(client.get('/ping'),.2)
                        self.assertEqual(200,response.status_code);self.assertFalse(task.done(),'inference monopolized the event loop until it finished')
                        self.assertTrue(states['dataset_builder']['running'])
                        self.assertEqual('voice',states['dataset_builder']['dataset_name'])
                    finally:release.set();response=await task
                    return response
            try:
                with patch.object(routes,'DATASET_BUILDER_DIR',str(root)),patch.object(routes,'process_state',states),patch.object(routes,'check_global_gpu_lock'),patch.object(routes,'claim_gpu_task',side_effect=claim),patch.object(routes,'release_gpu_task_claim',side_effect=finish),patch.object(routes.project_manager,'get_engine',return_value=SimpleNamespace(generate_voice_design=render)):
                    response=asyncio.run(exercise())
            finally:release.set();timer.cancel();timer.join()
            self.assertEqual(200,response.status_code,response.text);self.assertEqual('done',response.json()['status'])
            self.assertFalse(states['dataset_builder']['running']);self.assertEqual(['claim','render','release'],[name for name,_ in threads])
            self.assertEqual(1,len({ident for _,ident in threads}));self.assertNotEqual(threading.get_ident(),threads[0][1])
            self.assertEqual(source.read_bytes(),(work/'sample_000.wav').read_bytes())
            row=json.loads((work/'state.json').read_text())['samples'][0];self.assertEqual(('done','Known',0),(row['status'],row['text'],row['seed']))

    def test_cancelled_request_does_not_release_the_still_running_worker_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'voice';work.mkdir();(work/'state.json').write_text(json.dumps({'samples':[{'text':'Old','status':'pending'}]}))
            source=root/'source.wav';sf.write(source,np.full(240,.25),24000)
            entered=threading.Event();release=threading.Event();finished=threading.Event();states={'dataset_builder':{'running':False}}
            def claim(task):states[task]['running']=True;return 'owned-fixture'
            def finish(task,claim_id):
                self.assertEqual('owned-fixture',claim_id);states[task]['running']=False;finished.set()
            def render(**kwargs):entered.set();self.assertTrue(release.wait(2));return str(source),24000
            async def exercise():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api()),base_url='http://fixture') as client:
                    task=asyncio.create_task(client.post('/api/dataset_builder/generate_sample',json={'dataset_name':'voice','sample_index':0,'description':'Warm','text':'Known','seed':0}))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait,2));task.cancel()
                        with self.assertRaises(asyncio.CancelledError):await task
                        self.assertTrue(states['dataset_builder']['running']);self.assertFalse(finished.is_set())
                    finally:release.set()
                    self.assertTrue(await asyncio.to_thread(finished.wait,2))
            with patch.object(routes,'DATASET_BUILDER_DIR',str(root)),patch.object(routes,'process_state',states),patch.object(routes,'check_global_gpu_lock'),patch.object(routes,'claim_gpu_task',side_effect=claim),patch.object(routes,'release_gpu_task_claim',side_effect=finish),patch.object(routes.project_manager,'get_engine',return_value=SimpleNamespace(generate_voice_design=render)):
                asyncio.run(exercise())
            self.assertFalse(states['dataset_builder']['running']);self.assertEqual(source.read_bytes(),(work/'sample_000.wav').read_bytes())
