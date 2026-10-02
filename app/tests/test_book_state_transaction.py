import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import book_state_transaction as books


class BookStateTransactionTests(unittest.TestCase):
    def prepare(self, root):
        (root/'script.json').write_bytes(b'old script')
        (root/'voice.json').write_bytes(b'old voices')
        (root/'state.json').write_bytes(b'old identity')
        (root/'chapters').mkdir()
        (root/'chapters/audio.wav').write_bytes(b'old chapter audio')
        (root/'export.mp3').write_bytes(b'old export')
        (root/'checkpoint.json').write_bytes(b'old checkpoint')
        return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def snapshot(self, root):
        return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}

    def switch(self, root):
        with books.ensure_book_state(str(root)):
            books.apply_book_state_locked(str(root), {'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'}, ['chapters','export.mp3','checkpoint.json'])

    def test_success_publishes_complete_set_and_removes_prior_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.prepare(root);self.switch(root)
            self.assertEqual({'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'},self.snapshot(root))

    def test_move_failure_restores_exact_previous_book_exports_and_checkpoint(self):
        for failure_at in range(1,11):
            with self.subTest(move=failure_at),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);originals=self.prepare(root);move=books._move;counter=0
                def fail_once(source,destination):
                    nonlocal counter
                    counter+=1
                    if counter==failure_at:raise OSError('fixture publication disk full')
                    return move(source,destination)
                with patch.object(books,'_move',side_effect=fail_once):
                    with self.assertRaisesRegex(OSError,'publication disk full'):self.switch(root)
                self.assertEqual(originals,self.snapshot(root))

    def test_process_death_recovers_idempotently_in_fresh_process(self):
        worker='''
import os,sys
from pathlib import Path
import book_state_transaction as books
root=Path(sys.argv[1]);boundary=sys.argv[2];point=int(sys.argv[3]);count=0
original=books._move if boundary=='move' else books._save_journal
def exit_after(*args,**kwargs):
 global count
 result=original(*args,**kwargs);count+=1
 if count==point:os._exit(77)
 return result
if boundary=='move':books._move=exit_after
else:books._save_journal=exit_after
with books.ensure_book_state(str(root)):
 books.apply_book_state_locked(str(root),{'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'},['chapters','export.mp3','checkpoint.json'])
'''
        recovery="import sys;from book_state_transaction import ensure_book_state;\nwith ensure_book_state(sys.argv[1]):pass"
        cases=[('move',n) for n in range(1,11)]+[('journal',1),('journal',2)]
        for boundary,point in cases:
            with self.subTest(boundary=boundary,point=point),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);originals=self.prepare(root)
                result=subprocess.run([sys.executable,'-c',worker,tmp,boundary,str(point)],capture_output=True,text=True,timeout=15)
                self.assertEqual(77,result.returncode,result.stderr)
                for repeat in range(2):
                    result=subprocess.run([sys.executable,'-c',recovery,tmp],capture_output=True,text=True,timeout=15)
                    self.assertEqual(0,result.returncode,result.stderr)
                expected={'script.json':b'new script','voice.json':b'new voices','state.json':b'new identity','new.json':b'new only'} if boundary=='journal' and point==2 else originals
                self.assertEqual(expected,self.snapshot(root))

    def test_corrupt_original_fails_before_restoring_any_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.prepare(root);move=books._move;counter=0
            def leave_pending(source,destination):
                nonlocal counter
                move(source,destination);counter+=1
                if counter==3:raise SystemExit('fixture stop')
            with patch.object(books,'_move',side_effect=leave_pending),patch.object(books,'recover_book_state_locked'):
                with self.assertRaises(SystemExit):self.switch(root)
            data=json.loads((root/books.JOURNAL).read_text());original=root/data['workspace']/'old-0';original.write_bytes(b'corrupt snapshot')
            before=self.snapshot(root)
            with self.assertRaisesRegex(ValueError,'missing or changed'):
                with books.ensure_book_state(tmp):pass
            self.assertEqual(before,self.snapshot(root))

    def test_external_symlink_and_duplicate_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            root=Path(tmp);other=Path(outside)/'protected';other.write_bytes(b'protected')
            (root/'link').symlink_to(other)
            for replacements,removals in [({'../protected':b'bad'},[]),({'link':b'bad'},[]),({'same':b'bad'},['same'])]:
                with self.subTest(replacements=replacements),self.assertRaises(ValueError):
                    with books.ensure_book_state(tmp):books.apply_book_state_locked(tmp,replacements,removals)
            self.assertEqual(b'protected',other.read_bytes())


