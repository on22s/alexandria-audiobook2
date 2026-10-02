"""Native task admission/status exposes export outcomes independent of prose logs."""
from contextlib import contextmanager,ExitStack
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import editor,script
from tests.test_chapter_export import _project


class ExportTaskResultTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as root,ExitStack() as stack:
            states={name:{'running':False,'logs':[],'cancel':False} for name in core.process_state}
            for module in [core,editor,script]:
                stack.enter_context(patch.object(module,'process_state',states));stack.enter_context(patch.object(module,'DATA_DIR',root))
            stack.enter_context(patch.object(core,'_task_claims',{}));stack.enter_context(patch.object(core,'_gpu_leases',{}))
            app=FastAPI();app.include_router(editor.router);app.include_router(script.router)
            with TestClient(app) as client:yield root,states,client

    def test_native_http_success_failure_exception_and_cancellation_have_results(self):
        cases=[(True,'No complete keyword','done'),(False,'Export complete: deliberately misleading','failed'),(False,'Export cancelled','cancelled'),(RuntimeError('<script>encoding error</script>'),None,'failed')]
        with self.fixture() as (_,states,client):
            for task,route,method in [('audacity_export','/api/export_audacity','export_audacity'),('m4b_export','/api/merge_m4b','merge_m4b'),('chapter_export','/api/export_chapters','export_chapters')]:
                for success,message,status in cases:
                    with self.subTest(task=task,status=status),patch.object(editor.project_manager,method,**({'side_effect':success} if isinstance(success,Exception) else {'return_value':(success,message)})):
                        states[task]['result']={'status':'done','message':'prior run'}
                        response=client.post(route,json={});self.assertEqual(200,response.status_code,response.text)
                        fetched=client.get('/api/status/'+task);self.assertEqual(200,fetched.status_code,fetched.text)
                        data=fetched.json();self.assertFalse(data['running']);self.assertEqual({'status':status,'message':str(success) if isinstance(success,Exception) else message},data.get('result'))
                        self.assertTrue(data['logs']);self.assertFalse(core._task_claims)

    def test_held_native_worker_clears_prior_result_and_duplicate_does_not_reset_it(self):
        with self.fixture() as (_,states,client):
            entered=threading.Event();release=threading.Event();responses=[]
            def held():entered.set();self.assertTrue(release.wait(3));return True,'native success'
            def request():responses.append(client.post('/api/merge_m4b',json={}))
            states['m4b_export']['result']={'status':'done','message':'old output'}
            with patch.object(editor.project_manager,'merge_m4b',side_effect=lambda **kw:held()):
                worker=threading.Thread(target=request);worker.start()
                try:
                    self.assertTrue(entered.wait(2));live=client.get('/api/status/m4b_export').json();self.assertTrue(live['running']);self.assertIsNone(live['result'])
                    before=copy.deepcopy(states['m4b_export']);duplicate=client.post('/api/merge_m4b',json={});self.assertEqual(400,duplicate.status_code);self.assertEqual(before,states['m4b_export'])
                finally:release.set();worker.join(timeout=3)
                self.assertFalse(worker.is_alive());self.assertEqual(200,responses[0].status_code)
                self.assertEqual({'status':'done','message':'native success'},client.get('/api/status/m4b_export').json()['result'])

    def test_native_cancel_reaches_real_chapter_export_and_preserves_prior_files(self):
        with self.fixture() as (root,states,client):
            pm,chunks=_project(root);out=Path(root,'chapter_exports');out.mkdir();prior=out/'prior.wav';prior.write_bytes(Path(root,'voicelines','c0.wav').read_bytes());before=prior.read_bytes()
            entered=threading.Event();release=threading.Event();responses=[];load=pm._load_chunks_with_audio
            def held_load(**kw):entered.set();self.assertTrue(release.wait(3));return load(**kw)
            def request():responses.append(client.post('/api/export_chapters',json={'format':'wav'}))
            with patch.object(editor,'project_manager',pm),patch.object(pm,'load_chunks',return_value=chunks),patch.object(pm,'_load_chunks_with_audio',side_effect=held_load):
                worker=threading.Thread(target=request);worker.start()
                try:
                    self.assertTrue(entered.wait(2));self.assertEqual(200,client.post('/api/export_chapters/cancel').status_code)
                finally:release.set();worker.join(timeout=3)
                self.assertFalse(worker.is_alive());self.assertEqual(200,responses[0].status_code)
                state=client.get('/api/status/chapter_export').json();self.assertEqual({'status':'cancelled','message':'Export cancelled'},state['result']);self.assertFalse(state['running']);self.assertFalse(state['cancel']);self.assertEqual(before,prior.read_bytes());self.assertEqual([prior],list(out.iterdir()))

    def test_real_audacity_archive_and_chapter_pcm_match_native_success_results(self):
        with self.fixture() as (root,states,client):
            pm,chunks=_project(root)
            with patch.object(editor,'project_manager',pm),patch.object(pm,'load_chunks',return_value=chunks):
                self.assertEqual(200,client.post('/api/export_audacity').status_code)
                result=client.get('/api/status/audacity_export').json().get('result');self.assertIsNotNone(result);self.assertEqual('done',result['status'])
                archive=Path(root,'audacity_export.zip');self.assertTrue(archive.is_file())
                with zipfile.ZipFile(archive) as bundle:self.assertTrue(any(name.endswith('.wav') for name in bundle.namelist()));self.assertIsNone(bundle.testzip())
                self.assertEqual(200,client.post('/api/export_chapters',json={'format':'wav'}).status_code)
                result=client.get('/api/status/chapter_export').json().get('result');self.assertIsNotNone(result);self.assertEqual('done',result['status'])
                manifest=json.loads(Path(root,'chapter_exports','manifest.json').read_text());self.assertTrue(manifest)
                import soundfile as sf
                clips=list(Path(root,'chapter_exports').glob('*.wav'));self.assertTrue(clips)
                for clip in clips:
                    pcm,rate=sf.read(clip);self.assertGreater(len(pcm),0);self.assertGreater(rate,0)
