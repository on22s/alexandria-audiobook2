"""Actual encoded-byte/store restore boundaries with declared serving identity.

No native model, GPU, ASR or listening claim follows from these CPU fixtures.
"""
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio_take_history import AudioTakeHistory
from book_state_transaction import get_book_operation_token
from synthesis_recovery_serving import NativeRecoveryServingIdentity
from synthesis_workspace import is_synthesis_workspace_supported
from take_publication import KEY
from take_restoration import capture_restore_target, restore_editor_take
from tests import test_take_publication as publication_cases


class TakeRestorationTests(unittest.TestCase):
    fixture=publication_cases.TakePublicationTests.fixture
    owner=publication_cases.TakePublicationTests.owner
    engine=publication_cases.TakePublicationTests.engine
    admission=publication_cases.TakePublicationTests.admission
    render=publication_cases.TakePublicationTests.render

    @contextmanager
    def prepared(self,journal=False,encoded_suffix=None,before_retake=None):
        with self.fixture(journal) as (root,manager,_,fresh),self.owner():
            engine=self.engine()
            Path(manager.config_path).write_text(json.dumps({'tts':{
                **engine.get_model_settings(),'synthesis_recovery':engine._synthesis_recovery}}))
            manager.invalidate_config_cache()
            Path(manager.voice_config_path).write_text(json.dumps(
                {'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
            chunk=manager.load_chunks()[0];parent,admitted=self.admission(manager,engine,chunk)
            pcm,_,synthesis,observed=self.render(root,engine,parent,chunk)
            def encode(source,name):
                import soundfile as sf
                destination=root/'voicelines'/(name+encoded_suffix)
                if encoded_suffix=='.mp3':
                    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),str(destination)],
                        check=True,capture_output=True,timeout=15)
                else:
                    samples,rate=sf.read(source)
                    sf.write(destination,samples,rate)
                return destination.relative_to(root).as_posix()
            with ExitStack() as stack:
                if encoded_suffix:stack.enter_context(patch.object(manager,'_export_chunk_audio',side_effect=encode))
                manager._publish_generated_chunk_audio({**chunk,'_synthesis_receipt':synthesis,'_speech_input':observed},
                    str(pcm),'NARRATOR',recovery_parent=parent,publication_admission=admitted)
            origin=copy.deepcopy(manager.load_chunks()[0]);original=(root/origin['audio_path']).read_bytes()
            if before_retake:before_retake(root,manager,origin)
            manager.update_chunk(0,{'take':3})
            manager._publish_generated_chunk_audio(manager.load_chunks()[0],str(fresh),'NARRATOR')
            current=copy.deepcopy(manager.load_chunks()[0]);displaced=(root/current['audio_path']).read_bytes()
            self.assertNotEqual(original,displaced)
            with AudioTakeHistory(root)._locked() as document:
                saved=copy.deepcopy(next(row for row in document['records']
                    if row['audio_revision']==origin['audio_revision']))
            request=SimpleNamespace(expected_book_token=get_book_operation_token(root),
                source_book_token=get_book_operation_token(root),expected_uid=current['uid'],
                expected_audio_revision=current['audio_revision'],expected_audio_path=current['audio_path'],
                expected_take=current['take'],version=saved['version'],sha256=saved['sha256'])
            identity=NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)
            with patch.object(manager,'get_engine',return_value=engine), \
                    patch('synthesis_recovery_serving.capture_native_custom_serving_identity',return_value=identity):
                yield root,manager,engine,request,origin,original,current,displaced,saved

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_restore_selects_original_bytes_counter_and_complete_proof_in_both_stores(self):
        for journal in (False,True):
            with self.subTest(journal=journal),self.prepared(journal) as args:
                root,manager,_,request,origin,original,current,displaced,saved=args
                expected=capture_restore_target(manager,0,request)
                row=restore_editor_take(manager,0,request,expected)
                self.assertEqual(row['take'],origin['take'])
                self.assertEqual(row['audio_revision'],origin['audio_revision'])
                self.assertEqual(row[KEY],origin[KEY])
                self.assertEqual(row['synthesis_receipt'],origin['synthesis_receipt'])
                self.assertEqual((root/row['audio_path']).read_bytes(),original)
                self.assertEqual(hashlib.sha256(original).hexdigest(),row[KEY]['audio']['sha256'])
                self.assertEqual(row['text'],current['text'])
                self.assertEqual(row['speaker'],current['speaker'])
                with AudioTakeHistory(root)._locked() as document:
                    held=next(record for record in document['records']
                        if record['audio_revision']==current['audio_revision'])
                    self.assertEqual((root/'take_history'/f"{held['version']}{held['suffix']}").read_bytes(),displaced)
                    self.assertEqual(next(record for record in document['records']
                        if record['version']==saved['version']),saved)
                from synthesis_workspace import SynthesisWorkspaceRegistry
                with SynthesisWorkspaceRegistry(root) as registry:self.assertEqual(registry.body['rows'],{})

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_failed_final_metadata_keeps_displaced_bytes_revision_and_take(self):
        for journal in (False,True):
            with self.subTest(journal=journal),self.prepared(journal) as args:
                root,manager,_,request,_,_,current,displaced,_=args
                expected=capture_restore_target(manager,0,request)
                target,method=(manager._get_status_journal_locked(),'_append') if journal else (manager,'_save_chunks_locked')
                write=getattr(target,method)
                def fail(*args,**kwargs):
                    data=args[0].get('changes',{}) if journal else args[0][0]
                    if data.get(KEY) is not None:raise OSError('restore metadata refused')
                    return write(*args,**kwargs)
                with patch.object(target,method,side_effect=fail),self.assertRaisesRegex(OSError,'restore metadata refused'):
                    restore_editor_take(manager,0,request,expected)
                row=manager.load_chunks()[0]
                for field in ('audio_path','audio_revision','take'):self.assertEqual(row[field],current[field])
                self.assertEqual((root/row['audio_path']).read_bytes(),displaced)

    def test_stale_current_counter_or_path_is_refused_without_loading_engine(self):
        with self.prepared() as args:
            _,manager,_,request,_,_,_,_,_=args
            with patch.object(manager,'get_engine',side_effect=AssertionError('stale restore loaded engine')):
                for field,value in (('expected_take',999),('expected_audio_path',None),
                                    ('expected_uid','other'),('expected_audio_revision','old')):
                    changed=SimpleNamespace(**{**vars(request),field:value})
                    with self.subTest(field=field),self.assertRaises(ValueError):
                        capture_restore_target(manager,0,changed)

    def test_unsupported_workspace_host_refuses_before_engine_or_archive_mutation(self):
        with self.prepared() as args:
            root,manager,_,request,_,_,current,displaced,_=args
            raw=(root/'take_history/manifest.json').read_bytes()
            with patch('synthesis_workspace.is_synthesis_workspace_supported',return_value=False), \
                    patch.object(manager,'get_engine',side_effect=AssertionError('unsupported restore loaded engine')), \
                    self.assertRaisesRegex(ValueError,'not qualified on this host'):
                restore_editor_take(manager,0,request,current)
            self.assertEqual((root/'take_history/manifest.json').read_bytes(),raw)
            self.assertEqual((root/current['audio_path']).read_bytes(),displaced)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_changed_dependencies_and_corrupt_saved_bindings_never_replace_selected_audio(self):
        cases=('inputs','voice','model','pronunciation','recipe','speech','serving','synthesis')
        for journal in (False,True):
            for case in cases:
                with self.subTest(journal=journal,case=case),self.prepared(journal) as args,ExitStack() as stack:
                    root,manager,engine,request,_,_,current,displaced,saved=args
                    if case=='voice':
                        Path(manager.voice_config_path).write_text(json.dumps(
                            {'NARRATOR':{'type':'custom','voice':'Ryan','seed':43}}))
                    elif case=='model':
                        engine._max_new_tokens=257
                        config=json.loads(Path(manager.config_path).read_text())
                        config['tts']['max_new_tokens']=257
                        Path(manager.config_path).write_text(json.dumps(config));manager.invalidate_config_cache()
                    elif case=='pronunciation':
                        lexicon=root/'changed-pronunciation.json'
                        lexicon.write_text(json.dumps({'Original':'Alternate'}))
                        stack.enter_context(patch('pronunciation.DEFAULT_PATH',str(lexicon)))
                    elif case in ('inputs','recipe','speech','synthesis'):
                        history=AudioTakeHistory(root)
                        with history._locked() as document:
                            row=next(row for row in document['records'] if row['version']==saved['version'])
                            if case=='inputs':row['captured']['generation_inputs']['text']='Changed authored words.'
                            elif case=='recipe':row['captured'][KEY]['dependencies']['recipe_sha256']='f'*64
                            elif case=='speech':row['captured'][KEY]['speech_sha256']='f'*64
                            else:row['captured']['synthesis_receipt']['output_frames']+=1
                            history._save(document)
                    identity=NativeRecoveryServingIdentity(('f' if case=='serving' else 'b')*64,'c'*64,'d'*64)
                    capture=stack.enter_context(patch(
                        'synthesis_recovery_serving.capture_native_custom_serving_identity',return_value=identity))
                    with self.assertRaises(ValueError):restore_editor_take(manager,0,request,current)
                    self.assertEqual(capture.call_count,1 if case=='serving' else 0)
                    row=manager.load_chunks()[0]
                    for field in ('audio_path','audio_revision','take','text','speaker','instruct'):
                        self.assertEqual(row.get(field),current.get(field))
                    self.assertEqual((root/row['audio_path']).read_bytes(),displaced)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_capacity_refuses_before_loading_engine(self):
        with self.prepared() as args:
            root,manager,_,request,_,_,current,displaced,_=args
            with AudioTakeHistory(root)._locked() as document:count=len(document['records'])
            with patch('audio_take_history.MAX_RECORDS',count), \
                    patch.object(manager,'get_engine',side_effect=AssertionError('full archive loaded engine')), \
                    self.assertRaisesRegex(ValueError,'full'):
                restore_editor_take(manager,0,request,current)
            row=manager.load_chunks()[0]
            self.assertEqual(row['audio_path'],current['audio_path'])
            self.assertEqual((root/row['audio_path']).read_bytes(),displaced)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_edits_during_native_initialization_are_preserved(self):
        for field,value in (('take',4),('text','Changed synthetic words.')):
            with self.subTest(field=field),self.prepared() as args:
                root,manager,_,request,_,_,current,displaced,_=args
                def edit(*args,**kwargs):
                    manager.update_chunk(0,{field:value})
                    return NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)
                with patch('synthesis_recovery_serving.capture_native_custom_serving_identity',side_effect=edit), \
                        self.assertRaisesRegex(ValueError,'line or take changed'):
                    restore_editor_take(manager,0,request,current)
                row=manager.load_chunks()[0]
                self.assertEqual(row[field],value)
                self.assertIsNone(row['audio_path'])
                self.assertEqual((root/current['audio_path']).read_bytes(),displaced)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_shared_pronunciation_save_before_final_admission_refuses_even_with_inherited_snapshot(self):
        from routers.pronunciation_panel import (PronunciationSave, get_pronunciation_document,
                                                 save_pronunciation_document)
        import pronunciation
        for journal in (False,True):
            with self.subTest(journal=journal),self.prepared(journal) as args:
                root,manager,_,request,_,_,current,displaced,_=args
                lexicon=root/'shared-pronunciation.json';lexicon.write_text('{}')
                verify=AudioTakeHistory.verify_retained
                def save_before_final(history,*args,**kwargs):
                    result=verify(history,*args,**kwargs)
                    _,snapshot=get_pronunciation_document()
                    save_pronunciation_document(PronunciationSave(
                        names={'Original':'Different'},revision=snapshot['revision']))
                    return result
                with patch.object(pronunciation,'DEFAULT_PATH',str(lexicon)), \
                        pronunciation.ensure_pronunciation_audition({}), \
                        patch.object(AudioTakeHistory,'verify_retained',save_before_final), \
                        self.assertRaisesRegex(ValueError,'pronunciation or synthesis windows'):
                    restore_editor_take(manager,0,request,current)
                row=manager.load_chunks()[0]
                for field in ('take','audio_revision','audio_path'):self.assertEqual(row[field],current[field])
                self.assertEqual((root/row['audio_path']).read_bytes(),displaced)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_later_shared_pronunciation_saver_waits_for_selection_without_holding_owner(self):
        from routers.pronunciation_panel import (PronunciationSave, get_pronunciation_document,
                                                 save_pronunciation_document)
        from utils import file_lock
        import pronunciation
        for journal in (False,True):
            with self.subTest(journal=journal),self.prepared(journal) as args:
                root,manager,_,request,_,original,current,_,_=args
                lexicon=root/'shared-pronunciation.json';lexicon.write_text('{}')
                attempted=threading.Event();finished=threading.Event();errors=[];threads=[];calls=[]
                real_lock=file_lock
                def tracked_lock(path,*args,**kwargs):
                    if threading.current_thread() is not threading.main_thread():attempted.set()
                    return real_lock(path,*args,**kwargs)
                def save():
                    try:
                        _,snapshot=get_pronunciation_document()
                        save_pronunciation_document(PronunciationSave(
                            names={'Original':'Different'},revision=snapshot['revision']))
                    except BaseException as error:errors.append(error)
                    finally:finished.set()
                def identity(*args,**kwargs):
                    calls.append(1)
                    if len(calls)==2:
                        thread=threading.Thread(target=save);threads.append(thread);thread.start()
                        self.assertTrue(attempted.wait(2),'shared saver never attempted admission')
                        self.assertFalse(finished.is_set(),'shared saver passed final restore admission')
                        # This real nonreentrant fixture callback would time out
                        # if the final native capture retained the owner mutex.
                        from audio_generation_owner import ensure_audio_generation_active
                        ensure_audio_generation_active()
                    return NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)
                with patch.object(pronunciation,'DEFAULT_PATH',str(lexicon)), \
                        patch('routers.pronunciation_panel.file_lock',side_effect=tracked_lock), \
                        patch('synthesis_recovery_serving.capture_native_custom_serving_identity',side_effect=identity):
                    try:
                        row=restore_editor_take(manager,0,request,current)
                    finally:
                        for thread in threads:thread.join(3)
                    self.assertTrue(finished.is_set());self.assertEqual(errors,[])
                    self.assertEqual((root/row['audio_path']).read_bytes(),original)
                    self.assertEqual(get_pronunciation_document()[1]['names'],{'Original':'Different'})

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_saved_speech_evidence_rebinds_only_verified_installed_file_version(self):
        from speech_verification_receipt import (get_speech_verification_receipt,
                                                 get_current_speech_verification_receipt)
        from tests.test_speech_verification_receipt import get_report
        for journal in (False,True):
            for case in ('valid','confirmed_difference','wrong_audio','wrong_speech','malformed'):
                with self.subTest(journal=journal,case=case),self.prepared(journal) as args:
                    root,manager,_,request,origin,original,current,displaced,saved=args
                    source=root/'take_history'/f"{saved['version']}{saved['suffix']}"
                    text=origin['text'] if case!='wrong_speech' else 'Unrelated synthetic words.'
                    report=get_report(source,text,text if case!='confirmed_difference' else 'Original.',
                        status='no_content_candidate' if case!='confirmed_difference' else 'corroborated_content_difference')
                    receipt=get_speech_verification_receipt(report,origin,request.expected_book_token,
                        origin['audio_revision'],dict(text=text,language='English',provenance='declared_cpu_fixture'))
                    receipt['audio_file_version']=[1,2,3,4]
                    if case=='wrong_audio':receipt['audio']['sha256']='f'*64
                    if case=='malformed':receipt['status']='passed'
                    history=AudioTakeHistory(root)
                    with history._locked() as document:
                        record=next(row for row in document['records'] if row['version']==saved['version'])
                        record['captured']['speech_verification']=copy.deepcopy(receipt);history._save(document)
                    if case=='valid':
                        row=restore_editor_take(manager,0,request,current)
                        restored=row['speech_verification'];info=(root/row['audio_path']).stat()
                        self.assertEqual(restored['audio_file_version'],
                            [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns])
                        self.assertEqual({**restored,'audio_file_version':[1,2,3,4]},receipt)
                        self.assertIsNotNone(get_current_speech_verification_receipt(row,request.expected_book_token,
                            resolve_audio=lambda path:root/path))
                        self.assertEqual((root/row['audio_path']).read_bytes(),original)
                        with history._locked() as document:
                            record=next(row for row in document['records'] if row['version']==saved['version'])
                            self.assertEqual(record['captured']['speech_verification'],receipt)
                    else:
                        with patch.object(manager,'get_engine',side_effect=AssertionError('bad evidence loaded engine')), \
                                self.assertRaisesRegex(ValueError,'Saved speech evidence needs inspection'):
                            restore_editor_take(manager,0,request,current)
                        row=manager.load_chunks()[0]
                        for field in ('take','audio_revision','audio_path'):self.assertEqual(row[field],current[field])
                        self.assertEqual((root/row['audio_path']).read_bytes(),displaced)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_cancelled_or_replaced_owner_at_final_capture_cannot_select(self):
        from audio_generation_owner import (AudioGenerationOwner, AudioGenerationCancelled,
                                             ensure_audio_generation_owner)
        from synthesis_workspace import SynthesisWorkspaceRegistry
        for journal in (False,True):
            for case in ('cancelled','replaced'):
                with self.subTest(journal=journal,case=case),self.prepared(journal) as args,ExitStack() as stack:
                    root,manager,_,request,_,_,current,displaced,_=args
                    active=[True];mutex=threading.Lock();calls=[]
                    @contextmanager
                    def admit(**kwargs):
                        if not mutex.acquire(timeout=.25):raise TimeoutError('restore owner mutex reentry')
                        try:
                            if not active[0] and not kwargs.get('allow_cancelled',False):raise AudioGenerationCancelled()
                            yield
                        finally:mutex.release()
                    stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner('a'*32,admit)))
                    def identity(*args,**kwargs):
                        calls.append(1)
                        if len(calls)==2:
                            if case=='cancelled':active[0]=False
                            else:stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner('f'*32,admit)))
                        return NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)
                    with patch('synthesis_recovery_serving.capture_native_custom_serving_identity',side_effect=identity), \
                            self.assertRaises((AudioGenerationCancelled,ValueError)):
                        restore_editor_take(manager,0,request,current)
                    active[0]=True
                    row=manager.load_chunks()[0]
                    for field in ('take','audio_revision','audio_path'):self.assertEqual(row[field],current[field])
                    self.assertEqual((root/row['audio_path']).read_bytes(),displaced)
                    with SynthesisWorkspaceRegistry(root) as registry:self.assertEqual(registry.body['rows'],{})

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_real_encoded_formats_restore_identical_bytes_with_original_suffix_and_shape(self):
        import soundfile as sf
        from synthesis_workspace import SynthesisWorkspaceRegistry
        for journal in (False,True):
            for suffix in ('.mp3','.flac','.ogg'):
                with self.subTest(journal=journal,suffix=suffix),self.prepared(journal,suffix) as args:
                    root,manager,_,request,origin,original,current,displaced,_=args
                    restored=restore_editor_take(manager,0,request,current)
                    path=root/restored['audio_path']
                    self.assertEqual(path.suffix,suffix)
                    self.assertEqual(path.read_bytes(),original)
                    self.assertEqual(restored[KEY],origin[KEY])
                    info=sf.info(path)
                    self.assertEqual({key:restored[KEY]['audio'][key] for key in ('frames','sample_rate','channels')},
                        dict(frames=info.frames,sample_rate=info.samplerate,channels=info.channels))
                    with AudioTakeHistory(root)._locked() as document:
                        record=next(row for row in document['records'] if row['audio_revision']==current['audio_revision'])
                        self.assertEqual((root/'take_history'/f"{record['version']}{record['suffix']}").read_bytes(),displaced)
                    with SynthesisWorkspaceRegistry(root) as registry:self.assertEqual(registry.body['rows'],{})

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_late_original_timing_result_cannot_attach_after_distinct_take_and_restore(self):
        from editor_word_timing import publish_word_timing, get_timing_cache_path
        for journal in (False,True):
            with self.subTest(journal=journal),ExitStack() as stack:
                entered=threading.Event();release=threading.Event();threads=[];errors=[];heard=[]
                def render(audio,runtime,text,cancel,directory):
                    heard.append(Path(audio).read_bytes());entered.set()
                    if not release.wait(5):raise TimeoutError('timing fixture never released')
                    return dict(display_text=text,spans=[],duration=.3,notice='Declared timing fixture',
                                provider='declared_cpu_fixture',language='en')
                stack.enter_context(patch('editor_word_timing.render_word_timing',side_effect=render))
                def start(root,manager,origin):
                    runtime={key:str(root/key) for key in ('binary','model')}
                    for path in runtime.values():Path(path).write_bytes(b'declared timing provider fixture')
                    stack.enter_context(patch('cpu_asr_scratch.get_cpu_asr_scratch_parent',return_value=root))
                    request=SimpleNamespace(expected_book_token=get_book_operation_token(root),
                        expected_uid=origin['uid'],expected_audio_revision=origin['audio_revision'])
                    def run():
                        try:publish_word_timing(manager,0,request,runtime,lambda *args:None,lambda:False)
                        except BaseException as error:errors.append(error)
                    thread=threading.Thread(target=run);threads.append(thread);thread.start()
                    self.assertTrue(entered.wait(3),'timing never captured original take')
                try:
                    with self.prepared(journal,before_retake=start) as args:
                        root,manager,_,request,origin,original,current,displaced,_=args
                        self.assertEqual(heard,[original]);self.assertNotEqual(original,displaced)
                        restored=restore_editor_take(manager,0,request,current)
                        self.assertEqual(restored['audio_revision'],origin['audio_revision'])
                        self.assertEqual((root/restored['audio_path']).read_bytes(),original)
                        self.assertNotEqual(restored['audio_path'],origin['audio_path'])
                        release.set()
                        for thread in threads:thread.join(3)
                        self.assertFalse(any(thread.is_alive() for thread in threads))
                        self.assertEqual(len(errors),1)
                        self.assertIsInstance(errors[0],ValueError)
                        self.assertIn('changed',str(errors[0]))
                        self.assertFalse(get_timing_cache_path(manager,request.expected_book_token,request.expected_uid).exists())
                        self.assertEqual(manager.get_saved_chunks()[0]['audio_path'],restored['audio_path'])
                finally:
                    release.set()
                    for thread in threads:thread.join(3)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_protected_source_can_restore_but_deleted_legacy_and_unproven_sources_refuse_cheaply(self):
        for journal in (False,True):
            for case in ('protected','deleted','legacy','unproven'):
                with self.subTest(journal=journal,case=case),self.prepared(journal) as args:
                    root,manager,_,request,_,original,current,displaced,saved=args
                    history=AudioTakeHistory(root)
                    with history._locked() as document:
                        record=next(row for row in document['records'] if row['version']==saved['version'])
                        if case=='protected':record['protected']=True
                        elif case=='deleted':record['state']='deleted'
                        elif case=='legacy':document['version']=1
                        else:record['captured'][KEY]=None
                        history._save(document)
                    if case=='protected':
                        restored=restore_editor_take(manager,0,request,capture_restore_target(manager,0,request))
                        self.assertEqual((root/restored['audio_path']).read_bytes(),original)
                        with history._locked() as document:
                            self.assertTrue(next(row for row in document['records'] if row['version']==saved['version'])['protected'])
                    else:
                        with patch.object(manager,'get_engine',side_effect=AssertionError('ineligible archive loaded engine')), \
                                self.assertRaises(ValueError):capture_restore_target(manager,0,request)
                        row=manager.get_saved_chunks()[0]
                        for field in ('take','audio_revision','audio_path'):self.assertEqual(row[field],current[field])
                        self.assertEqual((root/row['audio_path']).read_bytes(),displaced)

    @unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
    def test_source_replacement_links_and_stage_aliases_refuse_without_overwriting_current_or_outside_bytes(self):
        import os
        import shutil
        import sys
        import tempfile
        import project
        from synthesis_workspace import SynthesisWorkspaceRegistry
        for journal in (False,True):
            for case in ('source_changed','source_replaced','source_hardlink','stage_symlink','stage_hardlink'):
                with self.subTest(journal=journal,case=case),self.prepared(journal) as args:
                    root,manager,_,request,_,_,current,displaced,saved=args
                    source=root/'take_history'/f"{saved['version']}{saved['suffix']}"
                    outside=root/'preserved-outside.wav';outside.write_bytes(displaced)
                    create=SynthesisWorkspaceRegistry.create_encoded_parent
                    def change(registry,name,suffix):
                        fd=create(registry,name,suffix)
                        if case=='source_changed':
                            with source.open('ab') as stream:stream.write(b'changed')
                        elif case=='source_replaced':
                            replacement=root/'replacement.wav';replacement.write_bytes(displaced);os.replace(replacement,source)
                        elif case=='source_hardlink':os.link(source,root/'source-hardlink.wav')
                        else:
                            from synthesis_workspace import DIRECTORY
                            path=root/DIRECTORY/name/('assembled-parent'+suffix)
                            path.unlink()
                            if case=='stage_symlink':path.symlink_to(outside)
                            else:os.link(outside,path)
                        return fd
                    with patch.object(SynthesisWorkspaceRegistry,'create_encoded_parent',change), \
                            self.assertRaises((ValueError,OSError)):
                        restore_editor_take(manager,0,request,current)
                    row=manager.get_saved_chunks()[0]
                    for field in ('take','audio_revision','audio_path'):self.assertEqual(row[field],current[field])
                    self.assertEqual((root/row['audio_path']).read_bytes(),displaced)
                    self.assertEqual(outside.read_bytes(),displaced)

        # Qualify the existing shared restore guards rather than changing the
        # protocol used by all four saved formats. A copied registry retains
        # the old inode identities; it cannot authorize writing in another root.
        for journal in (False,True):
            for boundary in ('registry','read'):
                with self.subTest(journal=journal,root_boundary=boundary),self.prepared(journal) as args, \
                        tempfile.TemporaryDirectory() as tmp:
                    root,manager,_,request,_,_,current,displaced,_=args
                    other=Path(tmp)/'other-book';initial_root=manager.root_dir
                    old_profile=sys.getprofile();admissions=0;changed=[];clone_before={}
                    def switch_root(frame,event,value):
                        nonlocal admissions
                        if boundary=='registry':
                            if (event!='call' or frame.f_code.co_name!='admit'
                                    or frame.f_code.co_filename!=project.__file__):return
                            admissions+=1
                            if admissions!=2:return
                        elif not (event=='return' and frame.f_code is SynthesisWorkspaceRegistry.create_encoded_parent.__code__):return
                        sys.setprofile(old_profile);shutil.copytree(root,other,symlinks=True)
                        namespace=other/'.synthesis-workspaces-v1'
                        clone_before.update({str(path.relative_to(other)):path.read_bytes()
                            for path in namespace.rglob('*') if path.is_file() and not path.is_symlink()})
                        (other/'sentinel').write_bytes(b'unrelated book');manager.root_dir=str(other);changed.append(True)
                    try:
                        sys.setprofile(switch_root)
                        with self.assertRaises((ValueError,OSError,RuntimeError)):
                            restore_editor_take(manager,0,request,current)
                    finally:sys.setprofile(old_profile);manager.root_dir=initial_root
                    self.assertEqual([True],changed)
                    row=manager.get_saved_chunks()[0]
                    for field in ('take','audio_revision','audio_path'):self.assertEqual(current[field],row[field])
                    self.assertEqual(displaced,(root/row['audio_path']).read_bytes())
                    self.assertEqual(b'unrelated book',(other/'sentinel').read_bytes())
                    for name,content in clone_before.items():self.assertEqual(content,(other/name).read_bytes())


if __name__=='__main__':unittest.main()
