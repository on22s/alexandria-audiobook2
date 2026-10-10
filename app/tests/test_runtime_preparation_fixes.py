"""Regressions found while exercising the production preparation CLIs."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import corpus_run_report as corpus
from experiments import llm_preflight as preflight
import llm_provider


class PreflightProviderTests(unittest.TestCase):
    def client(self, reply):
        return NS(models=NS(list=lambda: NS(data=[NS(id='test-model')])),
                  chat=NS(completions=NS(create=lambda **kw: reply)))

    def test_selected_provider_auth_headers_and_request_options_reach_sdk(self):
        calls = []
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            return NS(choices=[NS(message=NS(content='["Aiko"]'))])
        def constructor(**kwargs):
            calls.append(kwargs)
            client = self.client(None)
            client.chat.completions.create = create
            return client
        with patch.object(llm_provider, 'OpenAI', side_effect=constructor), \
                patch.dict(os.environ, {'PREFLIGHT_TEST_KEY': 'fixture-secret'}):
            ok, detail = preflight.check('http://example.invalid/v1', 'test-model', profile={
                'api_key': 'env:PREFLIGHT_TEST_KEY', 'provider_headers': {'X-Test': 'fixture'},
                'provider_extra_body': {'reasoning_effort': 'none'}})
        self.assertTrue(ok, detail)
        self.assertEqual('fixture-secret', calls[0]['api_key'])
        self.assertEqual({'X-Test': 'fixture'}, calls[0]['default_headers'])
        self.assertEqual({'reasoning_effort': 'none'}, requests[0]['extra_body'])
        self.assertIn('unknown completion tokens', detail)

    def test_constructor_and_malformed_completions_fail_without_raising(self):
        with patch.object(llm_provider, 'make_llm_client', side_effect=ValueError('bad endpoint')):
            self.assertFalse(preflight.check('bad', 'm')[0])
        for reply in (None, NS(), NS(choices=[]), NS(choices=[NS()]),
                      NS(choices=[NS(message=NS(content=[]))])):
            with self.subTest(reply=reply), patch.object(
                    llm_provider, 'make_llm_client', return_value=self.client(reply)):
                ok, detail = preflight.check('http://example.invalid/v1', 'm')
                self.assertFalse(ok)
                self.assertIn('malformed', detail)

    def test_config_selects_complete_profile_and_rejects_wrong_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            profile = {'base_url': 'http://example.invalid/v1', 'api_key': 'env:KEY'}
            config.write_text(json.dumps({'llm_mode': 'remote', 'llm_remote': profile}))
            self.assertEqual(profile, preflight.get_endpoint_profile(config))
            for data in (None, [], {'llm_local': ['invalid']}):
                config.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    preflight.get_endpoint_profile(config)


class CorpusOptionalModelTests(unittest.TestCase):
    def test_missing_model_is_rejected_before_creating_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for model in (None, '', ' '):
                with self.assertRaisesRegex(ValueError, '--model is required'):
                    corpus.run_corpus(root, root / 'missing.json', root, model, None)
            self.assertFalse((root / 'annotation_reports').exists())

    def test_optional_fallback_is_omitted_from_subprocess_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, source = root / 'audio.wav', root / 'source.txt'
            audio.write_bytes(b'fixture')
            source.write_text('fixture')
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps([{'audio': str(audio), 'source': str(source),
                                          'output_name': 'book.zip'}]))
            for fallback in (None, '', 'fallback.gguf'):
                with patch.object(corpus.subprocess, 'run', return_value=NS(returncode=1)) as run:
                    corpus.run_corpus(root, pairs, root, 'model.gguf', fallback)
                command = run.call_args.args[0]
                index = json.loads((root / 'corpus_attempts.json').read_text())
                self.assertEqual(fallback or None, index['rows'][0]['options']['fallback_model'])
                self.assertTrue(all(isinstance(arg, str) for arg in command))
                self.assertEqual(bool(fallback), '--fallback-model' in command)
                if fallback:
                    self.assertEqual(fallback, command[command.index('--fallback-model') + 1])
