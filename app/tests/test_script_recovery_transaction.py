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
from tests.test_script_recovery import _write_failed_run, _write_run_receipt, SOURCE
from tests.test_support import capture_script_worker_starts, intercept_book_publications
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
            failed_once=False
            def fail(target):
                nonlocal failed_once
                if str(target)==checkpoint_path and not failed_once:
                    failed_once=True
                    raise OSError('delta recovery publication failed')
            with intercept_book_publications(books,before=fail),self.assertRaisesRegex(OSError,'delta recovery publication failed'):
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
        # v4 publishes each row with one exchange (#1660); v3 publishes it with
        # a publish move after a separate backup move. Failing the publication
        # itself exercises the promise on either protocol: for v4 the exchange
        # is backup and publication at once, for v3 its publish move fails.
        for label, target_of in (('checkpoint', script.three_pass_checkpoint_path),
                                 ('manifest', script.three_pass_manifest_path)):
            with self.subTest(publication=label),self.fixture() as (_,path):
                before=self.pair(path);failed=[]
                def fail(target):
                    if str(target)==target_of(path) and not failed:
                        failed.append(target)
                        raise OSError('recovery disk failure')
                with intercept_book_publications(books,before=fail),self.assertRaisesRegex(OSError,'recovery disk failure'):
                    script.apply_manual_recovery(self.entries(),'manual')
                self.assertEqual([target_of(path)],[str(target) for target in failed])
                self.assertEqual(before,self.pair(path))
                self.assertTrue(script.apply_manual_recovery(self.entries(),'manual')['accepted'])

    def test_each_v3_backup_and_publication_failure_restores_exact_pair(self):
        # #1880 dropped the v3 backup-move failure points when publication
        # moved onto the shared seam. They still exist on the protocol that
        # has them: v3 moves each existing target aside and then publishes it,
        # so two rows give four move points (#1660 step 4 follow-up).
        from tests.test_support import legacy_book_publication_protocol
        for point in range(1, 5):
            with self.subTest(move=point),self.fixture() as (_,path):
                before=self.pair(path);move=books._move;count=[0]
                def fail(source,destination, **kwargs):
                    count[0]+=1
                    if count[0]==point:raise OSError('recovery disk failure')
                    return move(source,destination, **kwargs)
                with legacy_book_publication_protocol(), \
                        patch.object(books,'_move',side_effect=fail), \
                        self.assertRaisesRegex(OSError,'recovery disk failure'):
                    script.apply_manual_recovery(self.entries(),'manual')
                self.assertEqual(before,self.pair(path))
                self.assertTrue(script.apply_manual_recovery(self.entries(),'manual')['accepted'])

    def test_http_reader_waits_for_complete_recovery_publication(self):
        with self.fixture() as (_,path):
            app=FastAPI();app.include_router(script.router)
            entered=threading.Event();release=threading.Event();read=threading.Event();errors=[];responses=[]
            def paused(target):
                if str(target)==script.three_pass_checkpoint_path(path):
                    entered.set()
                    if not release.wait(5):raise AssertionError('publication not released')
            def publish():
                try:script.apply_manual_recovery(self.entries(),'manual')
                except BaseException as error:errors.append(error)
            def reader(client):
                try:responses.append(client.get('/api/generate_script/recovery'));read.set()
                except BaseException as error:errors.append(error)
            worker=threading.Thread(target=publish)
            with TestClient(app) as client,intercept_book_publications(books,after=paused):
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
                app=FastAPI();app.include_router(script.router);read=script.get_recovery_admission
                def changed_after_read():
                    fenced,result=read();current=copy.deepcopy(result);current['failed']['reason']='new failed request'
                    script.atomic_json_write(current,script.three_pass_checkpoint_path(path))
                    return fenced,result
                payload={'chunk':2}
                if route=='inject':payload['entries']=self.entries()
                with TestClient(app) as client,patch.object(script,'get_recovery_admission',side_effect=changed_after_read):
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
from tests.test_support import intercept_book_publications
script.DATA_DIR=sys.argv[1];script.SCRIPT_PATH=sys.argv[2];script.CONFIG_PATH=os.path.join(sys.argv[1],'config.json');script.process_state['script']['running']=False
boundary=sys.argv[3];point=int(sys.argv[4]);count=0
def stop(*args,**kwargs):
 global count
 result=native(*args,**kwargs);count+=1
 if count==point:os._exit(77)
 return result
def published(target):
 global count
 count+=1
 if count==point:os._exit(77)
def run():
 asyncio.run(script.generate_script_skip(script.SkipChunkRequest(chunk=2)))
if boundary=='journal':
 native=books._save_journal;books._save_journal=stop;run()
else:
 with intercept_book_publications(books,after=published):run()
'''
        recovery='''
