"""Editor polling exposes only current bounded evidence and never invokes ASR."""
from contextlib import contextmanager
import copy
import json
import os
from unittest.mock import patch
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from tests import test_speech_verification_publication as publication
from tests.test_speech_verification_receipt import get_report


class SpeechVerificationEditorTests(unittest.TestCase):
    @contextmanager
    def fixture(self,*,reject=False):
        source=publication.SpeechVerificationPublicationTests()
        with source.fixture() as (root,manager,before,prior,flags):
            def report(path,text,*args,**kwargs):
                return get_report(path,text,'The calm guide closed.' if reject else None,
                    status='corroborated_content_difference' if reject else 'no_content_candidate')
            with patch('speech_verification.verify_finished_speech',side_effect=report):
                self.assertEqual(not reject,source.generate(manager,'single'))
            api=FastAPI();api.include_router(editor.router)
            with patch.object(editor,'project_manager',manager), \
                    patch.object(editor,'SCRIPT_PATH',str(root/'annotated_script.json')), \
                    patch('speech_verification.verify_finished_speech',side_effect=AssertionError('poll ran ASR')), \
                    patch('speech_verification_runtime.get_verifier_file_identity',side_effect=AssertionError('poll hashed audio')), \
                    TestClient(api) as client:
                yield root,manager,client

    def quality(self,client,token):
        response=client.get('/api/chunks/quality',params={'expected_book_token':token})
        self.assertEqual(200,response.status_code,response.text)
        return response.json()

    def test_status_listing_and_review_agree_without_audio_hash_or_provider_work(self):
        with self.fixture() as (root,manager,client):
            first=client.get('/api/chunks/status').json()
            row=first['chunks'][0];receipt=row['speech_verification']
            self.assertEqual(row['audio_revision'],receipt['audio_revision'])
            self.assertEqual(publication.TEXT,receipt['expected_text'])
            self.assertEqual([row],client.get('/api/chunks').json())
            quality=self.quality(client,first['book_token'])
            self.assertEqual(first['revision'],quality['revision'])
            self.assertEqual('no_content_candidate',quality['rows'][0]['speech'])
            self.assertEqual([],quality['rows'][0]['issues'])
            before={path.name:path.read_bytes() for path in root.iterdir() if path.is_file()}
            same=client.get('/api/chunks/status',params={'revision':first['revision']}).json()
            self.assertEqual([],same['chunks']);self.assertFalse(same['full'])
            self.assertEqual(before,{path.name:path.read_bytes() for path in root.iterdir() if path.is_file()})

    def test_replaced_or_missing_selected_audio_invalidates_poll_evidence_without_writing_receipt(self):
        for event in ('replace','same_stat','delete'):
            with self.subTest(event=event),self.fixture() as (root,manager,client):
                first=client.get('/api/chunks/status').json();raw=copy.deepcopy(manager.load_chunks()[0])
                path=root/raw['audio_path'];info=path.stat()
                if event=='delete':path.unlink()
                elif event=='same_stat':
                    data=bytearray(path.read_bytes());data[0]^=1
                    path.write_bytes(data);os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
                    self.assertNotEqual(info.st_ctime_ns,path.stat().st_ctime_ns)
                else:path.write_bytes(b'new unrelated artifact')
                changed=client.get('/api/chunks/status',params={'revision':first['revision']}).json()
                self.assertFalse(changed['full']);self.assertEqual([0],changed['changed_ids'])
                self.assertIsNone(changed['chunks'][0]['speech_verification'])
                if event=='same_stat':
                    self.assertIsNotNone(changed['chunks'][0]['speech_verification_historical'])
                    self.assertEqual('changed',changed['chunks'][0]['speech_verification_historical_reason'])
                if event=='delete':
                    self.assertEqual('unavailable',changed['chunks'][0]['speech_verification_historical_reason'])
                    self.assertIn('speech_historical',self.quality(client,first['book_token'])['rows'][0]['issues'])
                self.assertEqual(raw,manager.load_chunks()[0])
                self.assertIsNone(client.get('/api/chunks').json()[0]['speech_verification'])
                quality=self.quality(client,first['book_token'])['rows'][0]
                self.assertEqual('not_checked',quality['speech'])
                if event=='same_stat':
                    self.assertIn('speech_historical',quality['issues'])

    def test_edited_source_or_reselected_book_does_not_expose_old_evidence(self):
        for event in ('source','book'):
            with self.subTest(event=event),self.fixture() as (root,manager,client):
                first=client.get('/api/chunks/status').json()
                if event=='source':manager.update_chunk(0,{'text':'Changed source words.'})
                else:(root/'state.json').write_text(json.dumps({'active_book_id':'other-book'}))
                fresh=client.get('/api/chunks/status').json()
                self.assertIsNone(fresh['chunks'][0]['speech_verification'])
                self.assertNotIn('speech_verification_historical',fresh['chunks'][0])
                self.assertEqual('not_checked',self.quality(client,fresh['book_token'])['rows'][0]['speech'])
                self.assertIsNotNone(manager.load_chunks()[0]['speech_verification'])

    def test_rejected_attempt_is_separate_from_prior_selected_take_in_quality_review(self):
        with self.fixture(reject=True) as (root,manager,client):
            first=client.get('/api/chunks/status').json();row=first['chunks'][0]
            self.assertEqual('no_content_candidate',row['speech_verification']['status'])
            self.assertEqual('corroborated_content_difference',row['speech_verification_failure']['status'])
            self.assertNotEqual(row['audio_revision'],row['speech_verification_failure']['audio_revision'])
            quality=self.quality(client,first['book_token'])['rows'][0]
            self.assertEqual('no_content_candidate',quality['speech'])
            self.assertEqual('rejected',quality['speech_attempt'])
            self.assertIn('speech_rejected',quality['issues'])
            self.assertNotIn('speech_difference',quality['issues'])

    def test_malformed_persisted_evidence_is_hidden_without_mutating_saved_chunks(self):
        with self.fixture() as (root,manager,client):
            manager._update_chunk_fields_by_uid('line-0',speech_verification={'version':True,'status':'passed'})
            before=(root/'chunks.json').read_bytes()
            first=client.get('/api/chunks/status').json()
            self.assertIsNone(first['chunks'][0]['speech_verification'])
            self.assertEqual('not_checked',self.quality(client,first['book_token'])['rows'][0]['speech'])
            self.assertEqual(before,(root/'chunks.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
