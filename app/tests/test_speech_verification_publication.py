"""Real staged publication/journals retain prior takes and fence stale QA results."""
from contextlib import contextmanager, ExitStack
import copy
import json
import os
from pathlib import Path
import unittest
import struct
import wave
import numpy as np
from unittest.mock import patch

from audio_generation_owner import (AudioGenerationOwner, AudioGenerationCancelled,
                                   ensure_audio_generation_owner)
from book_state_transaction import get_book_operation_token
from chunk_status_journal import ChunkStatusJournal
from speech_verification_receipt import (get_speech_verification_receipt,
    get_current_speech_verification_receipt)
from tests import test_audio_generation_cancellation as audio_fixture
from tests import test_synthesis_window_dispatch as window_fixture
from tests.test_speech_verification_receipt import get_report

TEXT='The calm guide closed the quiet garden gate before leaving.'


class SpeechVerificationPublicationTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        fixture=audio_fixture.AudioGenerationCancellationTests()
        synthesis=window_fixture.SynthesisWindowDispatchTests()
        with fixture.fixture(1) as (root,manager,original,state), ExitStack() as stack:
            config=json.loads((root/'config.json').read_text())
            config['tts']['speech_verification']={'enabled':True}
            (root/'config.json').write_text(json.dumps(config));manager.invalidate_config_cache()
            engine,voices,seen,model=stack.enter_context(synthesis.fixture(root,chars=1000))
            engine._synthesis_windows=None;engine._speech_verification={'enabled':True};manager.engine=engine
            manager._update_chunk_fields_by_uid('line-0',text=TEXT)
            row=manager.load_chunks()[0]
            old=get_speech_verification_receipt(get_report(root/row['audio_path'],TEXT),row,
                get_book_operation_token(root),row['audio_revision'],
                dict(text=TEXT,language='English',provenance='previous_fixture'))
            old['audio_file_version']=list(manager._get_speech_staged_version(root/row['audio_path'])[:5])
            manager._update_chunk_fields_by_uid('line-0',speech_verification=old)
            before=copy.deepcopy(manager.load_chunks()[0]);prior=(root/before['audio_path']).read_bytes()
            flags={'cancelled':False,'released':False}
            @contextmanager
            def admit(*,allow_cancelled=False):
                if flags['released'] or flags['cancelled'] and not allow_cancelled:
                    raise AudioGenerationCancelled()
                yield
            stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner('a'*32,admit)))
            yield root,manager,before,prior,flags

    def generate(self,manager,mode):
        if mode=='single':
            return manager.generate_chunk_audio(0)[0]
        call=manager.generate_chunks_parallel if mode=='parallel' else manager.generate_chunks_batch
        result=call([0],**({'max_workers':1} if mode=='parallel' else {'batch_size':1}))
        return result['completed']==[0]

    def assert_prior(self,root,manager,before,prior):
        row=manager.load_chunks()[0]
        self.assertEqual(before['audio_path'],row['audio_path'])
        self.assertEqual(before['audio_revision'],row['audio_revision'])
        self.assertEqual(prior,(root/row['audio_path']).read_bytes())
        self.assertEqual(before['speech_verification'],row['speech_verification'])
        self.assertEqual([],list((root/'voicelines').glob('.render-*')))
        return row

    def test_single_parallel_native_attach_only_finished_revision_bound_evidence(self):
        for mode in ('single','parallel','native'):
            with self.subTest(mode=mode),self.fixture() as (root,manager,before,prior,flags):
                def verify(path,text,*args,**kwargs):
                    self.assertTrue(Path(path).name.startswith('.render-'))
                    self.assertEqual(TEXT,text)
                    return get_report(path,text)
                with patch('speech_verification.verify_finished_speech',side_effect=verify) as checker:
                    self.assertTrue(self.generate(manager,mode))
                checker.assert_called_once()
                row=manager.load_chunks()[0];receipt=row['speech_verification']
                self.assertEqual('no_content_candidate',receipt['status'])
                self.assertEqual(row['audio_revision'],receipt['audio_revision'])
                self.assertEqual(TEXT,receipt['expected_text'])
                self.assertIsNotNone(get_current_speech_verification_receipt(row,get_book_operation_token(root)))
                self.assertIsNone(row['speech_verification_failure'])
                self.assertNotEqual(before['audio_revision'],row['audio_revision'])

    def test_confirmed_failure_keeps_prior_and_records_rejected_attempt_separately(self):
        for mode in ('single','parallel','native'):
            with self.subTest(mode=mode),self.fixture() as (root,manager,before,prior,flags):
                with patch('speech_verification.verify_finished_speech',side_effect=lambda path,text,*a,**kw:
                    get_report(path,text,'The calm guide closed.',status='corroborated_content_difference')):
                    self.assertFalse(self.generate(manager,mode))
                row=self.assert_prior(root,manager,before,prior)
                self.assertEqual('error',row['status'])
                failure=row['speech_verification_failure']
                self.assertEqual('corroborated_content_difference',failure['status'])
                self.assertNotEqual(row['audio_revision'],failure['audio_revision'])
                self.assertEqual(1,failure['corroborated_region_count'])
                self.assertIsNotNone(get_current_speech_verification_receipt(row,get_book_operation_token(root),failure=True))
                self.assertIn('previous take was kept',row['error'])

    def test_unavailable_is_saved_explicitly_and_off_regeneration_clears_old_receipts(self):
        with self.fixture() as (root,manager,before,prior,flags):
            with patch('speech_verification.verify_finished_speech',return_value=
                       {'version':1,'status':'unavailable','stage':'primary_identity','reason':'cached model missing'}):
                self.assertTrue(self.generate(manager,'single'))
            row=manager.load_chunks()[0]
            self.assertEqual('unavailable',row['speech_verification']['status'])
            self.assertEqual('cached model missing',row['speech_verification']['reason'])
            config=json.loads((root/'config.json').read_text());config['tts']['speech_verification']={'enabled':False}
            (root/'config.json').write_text(json.dumps(config));manager.invalidate_config_cache()
            manager.engine._speech_verification=None
            with patch('speech_verification.verify_finished_speech',side_effect=AssertionError('off QA ran')):
                self.assertTrue(self.generate(manager,'single'))
            row=manager.load_chunks()[0]
            self.assertIsNone(row['speech_verification']);self.assertIsNone(row['speech_verification_failure'])

    def test_source_edit_during_asr_refuses_new_take_and_does_not_overwrite_edit(self):
        for mode in ('single','parallel','native'):
            with self.subTest(mode=mode),self.fixture() as (root,manager,before,prior,flags):
                edited=[]
                def verify(path,text,*args,**kwargs):
                    manager.update_chunk(0,{'text':'New edited words.'})
                    edited.append(copy.deepcopy(manager.load_chunks()[0]))
                    return get_report(path,text)
                with patch('speech_verification.verify_finished_speech',side_effect=verify):
                    self.assertFalse(self.generate(manager,mode))
                row=manager.load_chunks()[0]
                self.assertEqual([row],edited)
                self.assertEqual(prior,(root/before['audio_path']).read_bytes())
                self.assertEqual([],list((root/'voicelines').glob('.render-*')))
                self.assertEqual('New edited words.',row['text'])
                self.assertEqual('pending',row['status'])
                self.assertIsNone(get_current_speech_verification_receipt(row,get_book_operation_token(root)))

    def test_changed_staged_bytes_or_cancelled_owner_never_attach_success(self):
        for event in ('bytes','cancel'):
            with self.subTest(event=event),self.fixture() as (root,manager,before,prior,flags):
                def verify(path,text,*args,**kwargs):
                    report=get_report(path,text)
                    if event=='bytes':
                        with open(path,'ab') as stream:stream.write(b'changed staged bytes')
                    else: flags['cancelled']=True
                    return report
                with patch('speech_verification.verify_finished_speech',side_effect=verify):
                    self.assertFalse(self.generate(manager,'single'))
                self.assert_prior(root,manager,before,prior)

    def test_late_same_size_mutation_with_restored_mtime_refuses_publication(self):
        from speech_verification_runtime import get_verifier_file_identity
        with self.fixture() as (root,manager,before,prior,flags):
            def hash_and_change(path,*args,**kwargs):
                identity=get_verifier_file_identity(path,*args,**kwargs)
                if Path(path).name.startswith('.render-'):
                    info=os.stat(path)
                    with open(path,'r+b') as stream:
                        stream.seek(-1,os.SEEK_END);value=stream.read(1)
                        stream.seek(-1,os.SEEK_END);stream.write(bytes([value[0]^1]))
                    os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
                return identity
            with patch('speech_verification.verify_finished_speech',side_effect=lambda path,text,*a,**kw:get_report(path,text)), \
                    patch('speech_verification_runtime.get_verifier_file_identity',side_effect=hash_and_change):
                self.assertFalse(self.generate(manager,'single'))
            self.assert_prior(root,manager,before,prior)

    def test_large_valid_audio_records_unavailable_without_exceeding_hash_bound(self):
        from audio_validation import validate_generated_audio
        from word_timing import MAX_AUDIO_BYTES
        with self.fixture() as (root,manager,before,prior,flags):
            path=root/'large.wav'
            with wave.open(str(path),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(48000)
                stream.writeframes(b'\x01\0'*16)
            with path.open('r+b') as stream:
                stream.seek(4);stream.write(struct.pack('<I',36+MAX_AUDIO_BYTES))
                stream.seek(40);stream.write(struct.pack('<I',MAX_AUDIO_BYTES))
                stream.truncate(44+MAX_AUDIO_BYTES)
            validate_generated_audio(path,expected_text=TEXT)
            chunk={**before,'_speech_verification_admission':manager._capture_speech_verification_admission(manager.engine)}
            with patch('speech_verification.verify_finished_speech',side_effect=AssertionError('oversized ASR ran')), \
                    patch('speech_verification_runtime.get_verifier_file_identity',side_effect=AssertionError('oversized hashing')):
                receipt,version=manager._verify_generated_chunk_speech(chunk,path,path,{'text':TEXT},'attempt')
            self.assertEqual('unavailable',receipt['status']);self.assertIn('64 MiB',receipt['reason'])
            self.assertEqual({'sha256':None,'size':path.stat().st_size},receipt['audio'])
            self.assertEqual(manager._get_speech_staged_version(path),version)
            with patch('speech_verification_runtime.get_verifier_file_identity',
                    side_effect=AssertionError('oversized publication hashed')):
                updated=manager._update_chunk_fields_by_uid(before['uid'],
                    audio_source=str(path),status='done',audio_revision=receipt['audio_revision'],
                    speech_verification=receipt)
            self.assertIsNotNone(updated)
            self.assertEqual(5,len(updated['speech_verification']['audio_file_version']))
            self.assertIsNotNone(get_current_speech_verification_receipt(updated,
                get_book_operation_token(root),resolve_audio=manager.get_chunk_audio_path))

    def test_publication_without_take_receipt_hashes_and_rejects_wrong_speech_evidence(self):
        with self.fixture() as (root,manager,before,prior,flags):
            source=root/'source.wav';source.write_bytes((root/before['audio_path']).read_bytes())
            receipt=copy.deepcopy(before['speech_verification'])
            updated=manager._update_chunk_fields_by_uid(before['uid'],audio_source=str(source),
                status='done',audio_revision=receipt['audio_revision'],speech_verification=receipt)
            self.assertIsNotNone(updated)
            self.assertIsNotNone(get_current_speech_verification_receipt(updated,
                get_book_operation_token(root),resolve_audio=manager.get_chunk_audio_path))
            bad_source=root/'bad.wav';bad_source.write_bytes((root/updated['audio_path']).read_bytes())
            bad=copy.deepcopy(receipt);bad['audio']={'sha256':'f'*64,'size':receipt['audio']['size']}
            with self.assertRaisesRegex(ValueError,'differs from the published audio'):
                manager._update_chunk_fields_by_uid(updated['uid'],audio_source=str(bad_source),
                    status='done',audio_revision=bad['audio_revision'],speech_verification=bad)

    def test_unrepresentable_comparison_records_unavailable_and_can_publish_audio(self):
        for text in ('word '*2049,'The name q\u0323 was spoken clearly.'):
            with self.subTest(text=text[:25]),self.fixture() as (root,manager,before,prior,flags):
                manager.update_chunk(0,{'text':text})
                model=manager.engine._init_local_custom()
                seconds=max(.1,len(text)/80)
                with patch.object(model,'generate_custom_voice',return_value=
                        ([np.full(int(24000*seconds),.2,dtype=np.float32)],24000)), \
                        patch('speech_verification.verify_finished_speech',side_effect=AssertionError('ineligible ASR ran')):
                    self.assertTrue(self.generate(manager,'single'))
                row=manager.load_chunks()[0]
                self.assertEqual('unavailable',row['speech_verification']['status'])
                self.assertIn('Speech comparison is unavailable',row['speech_verification']['reason'])

    def test_snapshot_or_journal_metadata_failure_cannot_replace_prior_take_or_proof(self):
        for mode in ('single','parallel','native'):
            with self.subTest(mode=mode),self.fixture() as (root,manager,before,prior,flags):
                publish=manager._update_chunk_fields_by_uid
                def fault(*args,**kwargs):
                    if kwargs.get('audio_source'):
                        with ExitStack() as stack:
                            stack.enter_context(patch.object(manager,'_save_chunks_locked',side_effect=OSError('metadata save fault')))
                            stack.enter_context(patch.object(ChunkStatusJournal,'apply_update',side_effect=OSError('journal save fault')))
                            return publish(*args,**kwargs)
                    return publish(*args,**kwargs)
                with patch('speech_verification.verify_finished_speech',side_effect=lambda path,text,*a,**kw:get_report(path,text)), \
                        patch.object(manager,'_update_chunk_fields_by_uid',side_effect=fault):
                    self.assertFalse(self.generate(manager,mode))
                self.assert_prior(root,manager,before,prior)

    def test_book_replacement_with_matching_uid_and_inputs_is_not_an_authorized_target(self):
        with self.fixture() as (root,manager,before,prior,flags):
            changed=[]
            def verify(path,text,*args,**kwargs):
                report=get_report(path,text)
                (root/'state.json').write_text(json.dumps({'active_book_id':'other-book'}))
                row=manager.load_chunks()[0];row.update(status='done',error='other book marker',audio_revision='other-take')
                manager.save_chunks([row]);changed.append(copy.deepcopy(manager.load_chunks()))
                return report
            with patch('speech_verification.verify_finished_speech',side_effect=verify):
                self.assertFalse(self.generate(manager,'single'))
            self.assertEqual(changed[0],manager.load_chunks())
            self.assertEqual(prior,(root/before['audio_path']).read_bytes())


if __name__ == '__main__':
    unittest.main()
