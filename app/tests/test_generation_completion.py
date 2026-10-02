"""Completion evidence rejects interrupted, stale and swapped book artifacts."""
import json
from pathlib import Path
import tempfile
import unittest
from generation_completion import get_file_sha256,get_generation_completion_error


class GenerationCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.source=root/'source.txt';self.output=root/'book.json'
        self.source.write_text('complete source')
        self.output.write_text(json.dumps([{'text':'short complete book'}]))
        self.manifest={'status':'complete','total_chunks':2,'accepted_chunk_count':2,
            'fingerprint':{'chunk_sha256':['a'*64,'b'*64]},
            'chunks':[{'chunk_number':1,'source_sha256':'a'*64},{'chunk_number':2,'source_sha256':'b'*64}],
            'completion_artifact':{'version':1,'input_sha256':get_file_sha256(self.source),
                'output_sha256':get_file_sha256(self.output)}}
        self.path=Path(str(self.output)+'.generation_quality.json')

    def save(self):self.path.write_text(json.dumps(self.manifest))

    def test_short_complete_book_does_not_need_arbitrary_entry_minimum(self):
        self.save();self.assertIsNone(get_generation_completion_error(self.output,self.source))

    def test_interrupted_51_entry_book_cannot_pass_complete_check(self):
        self.output.write_text(json.dumps([{'text':str(i)} for i in range(51)]))
        self.manifest.update(total_chunks=110,accepted_chunk_count=51)
        self.manifest['completion_artifact']['output_sha256']=get_file_sha256(self.output)
        self.save();self.assertIsNotNone(get_generation_completion_error(self.output,self.source))

    def test_changed_source_or_output_invalidates_old_manifest(self):
        self.save();self.source.write_text('another source')
        self.assertEqual('Generation source file changed',get_generation_completion_error(self.output,self.source))
        self.source.write_text('complete source');self.output.write_text('[{"text":"partial replacement"}]')
        self.assertEqual('Generated output changed after completion',get_generation_completion_error(self.output,self.source))

    def test_malformed_status_counts_order_and_missing_evidence_reject(self):
        baseline=json.loads(json.dumps(self.manifest))
        cases=[{'status':'verified'},{'total_chunks':True},{'accepted_chunk_count':True},
               {'chunks':[]},{'chunks':[baseline['chunks'][1],baseline['chunks'][0]]},
               {'completion_artifact':None},{'completion_artifact':{'version':True}}]
        for change in cases:
            with self.subTest(change=change):
                self.manifest={**baseline,**change};self.save()
                self.assertIsNotNone(get_generation_completion_error(self.output,self.source))
        self.path.write_text('{bad')
        self.assertIsNotNone(get_generation_completion_error(self.output,self.source))

    def test_native_publication_binds_completed_artifact_without_mutating_manifest(self):
        from generate_script import publish_completed_generation
        self.manifest['status']='verified'
        before=json.loads(json.dumps(self.manifest))
        publish_completed_generation(str(self.output),[{'text':'new complete book'}],self.manifest,
                                     self.source,get_file_sha256(self.source))
        self.assertEqual(before,self.manifest)
        self.assertIsNone(get_generation_completion_error(self.output,self.source))

    def test_changed_input_or_unverified_generation_preserves_previous_output(self):
        from generate_script import publish_completed_generation
        before=self.output.read_bytes();digest=get_file_sha256(self.source)
        self.source.write_text('changed during processing')
        with self.assertRaisesRegex(ValueError,'changed'):
            publish_completed_generation(str(self.output),[{'text':'replacement'}],
                {**self.manifest,'status':'verified'},self.source,digest)
        self.assertEqual(before,self.output.read_bytes())
        with self.assertRaisesRegex(ValueError,'unverified'):
            publish_completed_generation(str(self.output),[{'text':'replacement'}],
                {**self.manifest,'status':'failed'},self.source,get_file_sha256(self.source))
        self.assertEqual(before,self.output.read_bytes())

    def test_source_text_and_raw_hash_come_from_one_read_with_existing_newline_policy(self):
        import hashlib
        from generation_completion import get_generation_input
        data='éclair\r\nsecond\rthird\n'.encode('utf-8')
        self.source.write_bytes(data)
        text,digest=get_generation_input(self.source)
        self.assertEqual('éclair\nsecond\nthird\n',text)
        self.assertEqual(hashlib.sha256(data).hexdigest(),digest)
