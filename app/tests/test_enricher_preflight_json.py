"""Actual enricher dispatch with CPU-only providers and persisted JSON checks."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent.parent


def load_enricher():
    provider = SimpleNamespace(Llama=Mock(), llama_supports_gpu_offload=lambda: True)
    with patch.dict(sys.modules, {'llama_cpp': provider}):
        spec = importlib.util.spec_from_file_location('enricher_regression_fixture', ROOT/'llm_enricher.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, provider


class EnricherPreflightJsonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environment = patch.dict('os.environ', GPU_LOCK=str(Path(tmp.name)/'gpu.lock'),
                                 ALEXANDRIA_GPU_LOCK_HELD='0')
        environment.start()
        self.addCleanup(environment.stop)

    def test_nested_objects_arrays_and_quoted_braces_survive_actual_enrichment(self):
        module, provider = load_enricher()
        metadata = {'emotional_tone': {'label': 'calm', 'evidence': ['}', {'word': 'hello'}]}}
        for response in (json.dumps(metadata), 'Analysis: '+json.dumps(metadata),
                         '```json\n'+json.dumps(metadata)+'\n```'):
            with self.subTest(response=response):
                provider.Llama.return_value = Mock(return_value={'choices': [{'text': response}]})
                enricher = module.LLMEnricher('fixture.gguf', ['emotional_tone'])
                self.addCleanup(enricher.close)
                chunk = {'text': 'hello', 'start': 0, 'end': 1, 'custom': {'preserve': True}}
                original = json.loads(json.dumps(chunk))
                self.assertEqual({**original, **metadata}, enricher.enrich_transcript_chunk(chunk))
                self.assertEqual(original, chunk)
                enricher.close()

    def test_invalid_input_is_rejected_before_model_loading_and_preserves_output(self):
        module, provider = load_enricher()
        cases = (None, b'{bad', b'\xff', b'{}', b'[null]')
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'input.json', Path(tmp)/'output.json'
            prior = b'[{"prior":true}]'
            for content in cases:
                with self.subTest(content=content):
                    if source.exists():
                        source.unlink()
                    if content is not None:
                        source.write_bytes(content)
                    output.write_bytes(prior)
                    provider.Llama.reset_mock()
                    with patch.object(sys, 'argv', ['llm_enricher.py', '--model-path', 'fixture.gguf',
                         '--input-file', str(source), '--output-file', str(output)]):
                        with self.assertRaises(SystemExit) as raised:
                            module.main()
                    self.assertEqual(1, raised.exception.code)
                    provider.Llama.assert_not_called()
                    self.assertEqual(prior, output.read_bytes())
                    if content is not None:
                        self.assertEqual(content, source.read_bytes())

    def test_valid_input_is_enriched_and_exact_nested_output_is_published(self):
        module, provider = load_enricher()
        metadata = {'emotional_tone': {'label': 'calm', 'evidence': ['hello']}}
        provider.Llama.return_value = Mock(return_value={'choices': [{'text': json.dumps(metadata)}]})
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'input.json', Path(tmp)/'output.json'
            chunk = {'text': 'hello', 'speaker': 'ALICE', 'start': 0, 'end': 1}
            source.write_text(json.dumps([chunk])); original = source.read_bytes()
            with patch.object(sys, 'argv', ['llm_enricher.py', '--model-path', 'fixture.gguf',
                 '--input-file', str(source), '--output-file', str(output), '--emotional-tone']):
                module.main()
            self.assertEqual([{**chunk, **metadata}], json.loads(output.read_text()))
            self.assertEqual(original, source.read_bytes())
            provider.Llama.assert_called_once()

    def test_unparseable_response_remains_marked_as_failed(self):
        module, provider = load_enricher()
        enricher = module.LLMEnricher.__new__(module.LLMEnricher)
        for response in ('no JSON', '{"emotional_tone":', '["calm"]'):
            with self.subTest(response=response):
                result = enricher._parse_llm_output(response)
                self.assertTrue(result['_enrichment_failed'])
                self.assertEqual('N/A', result['emotional_tone'])
