"""Concurrent API progress during real route preflight and narrator parity."""
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI
import core
from routers import script


class ScriptDispatchResponsivenessTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
            root=Path(tmp);source=root/'book.txt';source.write_text('Alexis entered. Alexis spoke.')
            (root/'state.json').write_text(json.dumps({'input_file_path':str(source)}))
            states=copy.deepcopy(core.process_state)
            for state in states.values():state['running']=False
            for owner,name,value in ((script,'DATA_DIR',tmp),(script,'UPLOADS_DIR',tmp),(script,'SCRIPTS_DIR',tmp),
                                     (script,'SCRIPT_PATH',str(root/'annotated_script.json')),
                                     (script,'process_state',states),(core,'process_state',states),
                                     (core,'_task_claims',{}),(core,'_gpu_leases',{})):
                stack.enter_context(patch.object(owner,name,value))
            stack.enter_context(patch.object(core,'acquire_gpu_lock',return_value=None))
            stack.enter_context(patch.object(script,'get_active_reasoning_effort',return_value=None))
            stack.enter_context(patch.object(script,'load_app_config',return_value={}))
            stack.enter_context(patch.object(script,'run_process',return_value=None))
            stack.enter_context(patch.object(script,'_init_task_log',return_value=str(root/'task.log')))
            stack.enter_context(patch.object(script,'ensure_ideal_settings',return_value=(True,{},'CPU stand-in')))
            stack.enter_context(patch.object(script,'_get_batch_script_workers',return_value=(1,10,32768)))
            stack.enter_context(patch.object(script,'_stream_subprocess_to_logs',return_value=(1,[])))
            app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(script.router)
            @app.get('/ping')
            async def ping():return {'ok':True}
            yield root,app

    def test_api_ping_progresses_while_single_batch_and_retry_preflight_waits(self):
        for route in ('/api/generate_script','/api/generate_script/batch/start','/api/generate_script/retry'):
            with self.subTest(route=route),self.fixture() as (root,app):
                entered=threading.Event();release=threading.Event();finished=threading.Event()
                if route.endswith('/retry'):
                    state=json.loads((root/'state.json').read_text());state['script_generation_input_file']=state['input_file_path']
                    state['script_generation_options']={'first_person_narrator':'ALEXIS'}
                    (root/'state.json').write_text(json.dumps(state))
                    Path(script.three_pass_manifest_path(script.SCRIPT_PATH)).write_text('{"status":"failed"}')
                def slow(*args,**kwargs):
                    entered.set();release.wait(1);finished.set()
                    return ('Alexis entered. Alexis spoke. Alexis left.',[]) if route.endswith('/retry') else None
                async def run():
                    transport=httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
                        payload={'tasks':[{'filename':'book.txt'}]} if route.endswith('/start') else {}
                        request=asyncio.create_task(client.post(route,json=payload))
                        try:
                            await asyncio.sleep(.03)
                            ping=await client.get('/ping');self.assertEqual(200,ping.status_code)
                            self.assertTrue(entered.is_set());self.assertFalse(finished.is_set(),'preflight blocked unrelated API progress')
                        finally:release.set();response=await request
                        self.assertEqual(200,response.status_code,response.text)
                target='_read_and_validate_batch_script_source' if route.endswith('/retry') else 'three_pass_refusal'
                with patch.object(script,target,side_effect=slow):asyncio.run(run())
                self.assertFalse(core.is_task_running('script'));self.assertFalse(core.is_task_running('batch_script'))
                self.assertEqual({},core._task_claims)

    def test_single_and_batch_reject_same_unattested_narrator_without_state_mutation(self):
        for route in ('/api/generate_script','/api/generate_script/batch/start'):
            with self.subTest(route=route),self.fixture() as (root,app):
                before=(root/'state.json').read_bytes()
                async def run():
                    payload={'first_person_narrator':'ALEXIS'}
                    if route.endswith('/start'):payload={'tasks':[{'filename':'book.txt','first_person_narrator':'ALEXIS'}]}
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                        response=await client.post(route,json=payload)
                    self.assertEqual(400,response.status_code);self.assertIn('at least three times',response.json()['detail'])
                with patch.object(core,'claim_gpu_task',wraps=core.claim_gpu_task) as claim:
                    asyncio.run(run());claim.assert_not_called()
                self.assertEqual(before,(root/'state.json').read_bytes())

    def test_batch_preparation_reads_each_real_source_once_and_preserves_request(self):
        with self.fixture() as (root,_app):
            (root/'second.txt').write_text('A different source story.')
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt'),
                                                     script.BatchScriptTask(filename='second.txt')])
            before=request.dict();read=script._read_and_validate_batch_script_source
            with patch.object(script,'_read_and_validate_batch_script_source',wraps=read) as observed:
                self.assertEqual([None,None],script.get_validated_batch_script_narrators(request))
            self.assertEqual([str(root/'book.txt'),str(root/'second.txt')],
                             [call.args[0]['input_path'] for call in observed.call_args_list])
            self.assertEqual(before,request.dict())

    def test_cancelled_request_cannot_reserve_a_late_preflight_worker(self):
        for route in ('/api/generate_script','/api/generate_script/batch/start'):
            with self.subTest(route=route),self.fixture() as (_root,app):
                entered=threading.Event();release=threading.Event();ended=threading.Event();finished=threading.Event();errors=[]
                def slow(*args,**kwargs):entered.set();release.wait(3);finished.set();return None
                reserve=core.reserve_background_task
                def observed(*args,**kwargs):
                    try:return reserve(*args,**kwargs)
                    except BaseException as error:errors.append(error);raise
                    finally:ended.set()
                async def run():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                        payload={'tasks':[{'filename':'book.txt'}]} if route.endswith('/start') else {}
                        request=asyncio.create_task(client.post(route,json=payload))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait,2))
                            request.cancel()
                            with self.assertRaises(asyncio.CancelledError):await request
                        finally:release.set()
                        if route.endswith('/start'):
                            self.assertTrue(await asyncio.to_thread(finished.wait,2))
                            self.assertFalse(ended.is_set());self.assertEqual([],errors)
                        else:
                            self.assertTrue(await asyncio.to_thread(ended.wait,2))
                            self.assertEqual(1,len(errors));self.assertEqual(409,errors[0].status_code)
                            self.assertIn('no longer active',errors[0].detail)
                        self.assertFalse(core.is_task_running('script'));self.assertFalse(core.is_task_running('batch_script'))
                        self.assertEqual({},core._task_claims)
                with patch.object(script,'three_pass_refusal',side_effect=slow),patch.object(core,'reserve_background_task',side_effect=observed):
                    asyncio.run(run())
