"""Actual CLI recovery must skip accepted calls and reject stale/corrupt rows."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.test_enricher_preflight_json import load_enricher

RESPONSE = {'choices': [{'text': '{"emotional_tone":"calm"}'}]}

class EnrichmentResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.output, self.model = [self.root / name for name in ('input.json','output.json','model.gguf')]
        self.model.write_bytes(b'fixture model A')
        self.rows = [{'text':'Line '+str(i),'start':i,'end':i+1} for i in range(5)]
        self.source.write_text(json.dumps(self.rows))
        self.output.write_text('[{"old":true}]')
        self.checkpoint = Path(str(self.output)+'.enrichment_checkpoint.json')
        self.module, self.provider = load_enricher()
        environment = patch.dict(os.environ, GPU_LOCK=str(self.root/'gpu.lock'), ALEXANDRIA_GPU_LOCK_HELD='0')
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self):
        argv = ['enricher','--model-path',str(self.model),'--input-file',str(self.source),
                '--output-file',str(self.output),'--emotional-tone','--resume']
        with patch.object(sys,'argv',argv):
            self.module.main()

    def interrupt(self, responses=None):
        worker = Mock(side_effect=responses or [RESPONSE]*3+[KeyboardInterrupt()])
        self.provider.Llama.return_value = worker
        with self.assertRaises(KeyboardInterrupt):
            self.invoke()
        worker.close.assert_called_once()
        self.assertEqual([{'old':True}],json.loads(self.output.read_text()))

    def test_interrupted_run_skips_three_accepted_rows_preserving_source_and_order(self):
        self.interrupt()
        self.provider.Llama.return_value = Mock(return_value=RESPONSE)
        self.invoke()
        self.assertEqual(2,self.provider.Llama.return_value.call_count)
        self.assertEqual([{**row,'emotional_tone':'calm'} for row in self.rows],json.loads(self.output.read_text()))
        self.assertFalse(self.checkpoint.exists())

    def test_changed_source_and_same_size_mtime_model_invalidate_rows(self):
        for changed in ('source','model'):
            with self.subTest(changed=changed):
                self.output.write_text('[{"old":true}]')
                self.interrupt()
                if changed == 'source':
                    self.rows[0]['text'] = 'Changed line'
                    self.source.write_text(json.dumps(self.rows))
                else:
                    stamp = self.model.stat()
                    self.model.write_bytes(b'fixture model B')
                    os.utime(self.model, ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
                self.provider.Llama.return_value = Mock(return_value=RESPONSE)
                self.invoke()
                self.assertEqual(5,self.provider.Llama.return_value.call_count)

    def test_failed_row_is_retried_after_interruption(self):
        self.interrupt([RESPONSE,{'choices':[{'text':'not JSON'}]},KeyboardInterrupt()])
        self.provider.Llama.return_value = Mock(return_value=RESPONSE)
        self.invoke()
        self.assertEqual(4,self.provider.Llama.return_value.call_count)

    def test_corrupt_source_row_fails_before_model_load_or_publication(self):
        self.interrupt()
        doc = json.loads(self.checkpoint.read_text())
        doc['rows'][0]['text'] = 'Corrupt words'
        self.checkpoint.write_text(json.dumps(doc))
        self.provider.Llama.reset_mock()
        with self.assertRaises(SystemExit) as stopped:
            self.invoke()
        self.assertEqual(1,stopped.exception.code)
        self.provider.Llama.assert_not_called()
        self.assertEqual([{'old':True}],json.loads(self.output.read_text()))

    def test_complete_checkpoint_recovers_publication_without_loading_model(self):
        self.provider.Llama.return_value = Mock(return_value=RESPONSE)
        with patch.object(self.module,'save_enriched_transcript',side_effect=OSError('disk full')):
            with self.assertRaises(SystemExit):
                self.invoke()
        self.assertTrue(self.checkpoint.exists())
        self.provider.Llama.reset_mock()
        self.invoke()
        self.provider.Llama.assert_not_called()
        self.assertEqual(5,len(json.loads(self.output.read_text())))

    def test_changed_fields_or_runtime_identity_repeats_all_rows(self):
        for changed in ('fields','llama_cpp_version','implementation'):
            with self.subTest(changed=changed):
                self.output.write_text('[{"old":true}]')
                self.interrupt()
                doc = json.loads(self.checkpoint.read_text())
                doc['identity'][changed] = 'different'
                self.checkpoint.write_text(json.dumps(doc))
                self.provider.Llama.return_value = Mock(return_value=RESPONSE)
                self.invoke()
                self.assertEqual(5,self.provider.Llama.return_value.call_count)

    def test_checkpoint_write_failure_stops_calls_and_closes_model(self):
        self.provider.Llama.return_value = Mock(return_value=RESPONSE)
        with patch.object(self.module,'atomic_json_write',side_effect=OSError('checkpoint full')):
            with self.assertRaisesRegex(OSError,'checkpoint full'):
                self.invoke()
        self.assertEqual(1,self.provider.Llama.return_value.call_count)
        self.provider.Llama.return_value.close.assert_called_once()
        self.assertEqual([{'old':True}],json.loads(self.output.read_text()))

    def test_model_changed_during_load_is_closed_without_calls(self):
        worker = Mock(return_value=RESPONSE)
        def load(**kwargs):
            self.model.write_bytes(b'fixture model B')
            return worker
        self.provider.Llama.side_effect = load
        with self.assertRaises(SystemExit):
            self.invoke()
        worker.assert_not_called()
        worker.close.assert_called_once()
