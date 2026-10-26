"""A recheck uses captured API input, retains audio, and fences stale/cancelled saves."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio_generation_owner import AudioGenerationCancelled
from book_state_transaction import get_book_operation_token
from editor_speech_verification import recheck_editor_speech
from speech_verification_receipt import get_current_speech_verification_receipt
from tests import test_speech_verification_publication as publication
from tests.test_speech_verification_receipt import get_report


class EditorSpeechVerificationTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        source=publication.SpeechVerificationPublicationTests()
        with source.fixture() as (root,manager,before,prior,flags):
            with patch('speech_verification.verify_finished_speech',side_effect=lambda path,text,*a,**kw:get_report(path,text)):
                self.assertTrue(source.generate(manager,'single'))
            row=copy.deepcopy(manager.load_chunks()[0]);path=root/row['audio_path']
            request=SimpleNamespace(expected_book_token=get_book_operation_token(root),
                expected_uid=row['uid'],expected_audio_revision=row['audio_revision'])
            def check(index,book,uid):
                if get_book_operation_token(root) != book or manager.load_chunks()[index]['uid'] != uid:
                    raise ValueError('Book or line changed')
            def run():return recheck_editor_speech(manager,0,request,{}, {},check)
            yield root,manager,row,path.read_bytes(),flags,run

    def test_same_stat_byte_change_refuses_before_asr_and_retains_prior_audio(self):
        with self.fixture() as (root,manager,before,prior,flags,run):
            path=root/before['audio_path'];info=path.stat()
            changed=bytearray(path.read_bytes());changed[0]^=1;path.write_bytes(changed)
            os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
            with patch('editor_speech_verification.verify_finished_speech',
                    side_effect=AssertionError('ASR ran')), \
                    self.assertRaisesRegex(ValueError,'bytes changed'):
                run()
            self.assertEqual(bytes(changed),(root/before['audio_path']).read_bytes())
            self.assertEqual(before,manager.load_chunks()[0])

    def test_changed_ctime_metadata_recheck_verifies_unchanged_bytes_and_upgrades(self):
        with self.fixture() as (root,manager,before,prior,flags,run):
            row=manager.load_chunks()[0];old=copy.deepcopy(row['speech_verification'])
            old['audio_file_version'][4]+=1
            manager._update_chunk_fields_by_uid(row['uid'],speech_verification=old)
            self.assertIsNone(get_current_speech_verification_receipt(manager.load_chunks()[0],
                get_book_operation_token(root),resolve_audio=manager.get_chunk_audio_path))
            with patch('editor_speech_verification.verify_finished_speech',
                    side_effect=lambda path,text,*a,**kw:get_report(path,text)) as verifier:
                run()
            verifier.assert_called_once()
            self.assertEqual(prior,(root/before['audio_path']).read_bytes())
            self.assertIsNotNone(get_current_speech_verification_receipt(
                manager.load_chunks()[0],get_book_operation_token(root),
                resolve_audio=manager.get_chunk_audio_path))

    def test_legacy_saved_evidence_can_be_rechecked_and_is_upgraded(self):
        with self.fixture() as (root,manager,before,prior,flags,run):
            row=manager.load_chunks()[0];old=copy.deepcopy(row['speech_verification'])
            old['audio_file_version']=old['audio_file_version'][:4]
            manager._update_chunk_fields_by_uid(row['uid'],speech_verification=old)
            with patch('editor_speech_verification.verify_finished_speech',
                    side_effect=lambda path,text,*a,**kw:get_report(path,text)) as verifier:
                run()
            verifier.assert_called_once()
            updated=manager.load_chunks()[0]['speech_verification']
            self.assertEqual(5,len(updated['audio_file_version']))
            self.assertEqual(prior,(root/before['audio_path']).read_bytes())
            self.assertIsNotNone(get_current_speech_verification_receipt(
                manager.load_chunks()[0],get_book_operation_token(root),
                resolve_audio=manager.get_chunk_audio_path))

    def test_recheck_binds_exact_original_input_and_retains_audio_even_when_content_difference_found(self):
        with self.fixture() as (root,manager,before,prior,flags,run):
            def verify(path,text,language,*args,**kwargs):
                self.assertEqual(publication.TEXT,text);self.assertEqual('English',language)
                return get_report(path,text,'The calm guide closed.',status='corroborated_content_difference')
            with patch('editor_speech_verification.verify_finished_speech',side_effect=verify) as verifier:
                result=run()
            verifier.assert_called_once();row=manager.load_chunks()[0]
            self.assertEqual(before['audio_path'],row['audio_path']);self.assertEqual(before['audio_revision'],row['audio_revision'])
            self.assertEqual(prior,(root/row['audio_path']).read_bytes())
            self.assertEqual('corroborated_content_difference',row['speech_verification']['status'])
            self.assertIsNotNone(get_current_speech_verification_receipt(row,get_book_operation_token(root),resolve_audio=manager.get_chunk_audio_path))
            self.assertEqual(result['receipt'],row['speech_verification'])

    def test_unavailable_recheck_is_explicit_and_prior_audio_is_unchanged(self):
        with self.fixture() as (root,manager,before,prior,flags,run):
            with patch('editor_speech_verification.verify_finished_speech',return_value=
                    {'version':1,'status':'unavailable','stage':'primary_identity','reason':'cached model missing'}):
                run()
            row=manager.load_chunks()[0]
            self.assertEqual(before['speech_verification'],row['speech_verification'])
            self.assertEqual('unavailable',row['speech_verification_recheck']['status'])
            self.assertEqual(prior,(root/row['audio_path']).read_bytes())

    def test_unavailable_recheck_cannot_clear_a_known_content_difference(self):
        from editor_review_facts import get_editor_review_facts
        with self.fixture() as (root,manager,before,prior,flags,run):
            with patch('editor_speech_verification.verify_finished_speech',side_effect=lambda path,text,*a,**kw:
                    get_report(path,text,'The calm guide closed.',status='corroborated_content_difference')):
                run()
            confirmed=copy.deepcopy(manager.load_chunks()[0]['speech_verification'])
            with patch('editor_speech_verification.verify_finished_speech',return_value=
                    {'version':1,'status':'unavailable','stage':'primary_identity','reason':'cached model missing'}):
                run()
            row=manager.load_chunks()[0]
            self.assertEqual(confirmed,row['speech_verification'])
            self.assertEqual('unavailable',row['speech_verification_recheck']['status'])
            facts=get_editor_review_facts([row],manager.get_chunk_audio_path,get_book_operation_token(root))[0]
            self.assertIn('speech_difference',facts['issues']);self.assertIn('speech_unavailable',facts['issues'])
            self.assertEqual(prior,(root/row['audio_path']).read_bytes())

    def test_changed_source_take_book_and_bytes_never_accept_a_late_report(self):
        for event in ('text','revision','book','bytes','cancel'):
            with self.subTest(event=event),self.fixture() as (root,manager,before,prior,flags,run):
                changed=[]
                def verify(path,text,*args,**kwargs):
                    report=get_report(path,text)
                    if event=='text':manager.update_chunk(0,{'text':'Edited source words.'})
                    elif event=='revision':manager._update_chunk_fields_by_uid(before['uid'],audio_revision='new-take')
                    elif event=='book':(root/'state.json').write_text(json.dumps({'active_book_id':'other'}))
                    elif event=='bytes':Path(path).write_bytes(Path(path).read_bytes()+b'changed')
                    else:flags['cancelled']=True
                    changed.append(copy.deepcopy(manager.load_chunks()))
                    return report
                with patch('editor_speech_verification.verify_finished_speech',side_effect=verify), \
                        self.assertRaises((ValueError,AudioGenerationCancelled)):
                    run()
                self.assertEqual(changed[0],manager.load_chunks())

    def test_missing_proven_input_or_old_take_revision_refuses_asr(self):
        for event in ('proof','revision','audio'):
            with self.subTest(event=event),self.fixture() as (root,manager,before,prior,flags,run):
                if event=='proof':manager._update_chunk_fields_by_uid(before['uid'],speech_verification=None)
                elif event=='revision':manager._update_chunk_fields_by_uid(before['uid'],audio_revision='new-take')
                else:(root/before['audio_path']).unlink()
                with patch('editor_speech_verification.verify_finished_speech',side_effect=AssertionError('unproven ASR ran')), \
                        self.assertRaises(ValueError):
                    run()

    def test_failed_receipt_save_retains_original_selected_evidence_and_audio(self):
        with self.fixture() as (root,manager,before,prior,flags,run):
            with patch('editor_speech_verification.verify_finished_speech',side_effect=lambda path,text,*a,**kw:get_report(path,text)), \
                    patch.object(manager,'_update_chunk_fields_by_uid',side_effect=OSError('save failed')), \
                    self.assertRaisesRegex(OSError,'save failed'):
                run()
            self.assertEqual([before],manager.load_chunks());self.assertEqual(prior,(root/before['audio_path']).read_bytes())


if __name__ == '__main__':
    unittest.main()