class SavedBookTransactionHttpTests(unittest.TestCase):
    def test_http_load_failure_preserves_prior_book_and_success_publishes_all(self):
        from contextlib import ExitStack
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import scripts_library as lib
        from project import CHAPTER_EXPORT_DIR
        for mode in ("success", "move_failure", "bad_voices"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);saved=root/'scripts';saved.mkdir()
                (saved/'new.json').write_bytes(b'[{"speaker":"NEW","text":"new book"}]')
                (saved/'new.voice_config.json').write_bytes(b'[]' if mode=='bad_voices' else b'{"NEW":{"voice":"Aiden"}}')
                active=root/'annotated_script.json';active.write_bytes(b'[{"speaker":"OLD","text":"old book"}]')
                voice=root/'voice_config.json';voice.write_bytes(b'{"OLD":{"voice":"Ryan"}}')
                state=root/'state.json';state.write_bytes(b'{"active_book_id":"old","keep":17}')
                export=root/'audiobook.mp3';export.write_bytes(b'old export')
                chunks=root/'chunks.json';chunks.write_bytes(b'[{"id":1}]')
                chunk_journal=root/'chunks.json.status.jsonl';chunk_journal.write_bytes(b'old chunk status journal')
                from generation_checkpoint_shards import GenerationCheckpointShards
                from generate_script import get_generation_checkpoint_path
                generation=Path(get_generation_checkpoint_path(str(active)))
                generation_writer=GenerationCheckpointShards(generation, {'source_sha256':'old'})
                generation_writer.save_chunks([{'entries':[{'text':'accepted old source'}]}])
                generation_shard=generation_writer.directory/'00000000.json'
                chapters=root/CHAPTER_EXPORT_DIR;chapters.mkdir();(chapters/'part.mp3').write_bytes(b'chapter bytes')
                checkpoint=Path(str(active)+'.review_checkpoint.json');checkpoint.write_bytes(b'{"old":true}')
                originals={p:p.read_bytes() for p in (active,voice,state,export,chunks,chunk_journal,generation,generation_shard,chapters/'part.mp3',checkpoint)}
                app=FastAPI();app.include_router(lib.router)
                with ExitStack() as stack:
                    for name,value in (("SCRIPT_PATH",str(active)),("VOICE_CONFIG_PATH",str(voice)),("DATA_DIR",tmp),("SCRIPTS_DIR",str(saved)),("CHUNKS_PATH",str(chunks)),("AUDIOBOOK_PATH",str(export)),("M4B_PATH",str(root/'audiobook.m4b')),("process_state",{})):
                        stack.enter_context(patch.object(lib,name,value))
                    stack.enter_context(patch.object(lib,'_get_saved_book_id',return_value='new-book'))
                    if mode=='move_failure':
                        move=books._move;count=0
                        def fail_once(source,destination):
                            nonlocal count
                            count+=1
                            if count==4:raise OSError('fixture load disk full')
                            return move(source,destination)
                        stack.enter_context(patch.object(books,'_move',side_effect=fail_once))
                    client=stack.enter_context(TestClient(app,raise_server_exceptions=False))
                    response=client.post('/api/scripts/load',json={'name':'new'})
                if mode=='success':
                    self.assertEqual(200,response.status_code,response.text)
                    self.assertEqual((saved/'new.json').read_bytes(),active.read_bytes())
                    self.assertEqual((saved/'new.voice_config.json').read_bytes(),voice.read_bytes())
                    identity=json.loads(state.read_text());self.assertEqual('new-book',identity['active_book_id']);self.assertEqual(17,identity['keep']);self.assertEqual(32,len(identity['book_generation']))
                    for path in (export,chunks,chunk_journal,generation,generation_writer.directory,chapters,checkpoint):self.assertFalse(path.exists())
                else:
                    self.assertEqual(500 if mode=='move_failure' else 400,response.status_code,response.text)
                    for path,expected in originals.items():self.assertEqual(expected,path.read_bytes())
                self.assertFalse((root/books.JOURNAL).exists())
                self.assertEqual([],list(root.glob('.book-switch-*')))


