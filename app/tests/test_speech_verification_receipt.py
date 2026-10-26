"""Receipts preserve exact input and bounded identities without audio polling."""
import hashlib
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from speech_verification_compare import get_speech_comparison
from speech_verification_receipt import (get_validated_speech_input,get_pronunciation_ambiguity_regions,
    get_speech_verification_receipt,get_current_speech_verification_receipt,
    get_historical_speech_verification_receipt,get_speech_verification_chunk_view,
    get_speech_verification_chunk_digest, MAX_RECEIPT_BYTES)


def get_identity(role='primary'):
    return dict(role=role,model={'sha256':('a' if role=='primary' else 'f')*64,'size':1},binary={'sha256':'b'*64,'size':2},
        decoder={'sha256':'c'*64,'size':3},libraries=[{'path':'/fixture/libwhisper.so.1','sha256':'d'*64,'size':4}],
        loaded_adapter_modules={'word_timing':'e'*64},architecture={'layers':12},
        options=['--no-gpu','--threads','2'],language='en',device='cpu')


def get_report(path,text,transcript=None,status='no_content_candidate'):
    data=Path(path).read_bytes()
    comparison=get_speech_comparison(text,text if transcript is None else transcript)
    result=dict(version=1,status=status,stage='complete',expected_text=text,
        audio={'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)},seconds=.1,duration=1.,
        primary={'identity':get_identity(),'transcript':text if transcript is None else transcript,
                 'comparison':comparison,'seconds':.1})
    if status=='corroborated_content_difference':
        result['confirmation']={**result['primary'],'identity':get_identity('confirmation')}
        result['corroborated_regions']=[{'kind':'delete'}]
    return result


class SpeechVerificationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'audio';self.path.write_bytes(b'finished artifact')
        self.text='Exact API speech.'
        self.input=dict(version=1,text=self.text,language='English',provenance='known_leaf',
                        audio={'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest(),
                               'size':len(self.path.read_bytes())})
        self.chunk=dict(uid='row',text='Authored source.',speaker='A',instruct='',audio_revision='take')

    def test_input_validation_requires_exact_completed_artifact_and_never_guesses_text(self):
        self.assertEqual(self.input,get_validated_speech_input(self.input,self.path))
        self.assertIsNone(get_validated_speech_input(None,self.path))
        self.assertIsNone(get_validated_speech_input({**self.input,'version':True},self.path))
        self.path.write_bytes(b'replaced artifact')
        self.assertIsNone(get_validated_speech_input(self.input,self.path))

    def test_pronunciation_ambiguity_is_scoped_to_complete_actual_replacement_sequences(self):
        preparation={'transformations':[{'type':'pronunciation_lexicon',
            'substitutions':[{'name':'Louis','spoken':'LOO ee'}]}]}
        text='LOO ee thanked the guide and ee closed the quiet gate.'
        self.assertEqual([(0,2)],get_pronunciation_ambiguity_regions(preparation,text))
        result=get_speech_comparison(text,'LOO ee thanked the guide and.',
            ambiguous_expected_regions=[(0,2)])
        self.assertNotIn('pronunciation_projection',result['differences'][0]['ambiguity'])

    def test_receipt_separates_actual_input_from_source_and_contains_full_asset_hashes(self):
        report=get_report(self.path,self.text)
        receipt=get_speech_verification_receipt(report,self.chunk,'f'*64,'take',self.input)
        self.assertEqual(self.text,receipt['expected_text'])
        self.assertEqual(self.chunk['uid'],receipt['uid'])
        self.assertEqual(self.input['audio'],receipt['audio'])
        self.assertEqual('a'*64,receipt['primary']['identity']['assets']['model']['sha256'])
        self.assertEqual('d'*64,receipt['primary']['identity']['libraries'][0]['sha256'])
        self.assertEqual({'word_timing':'e'*64},receipt['primary']['identity']['loaded_adapter_modules'])
        self.assertEqual(get_speech_verification_chunk_digest(self.chunk),receipt['input_digest'])

    def test_polling_refuses_stale_source_rules_uid_book_and_take_without_audio_io(self):
        receipt=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        row={**self.chunk,'speech_verification':receipt}
        with patch('speech_verification_receipt.get_verifier_file_identity',side_effect=AssertionError('poll hashed audio')):
            self.assertIsNotNone(get_current_speech_verification_receipt(row,'f'*64))
            for changes in [{'text':'Edited.'},{'uid':'other'},{'audio_revision':'new'},
                            {'speaker':'B'},{'instruct':'Changed'}]:
                with self.subTest(changes=changes):
                    self.assertIsNone(get_current_speech_verification_receipt({**row,**changes},'f'*64))
            self.assertIsNone(get_current_speech_verification_receipt(row,'0'*64))

    def test_rejected_attempt_is_not_misrepresented_as_the_selected_take_check(self):
        report=get_report(self.path,self.text,'Exact.',status='corroborated_content_difference')
        receipt=get_speech_verification_receipt(report,self.chunk,'f'*64,'rejected',self.input)
        row={**self.chunk,'speech_verification_failure':receipt}
        self.assertIsNotNone(get_current_speech_verification_receipt(row,'f'*64,failure=True))
        self.assertIsNone(get_current_speech_verification_receipt(row,'f'*64))
        self.assertIsNone(get_current_speech_verification_receipt({**row,'text':'Edited.'},'f'*64,failure=True))

    def test_diagnostic_abbreviation_preserves_complete_counts_and_is_explicit(self):
        text=' '.join('anchor'+str(i)+' apple' for i in range(12))
        heard=' '.join('anchor'+str(i)+' pear' for i in range(12))
        report=get_report(self.path,text,heard,status='review')
        receipt=get_speech_verification_receipt(report,self.chunk,'f'*64,'take',{**self.input,'text':text})
        self.assertEqual(12,receipt['primary']['difference_count'])
        self.assertEqual(8,len(receipt['primary']['differences']))
        self.assertTrue(receipt['primary']['diagnostics_abbreviated'])
        self.assertEqual(report['primary']['comparison']['unmatched_expected'],receipt['primary']['unmatched_expected'])

    def test_polling_refuses_malformed_or_incomplete_persisted_evidence(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        mutations=[{'version':True},{'status':'passed'},{'audio':{'sha256':None,'size':1}},
                   {'expected_sha256':'0'*64},{'seconds':float('inf')},{'primary':{}},
                   {'corroborated_region_count':True},{'audio_revision':4}]
        for changes in mutations:
            with self.subTest(changes=changes):
                self.assertIsNone(get_current_speech_verification_receipt(
                    {**self.chunk,'speech_verification':{**valid,**changes}},'f'*64))
        for key in valid:
            bad=copy.deepcopy(valid);bad.pop(key)
            if key in ('notice','short_acoustic'):
                continue
            with self.subTest(missing=key):
                self.assertIsNone(get_current_speech_verification_receipt(
                    {**self.chunk,'speech_verification':bad},'f'*64))

    def test_selected_take_quick_stat_invalidates_changed_or_missing_audio_without_hashing(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        info=self.path.stat();valid['audio_file_version']=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
        row={**self.chunk,'audio_path':str(self.path),'speech_verification':valid}
        with patch('speech_verification_receipt.get_verifier_file_identity',side_effect=AssertionError('poll hashed audio')):
            self.assertIsNotNone(get_current_speech_verification_receipt(row,'f'*64,resolve_audio=lambda path:path))
            self.path.write_bytes(b'new finished artifact')
            self.assertIsNone(get_current_speech_verification_receipt(row,'f'*64,resolve_audio=lambda path:path))
            self.path.unlink()
            self.assertIsNone(get_current_speech_verification_receipt(row,'f'*64,resolve_audio=lambda path:path))

    def test_same_inode_size_and_mtime_change_hides_current_evidence_as_historical(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        info=self.path.stat();valid['audio_file_version']=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
        row={**self.chunk,'audio_path':str(self.path),'speech_verification':valid}
        changed=bytearray(self.path.read_bytes());changed[0]^=1
        self.path.write_bytes(changed)
        os.utime(self.path,ns=(info.st_atime_ns,info.st_mtime_ns))
        after=self.path.stat()
        self.assertEqual((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns),
                         (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns))
        self.assertNotEqual(info.st_ctime_ns,after.st_ctime_ns)
        with patch('speech_verification_receipt.get_verifier_file_identity',side_effect=AssertionError('poll hashed audio')):
            self.assertIsNone(get_current_speech_verification_receipt(row,'f'*64,resolve_audio=lambda path:path))
            view=get_speech_verification_chunk_view(row,'f'*64,lambda path:path)
        self.assertIsNone(view['speech_verification'])
        self.assertEqual(valid,view['speech_verification_historical'])
        self.assertEqual('changed',view['speech_verification_historical_reason'])

    def test_legacy_version_is_historical_but_still_available_for_recheck(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        info=self.path.stat();valid['audio_file_version']=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]
        row={**self.chunk,'audio_path':str(self.path),'speech_verification':valid}
        with patch('speech_verification_receipt.get_verifier_file_identity',side_effect=AssertionError('poll hashed audio')):
            self.assertIsNone(get_current_speech_verification_receipt(row,'f'*64,resolve_audio=lambda path:path))
            self.assertEqual(valid,get_historical_speech_verification_receipt(
                row,'f'*64,resolve_audio=lambda path:path))
            view=get_speech_verification_chunk_view(row,'f'*64,lambda path:path)
            self.assertEqual('legacy',view['speech_verification_historical_reason'])

    def test_malformed_audio_file_version_is_not_exposed_as_historical(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        row={**self.chunk,'audio_path':str(self.path),'speech_verification':valid}
        for version in (None,'bad',[1,2,3],[-1,2,3,4,5],[True,2,3,4,5]):
            with self.subTest(version=version):
                bad={**valid}
                if version is None: bad.pop('audio_file_version',None)
                else: bad['audio_file_version']=version
                self.assertIsNone(get_historical_speech_verification_receipt(
                    {**row,'speech_verification':bad},'f'*64,resolve_audio=lambda path:path))

    def test_legacy_version_with_changed_four_field_stat_is_classified_changed(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        info=self.path.stat();valid['audio_file_version']=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]
        row={**self.chunk,'audio_path':str(self.path),'speech_verification':valid}
        self.path.write_bytes(b'replaced artifact with different size')
        view=get_speech_verification_chunk_view(row,'f'*64,lambda path:path)
        self.assertEqual('changed',view['speech_verification_historical_reason'])

    def test_historical_receipt_requires_saved_input_binding_and_size_bound(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        info=self.path.stat();valid['audio_file_version']=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]
        row={**self.chunk,'audio_path':str(self.path),'speech_verification':valid}
        for changes in ({'text':'Edited source.'},{'uid':'other'},{'audio_revision':'new'}):
            with self.subTest(changes=changes):
                self.assertIsNone(get_historical_speech_verification_receipt(
                    {**row,**changes},'f'*64,resolve_audio=lambda path:path))
        for field in ('book_token','input_digest'):
            with self.subTest(field=field):
                bad=copy.deepcopy(row);bad['speech_verification'][field]='0'*64
                self.assertIsNone(get_historical_speech_verification_receipt(
                    bad,'f'*64,resolve_audio=lambda path:path))
        oversized={**valid,'notice':'x'*MAX_RECEIPT_BYTES}
        row={**row,'speech_verification':oversized}
        self.assertIsNone(get_historical_speech_verification_receipt(
            row,'f'*64,resolve_audio=lambda path:path))

    def test_windows_creation_time_keeps_new_receipts_historical(self):
        valid=get_speech_verification_receipt(get_report(self.path,self.text),self.chunk,'f'*64,'take',self.input)
        info=self.path.stat();valid['audio_file_version']=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
        row={**self.chunk,'audio_path':str(self.path),'speech_verification':valid}
        with patch('speech_verification_receipt.sys.platform','win32'):
            self.assertIsNone(get_current_speech_verification_receipt(row,'f'*64,resolve_audio=lambda path:path))
            view=get_speech_verification_chunk_view(row,'f'*64,lambda path:path)
        self.assertIsNone(view['speech_verification'])
        self.assertEqual(valid,view['speech_verification_historical'])
        self.assertEqual('platform',view['speech_verification_historical_reason'])

    def test_malformed_difference_entries_are_rejected_before_public_projection(self):
        report=get_report(self.path,self.text,'Exact.',status='review')
        valid=get_speech_verification_receipt(report,self.chunk,'f'*64,'take',self.input)
        block=valid['primary']['differences'][0]
        malformed=[None,{},'raw string',{**block,'kind':'passed'},
            {**block,'expected_from':True},{**block,'expected_to':99999},
            {**block,'observed':None},{**block,'expected':'x'*161},
            {**block,'transcript_repetition':'false'},{**block,'ambiguity':[{}]}]
        for value in malformed:
            with self.subTest(value=value):
                bad=copy.deepcopy(valid);bad['primary']['differences'][0]=value
                self.assertIsNone(get_current_speech_verification_receipt(
                    {**self.chunk,'speech_verification':bad},'f'*64))


if __name__ == '__main__':
    unittest.main()
