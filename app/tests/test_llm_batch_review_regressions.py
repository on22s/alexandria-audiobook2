"""Provider failures, malformed labels and source publication retain real guards."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import benchmark_runner as benchmark
import generate_personas as personas
import generate_script as generation
import review_script
import three_pass_generate as three_pass


def response(entries):
    return NS(choices=[NS(message=NS(content=json.dumps(entries)), finish_reason='stop')], usage=None)


class LlmBatchReviewRegressions(unittest.TestCase):
    def test_transport_budget_exhausts_then_reaches_failover_and_pause(self):
        create = Mock(side_effect=ConnectionError('temporary connection failure'))
        failover = Mock(return_value=False)
        client = NS(base_url='http://invalid.test', chat=NS(completions=NS(create=create)), failover=failover)
        params = generation.LLMGenParams(max_tokens=100, structured_output='off',
            api_retry_limit=5, on_api_exhaustion='pause')
        with patch.object(generation, 'pause_for_operator', return_value=False) as pause, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual([], generation.call_llm_for_entries(client, 'model', 'sys', 'prompt',
                params, 'review.log', 'test', max_retries=1))
        self.assertEqual(6, create.call_count)
        failover.assert_called_once()
        pause.assert_called_once()

    def test_malformed_provider_responses_exhaust_without_resetting_error_count(self):
        for temperature in (0, .5):
            for malformed in (NS(choices=[]), NS(choices=[NS(message=None)])):
                with self.subTest(temperature=temperature, malformed=malformed):
                    create = Mock(return_value=malformed)
                    failover = Mock(return_value=False)
                    client = NS(base_url='http://invalid.test', chat=NS(completions=NS(create=create)), failover=failover)
                    params = generation.LLMGenParams(max_tokens=100, structured_output='off', temperature=temperature, api_retry_limit=3)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual([], generation.call_llm_for_entries(client, 'model', 'sys', 'prompt',
                            params, 'review.log', 'test', max_retries=0))
                    self.assertEqual(4, create.call_count)
                    failover.assert_called_once()

    def test_transport_errors_do_not_spend_the_quality_retry(self):
        good = [{'speaker': 'NARRATOR', 'text': 'Known words.', 'instruct': ''}]
        create = Mock(side_effect=[ConnectionError('first'), response([]),
                                   ConnectionError('second'), response(good)])
        client = NS(base_url='http://invalid.test', chat=NS(completions=NS(create=create)))
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(generation, 'get_response_log_path', return_value=str(Path(folder, 'reply.log'))), \
             contextlib.redirect_stdout(io.StringIO()):
            result = generation.call_llm_for_entries(client, 'model', 'sys', 'prompt',
                generation.LLMGenParams(max_tokens=100, temperature=.5, structured_output='off', api_retry_limit=3),
                'reply.log', 'test', max_retries=1)
        self.assertEqual(good, result)
        self.assertEqual(4, create.call_count)

    def test_remote_wire_settings_reach_worker_as_json_with_fresh_runtime_state(self):
        params = generation.LLMGenParams(max_tokens=100, schema_rejected_by={'endpoint'}, request_admission=object())
        for stage in ('script-generation', 'script-review'):
            with self.subTest(stage=stage):
                payload = {'params': benchmark.get_llm_worker_params(params), 'fixtures': []}
                with patch.object(benchmark, 'run_benchmark_worker', return_value=[]) as worker:
                    benchmark._run_llm_worker(stage, payload,
                        {'remote_root': '/app', 'remote_python': '/python'}, 'fixture')
                wire = json.loads(worker.call_args.kwargs['input'])
                restored = generation.LLMGenParams(**wire['params'])
                self.assertEqual(100, restored.max_tokens)
                self.assertEqual(set(), restored.schema_rejected_by)
                self.assertIsNone(restored.request_admission)

    def test_review_rejects_bad_types_and_empty_speaker_but_accepts_empty_instruct(self):
        original = [{'speaker': 'NARRATOR', 'text': 'Known words.', 'instruct': ''}]
        for key, value in (('speaker', None), ('speaker', ' '), ('instruct', []), ('text', ['Known words.'])):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as folder:
                invalid = [{**original[0], key: value}]
                client = NS(base_url='http://invalid.test', chat=NS(completions=NS(create=lambda **kw: response(invalid))))
                with patch.object(generation, 'get_response_log_path', return_value=str(Path(folder, 'reply.log'))), contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(review_script.review_batch(client, 'model', original, 1, 1,
                        generation.LLMGenParams(max_tokens=100, structured_output='off'), max_retries=0))
                self.assertEqual('NARRATOR', original[0]['speaker'])
        with tempfile.TemporaryDirectory() as folder:
            client = NS(base_url='http://invalid.test', chat=NS(completions=NS(create=lambda **kw: response(original))))
            with patch.object(generation, 'get_response_log_path', return_value=str(Path(folder, 'reply.log'))), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(original, review_script.review_batch(client, 'model', original, 1, 1,
                    generation.LLMGenParams(max_tokens=100, structured_output='off'), max_retries=0))

    def test_three_pass_refuses_source_alias_before_loading_or_calling_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, 'book.txt')
            source.write_text('Original prose.')
            alias = Path(folder, 'alias.json')
            alias.symlink_to(source)
            for output in (source, alias):
                with self.subTest(output=output), patch.object(sys, 'argv', ['three-pass', str(source), '--output', str(output)]), \
                     patch.object(three_pass, 'make_run_client') as provider, \
                     patch.object(three_pass, 'get_prepared_source') as load, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        three_pass.main()
                    self.assertNotEqual(0, error.exception.code)
                    provider.assert_not_called()
                    load.assert_not_called()
                    self.assertEqual('Original prose.', source.read_text())

    def test_persona_removes_only_generated_preview_on_success_and_copy_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                previews = root / 'designed_voices/previews'
                previews.mkdir(parents=True)
                generated = previews / ('preview_' + 'a' * 32 + '.wav')
                generated.write_bytes(b'new preview')
                unrelated = previews / ('preview_' + 'b' * 32 + '.wav')
                unrelated.write_bytes(b'saved reference')
                engine = NS(generate_voice_design=lambda **kw: (str(generated), 24000))
                config = {}
                with contextlib.ExitStack() as stack:
                    if fails:
                        stack.enter_context(patch.object(personas.shutil, 'copy2', side_effect=OSError('disk full')))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    result = personas._save_generated_preview(folder, engine, config, 'A', 'voice', 'words')
                self.assertEqual(not fails, result)
                self.assertFalse(generated.exists())
                self.assertEqual(b'saved reference', unrelated.read_bytes())
                if result:
                    self.assertEqual(b'new preview', (root / config['A']['ref_audio']).read_bytes())