class BookPersonaBindingTests(unittest.TestCase):
    def test_actual_persona_main_rejects_changed_book_and_keeps_original_artifact_identity(self):
        from contextlib import ExitStack
        import array
        import wave
        from types import SimpleNamespace
        import generate_personas as personas
        for advanced in (False, True):
            with self.subTest(advanced=advanced),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                (root/'annotated_script.json').write_bytes(b'[{"speaker":"ALICE","text":"Original book source."}]')
                (root/'voice_config.json').write_bytes(b'{"ALICE":{"description":"initial","keep":17}}')
                (root/'state.json').write_bytes(b'{"active_book_id":"original book","book_generation":"original"}')
                source=root/'rendered.wav'
                with wave.open(str(source),'wb') as audio:
                    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000);audio.writeframes(array.array('h',[1000]*600).tobytes())
                next_voices=b'{"ALICE":{"description":"different book human voice","keep":17}}'
                def render(**kwargs):
                    with books.ensure_book_state(tmp):
                        books.apply_book_state_locked(tmp,{'annotated_script.json':b'[{"speaker":"ALICE","text":"Different book source."}]','voice_config.json':next_voices,'state.json':b'{"active_book_id":"different book","book_generation":"different"}'},[])
                    return str(source),None
                with ExitStack() as stack:
                    for name,value in (("get_runtime_data_dir",tmp),("load_app_config",{}),("get_active_llm_config",{"model_name":"fixture","base_url":"http://unused.invalid"}),("ensure_ideal_settings",(False,{"context_length":4096},"fixture")),("make_run_client",object()),("llm_timeout_seconds",30),("TTSEngine",SimpleNamespace(generate_voice_design=render)),("call_llm_for_object",{"description":"Warm natural voice.","ref_text":"Original book source."})):
                        stack.enter_context(patch.object(personas,name,return_value=value))
                    stack.enter_context(patch.object(personas.time,'sleep'))
                    stack.enter_context(patch.object(sys,'argv',['generate_personas.py']+(['--advanced'] if advanced else [])))
                    with self.assertRaisesRegex(ValueError,'Active book changed'):personas.main()
                self.assertEqual(next_voices,(root/'voice_config.json').read_bytes())
                metas=list((root/'designed_voices/persona').rglob('meta.json'));self.assertEqual(1,len(metas))
                self.assertEqual('original book',json.loads(metas[0].read_text())['book_id'])
                if advanced:
                    refs=list((root/'persona_refs').rglob('alice.json'));self.assertEqual(1,len(refs));self.assertEqual(['Original book source.'],json.loads(refs[0].read_text())['sample_lines'])

    def test_same_book_reload_nonce_rejects_and_newer_human_fields_are_preserved(self):
        import copy
        import generate_personas as personas
        from utils import atomic_json_write,file_lock
        for change in ('reload','human_edit'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'annotated_script.json').write_bytes(b'[]');(root/'state.json').write_bytes(b'{"active_book_id":"same","book_generation":"one"}')
                path=root/'voice_config.json';initial={'ALICE':{'description':'old','keep':17,'seed':3}};atomic_json_write(initial,str(path))
                with books.ensure_book_state(tmp):snapshot=books.get_book_snapshot(tmp)
                generated=copy.deepcopy(initial);generated['ALICE'].update(description='generated',persona_status='generated')
                if change=='reload':
                    with books.ensure_book_state(tmp):books.apply_book_state_locked(tmp,{'state.json':b'{"active_book_id":"same","book_generation":"two"}'},[])
                    before=path.read_bytes()
                    with self.assertRaisesRegex(ValueError,'Active book changed'):personas.save_generated_voice_config(str(path),generated,initial,['ALICE'],{},book_snapshot=snapshot)
                    self.assertEqual(before,path.read_bytes())
                else:
                    with file_lock(str(path)):atomic_json_write({'ALICE':{'description':'human latest','keep':17,'seed':0}},str(path))
                    result=personas.save_generated_voice_config(str(path),generated,initial,['ALICE'],{},book_snapshot=snapshot)
                    self.assertEqual('human latest',result['ALICE']['description']);self.assertEqual(0,result['ALICE']['seed']);self.assertEqual('generated',result['ALICE']['persona_status'])

    def test_application_startup_recovers_before_chunk_reset(self):
        import asyncio
        import app as app_module
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'script.json').write_bytes(b'old script');(root/'state.json').write_bytes(b'old identity')
            move=books._move
            def die(source,destination):
                move(source,destination)
                if destination.name=='script.json':raise SystemExit('fixture interruption')
            with books.ensure_book_state(tmp),patch.object(books,'_move',side_effect=die),patch.object(books,'recover_book_state_locked'):
                with self.assertRaises(SystemExit):books.apply_book_state_locked(tmp,{'script.json':b'new script','state.json':b'new identity'},[])
            self.assertEqual(b'new script',(root/'script.json').read_bytes())
            def reset():
                self.assertEqual(b'old script',(root/'script.json').read_bytes());self.assertEqual(b'old identity',(root/'state.json').read_bytes());self.assertFalse((root/books.JOURNAL).exists())
            async def start():
                async with app_module.lifespan(app_module.app):pass
            with patch.object(app_module,'DATA_DIR',tmp),patch.object(app_module,'reset_stuck_chunks',side_effect=reset) as check,patch.object(app_module,'mark_interrupted_runs',return_value=0),patch.object(app_module.evaluation_reviews,'prune_sessions',return_value=0):asyncio.run(start())
            check.assert_called_once()


