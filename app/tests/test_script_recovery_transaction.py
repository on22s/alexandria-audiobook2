"""Native recovery publication, interrupted processes and concurrent readers."""
import asyncio
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import book_state_transaction as books
import core
from generation_checkpoint_deltas import GenerationCheckpointDeltas
from generation_checkpoint_shards import get_generation_checkpoint_artifacts
from routers import script
from tests.test_script_recovery import _write_failed_run, SOURCE
from pass_quality import split_outer_quote_regions


class ScriptRecoveryTransactionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
            path=_write_failed_run(tmp);root=Path(tmp)
            states=copy.deepcopy(core.process_state)
            for state in states.values():state['running']=False
            for owner,name,value in ((script,'SCRIPT_PATH',path),(script,'DATA_DIR',tmp),
                                     (script,'CONFIG_PATH',str(root/'config.json')),
                                     (script,'process_state',states),(core,'process_state',states),
                                     (core,'_task_claims',{}),(core,'_gpu_leases',{})):
                stack.enter_context(patch.object(owner,name,value))
            yield root,path

    @staticmethod
    def entries():
        return [{'type':row['type'],'text':row['text'].strip()}
                for row in split_outer_quote_regions(SOURCE) if row.get('text','').strip()]

    @staticmethod
    def pair(path):
        return {name:Path(name).read_bytes() for name in
                (script.three_pass_checkpoint_path(path),script.three_pass_manifest_path(path))}

    def test_success_publishes_matching_pair_and_keeps_inputs(self):
        with self.fixture() as (root,path):
            entries=self.entries();before=copy.deepcopy(entries)
            result=script.apply_manual_recovery(entries,'manual')
            self.assertTrue(result['accepted']);self.assertEqual(before,entries)
            checkpoint=json.loads(Path(script.three_pass_checkpoint_path(path)).read_bytes())
            manifest=json.loads(Path(script.three_pass_manifest_path(path)).read_bytes())
            self.assertIsNone(checkpoint['failed']);self.assertEqual(2,checkpoint['chunks_done'])
            self.assertEqual('incomplete',manifest['status']);self.assertNotIn('failed_chunk',manifest)
            self.assertEqual([{'chunk':2,'pass':'segment','resolution':'manual'}],manifest['recovered_units'])
            self.assertFalse((root/books.JOURNAL).exists());self.assertEqual([],list(root.glob('.book-switch-*')))
            before_pair=self.pair(path)
            with self.assertRaises(HTTPException) as repeated:script.apply_manual_recovery(entries,'manual')
            self.assertEqual(409,repeated.exception.status_code);self.assertEqual(before_pair,self.pair(path))

    def test_delta_recovery_rolls_back_owned_parts_then_retires_them_on_success(self):
        with self.fixture() as (root,path):
            checkpoint_path=script.three_pass_checkpoint_path(path)
            state=json.loads(Path(checkpoint_path).read_text())
            writer=GenerationCheckpointDeltas(checkpoint_path)
            writer.save_checkpoint(state)
            state['failed']['reason']='updated failed unit'
            writer.save_checkpoint(state)
            before={str(p):p.read_bytes() for p in root.rglob('*')
                    if p.is_file() and not p.name.endswith('.lock')}
            move=books._move
            failed_once=False
            def fail(source,destination, **kwargs):
                nonlocal failed_once
                if str(destination)==checkpoint_path and not failed_once:
                    failed_once=True
                    raise OSError('delta recovery publication failed')
                return move(source,destination, **kwargs)
            with patch.object(books,'_move',side_effect=fail),self.assertRaises(OSError):
                script.apply_manual_recovery(self.entries(),'manual')
            after={str(p):p.read_bytes() for p in root.rglob('*')
                   if p.is_file() and not p.name.endswith('.lock')}
            self.assertEqual(before,after)
            self.assertEqual(script.ensure_failed_checkpoint()['failed']['reason'],'updated failed unit')
            self.assertTrue(script.apply_manual_recovery(self.entries(),'manual')['accepted'])
            self.assertEqual(get_generation_checkpoint_artifacts(checkpoint_path),[checkpoint_path])
            self.assertIsNone(json.loads(Path(checkpoint_path).read_text())['failed'])

    def test_live_snapshot_reads_latest_delta_and_start_over_removes_owned_parts(self):
        with self.fixture() as (root,path):
            checkpoint_path=script.three_pass_checkpoint_path(path)
            writer=GenerationCheckpointDeltas(checkpoint_path)
            state={'stage':'instruct','chunks_done':1,'segmented':[], 'named':[], 'annotated':[]}
            writer.save_checkpoint(state)
            state['annotated']=[{'speaker':'NARRATOR','text':'Latest accepted row.','instruct':'Neutral.'}]
            state['named']=[{'speaker':'NARRATOR','text':'Latest accepted row.'}]
            state['segmented']=[{'type':'NARRATOR','text':'Latest accepted row.'}]
            writer.save_checkpoint(state)
            script.process_state['script']['running']=True
            source=root/'book.txt';source.write_text('Latest accepted row.')
            from script_selection_binding import capture_generation_selection
            book_state={'input_file_path':str(source)}
            (root/'state.json').write_text(json.dumps(book_state))
            with books.ensure_book_state(str(root)):
                selection=capture_generation_selection(str(root),str(source))
            book_state['script_generation_selection']=selection
            book_state['script_generation_run_id']='a'*32
            (root/'state.json').write_text(json.dumps(book_state))
            core._task_claims['script']={'id':'a'*32,'phase':'started'}
            library=root/'scripts'
            with patch.object(script,'SCRIPTS_DIR',str(library)),\
                 patch.object(script,'VOICE_CONFIG_PATH',str(root/'missing-voices.json')),\
                 patch.object(script,'_saved_book_meta_path',side_effect=lambda name:str(library/(name+'.meta.json'))):
                source.write_text('A different source.')
                with self.assertRaises(HTTPException) as wrong:
                    asyncio.run(script.snapshot_script(script.SnapshotRequest(name='wrong')))
                self.assertEqual(409,wrong.exception.status_code)
                self.assertFalse((library/'wrong.json').exists())
                source.write_text('Latest accepted row.')
                core._task_claims['script']['id']='b'*32
                with self.assertRaises(HTTPException) as wrong_owner:
                    asyncio.run(script.snapshot_script(script.SnapshotRequest(name='wrong-owner')))
                self.assertEqual(409,wrong_owner.exception.status_code)
                self.assertFalse((library/'wrong-owner.json').exists())
                core._task_claims['script']['id']='a'*32
                response=asyncio.run(script.snapshot_script(script.SnapshotRequest(name='latest')))
            self.assertEqual(response['entries'],1)
            self.assertEqual(json.loads((library/'latest.json').read_text()),state['annotated'])
            script.discard_script_progress()
            self.assertFalse(Path(checkpoint_path).exists())
            self.assertEqual(get_generation_checkpoint_artifacts(checkpoint_path),[checkpoint_path])

    def test_each_publication_failure_restores_exact_pair(self):
        for point in range(1,5):
            with self.subTest(move=point),self.fixture() as (_,path):
                before=self.pair(path);move=books._move;count=0
                def fail(source,destination, **kwargs):
                    nonlocal count
                    count+=1
                    if count==point:raise OSError('recovery disk failure')
                    return move(source,destination, **kwargs)
                with patch.object(books,'_move',side_effect=fail),self.assertRaisesRegex(OSError,'recovery disk failure'):
                    script.apply_manual_recovery(self.entries(),'manual')
                self.assertEqual(before,self.pair(path))
                self.assertTrue(script.apply_manual_recovery(self.entries(),'manual')['accepted'])

    def test_http_reader_waits_for_complete_recovery_publication(self):
        with self.fixture() as (_,path):
            app=FastAPI();app.include_router(script.router)
            entered=threading.Event();release=threading.Event();read=threading.Event();errors=[];responses=[]
            move=books._move
            def paused(source,destination, **kwargs):
                result=move(source,destination, **kwargs)
                if str(destination)==script.three_pass_checkpoint_path(path):
                    entered.set()
                    if not release.wait(5):raise AssertionError('publication not released')
                return result
            def publish():
                try:script.apply_manual_recovery(self.entries(),'manual')
                except BaseException as error:errors.append(error)
            def reader(client):
                try:responses.append(client.get('/api/generate_script/recovery'));read.set()
                except BaseException as error:errors.append(error)
            worker=threading.Thread(target=publish)
            with TestClient(app) as client,patch.object(books,'_move',side_effect=paused):
                observer=threading.Thread(target=reader,args=(client,));worker.start()
                try:
                    self.assertTrue(entered.wait(2));observer.start()
                    self.assertFalse(read.wait(.15),'reader observed a half-published pair')
                finally:
                    release.set();worker.join(5)
                    if observer.ident is not None:observer.join(5)
            self.assertFalse(worker.is_alive() or observer.is_alive());self.assertEqual([],errors)
            self.assertEqual(200,responses[0].status_code);self.assertEqual('incomplete',responses[0].json()['status'])
            self.assertIsNone(responses[0].json()['failed_chunk'])

    def test_stale_inject_and_skip_decisions_cannot_apply_to_changed_checkpoint(self):
        for route in ('inject','skip'):
            with self.subTest(route=route),self.fixture() as (_,path):
                app=FastAPI();app.include_router(script.router);read=script.ensure_failed_checkpoint
                def changed_after_read():
                    result=read();current=copy.deepcopy(result);current['failed']['reason']='new failed request'
                    script.atomic_json_write(current,script.three_pass_checkpoint_path(path))
                    return result
                payload={'chunk':2}
                if route=='inject':payload['entries']=self.entries()
                with TestClient(app) as client,patch.object(script,'ensure_failed_checkpoint',side_effect=changed_after_read):
                    response=client.post('/api/generate_script/'+route,json=payload)
                self.assertEqual(409,response.status_code);self.assertIn('changed',response.json()['detail'])
                checkpoint=json.loads(Path(script.three_pass_checkpoint_path(path)).read_text())
                self.assertEqual('new failed request',checkpoint['failed']['reason']);self.assertEqual(1,checkpoint['chunks_done'])
                self.assertEqual('failed',json.loads(Path(script.three_pass_manifest_path(path)).read_text())['status'])

    def test_pending_owner_blocks_recovery_even_if_legacy_flag_was_cleared(self):
        with self.fixture() as (_,path),patch.object(core,'acquire_gpu_lock',return_value=None):
            owner=core.reserve_background_task('script');core.process_state['script']['running']=False
            before=self.pair(path)
            try:
                with self.assertRaises(HTTPException) as refused:script.apply_manual_recovery(self.entries(),'manual')
                self.assertEqual(409,refused.exception.status_code);self.assertEqual(before,self.pair(path))
            finally:core.release_gpu_task_claim('script',owner,pending_only=True)

    def test_process_death_recovery_is_idempotent_through_real_reader(self):
        worker='''
import asyncio,os,sys
import book_state_transaction as books
from routers import script
script.DATA_DIR=sys.argv[1];script.SCRIPT_PATH=sys.argv[2];script.process_state['script']['running']=False
boundary=sys.argv[3];point=int(sys.argv[4]);count=0
original=books._move if boundary=='move' else books._save_journal
def stop(*args,**kwargs):
 global count
 result=original(*args,**kwargs);count+=1
 if count==point:os._exit(77)
 return result
if boundary=='move':books._move=stop
else:books._save_journal=stop
asyncio.run(script.generate_script_skip(script.SkipChunkRequest(chunk=2)))
'''
        recovery='''
import asyncio,json,sys
from routers import script
script.DATA_DIR=sys.argv[1];script.SCRIPT_PATH=sys.argv[2]
print(json.dumps(asyncio.run(script.generate_script_recovery())))
'''
        for boundary,point in [('move',n) for n in range(1,5)]+[('journal',1),('journal',2)]:
            with self.subTest(boundary=boundary,point=point),self.fixture() as (root,path):
                before=self.pair(path)
                result=subprocess.run([sys.executable,'-c',worker,str(root),path,boundary,str(point)],capture_output=True,text=True,timeout=15)
                self.assertEqual(77,result.returncode,result.stderr)
                for repeat in range(2):
                    result=subprocess.run([sys.executable,'-c',recovery,str(root),path],capture_output=True,text=True,timeout=15)
                    self.assertEqual(0,result.returncode,result.stderr)
                if boundary=='journal' and point==2:
                    checkpoint=json.loads(Path(script.three_pass_checkpoint_path(path)).read_text())
                    manifest=json.loads(Path(script.three_pass_manifest_path(path)).read_text())
                    self.assertIsNone(checkpoint['failed']);self.assertEqual(2,checkpoint['chunks_done'])
                    self.assertEqual('incomplete',manifest['status']);self.assertEqual(1,len(manifest['recovered_units']))
                else:self.assertEqual(before,self.pair(path))
                self.assertFalse((root/books.JOURNAL).exists());self.assertEqual([],list(root.glob('.book-switch-*')))

    def test_retry_recovers_interrupted_pair_before_dispatching_owned_worker(self):
        with self.fixture() as (root,path):
            source=root/'book.txt';source.write_text('A short source story.')
            script.atomic_json_write({'input_file_path':str(source),
                                      'script_generation_input_file':str(source)},root/'state.json')
            from script_selection_binding import capture_generation_selection
            with books.ensure_book_state(str(root)):
                captured = capture_generation_selection(str(root), str(source))
            book_state = json.loads((root/'state.json').read_text())
            book_state['script_generation_selection'] = captured
            script.atomic_json_write(book_state, root/'state.json')
            before=self.pair(path);move=books._move
            def leave_pending(src,destination, **kwargs):
                result=move(src,destination, **kwargs)
                if str(destination)==script.three_pass_checkpoint_path(path):raise SystemExit('interrupted recovery')
                return result
            with patch.object(books,'_move',side_effect=leave_pending),patch.object(books,'recover_book_state_locked'),self.assertRaises(SystemExit):
                script.apply_manual_recovery(self.entries(),'manual')
            self.assertTrue((root/books.JOURNAL).exists())
            app=FastAPI();app.add_middleware(core.TaskClaimMiddleware);app.include_router(script.router)
            observed=[]
            def worker(*args,**kwargs):observed.append(self.pair(path))
            with TestClient(app) as client,patch.object(core,'acquire_gpu_lock',return_value=None), \
                 patch.object(script,'build_generate_script_command',return_value=['CPU provider stand-in']), \
                 patch.object(script,'run_process',side_effect=worker):
                response=client.post('/api/generate_script/retry')
            self.assertEqual(200,response.status_code,response.text);self.assertEqual([before],observed)
            self.assertEqual(before,self.pair(path));self.assertFalse((root/books.JOURNAL).exists())
            self.assertFalse(core.is_task_running('script'));self.assertEqual({},core._task_claims)


class StartOverAdmissionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp); source = root / 'book.txt'; source.write_text('“Synthetic sentence.”', encoding='utf-8')
            output = root / 'annotated_script.json'; config = root / 'config.json'; config.write_text('{}')
            checkpoint = Path(script.three_pass_checkpoint_path(str(output))); checkpoint.write_text('{"accepted":"synthetic progress"}')
            manifest = Path(script.three_pass_manifest_path(str(output))); manifest.write_text('{"status":"failed"}')
            state = root / 'state.json'; state.write_text(json.dumps({'input_file_path': str(source), 'active_book_id': 'synthetic-book'}))
            states = copy.deepcopy(core.process_state)
            for value in states.values(): value['running'] = False
            for obj, name, value in [(core, 'DATA_DIR', tmp), (script, 'DATA_DIR', tmp), (script, 'SCRIPT_PATH', str(output)), (script, 'CONFIG_PATH', str(config)), (core, 'process_state', states), (script, 'process_state', states), (core, '_task_claims', {}), (core, '_gpu_leases', {})]:
                stack.enter_context(patch.object(obj, name, value))
            stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
            stack.enter_context(patch.object(script, 'get_active_reasoning_effort', return_value=None))
            try:
                yield root, source, checkpoint, manifest, state
            finally:
                for task, claim in list(core._task_claims.items()):
                    core.release_gpu_task_claim(task, claim['id'])

    def test_competing_claim_refuses_before_start_over_discards_progress_or_state(self):
        from fastapi import BackgroundTasks
        with self.fixture() as (root, source, checkpoint, manifest, state):
            before = {path: path.read_bytes() for path in (source, checkpoint, manifest, state)}
            original_check = core.check_global_gpu_lock
            def admit_then_compete(task):
                original_check(task); core.claim_gpu_task('audio')
            tasks = BackgroundTasks()
            with patch.object(script, 'check_global_gpu_lock', side_effect=admit_then_compete):
                with self.assertRaises(HTTPException) as refused:
                    script.start_script_generation(tasks, str(source), script.GenerateScriptRequest(start_over=True))
            self.assertEqual(400, refused.exception.status_code)
            for path in before:
                self.assertTrue(path.exists(), f"Refused start removed {path.name}")
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertEqual([], tasks.tasks)
            self.assertNotIn('script', core._task_claims)
            self.assertIn('audio', core._task_claims)

    def test_unverifiable_generation_selection_returns_conflict_and_keeps_progress(self):
        from fastapi import BackgroundTasks
        import script_selection_binding
        with self.fixture() as (root, source, checkpoint, manifest, state):
            before = {path: path.read_bytes() for path in (checkpoint, manifest, state)}
            tasks = BackgroundTasks()
            with patch.object(script_selection_binding, 'capture_generation_selection',
                              side_effect=ValueError('selection changed')):
                with self.assertRaises(HTTPException) as refused:
                    script.start_script_generation(tasks, str(source),
                        script.GenerateScriptRequest(start_over=True))
            self.assertEqual(409, refused.exception.status_code)
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertEqual([], tasks.tasks)
            self.assertNotIn('script', core._task_claims)

    def test_admitted_start_over_registers_same_claim_and_removes_owned_progress(self):
        from fastapi import BackgroundTasks
        with self.fixture() as (root, source, checkpoint, manifest, state):
            tasks = BackgroundTasks()
            self.assertEqual({'status': 'started'}, script.start_script_generation(tasks, str(source), script.GenerateScriptRequest(start_over=True)))
            self.assertFalse(checkpoint.exists()); self.assertFalse(manifest.exists())
            self.assertEqual(1, len(tasks.tasks))
            claim = core._task_claims['script']
            self.assertEqual('pending', claim['phase'])
            self.assertEqual(claim['id'], tasks.tasks[0].args[1])
            self.assertEqual(str(source), json.loads(state.read_text())['script_generation_input_file'])
            captured = json.loads(state.read_text())['script_generation_selection']
            self.assertEqual(str(source), captured['source_path'])
            command = tasks.tasks[0].args[2].args[0]
            self.assertIn('--active-book-token', command)
            self.assertIn('--active-source-sha256', command)

    def test_retry_refuses_changed_source_without_removing_checkpoint(self):
        from fastapi import BackgroundTasks
        from script_selection_binding import capture_generation_selection
        with self.fixture() as (root, source, checkpoint, manifest, state):
            captured = capture_generation_selection(str(root), str(source))
            data = json.loads(state.read_text())
            data['script_generation_selection'] = captured
            state.write_text(json.dumps(data))
            source.write_text('Changed source after checkpoint.')
            before = {path: path.read_bytes() for path in (checkpoint, manifest, state)}
            tasks = BackgroundTasks()
            with patch.object(script, 'ensure_script_recovery_manifest', return_value={'status':'failed'}), \
                 patch.object(script, 'get_script_recovery_manifest', return_value={'status':'failed'}):
                with self.assertRaises(HTTPException) as refused:
                    script.start_script_generation(tasks, str(source), None, require_recovery=True)
            self.assertEqual(409, refused.exception.status_code)
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertEqual([], tasks.tasks)

    def test_generate_does_not_resume_other_selection_checkpoint(self):
        from fastapi import BackgroundTasks
        from script_selection_binding import capture_generation_selection
        from book_state_transaction import apply_book_input_selection
        with self.fixture() as (root, source, checkpoint, manifest, state):
            captured = capture_generation_selection(str(root), str(source))
            data = json.loads(state.read_text())
            data['script_generation_selection'] = captured
            state.write_text(json.dumps(data))
            apply_book_input_selection(str(root), str(source), 'same-source-new-selection')
            before = {path: path.read_bytes() for path in (checkpoint, manifest, state)}
            tasks = BackgroundTasks()
            with self.assertRaises(HTTPException) as refused:
                script.start_script_generation(tasks, str(source), None)
            self.assertEqual(409, refused.exception.status_code)
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertEqual([], tasks.tasks)

    def test_explicit_legacy_retry_requests_fingerprint_validation_and_binding(self):
        from fastapi import BackgroundTasks
        with self.fixture() as (root, source, checkpoint, manifest, state):
            data = json.loads(state.read_text())
            data.pop('script_generation_selection', None)
            state.write_text(json.dumps(data))
            before = checkpoint.read_bytes()
            tasks = BackgroundTasks()
            with patch.object(script, 'ensure_script_recovery_manifest', return_value={'status':'failed'}), \
                 patch.object(script, 'get_script_recovery_manifest', return_value={'status':'failed'}):
                with self.assertRaises(HTTPException) as missing:
                    script.start_script_generation(tasks, str(source), None, require_recovery=True)
                self.assertEqual(409, missing.exception.status_code)
                checkpoint_token = script.get_script_recovery_token(script.get_current_generation_checkpoint())
                book_token = books.get_book_operation_token(str(root))
                stale = script.GenerateScriptRequest(legacy_checkpoint_token='b'*64, legacy_book_token=book_token)
                with self.assertRaises(HTTPException) as changed:
                    script.start_script_generation(tasks, str(source), stale, require_recovery=True)
                self.assertEqual(409, changed.exception.status_code)
                self.assertEqual([], tasks.tasks)
                confirmation = script.GenerateScriptRequest(legacy_checkpoint_token=checkpoint_token,
                                                             legacy_book_token=book_token)
                self.assertEqual({'status':'resuming'},
                    script.start_script_generation(tasks, str(source), confirmation, require_recovery=True))
            command = tasks.tasks[0].args[2].args[0]
            self.assertIn('--require-matching-checkpoint', command)
            self.assertIn('--active-book-token', command)
            self.assertIn('--active-source-sha256', command)
            self.assertIn('--active-run-id', command)
            self.assertEqual(before, checkpoint.read_bytes())

    def test_setup_failure_releases_reservation_without_scheduling_worker(self):
        from fastapi import BackgroundTasks
        with self.fixture() as (root, source, checkpoint, manifest, state):
            tasks = BackgroundTasks()
            with patch.object(script, 'atomic_json_write', side_effect=OSError('synthetic state publication failure')):
                with self.assertRaisesRegex(OSError, 'state publication'):
                    script.start_script_generation(tasks, str(source), script.GenerateScriptRequest(start_over=True))
            self.assertNotIn('script', core._task_claims)
            self.assertFalse(core.process_state['script']['running'])
            self.assertEqual([], tasks.tasks)
