"""Restore routes use the actual shared audio lease, cancellation and stores.

Encoded media is real; native serving identity is explicitly a CPU fixture.
"""
import asyncio
from contextlib import contextmanager, ExitStack
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
import core
from routers import editor
from synthesis_recovery_serving import NativeRecoveryServingIdentity
from synthesis_workspace import is_synthesis_workspace_supported
from tests.test_support import use_private_cpu_fixture_lock
from tests import test_take_restoration as restore_cases


@unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
class EditorTakeRestorationTests(unittest.TestCase):
    @contextmanager
    def fixture(self,journal=False):
        with restore_cases.TakeRestorationTests().prepared(journal) as args,ExitStack() as stack:
            root,manager,_,request,*_=args
            state=copy.deepcopy(core.process_state)
            for entry in state.values():entry['running']=False
            for module,name,value in (
                (core,'process_state',state),(editor,'process_state',state),
                (core,'_task_claims',{}),(core,'_gpu_leases',{}),(core,'DATA_DIR',str(root)),
                (editor,'DATA_DIR',str(root)),(editor,'SCRIPT_PATH',str(root/'annotated_script.json')),
                (editor,'CONFIG_PATH',manager.config_path),(editor,'project_manager',manager)):
                stack.enter_context(patch.object(module,name,value))
            stack.enter_context(use_private_cpu_fixture_lock())
            api=FastAPI();api.include_router(editor.router)
            client=stack.enter_context(TestClient(api))
            try:yield args,client,state
            finally:
                for name,claim in list(core._task_claims.items()):core.release_gpu_task_claim(name,claim['id'])

    def test_real_route_selects_proven_bytes_and_releases_its_audio_lease(self):
        for journal in (False,True):
            with self.subTest(journal=journal),self.fixture(journal) as (args,client,state):
                root,manager,_,request,origin,original,*_=args
                response=client.post('/api/chunks/0/take_restore',json=vars(request))
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json(),{'status':'started'})
                self.assertEqual(state['audio']['status'],'done',state['audio'])
                self.assertFalse(state['audio']['running']);self.assertEqual(core._task_claims,{})
                selected=manager.get_saved_chunks()[0]
                self.assertEqual(selected['audio_revision'],origin['audio_revision'])
                self.assertEqual((root/selected['audio_path']).read_bytes(),original)

    def test_request_refusal_rolls_back_reservation_without_loading_engine_or_repairing_rows(self):
        with self.fixture() as (args,client,state):
            root,manager,_,request,_,_,current,displaced,_=args
            before=(root/'chunks.json').read_bytes()
            with patch.object(manager,'get_engine',side_effect=AssertionError('refused request loaded engine')):
                for change,code in (({'expected_take':True},422),({'expected_uid':'other'},409),
                                    ({'expected_audio_path':'wrong.wav'},409),({'version':'f'*32},409)):
                    response=client.post('/api/chunks/0/take_restore',json={**vars(request),**change})
                    self.assertEqual(response.status_code,code,response.text)
                    self.assertEqual(core._task_claims,{})
            self.assertEqual((root/'chunks.json').read_bytes(),before)
            self.assertEqual((root/current['audio_path']).read_bytes(),displaced)

    def test_queued_restore_excludes_other_audio_then_acknowledged_cancel_avoids_engine(self):
        with self.fixture() as (args,_,state):
            root,manager,_,request,_,_,current,displaced,_=args
            async def exercise():
                tasks=BackgroundTasks();model=editor.TakeRestoreRequest(**vars(request))
                await editor.restore_take_snapshot(0,model,tasks)
                claim=core._task_claims['audio']['id']
                with self.assertRaises(HTTPException):
                    await editor.restore_take_snapshot(0,model,BackgroundTasks())
                self.assertEqual(core._task_claims['audio']['id'],claim)
                self.assertEqual(await editor.cancel_audio(),{'status':'cancelling'})
                self.assertEqual(core._task_claims['audio']['id'],claim)
                await tasks()
            with patch.object(manager,'get_engine',side_effect=AssertionError('queued cancel loaded engine')):
                asyncio.run(exercise())
            self.assertEqual(core._task_claims,{})
            self.assertEqual(state['audio']['status'],'cancelled')
            self.assertEqual((root/current['audio_path']).read_bytes(),displaced)
            self.assertEqual(manager.get_saved_chunks()[0]['audio_revision'],current['audio_revision'])

    def test_cancel_during_final_native_check_retains_lease_until_worker_cleanup(self):
        for journal in (False,True):
            with self.subTest(journal=journal),self.fixture(journal) as (args,client,state):
                root,manager,_,request,_,_,current,displaced,_=args
                calls=[]
                def identity(*args,**kwargs):
                    calls.append(1)
                    if len(calls)==2:
                        claim=core._task_claims['audio']['id']
                        self.assertEqual(editor._apply_audio_cancel(),{'status':'cancelling'})
                        self.assertEqual(core._task_claims['audio']['id'],claim)
                        self.assertTrue(core.is_task_running('audio'))
                    return NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)
                with patch('synthesis_recovery_serving.capture_native_custom_serving_identity',side_effect=identity):
                    response=client.post('/api/chunks/0/take_restore',json=vars(request))
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(calls,[1,1]);self.assertEqual(state['audio']['status'],'cancelled')
                self.assertEqual(core._task_claims,{})
                row=manager.get_saved_chunks()[0]
                for field in ('audio_path','audio_revision','take'):self.assertEqual(row[field],current[field])
                self.assertEqual((root/row['audio_path']).read_bytes(),displaced)

    def test_worker_dependency_refusal_is_failed_status_with_prior_selection_preserved(self):
        for journal in (False,True):
            with self.subTest(journal=journal),self.fixture(journal) as (args,client,state):
                root,manager,_,request,_,_,current,displaced,_=args
                Path(manager.voice_config_path).write_text('{"NARRATOR":{"type":"custom","voice":"Ryan","seed":43}}')
                response=client.post('/api/chunks/0/take_restore',json=vars(request))
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(state['audio']['status'],'failed')
                self.assertIn('voice or seed',state['audio']['error'])
                self.assertEqual(core._task_claims,{})
                row=manager.get_saved_chunks()[0]
                for field in ('audio_path','audio_revision','take'):self.assertEqual(row[field],current[field])
                self.assertEqual((root/row['audio_path']).read_bytes(),displaced)


if __name__=='__main__':unittest.main()
