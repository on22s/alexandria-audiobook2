"""Exercise batch output claims against real saved-library writes."""
import contextlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script, scripts_library
from utils import file_lock


class BatchScriptOutputAdmissionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, policy='cancel', other_outputs=()):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root=Path(tmp);source=root/'active.json';source.write_text('[{"text":"manual save"}]')
            output=root/'book.json'
            for name,value in (('SCRIPTS_DIR',tmp),('SCRIPT_PATH',str(source)),
                               ('VOICE_CONFIG_PATH',str(root/'absent-voices.json'))):
                stack.enter_context(patch.object(scripts_library,name,value))
            stack.enter_context(patch.object(scripts_library,'get_active_book_id',return_value='book-id'))
            stack.enter_context(patch.object(scripts_library,'_saved_book_meta_path',side_effect=lambda name:str(root/f'{name}.meta.json')))
            stack.enter_context(patch.object(script,'get_active_reasoning_effort',return_value=None))
            app=FastAPI();app.include_router(scripts_library.router)
            client=stack.enter_context(TestClient(app))
            job={'index':0,'filename':'book.txt','input_path':str(root/'book.txt'),
                 'output_path':str(output),'safe_stem':'book','collision_policy':policy,
                 'other_outputs':tuple(other_outputs)}
            state={'cancel':False,'logs':[],'tasks':[{'status':'pending'}]}
            yield root,source,output,job,state,client

    @staticmethod
    def generate(command,*args,**kwargs):
        Path(command[command.index('--output')+1]).write_text('[{"text":"generated"}]')
        return 0,[]

    def test_cancel_rechecks_save_between_planning_and_worker_start(self):
        with self.fixture() as (_,source,output,job,state,client):
            self.assertEqual(200,client.post('/api/scripts/save',json={'name':'book'}).status_code)
            before=output.read_bytes()
            with patch.object(script,'_stream_subprocess_to_logs',side_effect=self.generate) as generate:
                script._run_batch_script_job(job,state,None,1)
            self.assertEqual(before,output.read_bytes());generate.assert_not_called()
            self.assertEqual('failed',state['tasks'][0]['status'])

    def test_version_rechecks_disk_and_other_batch_reservations(self):
        with self.fixture('version') as (root,source,output,job,state,client):
            client.post('/api/scripts/save',json={'name':'book'})
            occupied=root/'book_2.json';occupied.write_text('occupied-version')
            reserved=root/'book_3.json';job={**job,'other_outputs':(str(reserved),)}
            before=output.read_bytes()
            with patch.object(script,'_stream_subprocess_to_logs',side_effect=self.generate):
                script._run_batch_script_job(job,state,None,1)
            self.assertEqual(before,output.read_bytes());self.assertEqual('occupied-version',occupied.read_text())
            self.assertFalse(reserved.exists());self.assertEqual('generated',__import__('json').loads((root/'book_4.json').read_text())[0]['text'])
            self.assertEqual('book_4',state['tasks'][0]['saved_as']);self.assertEqual('book',job['safe_stem'])

    def test_replace_backs_up_actual_saved_bytes_before_overwriting(self):
        with self.fixture('replace') as (root,source,output,job,state,client):
            client.post('/api/scripts/save',json={'name':'book'});before=output.read_bytes()
            with patch.object(script,'_stream_subprocess_to_logs',side_effect=self.generate):
                script._run_batch_script_job(job,state,None,1)
            self.assertEqual('[{"text":"generated"}]',output.read_text())
            backups=[p for p in root.iterdir() if p.name.startswith('book.json.bak')]
            self.assertEqual(1,len(backups));self.assertEqual(before,backups[0].read_bytes())

    def test_save_waits_until_generation_releases_same_output_claim(self):
        with self.fixture() as (_,source,output,job,state,client):
            entered=threading.Event();release=threading.Event();saved=threading.Event();errors=[]
            def generate(*args,**kwargs):
                entered.set()
                if not release.wait(5):raise AssertionError('generation not released')
                return self.generate(*args,**kwargs)
            def run():
                try:script._run_batch_script_job(job,state,None,1)
                except BaseException as error:errors.append(error)
            def save():
                try:
                    self.assertEqual(200,client.post('/api/scripts/save',json={'name':'book'}).status_code)
                    saved.set()
                except BaseException as error:errors.append(error)
            worker=threading.Thread(target=run);saver=threading.Thread(target=save)
            with patch.object(script,'_stream_subprocess_to_logs',side_effect=generate):
                worker.start()
                try:
                    self.assertTrue(entered.wait(2));saver.start()
                    self.assertFalse(saved.wait(.15),'manual save entered a live generation claim')
                finally:
                    release.set();worker.join(5)
                    if saver.ident is not None:saver.join(5)
            self.assertFalse(worker.is_alive() or saver.is_alive());self.assertEqual([],errors)
            self.assertTrue(saved.is_set());self.assertEqual(source.read_bytes(),output.read_bytes())

    def test_failure_and_cancellation_release_claim(self):
        for cancel in (False,True):
            with self.subTest(cancel=cancel),self.fixture() as (_,source,output,job,state,client):
                def fail(*args,**kwargs):
                    state['cancel']=cancel
                    raise RuntimeError('provider failed')
                with patch.object(script,'_stream_subprocess_to_logs',side_effect=fail),self.assertRaisesRegex(RuntimeError,'provider failed'):
                    script._run_batch_script_job(job,state,None,1)
                with file_lock(output,timeout=.1):pass
                self.assertEqual(200,client.post('/api/scripts/save',json={'name':'book'}).status_code)
                self.assertEqual(source.read_bytes(),output.read_bytes())

    def test_actual_batch_dispatch_rechecks_save_after_jobs_are_planned(self):
        import asyncio
        import copy
        import core
        from fastapi import BackgroundTasks
        with self.fixture() as (root,source,output,job,state,client),contextlib.ExitStack() as stack:
            (root/'book.txt').write_text('A short source story.')
            states=copy.deepcopy(core.process_state)
            for value in states.values():value['running']=False
            for owner,name,value in ((core,'process_state',states),(script,'process_state',states),
                                     (core,'_task_claims',{}),(core,'_gpu_leases',{}),
                                     (script,'UPLOADS_DIR',str(root)),(script,'SCRIPTS_DIR',str(root))):
                stack.enter_context(patch.object(owner,name,value))
            stack.enter_context(patch.object(core,'acquire_gpu_lock',return_value=None))
            stack.enter_context(patch.object(script,'three_pass_refusal',return_value=None))
            stack.enter_context(patch.object(script,'load_app_config',return_value={}))
            stack.enter_context(patch.object(script,'_init_task_log',return_value=str(root/'batch.log')))
            stack.enter_context(patch.object(script,'_get_batch_script_workers',return_value=(1,100,4096)))
            def save_after_planning(*args,**kwargs):
                self.assertEqual(200,client.post('/api/scripts/save',json={'name':'book'}).status_code)
                return True,{},'CPU test provider'
            stack.enter_context(patch.object(script,'ensure_ideal_settings',side_effect=save_after_planning))
            generated=stack.enter_context(patch.object(script,'_stream_subprocess_to_logs',side_effect=self.generate))
            tasks=BackgroundTasks()
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            try:
                response=asyncio.run(script.generate_script_batch_start(request,tasks))
                self.assertEqual('started',response['status']);self.assertTrue(core.is_task_running('batch_script'))
                asyncio.run(tasks())
                self.assertEqual(source.read_bytes(),output.read_bytes());generated.assert_not_called()
                self.assertEqual('failed',states['batch_script']['tasks'][0]['status'])
                self.assertFalse(core.is_task_running('batch_script'));self.assertEqual({},core._task_claims)
            finally:core.release_pending_task_claims()
