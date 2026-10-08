"""Ownership and ASGI admission lifetimes without GPU/model inference."""
import asyncio
import copy
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from fastapi import BackgroundTasks,FastAPI,HTTPException
from fastapi.testclient import TestClient
import core
from routers import script


class TaskClaimOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.state=copy.deepcopy(core.process_state)
        for state in self.state.values():state['running']=False
        self.patches=[patch.object(core,'process_state',self.state),patch.object(core,'_task_claims',{}),
            patch.object(core,'_gpu_leases',{}),patch.object(core,'acquire_gpu_lock',return_value=None),
            patch.object(core,'llm_is_on_this_gpu',return_value=True)]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def test_actual_review_route_registration_failure_rolls_back_and_new_claim_can_start(self):
        class RejectTasks(BackgroundTasks):
            def add_task(self,*args,**kwargs):raise RuntimeError('registration rejected')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'script.json';path.write_text('[]')
            with patch.object(script,'SCRIPT_PATH',str(path)),self.assertRaisesRegex(RuntimeError,'registration rejected'):
                asyncio.run(script.review_script(RejectTasks()))
        self.assertFalse(self.state['review']['running']);self.assertNotIn('review',core._task_claims)
        owner=core.claim_gpu_task('review');self.assertTrue(core.release_gpu_task_claim('review',owner))

    def test_stale_claim_cannot_release_new_owner_or_run_queued_callback(self):
        tasks=BackgroundTasks();callback=Mock()
        first=core.schedule_claimed_background_task(tasks,'audio',callback)
        self.assertTrue(core.release_gpu_task_claim('audio',first,pending_only=True))
        second=core.claim_gpu_task('audio')
        self.assertNotEqual(first,second);self.assertFalse(core.release_gpu_task_claim('audio',first))
        asyncio.run(tasks());callback.assert_not_called();self.assertTrue(self.state['audio']['running'])
        self.assertTrue(core.release_gpu_task_claim('audio',second))

    def test_response_failure_releases_unstarted_claim_and_success_runs_real_background_callback(self):
        app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);calls=[]
        @app.post('/fail')
        async def fail(tasks:BackgroundTasks):
            core.schedule_claimed_background_task(tasks,'audio',lambda:calls.append('failed route'))
            raise HTTPException(409,'response construction failed')
        @app.post('/ok')
        async def ok(tasks:BackgroundTasks):
            core.schedule_claimed_background_task(tasks,'audio',lambda:calls.append('completed'))
            return {'status':'started'}
        with TestClient(app) as client:
            self.assertEqual(409,client.post('/fail').status_code)
            self.assertFalse(self.state['audio']['running']);self.assertEqual([],calls)
            self.assertEqual(200,client.post('/ok').status_code)
            self.assertEqual(['completed'],calls);self.assertFalse(self.state['audio']['running'])
            self.assertEqual({},core._task_claims)

    def test_asgi_send_failure_releases_pending_only(self):
        tasks=BackgroundTasks();calls=[]
        async def application(scope,receive,send):
            core.schedule_claimed_background_task(tasks,'audio',lambda:calls.append(True))
            await send({'type':'http.response.start','status':200,'headers':[]})
        async def send(message):raise OSError('client disconnected before worker start')
        async def receive():return {'type':'http.request','body':b''}
        with self.assertRaisesRegex(OSError,'client disconnected'):
            asyncio.run(core.TaskClaimMiddleware(application)({'type':'http'},receive,send))
        self.assertFalse(self.state['audio']['running']);asyncio.run(tasks());self.assertEqual([],calls)

    def test_shutdown_preserves_started_callback_even_if_legacy_flag_clears_early(self):
        tasks=BackgroundTasks();arrived=threading.Event();release=threading.Event();errors=[]
        def work():
            self.state['audio']['running']=False
            arrived.set()
            if not release.wait(5):raise AssertionError('owned worker was not released')
        core.schedule_claimed_background_task(tasks,'audio',work)
        def run():
            try:asyncio.run(tasks())
            except BaseException as error:errors.append(error)
        worker=threading.Thread(target=run);worker.start()
        try:
            self.assertTrue(arrived.wait(2));core.release_pending_task_claims()
            self.assertTrue(core.is_task_running('audio'))
            with self.assertRaises(HTTPException):core.claim_gpu_task('audio')
            with self.assertRaises(HTTPException):core.claim_gpu_task('dataset_builder')
        finally:release.set();worker.join(5)
        self.assertFalse(worker.is_alive());self.assertEqual([],errors);self.assertFalse(core.is_task_running('audio'))

    def test_pending_shutdown_releases_owned_kernel_handle_once(self):
        handle=object();tasks=BackgroundTasks()
        with patch.object(core,'acquire_gpu_lock',return_value=handle),patch.object(core,'release_gpu_lock') as release,patch.object(core,'_reap_gpu_leases',return_value=None):
            core.schedule_claimed_background_task(tasks,'audio',Mock())
            core.release_pending_task_claims();core.release_pending_task_claims();asyncio.run(tasks())
            release.assert_called_once_with(handle)
        self.assertFalse(self.state['audio']['running']);self.assertEqual({},core._gpu_leases)

    def test_cancelled_asgi_request_keeps_started_worker_admitted_until_completion(self):
        arrived=threading.Event();release=threading.Event();finished=threading.Event();tasks=BackgroundTasks()
        def work():
            arrived.set()
            try:
                if not release.wait(5):raise AssertionError('worker not released')
            finally:finished.set()
        async def application(scope,receive,send):
            core.schedule_claimed_background_task(tasks,'audio',work)
            await tasks()
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):pass
        async def exercise():
            request=asyncio.create_task(core.TaskClaimMiddleware(application)({'type':'http'},receive,send))
            try:
                self.assertTrue(await asyncio.to_thread(arrived.wait,2))
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):await request
                self.assertTrue(core.is_task_running('audio'))
                with self.assertRaises(HTTPException):core.claim_gpu_task('dataset_builder')
            finally:
                release.set();self.assertTrue(await asyncio.to_thread(finished.wait,2))
                for _ in range(100):
                    if not core.is_task_running('audio'):break
                    await asyncio.sleep(.01)
            self.assertFalse(core.is_task_running('audio'))
        asyncio.run(exercise())

    def test_early_reservation_adopts_same_token_and_stale_registration_preserves_new_owner(self):
        tasks=BackgroundTasks();callback=Mock()
        owner=core.reserve_background_task('audio')
        self.assertEqual(owner,core.register_claimed_background_task(tasks,'audio',owner,callback,'kept argument'))
        self.assertEqual(owner,core._task_claims['audio']['id'])
        asyncio.run(tasks());callback.assert_called_once_with('kept argument')
        newer=core.reserve_background_task('audio')
        with self.assertRaises(HTTPException):core.register_claimed_background_task(BackgroundTasks(),'audio',owner,callback)
        self.assertEqual(newer,core._task_claims['audio']['id']);self.assertTrue(core.is_task_running('audio'))
        core.release_pending_task_claims()

    def test_early_setup_exception_releases_reservation_in_request_finally(self):
        async def application(scope,receive,send):
            core.reserve_background_task('audio')
            raise ValueError('setup failed before worker registration')
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):pass
        with self.assertRaisesRegex(ValueError,'setup failed'):
            asyncio.run(core.TaskClaimMiddleware(application)({'type':'http'},receive,send))
        self.assertFalse(core.is_task_running('audio'));self.assertEqual({},core._task_claims)

    def test_actual_editor_and_script_http_registration_failures_release_only_their_claim(self):
        from routers import editor
        app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(editor.router);app.include_router(script.router)
        manager=Mock();manager.merge_audio.return_value=(True,'merged');manager.export_audacity.return_value=(True,'exported')
        with tempfile.TemporaryDirectory() as tmp:
            import json
            path=Path(tmp)/'script.json';path.write_text('[]')
            source=Path(tmp)/'source.txt';source.write_text('alpha beta')
            (Path(tmp)/'state.json').write_text(json.dumps({'input_file_path':str(source)}))
            manager.load_chunks.return_value=[{'text':'alpha beta'}]
            with patch.object(script,'SCRIPT_PATH',str(path)),patch.object(script,'process_state',self.state),patch.object(editor,'DATA_DIR',tmp),patch.object(editor,'SCRIPT_PATH',str(path)),patch.object(editor,'process_state',self.state),patch.object(editor,'project_manager',manager),patch.object(script,'run_process') as run,TestClient(app,raise_server_exceptions=False) as client:
                for url,name in (('/api/merge','audio'),('/api/export_audacity','audacity_export'),('/api/find_nicknames','nicknames'),('/api/review_script','review')):
                    with self.subTest(url=url):
                        with patch.object(BackgroundTasks,'add_task',side_effect=RuntimeError('registration rejected')):
                            response=client.post(url)
                        self.assertEqual(500,response.status_code)
                        self.assertFalse(core.is_task_running(name));self.assertNotIn(name,core._task_claims)
                        response=client.post(url)
                        self.assertEqual(200,response.status_code,response.text)
                        self.assertFalse(core.is_task_running(name));self.assertNotIn(name,core._task_claims)
                self.assertEqual(2,run.call_count)
                self.assertEqual(['nicknames','review'],[call.args[1] for call in run.call_args_list])
                manager.merge_audio.assert_called_once();manager.export_audacity.assert_called_once()

    def test_owned_thread_start_failure_and_stale_token_release_only_their_owner(self):
        callback=Mock();owner=core.reserve_background_task('dataset_builder')
        with patch.object(core.threading.Thread,'start',side_effect=RuntimeError('thread unavailable')),self.assertRaisesRegex(RuntimeError,'thread unavailable'):
            core.start_claimed_task_thread('dataset_builder',owner,callback)
        callback.assert_not_called();self.assertFalse(core.is_task_running('dataset_builder'))
        newer=core.reserve_background_task('dataset_builder')
        with self.assertRaises(HTTPException):core.start_claimed_task_thread('dataset_builder',owner,callback)
        self.assertEqual(newer,core._task_claims['dataset_builder']['id']);core.release_pending_task_claims()

    def test_thread_start_observation_failure_keeps_already_alive_worker_admitted(self):
        arrived=threading.Event();release=threading.Event();launched=[];errors=[]
        def work():
            arrived.set()
            if not release.wait(5):errors.append('owned worker not released')
        start=core.threading.Thread.start
        def started(worker):
            start(worker);launched.append(worker)
            self.assertTrue(arrived.wait(2))
            raise RuntimeError('observer failed after OS launch')
        owner=core.reserve_background_task('dataset_builder')
        try:
            with patch.object(core.threading.Thread,'start',new=started),self.assertRaisesRegex(RuntimeError,'observer failed'):
                core.start_claimed_task_thread('dataset_builder',owner,work)
            core.release_pending_task_claims();self.assertTrue(core.is_task_running('dataset_builder'))
            with self.assertRaises(HTTPException):core.claim_gpu_task('audio')
        finally:
            release.set()
            for worker in launched:worker.join(5);self.assertFalse(worker.is_alive())
        self.assertEqual([],errors);self.assertFalse(core.is_task_running('dataset_builder'))

    def test_benchmark_actual_http_setup_and_registration_failure_roll_back_before_worker_runs(self):
        from routers import benchmark
        manifest={'stage':'audacity_export','targets':['local'],'fixtures':[{'id':'export'}]}
        preflight={'manifest':manifest,'environments':{'local':{}},'preflight_id':'CPU-admission'}
        app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(benchmark.router)
        with tempfile.TemporaryDirectory() as tmp,patch.object(benchmark,'REPORTS_DIR',tmp),patch.object(benchmark,'process_state',self.state),patch.object(benchmark,'_build_benchmark_preflight',return_value=preflight),patch.object(benchmark,'_run_claimed_background_task',side_effect=lambda name,work:work()),patch.object(benchmark,'run_export_benchmark') as export,TestClient(app,raise_server_exceptions=False) as client:
            payload={'manifest':manifest,'preflight_id':'CPU-admission'}
            for failure in ('setup','registration'):
                with self.subTest(failure=failure):
                    provider=patch.object(benchmark,'_init_batch_state',side_effect=ValueError('setup failed')) if failure=='setup' else patch.object(BackgroundTasks,'add_task',side_effect=RuntimeError('registration rejected'))
                    with provider:self.assertEqual(500,client.post('/api/benchmark/start',json=payload).status_code)
                    self.assertFalse(core.is_task_running('benchmark'));self.assertNotIn('benchmark',core._task_claims)
                    self.assertEqual('failed', client.get('/api/benchmark/status').json()['status'])
                    self.assertEqual(400, client.post('/api/benchmark/cancel').status_code)
                    export.assert_not_called()
            response=client.post('/api/benchmark/start',json=payload)
            self.assertEqual(200,response.status_code,response.text)
            self.assertFalse(core.is_task_running('benchmark'));export.assert_called_once()
            self.assertEqual(manifest,export.call_args.args[0])

    def test_actual_book_load_keeps_started_export_reserved_after_legacy_flag_clears(self):
        from routers import scripts_library as library
        arrived=threading.Event();release=threading.Event()
        def export():
            self.state['audacity_export']['running']=False;arrived.set()
            if not release.wait(5):raise AssertionError('export worker not released')
        owner=core.reserve_background_task('audacity_export');worker=core.start_claimed_task_thread('audacity_export',owner,export)
        try:
            self.assertTrue(arrived.wait(2))
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);saved=root/'saved';saved.mkdir();data=root/'data';data.mkdir()
                active=data/'annotated.json';voices=data/'voices.json';active.write_text('[{"speaker":"OLD","text":"old"}]');voices.write_text('{}')
                (saved/'new.json').write_text('[{"speaker":"NEW","text":"new"}]');before=(active.read_bytes(),voices.read_bytes())
                app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(library.router)
                with patch.object(library,'SCRIPTS_DIR',str(saved)),patch.object(library,'DATA_DIR',str(data)),patch.object(library,'SCRIPT_PATH',str(active)),patch.object(library,'VOICE_CONFIG_PATH',str(voices)),patch.object(library,'process_state',self.state),TestClient(app) as client:
                    response=client.post('/api/scripts/load',json={'name':'new'})
                    self.assertEqual(409,response.status_code,response.text)
                    self.assertIn('audacity_export',response.json()['detail'])
                self.assertEqual(before,(active.read_bytes(),voices.read_bytes()));self.assertTrue(core.is_task_running('audacity_export'))
        finally:release.set();worker.join(5);self.assertFalse(worker.is_alive())
        self.assertFalse(core.is_task_running('audacity_export'))