class BookReaderAdmissionTests(unittest.TestCase):
    def test_http_voice_snapshot_waits_for_complete_switch(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import ExitStack
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import scripts_library as lib, voices
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);saved=root/'scripts';saved.mkdir()
            active=root/'annotated_script.json';active.write_bytes(b'[{"speaker":"OLD","text":"old"}]')
            voice=root/'voice_config.json';voice.write_bytes(b'{"OLD":{"type":"custom","voice":"Ryan"}}')
            (saved/'new.json').write_bytes(b'[{"speaker":"NEW","text":"new"}]')
            (saved/'new.voice_config.json').write_bytes(b'{"NEW":{"type":"custom","voice":"Aiden"}}')
            app=FastAPI();app.include_router(lib.router);app.include_router(voices.router)
            paused=threading.Event();release=threading.Event();reader_entered=threading.Event()
            move=books._move;read=voices._ensure_voice_listing
            def pause_mid_switch(source,destination):
                move(source,destination)
                if destination==active:
                    paused.set()
                    if not release.wait(5):raise TimeoutError('fixture did not release switch')
            def guarded_read():
                reader_entered.set()
                return read()
            with ExitStack() as stack:
                for module,values in ((lib,{'SCRIPT_PATH':str(active),'VOICE_CONFIG_PATH':str(voice),'DATA_DIR':tmp,'SCRIPTS_DIR':str(saved),'CHUNKS_PATH':str(root/'chunks.json'),'AUDIOBOOK_PATH':str(root/'audiobook.mp3'),'M4B_PATH':str(root/'audiobook.m4b'),'process_state':{}}),(voices,{'SCRIPT_PATH':str(active),'VOICE_CONFIG_PATH':str(voice)})):
                    for name,value in values.items():stack.enter_context(patch.object(module,name,value))
                stack.enter_context(patch.object(lib,'_get_saved_book_id',return_value='new'))
                stack.enter_context(patch.object(books,'_move',side_effect=pause_mid_switch))
                stack.enter_context(patch.object(voices,'_ensure_voice_listing',side_effect=guarded_read))
                first=stack.enter_context(TestClient(app));second=stack.enter_context(TestClient(app))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    load=pool.submit(first.post,'/api/scripts/load',json={'name':'new'})
                    try:
                        self.assertTrue(paused.wait(5))
                        response=pool.submit(second.get,'/api/voices')
                        self.assertTrue(reader_entered.wait(5))
                        self.assertFalse(response.done())
                    finally:release.set()
                    self.assertEqual(200,load.result(timeout=10).status_code)
                    listing=response.result(timeout=10)
                    self.assertEqual(200,listing.status_code)
                    self.assertEqual([{'name':'NEW','config':{'type':'custom','voice':'Aiden'},'persona_pending':True}],listing.json())
