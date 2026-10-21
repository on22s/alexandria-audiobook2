"""The HTTP recheck owns a CPU task through request cancellation and cleanup."""
import asyncio
from contextlib import ExitStack
import copy
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI,HTTPException
import httpx
import core
from routers import editor
from tests import test_editor_speech_verification as fixtures
from tests.test_speech_verification_receipt import get_report


class SpeechVerificationCPUAdmissionTests(unittest.TestCase):
    def test_request_cancellation_retains_cpu_claim_and_late_report_cannot_publish(self):
        fixture=fixtures.EditorSpeechVerificationTests()
        with fixture.fixture() as (root,manager,before,prior,flags,unused):
            state=copy.deepcopy(core.process_state)
            for row in state.values():row['running']=False
            arrived=threading.Event();release=threading.Event();finished=threading.Event()
            def verify(path,text,*args,**kwargs):
                try:
                    self.assertTrue(core._task_claims['speech_verification']['cpu_only'])
                    arrived.set()
                    if not release.wait(3):raise TimeoutError('recheck fixture release missing')
                    return get_report(path,text)
                finally:finished.set()
            async def run():
                api=FastAPI();api.include_router(editor.router)
                token=editor.get_snapshot_digest(editor.get_editor_book_identity(root))
                body={'expected_book_token':token,'expected_uid':before['uid'],
                      'expected_audio_revision':before['audio_revision']}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://fixture') as client:
                    task=asyncio.create_task(client.post('/api/chunks/0/speech_verification',json=body))
                    try:
                        self.assertTrue(await asyncio.to_thread(arrived.wait,1))
                        self.assertFalse(task.done())
                        self.assertTrue((root/'.task_ownership/task-cpu__speech_verification.lock').is_file())
                        self.assertEqual(400,(await client.post('/api/chunks/0/speech_verification',json=body)).status_code)
                        state['audio']['running']=False
                        try:
                            with self.assertRaises(HTTPException):
                                with core.ensure_book_switch_allowed():self.fail('Book switch was allowed')
                        finally:state['audio']['running']=True
                        with self.assertRaises(HTTPException):core.claim_gpu_task('voice_design')
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):await task
                        self.assertEqual('started',core._task_claims['speech_verification']['phase'])
                        self.assertEqual(200,(await client.post('/api/speech_verification/cancel')).status_code)
                    finally:
                        release.set();await asyncio.to_thread(finished.wait,2)
                        if not task.done():await task
                for _ in range(100):
                    if 'speech_verification' not in core._task_claims:break
                    await asyncio.sleep(.01)
                self.assertNotIn('speech_verification',core._task_claims)
                self.assertFalse(state['speech_verification']['running'])
                self.assertEqual([before],manager.load_chunks())
                self.assertEqual(prior,(root/before['audio_path']).read_bytes())
            with ExitStack() as stack:
                for module,name,value in ((core,'process_state',state),(editor,'process_state',state),
                    (core,'_task_claims',{}),(core,'_gpu_leases',{}),(core,'DATA_DIR',str(root)),
                    (editor,'project_manager',manager),(editor,'SCRIPT_PATH',str(root/'annotated_script.json'))):
                    stack.enter_context(patch.object(module,name,value))
                gpu=stack.enter_context(patch.object(core,'acquire_gpu_lock',side_effect=AssertionError('CPU check acquired GPU')))
                stack.enter_context(patch('editor_speech_verification.verify_finished_speech',side_effect=verify))
                stack.enter_context(patch('speech_verification_runtime.get_verification_runtime',return_value={}))
                state['audio']['running']=True
                try:
                    asyncio.run(run());gpu.assert_not_called()
                finally:
                    release.set()
                    for name,owner in list(core._task_claims.items()):core.release_gpu_task_claim(name,owner['id'])


if __name__ == '__main__':
    unittest.main()
