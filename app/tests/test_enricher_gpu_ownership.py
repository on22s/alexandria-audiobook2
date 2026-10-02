"""Real kernel leases around local LLM lifetime; model providers are CPU fixtures."""
import fcntl
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

from tests.test_enricher_preflight_json import load_enricher


class EnricherGpuOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / 'gpu.lock'
        self.environment = patch.dict('os.environ', GPU_LOCK=str(self.lock),
                                      ALEXANDRIA_GPU_LOCK_HELD='0')
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.module, self.provider = load_enricher()

    def assert_held(self):
        with self.lock.open('a') as handle:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def assert_free(self):
        with self.lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_foreign_lease_refuses_before_loading_model(self):
        with self.lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'held by another job'):
                self.module.LLMEnricher('fixture.gguf')
            self.provider.Llama.assert_not_called()
            self.assert_held()

    def test_lease_covers_loading_inference_and_model_close(self):
        model = Mock()
        def load(**kwargs):
            self.assert_held()
            return model
        def infer(*args, **kwargs):
            self.assert_held()
            return {'choices': [{'text': '{"emotional_tone":"calm"}'}]}
        self.provider.Llama.side_effect = load
        model.side_effect = infer
        model.close.side_effect = self.assert_held
        enricher = self.module.LLMEnricher('fixture.gguf', ['emotional_tone'])
        try:
            self.assertEqual('calm', enricher.enrich_transcript_chunk({'text':'hello'})['emotional_tone'])
            self.assert_held()
        finally:
            enricher.close()
        model.close.assert_called_once()
        self.assert_free()
        enricher.close()
        model.close.assert_called_once()

    def test_load_failure_releases_only_its_own_lease(self):
        self.provider.Llama.side_effect = RuntimeError('fixture model failure')
        with self.assertRaisesRegex(RuntimeError, 'fixture model failure'):
            self.module.LLMEnricher('fixture.gguf')
        self.assert_free()

    def test_failed_model_close_keeps_lease_until_close_succeeds(self):
        model = self.provider.Llama.return_value
        enricher = self.module.LLMEnricher('fixture.gguf')
        model.close.side_effect = RuntimeError('fixture close failure')
        try:
            with self.assertRaisesRegex(RuntimeError, 'fixture close failure'):
                enricher.close()
            self.assert_held()
        finally:
            model.close.side_effect = None
            enricher.close()
        self.assert_free()

    def test_cli_cleanup_on_success_inference_failure_and_publication_failure(self):
        source = Path(self.tmp.name) / 'input.json'
        output = Path(self.tmp.name) / 'output.json'
        chunk = {'text':'hello','start':0,'end':1}
        source.write_text(json.dumps([chunk]))
        save = self.module.save_enriched_transcript
        for mode in ('success', 'inference', 'publication'):
            with self.subTest(mode=mode):
                output.write_text('[{"prior":true}]')
                model = Mock(return_value={'choices':[{'text':'{"emotional_tone":"calm"}'}]})
                self.provider.Llama.return_value = model
                if mode == 'inference':
                    model.side_effect = RuntimeError('fixture inference failure')
                model.close.side_effect = self.assert_held
                def publish(data, path):
                    self.assert_held()
                    if mode == 'publication':
                        raise OSError('fixture publication failure')
                    save(data, path)
                with patch.object(sys, 'argv', ['llm_enricher.py', '--model-path', 'fixture.gguf',
                     '--input-file', str(source), '--output-file', str(output), '--emotional-tone']), \
                     patch.object(self.module, 'save_enriched_transcript', side_effect=publish):
                    if mode == 'success':
                        self.module.main()
                        self.assertEqual([{**chunk,'emotional_tone':'calm'}],json.loads(output.read_text()))
                    else:
                        with self.assertRaises(SystemExit) as raised:
                            self.module.main()
                        self.assertEqual(1, raised.exception.code)
                        self.assertEqual([{'prior':True}],json.loads(output.read_text()))
                model.close.assert_called_once()
                self.assert_free()