import asyncio,json,os,sys
from routers import script
script.DATA_DIR=sys.argv[1];script.SCRIPT_PATH=sys.argv[2];script.CONFIG_PATH=os.path.join(sys.argv[1],'config.json')
print(json.dumps(asyncio.run(script.generate_script_recovery())))
'''
        # Each row is one publication on both protocols: a v4 exchange is
        # backup and publication together, a v3 row publishes after its
        # separate backup move. Journal publication stays the same.
        for boundary,point in [('publish',n) for n in (1,2)]+[('journal',1),('journal',2)]:
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
            source = root / 'book.txt'
            source.write_text(SOURCE, encoding='utf-8')
            script.atomic_json_write({'input_file_path':str(source),
                                      'script_generation_input_file':str(source)},root/'state.json')
            from script_selection_binding import capture_generation_selection
            with books.ensure_book_state(str(root)):
                captured = capture_generation_selection(str(root), str(source))
            book_state = json.loads((root/'state.json').read_text())
            book_state['script_generation_selection'] = captured
            book_state.setdefault('script_generation_options', {
                'strip_front_matter': False, 'first_person_narrator': None, 'cast_sha256': None})
            script.atomic_json_write(book_state, root/'state.json')
            _write_run_receipt(str(root), SOURCE, with_receipt=False)
            before=self.pair(path)
            def leave_pending(target):
                if str(target)==script.three_pass_checkpoint_path(path):
                    raise SystemExit('interrupted recovery')
            with intercept_book_publications(books,after=leave_pending),patch.object(books,'recover_book_state_locked'),self.assertRaisesRegex(SystemExit,'interrupted recovery'):
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
            self.start_over_workers = {}
            start_thread = core.start_claimed_task_thread

            def record_start(task, claim, *args, **kwargs):
                worker = start_thread(task, claim, *args, **kwargs)
                self.start_over_workers[claim] = worker
                return worker

            stack.enter_context(patch.object(core, 'start_claimed_task_thread', side_effect=record_start))
            try:
                yield root, source, checkpoint, manifest, state
            finally:
                for worker in self.start_over_workers.values():
                    worker.join(15)
                    self.assertFalse(worker.is_alive(), 'Start Over worker outlived its fixture')
                for task, claim in list(core._task_claims.items()):
                    core.release_gpu_task_claim(task, claim['id'])

    def post_generation(self, client, route, **options):
        response = client.post(route, **options)
        if response.status_code == 200:
            worker = self.start_over_workers.get(response.json()['task_claim_id'])
            if worker is not None:
                worker.join(15)
                self.assertFalse(worker.is_alive(), 'Start Over worker did not settle')
        return response

    def stage_previous_script_claim(self, source):
        """Seed a parked legacy worker for cancellation and resume fixtures."""
        from fastapi import BackgroundTasks
        with patch.object(core, 'start_claimed_task_thread', return_value=None):
            admitted = script.start_script_generation(
                BackgroundTasks(), str(source), script.GenerateScriptRequest(start_over=True))
        state_path = source.parent / 'state.json'
        state = json.loads(state_path.read_bytes())
        state.pop(script.run_spec_receipt.STATE_DIGEST, None)
        script.atomic_json_write(state, state_path, allow_nonatomic_fallback=False)
        for path in (script.run_spec_receipt.receipt_path(source.parent),
                     script.three_pass_checkpoint_path(script.SCRIPT_PATH),
                     script.three_pass_manifest_path(script.SCRIPT_PATH)):
            Path(path).unlink(missing_ok=True)
        return admitted

    def assert_start_over_marker(self, checkpoint, manifest):
        receipt_module = script.run_spec_receipt
        receipt = receipt_module.load_receipt_file(receipt_module.receipt_path(checkpoint.parent))
        state = json.loads((checkpoint.parent / 'state.json').read_bytes())
        self.assertEqual(receipt['sha256'], state[receipt_module.STATE_DIGEST])
        self.assertEqual(receipt['run_id'], state['script_generation_run_id'])
        expected_checkpoint, expected_manifest = script.get_initial_three_pass_artifacts(receipt)
        self.assertEqual(expected_checkpoint, json.loads(checkpoint.read_bytes()))
        self.assertEqual(expected_manifest, json.loads(manifest.read_bytes()))

    def assert_seed_recovery(self, client, route):
        response = client.get(route + '/recovery', params={'include_detail': True})
        self.assertEqual(200, response.status_code, response.text)
        recovery = response.json()
        self.assertTrue(recovery['recoverable'])
        self.assertEqual('incomplete', recovery['status'])
        self.assertEqual(0, recovery['failure_count'])
        self.assertIsNone(recovery['detail'])

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

    def test_admitted_start_over_starts_same_claim_and_removes_owned_progress(self):
        from fastapi import BackgroundTasks
        with self.fixture() as (root, source, checkpoint, manifest, state):
            tasks = BackgroundTasks()
            observed = []

            def run(command, name):
                observed.append((command, core._task_claims[name].copy()))

            with capture_script_worker_starts(run):
                admitted = script.start_script_generation(
                    tasks, str(source), script.GenerateScriptRequest(start_over=True))
            self.assert_start_over_marker(checkpoint, manifest)
            self.assertEqual([], tasks.tasks)
            self.assertEqual(1, len(observed))
            command, claim = observed[0]
            self.assertEqual({'status': 'started', 'task_claim_id': claim['id']}, admitted)
            self.assertEqual('started', claim['phase'])
            self.assertEqual(claim['id'], command[command.index('--active-run-id') + 1])
            self.assertEqual(str(source), json.loads(state.read_text())['script_generation_input_file'])
            captured = json.loads(state.read_text())['script_generation_selection']
            self.assertEqual(str(source), captured['source_path'])
            self.assertIn('--active-book-token', command)
            self.assertIn('--active-source-sha256', command)

    def test_automatic_restart_checks_cancelled_claim_at_real_route_admission(self):

        route = '/api/generate_script'
        app = FastAPI()
        app.include_router(script.router)
        for successor in (False, True):
            with self.subTest(successor_after_last_status_read=successor), self.fixture() as (
                _, source, checkpoint, manifest, state_path
            ), TestClient(app) as client, patch.object(script, 'run_process', return_value=0) as worker:
                original_progress = {checkpoint: checkpoint.read_bytes(), manifest: manifest.read_bytes()}
                first = self.stage_previous_script_claim(source)
                original_claim = first['task_claim_id']
                for path, contents in original_progress.items():
                    path.write_bytes(contents)
                core.process_state['script'].update(start_time=101, logs=['A LIVE'])
                status = client.get('/api/status/script').json()
                self.assertEqual(original_claim, status['task_claim_id'])
                response = client.post(route + '/cancel', json={'expected_claim_id': original_claim})
                self.assertEqual(200, response.status_code, response.text)
                self.assertTrue(core.release_gpu_task_claim('script', original_claim))
                status = client.get('/api/status/script').json()
                self.assertFalse(status['running'])
                self.assertEqual(101, status['start_time'])
                self.assertIsNone(status['task_claim_id'])
                delayed = {'start_over': True, 'expected_restart_claim_id': original_claim}
                if successor:
                    def save_successor_progress(*args):
                        claim = json.loads(state_path.read_bytes())['script_generation_run_id']
                        books.finish_book_worker_handoff(state_path.parent, claim)
                        checkpoint.write_bytes(b'{"accepted":"successor progress"}')
                        manifest.write_bytes(b'{"status":"failed","run":"successor"}')
                        return 0

                    worker.side_effect = save_successor_progress
                    response = self.post_generation(client, route, json={'start_over': True})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertNotEqual(original_claim, response.json()['task_claim_id'])
                    self.assertFalse(core.is_task_running('script'))
                    worker.reset_mock(side_effect=True)
                    before = {path: path.read_bytes() for path in (checkpoint, manifest, state_path)}
                    response = self.post_generation(client, route, json=delayed)
                    self.assertEqual(409, response.status_code, {
                        'response': response.json(), 'checkpoint': checkpoint.exists(), 'manifest': manifest.exists(),
                    })
                    self.assertEqual(before, {path: path.read_bytes() for path in before})
                    worker.assert_not_called()
                    response = self.post_generation(client, route, json={'start_over': True})
                else:
                    response = self.post_generation(client, route, json=delayed)
                self.assertEqual(200, response.status_code, response.text)
                self.assert_start_over_marker(checkpoint, manifest)
                worker.assert_called_once()
                # Scoped requests are one-use, even if the replacement exits
                # before a replay arrives.
                self.assertEqual(409, self.post_generation(client, route, json=delayed).status_code)
                self.assertFalse(core.is_task_running('script'))

    def test_automatic_restart_preserves_progress_when_admitted_source_changes(self):

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        for edit in ('before cancel', 'after cancel', 'during admission'):
            with self.subTest(edit=edit), self.fixture() as (
                _, source, checkpoint, manifest, state_path
            ), TestClient(app) as client, patch.object(script, 'run_process', return_value=0) as worker:
                before = {path: path.read_bytes() for path in (checkpoint, manifest)}
                original = self.stage_previous_script_claim(source)
                claim = original['task_claim_id']
                for path, contents in before.items():
                    path.write_bytes(contents)
                if edit == 'before cancel':
                    source.write_text('New synthetic source before Cancel.')
                cancelled = client.post(route + '/cancel', json={'expected_claim_id': claim})
                self.assertEqual(200, cancelled.status_code, cancelled.text)
                self.assertTrue(core.release_gpu_task_claim('script', claim))
                self.assertFalse(client.get('/api/status/script').json()['running'])
                if edit == 'after cancel':
                    source.write_text('New synthetic source after Cancel.')
                before[state_path] = state_path.read_bytes()
                reserve = script.reserve_background_task

                def reserve_then_edit(*args, **kwargs):
                    reserved = reserve(*args, **kwargs)
                    if edit == 'during admission':
                        source.write_text('New synthetic source after the first admission check.')
                    return reserved

                with patch.object(script, 'reserve_background_task', side_effect=reserve_then_edit):
                    response = self.post_generation(client, route, json={'start_over': True, 'expected_restart_claim_id': claim})
                self.assertEqual(409, response.status_code, {
                    'response': response.json(), 'checkpoint': checkpoint.exists(),
                    'manifest': manifest.exists(), 'workers': worker.call_count,
                })
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                self.assertFalse(core.is_task_running('script'))
                self.assertFalse(core._task_claims)
                worker.assert_not_called()
                response = self.post_generation(client, route, json={'start_over': True})
                self.assertEqual(200, response.status_code, response.text)
                self.assert_start_over_marker(checkpoint, manifest)
                worker.assert_called_once()

    def test_script_reservation_hides_worker_activity_and_preserves_terminal_on_refusal(self):
        from concurrent.futures import ThreadPoolExecutor
        import script_selection_binding as binding
        from tests.test_rescore9_replays import (
            PENDING_SUCCESSOR_DURING_STOP, PENDING_WORKER_OUTCOMES, SUCCESSOR_AFTER_REFUSED_RESTART,
            TERMINAL_DURING_RESTART_ADMISSION, ScriptRunOwnershipTests,
        )

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        for refused in (False, True):
            with self.subTest(refused=refused), self.fixture() as (
                _, source, checkpoint, manifest, state_path
            ), TestClient(app) as client:
                def read_status():
                    response = client.get('/api/status/script')
                    response.raise_for_status()
                    return response.json()

                progress = {path: path.read_bytes() for path in (checkpoint, manifest)}
                original = self.stage_previous_script_claim(source)['task_claim_id']
                for path, contents in progress.items():
                    path.write_bytes(contents)
                core.process_state['script'].update(start_time=101, worker_claim_id=original, logs=['A LIVE'])
                live = read_status()
                self.assertEqual(200, client.post(route + '/cancel', json={'expected_claim_id': original}).status_code)
                core.process_state['script']['logs'] = ['A STOP']
                self.assertTrue(core.release_gpu_task_claim('script', original))
                stopped = read_status()
                progress[state_path] = state_path.read_bytes()
                checking, finish_check, working, finish_work = (threading.Event() for _ in range(4))
                check_source = binding.require_unchanged_source

                def verify(*args, **kwargs):
                    checking.set()
                    if not finish_check.wait(5):
                        raise AssertionError('verification was not released')
                    return check_source(*args, **kwargs)

                def stream(command, cwd, task_state, **kwargs):
                    task_state['logs'].append('C LIVE' if working.is_set() else 'B LIVE')
                    working.set()
                    if not finish_work.wait(5):
                        raise AssertionError('worker was not released')
                    return 0, None

                with ThreadPoolExecutor(1) as pool, \
                        patch.object(binding, 'require_unchanged_source', side_effect=verify), \
                        patch.object(core, '_init_task_log', return_value=None), \
                        patch.object(core, '_stream_subprocess_to_logs', side_effect=stream) as worker:
                    request = pool.submit(self.post_generation, client, route, json={
                        'start_over': True, 'expected_restart_claim_id': original,
                    })
                    successor = None
                    stale_restart = {'start_over': True, 'expected_restart_claim_id': original}
                    try:
                        self.assertTrue(checking.wait(5))
                        pending = read_status()
                        self.assertTrue(pending['running'])
                        self.assertFalse(pending['cancel'])
                        self.assertNotEqual(pending['task_claim_id'], original)
                        self.assertTrue(core.is_task_running('script'))
                        competitor = self.post_generation(client, route, json={'start_over': True})
                        self.assertEqual(400, competitor.status_code, competitor.text)
                        if refused:
                            source.write_text('The source changed after B reserved the slot.')
                        finish_check.set()
                        if not refused:
                            self.assertTrue(working.wait(5))
                            successor = read_status()
                            live_refusal = self.post_generation(client, route, json=stale_restart)
                            self.assertEqual(400, live_refusal.status_code, live_refusal.text)
                    finally:
                        finish_check.set()
                        finish_work.set()
                    response = request.result(5)
                    ended = read_status()
                    if refused:
                        self.assertEqual(409, response.status_code, response.text)
                        self.assertEqual(progress, {path: path.read_bytes() for path in progress})
                        worker.assert_not_called()
                    else:
                        self.assertEqual(200, response.status_code, response.text)
                        terminal_refusal = self.post_generation(client, route, json=stale_restart)
                        self.assertEqual(409, terminal_refusal.status_code, terminal_refusal.text)
                        worker.assert_called_once()
                        third_response = self.post_generation(client, route, json={'start_over': True})
                        self.assertEqual(200, third_response.status_code, third_response.text)
                        third = read_status()
                        self.assertEqual(third_response.json()['task_claim_id'], third['worker_claim_id'])
                        self.assertEqual(2, worker.call_count)

                for stop_first in (True, False):
                    with self.subTest(held_stop_first=stop_first):
                        fixture = json.dumps([live, stopped, pending, successor, ended, stop_first])
                        ScriptRunOwnershipTests().run_schedule(
                            'const [a,aEnd,pending,b,bEnd,stopFirst] = ' + fixture
                            + ';\n' + PENDING_SUCCESSOR_DURING_STOP
                        )
                if successor:
                    for mode in ('refuse', 'accept-same', 'accept-later', 'select-before', 'select-after'):
                        with self.subTest(terminal_before_admission_reply=mode):
                            fixture = json.dumps([live, stopped, ended, third, mode,
                                                  response.json(), terminal_refusal.json()])
                            ScriptRunOwnershipTests().run_schedule(
                                'const [a,aEnd,bEnd,cEnd,mode,ack,refusal] = ' + fixture
                                + ';\n' + TERMINAL_DURING_RESTART_ADMISSION
                            )
                    for mode in ('live', 'terminal', 'reserved', 'refusal-pending', 'unverified', 'selection'):
                        with self.subTest(refused_restart_browser=mode):
                            refusal = terminal_refusal if mode == 'terminal' else live_refusal
                            fixture = json.dumps([live, stopped, pending, successor, ended, mode,
                                                  refusal.status_code, refusal.json()])
                            ScriptRunOwnershipTests().run_schedule(
                                'const [a,aEnd,pending,b,bEnd,mode,code,reply] = ' + fixture
                                + ';\n' + SUCCESSOR_AFTER_REFUSED_RESTART
                            )
                    schedules = [
                        ('reload', False), ('ordinary', False), ('held', False), ('held', True),
                        ('direct-end', False), ('direct-live', False), ('direct-held-end', False),
                        ('direct-held-live', True), ('direct-held-live', False), ('same', False),
                    ]
                    for mode, stop_first in schedules:
                        with self.subTest(browser=mode, held_stop_first=stop_first):
                            fixture = json.dumps([live, stopped, pending, successor, ended, mode, stop_first])
                            ScriptRunOwnershipTests().run_schedule(
                                'const [a,aEnd,pending,b,bEnd,mode,stopFirst] = ' + fixture
                                + ';\n' + PENDING_WORKER_OUTCOMES
                            )
                self.assertIs(pending.get('worker_pending'), True)
                self.assertEqual([], pending['logs'])
                self.assertIsNone(pending['start_time'])
                if refused:
                    self.assertEqual(stopped, ended)
                else:
                    self.assertEqual(['B LIVE'], successor['logs'])
                    self.assertFalse(successor.get('worker_pending'))
                    self.assertEqual(pending['task_claim_id'], ended['worker_claim_id'])

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
            with patch.object(script, 'get_script_recovery_manifest', return_value={'status':'failed'}):
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
            with patch.object(script, 'get_script_recovery_manifest', return_value={'status':'failed'}):
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
                result = script.start_script_generation(tasks, str(source), confirmation, require_recovery=True)
                self.assertEqual({'status': 'resuming', 'task_claim_id': tasks.tasks[0].args[1]}, result)
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
            with patch.object(script, 'apply_book_state_locked', side_effect=OSError('synthetic state publication failure')):
                with self.assertRaisesRegex(OSError, 'state publication'):
                    script.start_script_generation(tasks, str(source), script.GenerateScriptRequest(start_over=True))
            self.assertNotIn('script', core._task_claims)
            self.assertFalse(core.process_state['script']['running'])
            self.assertEqual([], tasks.tasks)

    def test_start_over_commits_state_and_removes_recovery_together(self):
        from errno import ENOSPC
        from generation_checkpoint_shards import GenerationCheckpointShards, load_generation_shard_checkpoint

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        for scoped, phase in ((True, 'publishing'), (True, 'committed'), (False, 'committed'),
                              (True, 'source-edit'), (True, 'cleanup')):
            with self.subTest(scoped=scoped, phase=phase), self.fixture() as (
                root, source, checkpoint, manifest, state_path
            ), TestClient(app) as client:
                a = self.stage_previous_script_claim(source)['task_claim_id']
                core.process_state['script'].update(start_time=101, worker_claim_id=a, logs=['A LIVE'])
                cancelled = client.post(route + '/cancel', json={'expected_claim_id': a} if scoped else None)
                self.assertEqual(200, cancelled.status_code)
                core.process_state['script']['logs'] = ['A STOP']
                self.assertTrue(core.release_gpu_task_claim('script', a))
                a_terminal = client.get('/api/status/script').json()

                chunks = [[{'type': 'NARRATOR', 'text': 'A checkpoint line.'}],
                          [{'type': 'NARRATOR', 'text': 'Another checkpoint line.'}]]
                shards = GenerationCheckpointShards(checkpoint, {'source_sha256': 'a' * 64})
                shards.save_chunks(chunks)
                manifest.write_bytes(b'{"status":"failed"}')
                cast_path = Path(script.get_run_cast_path())
                cast_path.write_bytes(b'{"cast":[]}')
                progress = (checkpoint, manifest, cast_path, *sorted(shards.directory.iterdir()))
                before = {path: path.read_bytes() for path in (state_path, *progress)}
                request = {'start_over': True, **({'expected_restart_claim_id': a} if scoped else {})}
                publish_journal, finish_transaction = books._save_journal, books._finish_transaction
                legacy_write = script.atomic_json_write
                failed = False
                legacy_count = 0

                def fail_journal(journal_root, data, **kwargs):
                    nonlocal failed
                    if phase == 'source-edit' and data['phase'] == 'committed' and not failed:
                        failed = True
                        source.write_text('The source changed before the committed journal replace.')
                    if data['phase'] == phase and not failed:
                        if phase == 'committed':
                            self.assert_start_over_marker(checkpoint, manifest)
                            self.assertFalse(shards.directory.exists())
                        failed = True
                        raise OSError(ENOSPC, 'Synthetic journal write failed')
                    return publish_journal(journal_root, data, **kwargs)

                def fail_cleanup(*args, **kwargs):
                    nonlocal failed
                    if phase == 'cleanup' and not failed:
                        failed = True
                        raise OSError(ENOSPC, 'Synthetic cleanup failure after commit')
                    return finish_transaction(*args, **kwargs)

                def fail_legacy_write(*args, **kwargs):
                    nonlocal failed, legacy_count
                    legacy_count += 1
                    if phase == 'source-edit' and legacy_count == 2:
                        failed = True
                        source.write_text('The source changed before the replacement state write.')
                    if (phase != 'cleanup' and not failed
                            and legacy_count == (2 if scoped and phase in ('committed', 'source-edit') else 1)):
                        failed = True
                        raise OSError(ENOSPC, 'Synthetic legacy state write failed')
                    return legacy_write(*args, **kwargs)

                with patch.object(script, 'run_process', return_value=0) as worker:
                    with patch.object(books, '_save_journal', side_effect=fail_journal), \
                            patch.object(books, '_finish_transaction', side_effect=fail_cleanup), \
                            patch.object(script, 'atomic_json_write', side_effect=fail_legacy_write):
                        response = self.post_generation(client, route, json=request)
                    self.assertTrue(failed)
                    if phase != 'cleanup':
                        self.assertEqual(409, response.status_code, response.text)
                        self.assertEqual(a_terminal, client.get('/api/status/script').json())
                        actual = {path: path.read_bytes() if path.exists() else None for path in before}
                        self.assertEqual(before, actual, 'Refusal changed state or recovery data')
                        self.assertEqual(chunks, load_generation_shard_checkpoint(checkpoint)['accepted_chunks'])
                        worker.assert_not_called()
                        self.assertTrue(client.get(route + '/recovery').json()['recoverable'])
                        if phase == 'publishing':
                            resumed = client.post(route + '/retry')
                            self.assertEqual(200, resumed.status_code, resumed.text)
                            self.assertEqual('resuming', resumed.json()['status'])
                            self.assertEqual(chunks, load_generation_shard_checkpoint(checkpoint)['accepted_chunks'])
                            self.assertEqual(409, self.post_generation(client, route, json=request).status_code)
                            worker.assert_called_once()
                            continue
                        if phase == 'source-edit':
                            self.assertEqual(409, self.post_generation(client, route, json=request).status_code)
                            worker.assert_not_called()
                            request = {'start_over': True}
                        response = self.post_generation(client, route, json=request)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(response.json()['task_claim_id'],
                                     json.loads(state_path.read_bytes())['script_generation_run_id'])
                    self.assert_start_over_marker(checkpoint, manifest)
                    self.assertTrue(all(not path.exists() for path in progress if path not in (checkpoint, manifest)))
                    self.assertFalse(shards.directory.exists())
                    self.assertFalse((root / books.JOURNAL).exists())
                    self.assertEqual([], list(root.glob('.book-switch-*')))
                    if 'expected_restart_claim_id' in request:
                        self.assertEqual(409, self.post_generation(client, route, json=request).status_code)
                    worker.assert_called_once()

    def test_start_over_retains_progress_until_the_worker_handoff(self):
        from concurrent.futures import ThreadPoolExecutor
        from generation_checkpoint_shards import GenerationCheckpointShards

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        for launched in (False, True):
            with self.subTest(launched=launched), self.fixture() as (
                root, source, checkpoint, manifest, state
            ), TestClient(app) as client, ThreadPoolExecutor(1) as pool:
                a = self.stage_previous_script_claim(source)['task_claim_id']
                self.assertTrue(core.release_gpu_task_claim('script', a))
                shards = GenerationCheckpointShards(checkpoint, {'source_sha256': 'a' * 64})
                shards.save_chunks([[{'type': 'NARRATOR', 'text': 'Saved synthetic text.'}]])
                manifest.write_bytes(b'{"status":"failed"}')
                before = {path: path.read_bytes() for path in (checkpoint, manifest, state, *shards.directory.iterdir())}
                entered, proceed = threading.Event(), threading.Event()
                intent = root / books.DISPATCH_INTENT
                seen = []

                def dispatch(_command, _task):
                    seen.append(json.loads(state.read_bytes())['script_generation_run_id'])
                    entered.set()
                    if not proceed.wait(10):
                        raise AssertionError('Worker was not released')
                    if launched:
                        worker = subprocess.run(
                            [sys.executable, '-c',
                             'import sys, book_state_transaction as books; '
                             'books.finish_book_worker_handoff(sys.argv[1], sys.argv[2])',
                             str(root), seen[0]], cwd=Path(books.__file__).parent,
                            capture_output=True, text=True, timeout=15,
                        )
                        self.assertEqual(0, worker.returncode, worker.stderr)
                        return 0
                    return None

                with patch.object(script, 'run_process', side_effect=dispatch) as worker:
                    request = pool.submit(self.post_generation, client, route, json={'start_over': True})
                    try:
                        self.assertTrue(entered.wait(10))
                        self.assertTrue(intent.is_file())
                        [workspace] = list(root.glob('.book-switch-*'))
                        self.assertTrue(set(before.values()).issubset(
                            {path.read_bytes() for path in workspace.rglob('*') if path.is_file()}))
                        with self.assertRaises(books.BookJournalPublicationUncertain):
                            books.finish_book_worker_handoff(root, a)
                        self.assertEqual(409, client.get(route + '/recovery').status_code)
                        self.assertTrue(intent.is_file())
                        self.assertEqual(400, self.post_generation(client, route, json={'start_over': True}).status_code)
                    finally:
                        proceed.set()
                    response = request.result(20)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(seen, [response.json()['task_claim_id']])
                    worker.assert_called_once()
                if launched:
                    self.assertFalse(intent.exists())
                    self.assertFalse(workspace.exists())
                    self.assert_start_over_marker(checkpoint, manifest)
                    self.assert_seed_recovery(client, route)
                else:
                    self.assertTrue(intent.is_file())
                    self.assertTrue(client.get(route + '/recovery').json()['recoverable'])
                    self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_disputed_worker_handoff_preserves_evidence_on_every_admission(self):
        from routers import scripts_library

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        app.include_router(scripts_library.router)
        for portable in (False, True):
            for disputed in ('journal-inode', 'journal-bytes', 'claim',
                             'receipt-inode', 'receipt-bytes', 'state', None):
                with self.subTest(portable=portable, dispute=disputed), self.fixture() as (
                    root, source, checkpoint, manifest, state
                ), TestClient(app) as client, patch.object(scripts_library, 'DATA_DIR', str(root)):
                    progress = {p: p.read_bytes() for p in (checkpoint, manifest)}
                    original = self.stage_previous_script_claim(source)['task_claim_id']
                    for path, raw in progress.items():
                        path.write_bytes(raw)
                    core.process_state['script'].update(start_time=101, worker_claim_id=original)
                    self.assertEqual(200, client.post(route + '/cancel', json={'expected_claim_id': original}).status_code)
                    self.assertTrue(core.release_gpu_task_claim('script', original))
                    originals = {*progress.values(), state.read_bytes()}
                    journal, receipt, proof = (root / name for name in
                                               (books.JOURNAL, books.DISPATCH_INTENT, books.DISPATCH_PROOF))
                    save_journal, unlink = books._save_journal, os.unlink

                    def save(directory, data, **kwargs):
                        if portable and data['phase'] == 'committed':
                            kwargs['root_fd'] = None
                        return save_journal(directory, data, **kwargs)

                    def alter(path, kind):
                        raw = path.read_bytes()
                        if kind == 'inode':
                            alien = root / 'alien'
                            alien.write_bytes(raw)
                            self.assertNotEqual(path.stat().st_ino, alien.stat().st_ino)
                            alien.replace(path)
                        else:
                            changed = raw.replace(b': ', b':\t', 1)
                            self.assertNotEqual(raw, changed)
                            self.assertEqual(json.loads(raw), json.loads(changed))
                            path.write_bytes(changed)

                    def swap_at_unlink(path, *args, **kwargs):
                        if Path(path).name == books.DISPATCH_INTENT and disputed in ('journal-inode', 'journal-bytes'):
                            alter(journal, disputed.removeprefix('journal-'))
                        return unlink(path, *args, **kwargs)

                    def worker(command, _task):
                        admitted = command[command.index('--active-run-id') + 1]
                        self.assertNotEqual(admitted, original)
                        self.assertEqual(admitted, json.loads(state.read_bytes())['script_generation_run_id'])
                        with self.assertRaises(books.BookJournalPublicationUncertain):
                            books.finish_book_worker_handoff(root, original)
                        if disputed == 'claim':
                            receipt.write_bytes(receipt.read_bytes().replace(admitted.encode(), original.encode()))
                            with self.assertRaises(books.BookJournalPublicationUncertain):
                                books.finish_book_worker_handoff(root, original)
                        elif disputed in ('receipt-inode', 'receipt-bytes'):
                            alter(receipt, disputed.removeprefix('receipt-'))
                        elif disputed == 'state':
                            alter(state, 'bytes')
                        if disputed is not None:
                            with patch.object(os, 'unlink', swap_at_unlink), \
                                    self.assertRaises(ValueError):
                                books.finish_book_worker_handoff(root, admitted)
                            self.assertTrue(proof.is_file())
                            return 1
                        books.finish_book_worker_handoff(root, admitted)
                        books.finish_book_worker_handoff(root, admitted)
                        return 0

                    with patch.object(books, '_save_journal', side_effect=save), \
                            patch.object(script, 'run_process', side_effect=worker) as dispatched:
                        response = self.post_generation(client, route, json={'start_over': True, 'expected_restart_claim_id': original})
                    self.assertEqual(200, response.status_code, response.text)
                    dispatched.assert_called_once()
                    if disputed is None:
                        self.assertFalse(any(path.exists() for path in (journal, receipt, proof)))
                        self.assert_start_over_marker(checkpoint, manifest)
                        self.assert_seed_recovery(client, route)
                        continue
                    [workspace] = list(root.glob('.book-switch-*'))
                    files = [path for path in (journal, receipt, proof, state, *workspace.rglob('*')) if path.is_file()]
                    expected = {path: (path.stat().st_ino, path.read_bytes()) for path in files}
                    self.assertTrue(originals <= {path.read_bytes() for path in workspace.rglob('*') if path.is_file()})
                    with patch.object(script, 'run_process') as retry_worker:
                        for method, suffix in ((client.get, '/recovery'), (client.get, '/recovery'), (client.post, '/retry')):
                            refused = method(route + suffix)
                            self.assertEqual(409, refused.status_code, refused.text)
                            self.assertEqual(expected, {path: (path.stat().st_ino, path.read_bytes()) for path in files})
                        retry_worker.assert_not_called()
                    self.assertEqual('manual_resolution', client.get('/api/book/recovery').json()['status'])

    def test_cancel_queues_for_the_admitted_worker_before_handoff(self):

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        for scoped in (False, True):
            with self.subTest(scoped=scoped), self.fixture() as (
                root, source, checkpoint, manifest, state
            ), TestClient(app) as client:
                original = self.stage_previous_script_claim(source)['task_claim_id']
                self.assertTrue(core.release_gpu_task_claim('script', original))
                checkpoint.write_bytes(b'{"accepted":"Original synthetic progress"}')
                manifest.write_bytes(b'{"status":"failed"}')
                before = {path: path.read_bytes() for path in (state, checkpoint, manifest)}

                def startup(command, cwd, task_state, **kwargs):
                    claim = command[command.index('--active-run-id') + 1]
                    self.assertEqual(claim, task_state['worker_claim_id'])
                    self.assertTrue(task_state['running'])
                    journal, receipt, proof = (root / name for name in
                                               (books.JOURNAL, books.DISPATCH_INTENT, books.DISPATCH_PROOF))
                    [workspace] = list(root.glob('.book-switch-*'))
                    paths = [p for p in (state, journal, receipt, *workspace.rglob('*')) if p.is_file()]
                    snapshot = lambda: {path: (path.stat().st_ino, path.read_bytes()) for path in paths}
                    observed = snapshot()
                    original_proof = proof.read_bytes()
                    self.assertEqual(409, client.post(route + '/cancel', json={'expected_claim_id': original}).status_code)
                    self.assertEqual(original_proof, proof.read_bytes())
                    self.assertFalse(task_state['cancel'])
                    response = client.post(route + '/cancel', json={'expected_claim_id': claim} if scoped else None)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual({'status': 'cancel queued'}, response.json())
                    self.assertTrue(task_state['cancel'])
                    self.assertEqual('cancelled-for-restart' if scoped else 'cancelled',
                                     json.loads(proof.read_bytes())['phase'])
                    self.assertEqual(observed, snapshot())
                    with self.assertRaises(books.BookJournalPublicationUncertain):
                        books.finish_book_worker_handoff(root, claim)
                    return 1, []

                with patch.object(core, '_stream_subprocess_to_logs', side_effect=startup) as worker, \
                        patch.object(core, '_init_task_log', return_value=None):
                    response = self.post_generation(client, route, json={'start_over': True})
                self.assertEqual(200, response.status_code, response.text)
                worker.assert_called_once()
                self.assertTrue(client.get(route + '/recovery').json()['recoverable'])
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                with patch.object(script, 'run_process', return_value=0) as retry:
                    self.assertEqual(200, client.post(route + '/retry').status_code)
                    retry.assert_called_once()

    def test_startup_cancel_scopes_the_successor_across_book_recovery(self):

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        schedules = [
            (saved, read, 'matching') for saved in (False, True) for read in (False, True)
        ] + [
            (True, False, 'invalid-action'), (True, True, 'older-claim'),
            (True, False, 'ordinary-cancel'), (True, True, 'source-edit'),
            (True, True, 'book-change'), (True, True, 'same-book-update'),
            (True, False, 'disputed-handoff'),
        ]
        for saved_selection, recovery_read, scenario in schedules:
            with self.subTest(saved=saved_selection, recovery_read=recovery_read,
                              scenario=scenario), self.fixture() as (
                root, source, checkpoint, manifest, state
            ), TestClient(app) as client:
                older_claim = 'f' * 32
                if saved_selection:
                    older_claim = self.stage_previous_script_claim(source)['task_claim_id']
                    core.process_state['script'].update(start_time=101, worker_claim_id=older_claim)
                    cancelled = client.post(route + '/cancel', json={'expected_claim_id': older_claim})
                    self.assertEqual(200, cancelled.status_code, cancelled.text)
                    self.assertTrue(core.release_gpu_task_claim('script', older_claim))
                    self.assertEqual(older_claim, json.loads(state.read_bytes())[
                        'script_generation_restart_after_cancel']['claim_id'])
                checkpoint.write_bytes(b'{"accepted":"Original synthetic progress"}')
                manifest.write_bytes(b'{"status":"failed"}')
                progress = {path: path.read_bytes() for path in (checkpoint, manifest)}
                prior_state = state.read_bytes()
                stopped = []

                def startup(command, cwd, task, **kwargs):
                    claim = command[command.index('--active-run-id') + 1]
                    self.assertEqual(claim, task['worker_claim_id'])
                    cancel_body = None if scenario == 'ordinary-cancel' else {'expected_claim_id': claim}
                    cancelled = client.post(route + '/cancel', json=cancel_body)
                    self.assertEqual(200, cancelled.status_code, cancelled.text)
                    self.assertEqual({'status': 'cancel queued'}, cancelled.json())
                    self.assertTrue(task['cancel'])
                    stopped.append(claim)
                    if scenario == 'disputed-handoff':
                        receipt = root / books.DISPATCH_INTENT
                        raw = receipt.read_bytes()
                        receipt.write_bytes(raw.replace(b': ', b':\t', 1))
                    return 1, []

                with patch.object(core, '_stream_subprocess_to_logs', side_effect=startup), \
                        patch.object(core, '_init_task_log', return_value=None):
                    start = self.post_generation(client, route, json={'start_over': True})
                self.assertEqual(200, start.status_code, start.text)
                [claim] = stopped
                self.assertEqual(claim, start.json()['task_claim_id'])
                status = client.get('/api/status/script').json()
                self.assertEqual(claim, status['worker_claim_id'])
                self.assertFalse(status['running'])
                self.assertTrue(status['cancel'])
                if scenario == 'disputed-handoff':
                    [workspace] = list(root.glob('.book-switch-*'))
                    paths = [root / name for name in (books.JOURNAL, books.DISPATCH_INTENT, books.DISPATCH_PROOF)]
                    paths.extend(path for path in workspace.rglob('*') if path.is_file())
                    evidence = {path: (path.stat().st_ino, path.read_bytes()) for path in paths}
                    with patch.object(script, 'run_process') as worker:
                        self.assertEqual(409, self.post_generation(client, route, json={
                            'start_over': True, 'expected_restart_claim_id': claim,
                        }).status_code)
                        self.assertEqual(409, client.get(route + '/recovery').status_code)
                        worker.assert_not_called()
                    self.assertEqual(evidence, {path: (path.stat().st_ino, path.read_bytes())
                                                for path in paths})
                    continue
                if recovery_read:
                    for _ in range(2):
                        recovery = client.get(route + '/recovery')
                        self.assertEqual(200, recovery.status_code, recovery.text)
                        if saved_selection:
                            self.assertTrue(recovery.json()['recoverable'])
                        self.assertEqual(prior_state, state.read_bytes())
                        self.assertEqual(progress, {path: path.read_bytes() for path in progress})

                if scenario == 'source-edit':
                    source.write_text('The same path now contains different synthetic text.')
                if scenario in ('book-change', 'same-book-update'):
                    changed = json.loads(state.read_bytes())
                    key = 'book_generation' if scenario == 'book-change' else 'display_title'
                    changed[key] = 'Test selection update'
                    state.write_text(json.dumps(changed))
                request = {
                    'start_over': scenario != 'invalid-action',
                    'expected_restart_claim_id': older_claim if scenario == 'older-claim' else claim,
                }
                with patch.object(script, 'run_process', return_value=0) as worker:
                    successor = self.post_generation(client, route, json=request)
                    if scenario in ('matching', 'same-book-update'):
                        self.assertEqual(200, successor.status_code, successor.text)
                        self.assertNotEqual(claim, successor.json()['task_claim_id'])
                        self.assertEqual(409, self.post_generation(client, route, json=request).status_code)
                        worker.assert_called_once()
                    else:
                        self.assertEqual(409, successor.status_code, successor.text)
                        self.assertEqual(progress, {path: path.read_bytes() for path in progress})
                        if scenario == 'ordinary-cancel':
                            # Rollback can restore an older grant as well as a
                            # checkpoint. Ordinary Cancel of B cannot revive A.
                            request['expected_restart_claim_id'] = older_claim
                            self.assertEqual(409, self.post_generation(client, route, json=request).status_code)
                        worker.assert_not_called()

    def test_scoped_startup_consent_is_one_use_after_real_worker_launch(self):
        from errno import EIO
        from itertools import chain, product

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        writer_modes = (False, True) if sys.platform.startswith('linux') else (True,)
        schedules = chain(product((False, True), (False, True), writer_modes, ('exit', 'exec-rejected')),
                          ((True, True, True, 'lost-ack'), (True, False, True, 'cancel-before-launch')))
        for saved, recovery_read, portable, failure in schedules:
            with self.subTest(saved=saved, recovery_read=recovery_read, portable=portable, failure=failure), \
                    self.fixture() as (root, source, checkpoint, manifest, state), TestClient(app) as client:
                if saved:
                    initial = self.stage_previous_script_claim(source)
                    self.assertTrue(core.release_gpu_task_claim('script', initial['task_claim_id']))
                checkpoint.write_bytes(b'{"accepted":"Synthetic progress before startup"}')
                manifest.write_bytes(b'{"status":"failed"}')
                before = {path: path.read_bytes() for path in (checkpoint, manifest, state)}
                save_journal = books._save_journal
                start_owned_worker = core.start_owned_subprocess
                starts = root / 'actual-worker-starts'

                def save(directory, data, **kwargs):
                    if portable and data['phase'] == 'committed':
                        kwargs['root_fd'] = None
                    return save_journal(directory, data, **kwargs)

                def cancel_startup(command, cwd, task, **kwargs):
                    stopped = client.post(route + '/cancel', json={'expected_claim_id': task['worker_claim_id']})
                    self.assertEqual(200, stopped.status_code, stopped.text)
                    self.assertEqual({'status': 'cancel queued'}, stopped.json())
                    return 1, []

                def worker(code):
                    return [sys.executable, '-c',
                            'import sys; '
                            'open(sys.argv[1], "a").write(sys.argv[2] + "\\n"); sys.exit(int(sys.argv[2]))',
                            str(starts), str(code)]

                def lose_launch_ack(command, **options):
                    process = start_owned_worker(command, **options)
                    try:
                        process.communicate()
                        self.assertEqual(17, process.returncode)
                    finally:
                        control = getattr(process, '_alexandria_control', None)
                        if control is not None:
                            control.close()
                    raise OSError(EIO, 'Synthetic lost acknowledgment after child launch')

                def cancel_before_launch(*args, **kwargs):
                    cancelled = client.post(route + '/cancel')
                    self.assertEqual(200, cancelled.status_code, cancelled.text)
                    self.assertEqual({'status': 'cancel queued'}, cancelled.json())

                with patch.object(books, '_save_journal', side_effect=save), \
                        patch.object(core, '_init_task_log', return_value=None):
                    with patch.object(core, '_stream_subprocess_to_logs', side_effect=cancel_startup):
                        stopped = self.post_generation(client, route, json={'start_over': True})
                    self.assertEqual(200, stopped.status_code, stopped.text)
                    request = {'start_over': True, 'expected_restart_claim_id': stopped.json()['task_claim_id']}
                    command = [str(root / 'missing-interpreter')] if failure == 'exec-rejected' else worker(17)
                    start_worker = lose_launch_ack if failure == 'lost-ack' else start_owned_worker
                    cancel_setup = cancel_before_launch if failure == 'cancel-before-launch' else None
                    with patch.object(script, 'build_generate_script_command', return_value=command), \
                            patch.object(core, '_init_task_log', side_effect=cancel_setup, return_value=None), \
                            patch.object(core, 'start_owned_subprocess', side_effect=start_worker) as launch:
                        first = self.post_generation(client, route, json=request)
                    self.assertEqual(200, first.status_code, first.text)
                    status = client.get('/api/status/script').json()
                    self.assertFalse(status['running'])
                    self.assertEqual(first.json()['task_claim_id'], status['worker_claim_id'])
                    if failure == 'cancel-before-launch':
                        launch.assert_not_called()
                        self.assertFalse(starts.exists())
                        self.assertTrue(status['cancel'])
                        self.assertTrue(any('Task script cancelled.' in line for line in status['logs']))
                    elif failure == 'exec-rejected':
                        self.assertFalse(starts.exists())
                    else:
                        self.assertEqual(['17'], starts.read_text().splitlines())

                    if recovery_read:
                        self.assertEqual(200, client.get(route + '/recovery').status_code)
                        self.assertEqual(before, {path: path.read_bytes() for path in before})

                    with patch.object(script, 'build_generate_script_command', return_value=worker(0)):
                        duplicate = self.post_generation(client, route, json=request)
                        if failure == 'exec-rejected':
                            self.assertEqual(200, duplicate.status_code, duplicate.text)
                            self.assertEqual(['0'], starts.read_text().splitlines())
                        else:
                            self.assertEqual(409, duplicate.status_code, duplicate.text)
                            earlier_starts = [] if failure == 'cancel-before-launch' else ['17']
                            self.assertEqual(earlier_starts, starts.read_text().splitlines() if starts.exists() else [])
                            self.assertEqual(before, {path: path.read_bytes() for path in before})
                            self.assertEqual(200, client.get(route + '/recovery').status_code)
                            self.assertEqual(before, {path: path.read_bytes() for path in before})
                            fresh = self.post_generation(client, route, json={'start_over': True})
                            self.assertEqual(200, fresh.status_code, fresh.text)
                            self.assertEqual([*earlier_starts, '0'], starts.read_text().splitlines())
                        self.assertEqual(409, self.post_generation(client, route, json=request).status_code)
                        self.assert_start_over_marker(checkpoint, manifest)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux descriptor-relative journal publication')
    def test_start_over_rechecks_its_durable_journal_before_dispatch(self):
        from contextlib import contextmanager
        from errno import EIO, EMFILE, ENOSPC
        from generation_checkpoint_shards import GenerationCheckpointShards, load_generation_shard_checkpoint
        import book_state_recovery
        import resource
        from routers import scripts_library

        route = '/api/generate_script'
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        app.include_router(scripts_library.router)
        cases = [(portable, fault) for portable in (False, True)
                 for fault in ('precommit', 'stage-io', 'emfile', 'reader-close', 'altered', 'alien',
                               'intent-io', 'verify-io', 'directory-io', 'unlink-io',
                               'late-unlink-io', 'unreadable-after-unlink')]
        cases.extend(((False, 'writer-close'), (True, 'directory-close')))
        for scoped in (False, True):
            for portable, fault in cases:
                with self.subTest(scoped=scoped, portable=portable, fault=fault), self.fixture() as (
                    root, source, checkpoint, manifest, state_path
                ), TestClient(app) as client:
                    a = self.stage_previous_script_claim(source)['task_claim_id']
                    core.process_state['script'].update(start_time=101, worker_claim_id=a, logs=['A LIVE'])
                    cancel = client.post(route + '/cancel', json={'expected_claim_id': a} if scoped else None)
                    self.assertEqual(200, cancel.status_code, cancel.text)
                    core.process_state['script']['logs'] = ['A STOP']
                    self.assertTrue(core.release_gpu_task_claim('script', a))
                    a_terminal = client.get('/api/status/script').json()
                    chunks = [[{'type': 'NARRATOR', 'text': 'Saved synthetic text.'}]]
                    shards = GenerationCheckpointShards(checkpoint, {'source_sha256': 'a' * 64})
                    shards.save_chunks(chunks)
                    manifest.write_bytes(b'{"status":"failed"}')
                    cast = Path(script.get_run_cast_path())
                    cast.write_bytes(b'{"cast":[]}')
                    progress = (checkpoint, manifest, cast, *sorted(shards.directory.iterdir()))
                    before = {path: path.read_bytes() for path in (state_path, *progress)}
                    request = {'start_over': True, **({'expected_restart_claim_id': a} if scoped else {})}
                    save_journal = books._save_journal
                    open_file, replace, fsync, fdopen, close = os.open, os.replace, os.fsync, os.fdopen, os.close
                    unlink, stat_file = os.unlink, os.stat
                    root_stat = root.stat()
                    audit = []
                    hit = []
                    journal_path = root / books.JOURNAL
                    intent_path = root / (books.JOURNAL + '.commit-intent')

                    def sync(fd):
                        if fault in ('precommit', 'stage-io') and not hit:
                            hit.append('staged journal fsync')
                            raise OSError(ENOSPC if fault == 'precommit' else EIO,
                                          'Synthetic staged journal fsync failure')
                        is_directory = os.path.samestat(os.fstat(fd), root_stat)
                        if fault == 'intent-io' and not hit and is_directory and not audit and intent_path.is_file():
                            hit.append('intent directory fsync')
                            raise OSError(EIO, 'Synthetic intent directory fsync failure')
                        if fault == 'directory-io' and not hit and is_directory and audit == ['journal rename']:
                            hit.append('post-rename directory fsync')
                            raise OSError(EIO, 'Synthetic journal directory fsync failure')
                        fsync(fd)
                        if is_directory and audit == ['journal rename']:
                            audit.append('directory fsync')

                    def rename(src, dst, **kwargs):
                        result = replace(src, dst, **kwargs)
                        if Path(dst).name == books.JOURNAL:
                            audit.append('journal rename')
                            if fault == 'altered':
                                hit.append('parseable one-byte journal edit')
                                original = journal_path.stat()
                                prior = journal_path.read_bytes()
                                changed = prior.replace(b': ', b':\t', 1)
                                self.assertNotEqual(prior, changed)
                                self.assertEqual(json.loads(prior), json.loads(changed))
                                with journal_path.open('r+b') as stream:
                                    stream.write(changed)
                                os.utime(journal_path, ns=(original.st_atime_ns, original.st_mtime_ns))
                            if fault == 'alien':
                                hit.append('alien inode with identical bytes')
                                old_inode = journal_path.stat().st_ino
                                foreign = root / 'alien-journal'
                                foreign.write_bytes(journal_path.read_bytes())
                                replace(foreign, journal_path)
                                self.assertNotEqual(old_inode, journal_path.stat().st_ino)
                        return result

                    def open_journal(path, flags, *args, **kwargs):
                        if (fault == 'verify-io' and not hit and Path(path).name == books.JOURNAL
                                and not flags & (os.O_WRONLY | os.O_RDWR)
                                and audit == ['journal rename', 'directory fsync']):
                            hit.append('verification open')
                            raise OSError(EIO, 'Synthetic journal verification open failure')
                        if (fault in ('emfile', 'altered') and not any('verification' in x for x in hit)
                                and Path(path).name == books.JOURNAL and not flags & (os.O_WRONLY | os.O_RDWR)
                                and audit == ['journal rename', 'directory fsync']):
                            hit.append('verification open')
                            limit = resource.getrlimit(resource.RLIMIT_NOFILE)
                            try:
                                resource.setrlimit(resource.RLIMIT_NOFILE, (0, limit[1]))
                                with self.assertRaises(OSError) as exhausted:
                                    open_file(path, flags, *args, **kwargs)
                                self.assertEqual(EMFILE, exhausted.exception.errno)
                                raise exhausted.exception
                            finally:
                                resource.setrlimit(resource.RLIMIT_NOFILE, limit)
                        return open_file(path, flags, *args, **kwargs)

                    def open_stream(fd, mode, *args, **kwargs):
                        stream = fdopen(fd, mode, *args, **kwargs)
                        if not ((fault == 'reader-close' and mode == 'rb'
                                 and audit == ['journal rename', 'directory fsync']
                                 and os.path.samestat(os.fstat(fd), journal_path.stat()))
                                or (fault == 'writer-close' and mode == 'w+b')):
                            return stream

                        @contextmanager
                        def close_with_error():
                            with stream:
                                yield stream
                            self.assertTrue(stream.closed)
                            self.assertEqual(['journal rename', 'directory fsync'], audit)
                            hit.append('post-verification close')
                            raise OSError(EIO, 'Synthetic delayed close failure')

                        return close_with_error()

                    def close_directory(fd):
                        is_publication_directory = (fault == 'directory-close'
                                                    and not hit
                                                    and audit == ['journal rename', 'directory fsync']
                                                    and os.path.samestat(os.fstat(fd), root_stat))
                        close(fd)
                        if is_publication_directory:
                            hit.append('post-verification close')
                            raise OSError(EIO, 'Synthetic delayed directory close failure')

                    def unlink_intent(path, *args, **kwargs):
                        if (fault in ('unlink-io', 'late-unlink-io', 'unreadable-after-unlink') and not hit
                                and Path(path).name == intent_path.name):
                            if fault != 'unlink-io':
                                unlink(path, *args, **kwargs)
                            hit.append('intent unlink')
                            raise OSError(EIO, 'Synthetic intent unlink failure')
                        return unlink(path, *args, **kwargs)

                    def stat_intent(path, *args, **kwargs):
                        if (fault == 'unreadable-after-unlink' and hit == ['intent unlink']
                                and isinstance(path, (str, Path)) and Path(path).name == intent_path.name):
                            hit.append('intent stat')
                            raise OSError(EIO, 'Synthetic intent stat failure after unlink')
                        return stat_file(path, *args, **kwargs)

                    def inject(journal_root, data, **kwargs):
                        if data['phase'] != 'committed' or hit:
                            return save_journal(journal_root, data, **kwargs)
                        if portable:
                            kwargs = {**kwargs, 'root_fd': None}
                        with patch.object(os, 'fsync', sync), patch.object(os, 'replace', rename), \
                                patch.object(os, 'open', open_journal), patch.object(os, 'fdopen', open_stream), \
                                patch.object(os, 'close', close_directory), patch.object(os, 'unlink', unlink_intent), \
                                patch.object(os, 'stat', stat_intent):
                            return save_journal(journal_root, data, **kwargs)

                    with patch.object(core, '_init_task_log', return_value=None), \
                            patch.object(core, '_stream_subprocess_to_logs', return_value=(0, None)) as worker:
                        with patch.object(books, '_save_journal', side_effect=inject):
                            response = self.post_generation(client, route, json=request)
                        self.assertTrue(hit)
                        state = json.loads(state_path.read_bytes())
                        terminal = client.get('/api/status/script').json()
                        if fault in ('emfile', 'reader-close', 'writer-close', 'directory-close', 'late-unlink-io'):
                            self.assertEqual(['journal rename', 'directory fsync'], audit)
                            expected_fault = {'emfile': 'verification open', 'late-unlink-io': 'intent unlink'}
                            self.assertEqual([expected_fault.get(fault, 'post-verification close')], hit)
                            self.assertEqual(200, response.status_code, response.text)
                            claim = response.json()['task_claim_id']
                            self.assertNotEqual(a, claim)
                            self.assertEqual(claim, state['script_generation_run_id'])
                            self.assertEqual(claim, terminal['worker_claim_id'])
                            self.assertFalse((root / books.JOURNAL).exists())
                            self.assertFalse(intent_path.exists())
                            self.assertFalse((root / books.DISPATCH_INTENT).exists())
                            self.assertEqual([], list(root.glob('.book-switch-*')))
                            self.assert_start_over_marker(checkpoint, manifest)
                            self.assertTrue(all(not path.exists() for path in progress if path not in (checkpoint, manifest)))
                            self.assertFalse(shards.directory.exists())
                            self.assert_seed_recovery(client, route)
                            if scoped:
                                self.assertEqual(409, self.post_generation(client, route, json=request).status_code)
                            worker.assert_called_once()
                            retried = client.post(route + '/retry')
                            self.assertEqual(200, retried.status_code, retried.text)
                            self.assertEqual('resuming', retried.json()['status'])
                            self.assertEqual(2, worker.call_count)
                            continue

                        self.assertEqual(409, response.status_code, response.text)
                        self.assertEqual(a_terminal, terminal)
                        worker.assert_not_called()
                        self.assertFalse(core.is_task_running('script'))
                        if fault in ('altered', 'alien'):
                            self.assertEqual(['journal rename', 'directory fsync'], audit)
                            self.assertEqual(books.BOOK_JOURNAL_UNCERTAIN_DETAIL, response.json()['detail'])
                            [workspace] = list(root.glob('.book-switch-*'))
                            evidence = [journal_path, *(path for path in workspace.rglob('*') if path.is_file())]
                            if intent_path.is_file():
                                evidence.append(intent_path)
                            saved = {path: (path.stat().st_ino, path.read_bytes()) for path in evidence}
                            self.assertTrue(set(before.values()).issubset({raw for _, raw in saved.values()}))
                            for suffix, method in (('/recovery', client.get), ('/recovery', client.get),
                                                   ('/retry', client.post)):
                                refused = method(route + suffix)
                                self.assertEqual(409, refused.status_code, refused.text)
                                self.assertEqual(books.BOOK_JOURNAL_UNCERTAIN_DETAIL, refused.json()['detail'])
                                self.assertEqual(saved, {path: (path.stat().st_ino, path.read_bytes())
                                                         for path in evidence})
                            self.assertTrue(intent_path.is_file())
                            with patch.object(scripts_library, 'DATA_DIR', str(root)):
                                guidance = client.get('/api/book/recovery')
                            self.assertEqual(200, guidance.status_code, guidance.text)
                            self.assertEqual('manual_resolution', guidance.json()['status'])
                            self.assertEqual(['python app/book_state_recovery.py inspect --hash'],
                                             [entry['command'] for entry in guidance.json()['commands']])
                            code, message, _ = book_state_recovery.run(
                                book_state_recovery._get_parser().parse_args(
                                    ['--data-dir', str(root), 'quarantine', '--accept-current-files']))
                            self.assertNotEqual(0, code)
                            self.assertIn('manual resolution', message)
                            self.assertEqual(saved, {path: (path.stat().st_ino, path.read_bytes())
                                                     for path in evidence})
                            worker.assert_not_called()
                            continue

                        if fault in ('precommit', 'stage-io'):
                            self.assertEqual([], audit)
                        else:
                            marker = root / books.DISPATCH_INTENT if fault == 'unreadable-after-unlink' else intent_path
                            self.assertTrue(marker.is_file(), 'ambiguous publication needs a durable intent')
                            self.assertEqual(books.BOOK_JOURNAL_UNCERTAIN_DETAIL, response.json()['detail'])
                        if scoped and fault == 'directory-io':
                            resolved = subprocess.run(
                                [sys.executable, '-c',
                                 'import sys, book_state_transaction as books\n'
                                 'with books.ensure_book_state(sys.argv[1]): pass',
                                 str(root)], cwd=Path(books.__file__).parent,
                                capture_output=True, text=True, timeout=30,
                            )
                            self.assertEqual(0, resolved.returncode, resolved.stderr)
                        for _ in range(2):
                            available = client.get(route + '/recovery')
                            self.assertEqual(200, available.status_code, available.text)
                            self.assertTrue(available.json()['recoverable'])
                            self.assertEqual(before, {path: path.read_bytes() for path in before})
                            self.assertEqual(chunks, load_generation_shard_checkpoint(checkpoint)['accepted_chunks'])
                            self.assertEqual(a_terminal, client.get('/api/status/script').json())
                            worker.assert_not_called()
                        self.assertFalse(journal_path.exists())
                        self.assertFalse(intent_path.exists())
                        self.assertFalse((root / books.DISPATCH_INTENT).exists())
                        resumed = client.post(route + '/retry')
                        self.assertEqual(200, resumed.status_code, resumed.text)
                        self.assertEqual(chunks, load_generation_shard_checkpoint(checkpoint)['accepted_chunks'])
                        worker.assert_called_once()
