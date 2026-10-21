"""Real PCM/encoding/store boundaries with declared native serving fixtures.

These CPU tests establish storage/input binding, not actual model provenance or
listening acceptance. Native model qualification is performed separately.
"""
from contextlib import contextmanager
from dataclasses import asdict
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import tts
from audio_generation_owner import AudioGenerationOwner, AudioGenerationCancelled, ensure_audio_generation_owner
from audio_take_history import AudioTakeHistory
from book_state_transaction import ensure_book_state, get_book_operation_token
from project import get_chunk_generation_inputs
from speech_text import get_speech_normalization
from synthesis_input import ensure_speech_input_capture, capture_speech_input, get_captured_speech_input
from synthesis_recovery import RecoverySession
from synthesis_recovery_identity import get_recovery_digest
from synthesis_recovery_serving import NativeRecoveryServingIdentity
from synthesis_render import render_synthesis_windows
from synthesis_settings import get_synthesis_window_settings
from take_publication import (KEY, capture_take_publication_admission, get_take_publication_receipt,
                              validate_take_publication)
from take_retention import get_retained_take_bytes, get_take_history_policy, set_take_history_policy
from tests import test_automatic_take_history as history_cases


class TakePublicationTests(unittest.TestCase):
    fixture = history_cases.AutomaticTakeHistoryTests.fixture

    @contextmanager
    def owner(self):
        mutex = threading.Lock()
        @contextmanager
        def admit(**kwargs):
            if not mutex.acquire(timeout=.25):
                raise TimeoutError('fixture owner mutex reentry')
            try:
                yield
            finally:
                mutex.release()
        with ensure_audio_generation_owner(AudioGenerationOwner('a' * 32, admit)):
            yield

    def engine(self):
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        engine._mode, engine._device, engine._language = 'local', 'cpu', 'English'
        engine._compile_codec_enabled, engine._max_new_tokens = False, 256
        engine._synthesis_windows = get_synthesis_window_settings({'enabled':True,
            'budgets':{'local.custom':{'max_chars':12}}})
        engine._synthesis_recovery = {'enabled':True, 'max_audio_mb':16}
        engine._speech_verification = None
        return engine

    def render(self, root, engine, parent, chunk, seconds=None):
        preparation = get_speech_normalization(chunk['text'])
        plan = engine._get_synthesis_window_plan(preparation['text'], 'custom')
        self.assertGreaterEqual(len(plan.windows), 2)
        descriptor = dict(version=1, parent=parent.get_semantic_identity(), plan=asdict(plan),
            policy=engine._synthesis_windows, serving_sha256='b' * 64, input_proof_version=1)
        session = RecoverySession('eligible', engine=engine, parent=parent,
            identity=NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64),
            key=get_recovery_digest(descriptor), input_plan=plan)
        pcm = root/'observed.wav'
        frames = 2400 if seconds is None else 24000 * seconds // len(plan.windows)
        def provider(text, path):
            sf.write(path, np.sin(np.arange(frames) * .1) * .2, 24000)
            capture_speech_input(path, text, 'English', 'declared_cpu_fixture')
            return True
        with ensure_speech_input_capture():
            receipt = render_synthesis_windows(plan, provider, pcm, engine._synthesis_windows, recovery=session)
            observed = get_captured_speech_input(pcm)
        return pcm, preparation, receipt, observed

    def admission(self, manager, engine, chunk):
        parent = manager._capture_synthesis_recovery_parent(engine, 0, chunk)
        voices = {'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}
        admitted = manager._capture_take_publication_admission(engine, parent, 0, chunk, voices, 'NARRATOR')
        self.assertIsNotNone(admitted)
        return parent, admitted

    def test_input_capture_can_be_requested_without_asr_and_does_not_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp)/'output.wav'; engine = self.engine()
            def provider(text, instruct, speaker, voices, path):
                sf.write(path, np.ones(2400) * .2, 24000)
                capture_speech_input(path, text, 'English', 'declared_cpu_fixture')
                return True
            with patch.object(engine, 'generate_voice', side_effect=provider):
                for enabled in (False, True, False):
                    result = engine.generate_voice_result('Actual submitted words.', '', 'A', {}, str(audio),
                                                         capture_input=enabled)
                    self.assertTrue(result.success)
                    self.assertEqual(result.speech_input is not None, enabled)
                    if enabled: self.assertEqual(result.speech_input['text'], 'Actual submitted words.')
                    self.assertIsNone(get_captured_speech_input(audio))
            def batch(self, chunks, voices, directory, seed):
                for row in chunks:
                    path = Path(directory)/f"temp_batch_{row['index']}.wav"
                    provider(row['text'], '', 'A', {}, path)
                return {'completed':[row['index'] for row in chunks],'failed':[]}
            render = tts.capture_batch_speech_inputs(batch)
            for enabled in (False, True, False):
                result = render(engine, [{'index':0,'text':'Actual batch input.'}], {}, tmp, capture_input=enabled)
                self.assertEqual('speech_inputs' in result, enabled)
                if enabled:self.assertEqual(result['speech_inputs'][0]['text'], 'Actual batch input.')

    def test_unknown_overridden_unowned_and_random_renderers_remain_unproven(self):
        with self.fixture() as (root, manager, _, _), self.owner():
            engine = self.engine(); chunk = manager.load_chunks()[0]
            parent, admitted = self.admission(manager, engine, chunk)
            args = (parent, admitted.book_token, chunk['uid'], 0, get_chunk_generation_inputs(chunk),
                    {'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}, 'NARRATOR')
            self.assertIsNone(capture_take_publication_admission(SimpleNamespace(), *args))
            for name in ('generate_voice_result','generate_batch','get_model_settings'):
                with self.subTest(method=name), patch.object(engine, name, lambda *a,**k:None):
                    self.assertIsNone(capture_take_publication_admission(engine, *args))
            random = (*args[:5], {'NARRATOR':{'type':'custom','voice':'Ryan','seed':-1}}, 'NARRATOR')
            self.assertIsNone(capture_take_publication_admission(engine, *random))
            with patch('take_publication._read_audio', side_effect=AssertionError('unproven output read')):
                self.assertEqual(get_take_publication_receipt({'trusted':True}, {}, {}, {}, {}, '', '', 'a'*32), (None,None))
        with patch('take_publication._read_audio', side_effect=AssertionError('expired claim read')):
            self.assertEqual(get_take_publication_receipt(admitted, {}, {}, {}, {}, '', '', 'a'*32), (None,None))

    def test_encoded_bytes_are_bound_separately_from_observed_pcm(self):
        with self.fixture() as (root, manager, _, _), self.owner():
            engine = self.engine(); chunk = manager.load_chunks()[0]
            parent, admitted = self.admission(manager, engine, chunk)
            pcm, preparation, synthesis, observed = self.render(root, engine, parent, chunk)
            encoded = root/'encoded.flac'; samples, rate = sf.read(pcm); sf.write(encoded,samples,rate)
            revision = 'e'*32; synthesis = {**synthesis,'audio_revision':revision,'scope':'pre-encoding PCM'}
            result, version = get_take_publication_receipt(admitted, get_chunk_generation_inputs(chunk),
                preparation, observed, synthesis, pcm, encoded, revision)
            self.assertEqual(result['audio']['sha256'], hashlib.sha256(encoded.read_bytes()).hexdigest())
            self.assertEqual(result['pcm']['sha256'], hashlib.sha256(pcm.read_bytes()).hexdigest())
            self.assertNotEqual(result['audio']['sha256'], result['pcm']['sha256'])
            self.assertEqual(result['audio']['frames'], sf.info(encoded).frames)
            self.assertEqual(version[2], encoded.stat().st_size)
            self.assertEqual(result['synthesis_sha256'], get_recovery_digest(synthesis))
            row={'uid':chunk['uid'],'audio_revision':revision,KEY:result}
            self.assertEqual(validate_take_publication(row), result)
            self.assertEqual(get_retained_take_bytes(row), len(json.dumps(result,sort_keys=True,
                separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')))
            for field, value in (('audio_revision','f'*32),('uid','other')):
                bad={**row,field:value}
                with self.subTest(field=field), self.assertRaises(ValueError):validate_take_publication(bad)

    def test_missing_mismatched_and_deep_evidence_cannot_certify_a_file(self):
        with self.fixture() as (root, manager, _, _), self.owner():
            engine = self.engine(); chunk = manager.load_chunks()[0]
            parent, admitted = self.admission(manager, engine, chunk)
            pcm, preparation, synthesis, observed = self.render(root, engine, parent, chunk)
            revision='e'*32; synthesis={**synthesis,'audio_revision':revision,'scope':'pre-encoding PCM'}
            values=[None, {}, {**synthesis,'recovery':{**synthesis['recovery'],'input_proof_version':True}},
                    {**synthesis,'recovery':{**synthesis['recovery'],'serving_sha256':'f'*64}},
                    {**synthesis,'audio_revision':'f'*32}]
            for packet in values:
                with self.subTest(packet=packet), patch('take_publication._read_audio', side_effect=AssertionError('unproven file read')):
                    self.assertEqual(get_take_publication_receipt(admitted,get_chunk_generation_inputs(chunk),
                        preparation,observed,packet,pcm,pcm,revision),(None,None))
            pcm.write_bytes(b'changed original PCM')
            self.assertEqual(get_take_publication_receipt(admitted,get_chunk_generation_inputs(chunk),
                preparation,observed,synthesis,pcm,pcm,revision),(None,None))
            deep={}; cursor=deep
            for _ in range(500):cursor['next']={};cursor=cursor['next']
            with self.assertRaises(ValueError):validate_take_publication({KEY:deep})

    def test_selected_metadata_and_history_keep_certificate_in_both_stores(self):
        for journal in (False,True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, _), self.owner():
                engine=self.engine();chunk=manager.load_chunks()[0]
                parent,admitted=self.admission(manager,engine,chunk)
                pcm,_,synthesis,observed=self.render(root,engine,parent,chunk)
                chunk={**chunk,'_synthesis_receipt':synthesis,'_speech_input':observed}
                path=manager._publish_generated_chunk_audio(chunk,str(pcm),'NARRATOR',
                    recovery_parent=parent,publication_admission=admitted)
                row=manager.load_chunks()[0]; certificate=validate_take_publication(row)
                self.assertEqual(certificate['audio']['sha256'],hashlib.sha256((root/path).read_bytes()).hexdigest())
                self.assertEqual(certificate['book_token'],get_book_operation_token(root))
                # Regeneration without proven input clears the old certificate;
                # its immutable predecessor retains the complete prior proof.
                manager._publish_generated_chunk_audio(row,str(pcm),'NARRATOR')
                current=manager.load_chunks()[0];self.assertIsNone(current[KEY])
                with AudioTakeHistory(root)._locked() as document:
                    archived=next(r for r in document['records'] if r['audio_revision']==row['audio_revision'])
                    self.assertEqual(archived['captured'][KEY],certificate)

    def test_failed_final_metadata_preserves_prior_and_never_commits_new_certificate(self):
        for journal in (False,True):
            with self.subTest(journal=journal), self.fixture(journal) as (root, manager, previous, _), self.owner():
                before=manager.load_chunks()[0];original=previous.read_bytes();engine=self.engine()
                parent,admitted=self.admission(manager,engine,before)
                pcm,_,synthesis,observed=self.render(root,engine,parent,before)
                chunk={**before,'_synthesis_receipt':synthesis,'_speech_input':observed}
                target,method=(manager._get_status_journal_locked(),'_append') if journal else (manager,'_save_chunks_locked')
                original_write=getattr(target,method)
                def fail(*args,**kwargs):
                    data=args[0].get('changes',{}) if journal else args[0][0]
                    if data.get(KEY) is not None:raise OSError('certificate metadata refused')
                    return original_write(*args,**kwargs)
                with patch.object(target,method,side_effect=fail), self.assertRaisesRegex(OSError,'certificate metadata refused'):
                    manager._publish_generated_chunk_audio(chunk,str(pcm),'NARRATOR',
                        recovery_parent=parent,publication_admission=admitted)
                saved=manager.load_chunks()[0]
                self.assertEqual(saved['audio_path'],before['audio_path'])
                self.assertEqual(saved['audio_revision'],before['audio_revision'])
                self.assertIsNone(saved.get(KEY));self.assertEqual(previous.read_bytes(),original)

    def test_captured_book_fences_publication_after_history_is_disabled(self):
        import take_publication
        for journal in (False, True):
            for proven in (False, True):
                with self.subTest(journal=journal, proven=proven), self.fixture(journal) as (root,manager,previous,_), self.owner():
                    engine=self.engine();chunk=manager.load_chunks()[0]
                    parent,admitted=self.admission(manager,engine,chunk)
                    self.assertNotIn('_take_history_book_token',chunk)
                    pcm,_,synthesis,observed=self.render(root,engine,parent,chunk)
                    request={**chunk,'_synthesis_receipt':synthesis,'_speech_input':observed if proven else None}
                    with ensure_book_state(root):
                        set_take_history_policy(manager,False,get_take_history_policy(root)['revision'])
                    before=copy.deepcopy(manager.load_chunks()[0]);original=previous.read_bytes()
                    paths=[Path(manager.chunks_path)]
                    if journal:paths.append(manager._get_status_journal_locked().path)
                    stored={path:path.read_bytes() for path in paths}
                    receipt=take_publication.get_take_publication_receipt
                    def change_book(*args,**kwargs):
                        result=receipt(*args,**kwargs)
                        self.assertEqual(result[0] is not None,proven)
                        with ensure_book_state(root):
                            state=json.loads((root/'state.json').read_text())
                            state['book_generation']='another generation'
                            (root/'state.json').write_text(json.dumps(state))
                        return result
                    with patch.object(take_publication,'get_take_publication_receipt',side_effect=change_book), \
                            self.assertRaisesRegex(ValueError,'generation inputs changed'):
                        manager._publish_generated_chunk_audio(request,str(pcm),'NARRATOR',
                            recovery_parent=parent,publication_admission=admitted)
                    self.assertEqual(previous.read_bytes(),original)
                    # Journal identity is deliberately invalid in the new book.
                    self.assertEqual({path:path.read_bytes() for path in paths},stored)

    def test_stage_mutation_after_binding_refuses_publication_and_cancel_propagates(self):
        import take_publication
        for journal in (False,True):
            with self.subTest(journal=journal), self.fixture(journal) as (root,manager,previous,_), self.owner():
                engine=self.engine();chunk=manager.load_chunks()[0];original=previous.read_bytes()
                parent,admitted=self.admission(manager,engine,chunk)
                pcm,preparation,synthesis,observed=self.render(root,engine,parent,chunk)
                request={**chunk,'_synthesis_receipt':synthesis,'_speech_input':observed}
                read=take_publication._read_audio
                def mutate(root,path,cancel):
                    result=read(root,path,cancel)
                    if Path(path).parent==Path(manager.voicelines_dir):
                        sf.write(path,np.ones(2400)*.1,24000)
                    return result
                with patch.object(take_publication,'_read_audio',side_effect=mutate), \
                        self.assertRaisesRegex(ValueError,'changed before take evidence publication'):
                    manager._publish_generated_chunk_audio(request,str(pcm),'NARRATOR',
                        recovery_parent=parent,publication_admission=admitted)
                current=manager.load_chunks()[0]
                self.assertEqual(current['audio_path'],chunk['audio_path'])
                self.assertIsNone(current.get(KEY));self.assertEqual(previous.read_bytes(),original)
                def cancel():raise AudioGenerationCancelled()
                with self.assertRaises(AudioGenerationCancelled):
                    get_take_publication_receipt(admitted,get_chunk_generation_inputs(chunk),preparation,
                        observed,{**synthesis,'audio_revision':'e'*32,'scope':'pre-encoding PCM'},
                        pcm,pcm,'e'*32,cancel)

    def test_installed_slot_is_checked_before_metadata_commits(self):
        for journal in (False,True):
            with self.subTest(journal=journal), self.fixture(journal) as (root,manager,previous,_), self.owner():
                engine=self.engine();chunk=manager.load_chunks()[0];original=previous.read_bytes()
                parent,admitted=self.admission(manager,engine,chunk)
                pcm,_,synthesis,observed=self.render(root,engine,parent,chunk)
                request={**chunk,'_synthesis_receipt':synthesis,'_speech_input':observed}
                update=manager._update_chunk_fields_by_uid
                def mutate(*args,**kwargs):
                    if kwargs.get('audio_source') is not None:
                        sf.write(kwargs['audio_source'],np.ones(2400)*.1,24000)
                    return update(*args,**kwargs)
                with patch.object(manager,'_update_chunk_fields_by_uid',side_effect=mutate), \
                        self.assertRaisesRegex(ValueError,'changed during take evidence publication'):
                    manager._publish_generated_chunk_audio(request,str(pcm),'NARRATOR',
                        recovery_parent=parent,publication_admission=admitted)
                current=manager.load_chunks()[0]
                self.assertEqual(current['audio_path'],chunk['audio_path'])
                self.assertEqual(current['audio_revision'],chunk['audio_revision'])
                self.assertIsNone(current.get(KEY));self.assertEqual(previous.read_bytes(),original)

    def test_valid_output_outside_snapshot_limits_still_publishes_without_proof(self):
        with self.fixture(prior=False) as (root,manager,_,_), self.owner():
            engine=self.engine();chunk=manager.load_chunks()[0]
            parent,admitted=self.admission(manager,engine,chunk)
            pcm,_,synthesis,observed=self.render(root,engine,parent,chunk,seconds=660)
            self.assertEqual(sf.info(pcm).frames,24000*660)
            self.assertIsNotNone(observed)
            request={**chunk,'_synthesis_receipt':synthesis,'_speech_input':observed}
            path=manager._publish_generated_chunk_audio(request,str(pcm),'NARRATOR',
                recovery_parent=parent,publication_admission=admitted)
            row=manager.load_chunks()[0]
            self.assertEqual(row['status'],'done');self.assertIsNone(row.get(KEY))
            self.assertEqual((root/path).read_bytes(),pcm.read_bytes())

    def test_owned_builtin_single_parallel_and_batch_transport_without_asr(self):
        # Only serving identity is declared here. Original engine dispatch,
        # windowing, observed inputs, encoding, book admission and store writes
        # all run; no model hardware/listening claim follows from this fixture.
        class Model:
            def generate_custom_voice(self,**kwargs):
                return [np.sin(np.arange(2400)*.1)*.2],24000
        identity=NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)
        for mode in ('single','parallel','batch'):
            with self.subTest(mode=mode), self.fixture(mode=='batch') as (root,manager,previous,_), self.owner():
                engine=self.engine();engine._local_custom_model=Model()
                Path(manager.voice_config_path).write_text(json.dumps(
                    {'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
                with patch.object(manager,'get_engine',return_value=engine), \
                        patch('synthesis_recovery.capture_native_custom_serving_identity',return_value=identity), \
                        patch('synthesis_recovery_serving.capture_native_custom_serving_identity',return_value=identity), \
                        patch('speech_verification.verify_finished_speech',side_effect=AssertionError('ASR must stay off')):
                    if mode=='single':
                        success,payload=manager.generate_chunk_audio(0)
                        self.assertTrue(success,payload)
                    elif mode=='parallel':
                        result=manager.generate_chunks_parallel([0],max_workers=1)
                        self.assertEqual(result['completed'],[0],result)
                    else:
                        result=manager.generate_chunks_batch([0],batch_seed=42,batch_size=1)
                        self.assertEqual(result['completed'],[0],result)
                row=manager.load_chunks()[0]
                self.assertIsNotNone(validate_take_publication(row))
                self.assertIsNone(row['speech_verification'])
                self.assertEqual(row[KEY]['audio']['sha256'],hashlib.sha256((root/row['audio_path']).read_bytes()).hexdigest())

    def test_publication_metadata_is_bounded_before_json_copy_and_journal_append(self):
        with self.fixture(True) as (root,manager,_,_),self.owner():
            engine=self.engine();chunk=manager.load_chunks()[0]
            parent,admitted=self.admission(manager,engine,chunk)
            pcm,_,synthesis,observed=self.render(root,engine,parent,chunk)
            manager._publish_generated_chunk_audio({**chunk,'_synthesis_receipt':synthesis,'_speech_input':observed},
                str(pcm),'NARRATOR',recovery_parent=parent,publication_admission=admitted)
            journal=manager._get_status_journal_locked();row=manager.load_chunks()[0]
            self.assertEqual(journal._retained_bytes,get_retained_take_bytes(row))
            with patch.object(journal,'get_rows',side_effect=AssertionError('per-UID full-book read')):
                manager._update_chunk_fields_by_uid(row['uid'],error='bounded fixture update')
            original=journal.path.read_bytes();bad=copy.deepcopy(row[KEY]);bad['audio']['channels']=True
            with self.assertRaises(ValueError):journal.apply_update(row['uid'],{KEY:bad})
            self.assertEqual(journal.path.read_bytes(),original)
        with self.fixture(False) as (root,manager,previous,_):
            row=manager.load_chunks()[0];deep={};cursor=deep
            for _ in range(500):cursor['next']={};cursor=cursor['next']
            row[KEY]=deep;Path(manager.chunks_path).write_text(json.dumps([row]));raw=Path(manager.chunks_path).read_bytes()
            audio=previous.read_bytes()
            with self.assertRaises(ValueError):manager.load_chunks()
            self.assertEqual(Path(manager.chunks_path).read_bytes(),raw)
            self.assertEqual(previous.read_bytes(),audio)


if __name__ == '__main__':
    unittest.main()
