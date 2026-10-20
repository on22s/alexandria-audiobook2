"""A diagnostic fallback is pending work, not a completed Pass 3 result."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from generate_script import LLMGenParams
import three_pass_generate as tp


class InstructionDiagnosticResumeTests(unittest.TestCase):
    def test_legacy_record_only_and_marker_only_checkpoints_retry_the_failed_row(self):
        from tests.test_three_pass_checkpoint_recovery import delivery_client
        source = 'First narration. Second narration.'
        segmented = [{'type':'NARRATOR','text':text}
                     for text in ('First narration.','Second narration.')]
        named = [{**entry,'speaker':'NARRATOR'} for entry in segmented]
        params = LLMGenParams(max_tokens=500,segmentation='llm')
        fingerprint = tp.three_pass_fingerprint(source,'fixture',3000,params,collect_all_failures=True)
        for marker in (False,True):
            with self.subTest(marker_only=marker), tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory,'book.json'))
                annotated = [{**entry,'instruct':'Accepted instruction.'} for entry in named]
                failures = [tp.build_failure_record('instruct',1,named[1]['text'])]
                if marker:
                    annotated[1]['instruct_unchecked'] = True
                    failures = []
                tp._save_three_pass_checkpoint(output,fingerprint,'done',segmented,1,named,annotated,
                                                diagnostic_failures=failures)
                with patch.object(tp,'segment_chunk_adaptively') as segment, \
                     patch.object(tp,'attribute_batch_voted') as attribute, \
                     patch.object(tp,'instruct_batch',wraps=tp.instruct_batch) as instruct:
                    result = tp.run_three_pass(delivery_client(),'fixture',source,params,3000,
                                               output_path=output,collect_all_failures=True)
                    segment.assert_not_called();attribute.assert_not_called()
                    self.assertEqual(1,instruct.call_count)
                    self.assertEqual([named[1]],instruct.call_args.args[2])
                self.assertEqual(annotated[0],result[0])
                self.assertNotIn('instruct_unchecked',result[1])
                manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
                self.assertEqual('complete',manifest['status'])
                self.assertEqual([],manifest['diagnostic_failures'])

    def test_resume_retries_only_failed_instruction_and_clears_diagnostics(self):
        text = 'A patient narrator waited.'
        source = text + ' ' + text
        segmented = [{'type':'NARRATOR','text':text} for _ in range(2)]
        params = LLMGenParams(max_tokens=500,segmentation='llm')
        calls = []
        def client(healthy):
            def create(**kwargs):
                calls.append(kwargs)
                reply = [{'n':0,'head':'A patient narrator','instruct':'Read calmly.'}]
                if healthy is None or (not healthy and len(calls) > 1):reply = []
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content=json.dumps(reply)),finish_reason='stop')],usage=None)
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory,'book.json'))
            with patch.object(tp,'segment_chunk_adaptively',return_value=segmented):
                first = tp.run_three_pass(client(False),'fixture',source,params,3000,
                    output_path=output,collect_all_failures=True)
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
            self.assertEqual('incomplete',manifest['status'])
            self.assertEqual([1],[failure['entry'] for failure in manifest['diagnostic_failures']
                                   if failure['pass'] == 'instruct'])
            self.assertNotIn('instruct_unchecked',first[0])
            self.assertIs(True,first[1]['instruct_unchecked'])
            calls.clear()
            with patch.object(tp,'segment_chunk_adaptively') as segment, \
                 patch.object(tp,'attribute_batch_voted') as attribute:
                still_failed = tp.run_three_pass(client(None),'fixture',source,params,3000,
                    output_path=output,collect_all_failures=True)
                segment.assert_not_called();attribute.assert_not_called()
            self.assertEqual(first[0],still_failed[0])
            self.assertIs(True,still_failed[1]['instruct_unchecked'])
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
            self.assertEqual('incomplete',manifest['status'])
            self.assertEqual([1],[failure['entry'] for failure in manifest['diagnostic_failures']
                                   if failure['pass'] == 'instruct'])
            calls.clear()
            with patch.object(tp,'segment_chunk_adaptively') as segment, \
                 patch.object(tp,'attribute_batch_voted') as attribute:
                resumed = tp.run_three_pass(client(True),'fixture',source,params,3000,
                    output_path=output,collect_all_failures=True)
                segment.assert_not_called();attribute.assert_not_called()
            self.assertEqual(1,len(calls),'Resume must retry the one fallback instruction')
            self.assertEqual(first[0],resumed[0])
            self.assertTrue(all('instruct_unchecked' not in row for row in resumed))
            self.assertEqual('Read calmly.',resumed[1]['instruct'])
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
            self.assertEqual('complete',manifest['status'])
            self.assertEqual([],manifest['diagnostic_failures'])
            self.assertEqual('complete',manifest['passes']['instruct']['status'])


if __name__ == '__main__':unittest.main()
