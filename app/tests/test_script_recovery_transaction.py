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
            def fail(source,destination):
                nonlocal failed_once
                if str(destination)==checkpoint_path and not failed_once:
                    failed_once=True
                    raise OSError('delta recovery publication failed')
                return move(source,destination)
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
            library=root/'scripts'
            with patch.object(script,'SCRIPTS_DIR',str(library)),\
                 patch.object(script,'VOICE_CONFIG_PATH',str(root/'missing-voices.json')),\
                 patch.object(script,'_saved_book_meta_path',side_effect=lambda name:str(library/(name+'.meta.json'))):
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
                def fail(source,destination):
                    nonlocal count
                    count+=1
                    if count==point:raise OSError('recovery disk failure')
                    return move(source,destination)
                with patch.object(books,'_move',side_effect=fail),self.assertRaisesRegex(OSError,'recovery disk failure'):
                    script.apply_manual_recovery(self.entries(),'manual')
                self.assertEqual(before,self.pair(path))
                self.assertTrue(script.apply_manual_recovery(self.entries(),'manual')['accepted'])

    def test_http_reader_waits_for_complete_recovery_publication(self):
        with self.fixture() as (_,path):
            app=FastAPI();app.include_router(script.router)
            entered=threading.Event();release=threading.Event();read=threading.Event();errors=[];responses=[]
            move=books._move
            def paused(source,destination):
                result=move(source,destination)
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
            before=self.pair(path);move=books._move
            def leave_pending(src,destination):
                result=move(src,destination)
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
