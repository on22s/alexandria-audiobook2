"""Replay metadata qualifies captured arguments, provenance and failure behavior."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx2
from openai import OpenAI

import generate_script as gs
import llm_provider as provider
from tests import test_llm_request_log as request_tests


class ReplayMetadataTests(unittest.TestCase):
    run_sdk_call = request_tests.RequestLogTests.run_sdk_call
    sdk_response = request_tests.RequestLogTests.sdk_response

    def metadata(self, path, kind):
        prefix = kind + '_METADATA_JSON: '
        return [json.loads(next(line[len(prefix):] for line in block.split('─' * 80)[0].splitlines()
                                if line.startswith(prefix)))
                for block in request_tests.blocks(path)]

    def test_closed_prepared_metadata_matches_direct_sdk_and_pre_response_state(self):
        params = gs.LLMGenParams(max_tokens=4096, temperature=.4, top_p=.7,
            presence_penalty=.2, top_k=20, min_p=.08, banned_tokens=['¬'], seed=37,
            reasoning_effort='low', context_length=16384, hard_max_tokens=8192,
            reasoning_allowance=777, observed_calls=5,
            provider_extra_body={'chat_template_kwargs': {'enable_thinking': False}},
            response_schema={'name': 'entries', 'schema': {'type': 'array'}},
            reasoning_reservation={'entry_count': 2, 'per_entry_tokens': 64,
                                   'raw': 777, 'applied': 777, 'limit': 2048, 'scope': 'context'})
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            def handle(request):
                body = json.loads(request.content)
                [meta] = self.metadata(Path(tmp, 'llm_responses.requests.log'), 'REQUEST')
                self.assertEqual('shared_completion_arguments_before_provider_adaptation', meta['scope'])
                self.assertEqual(body['model'], meta['model'])
                self.assertEqual(body['response_format'], meta['response_format'])
                for key, value in meta['sampling'].items():
                    self.assertEqual(body[key], value)
                for key, value in meta['extra_body'].items():
                    self.assertEqual(body[key], value)
                self.assertEqual(body['max_tokens'], meta['budget']['effective_max_tokens'])
                self.assertEqual(4096, meta['budget']['requested_max_tokens'])
                self.assertEqual(777, meta['budget']['reasoning_allowance'])
                self.assertEqual(5, meta['budget']['observed_calls'])
                self.assertFalse(meta['provider_options_omitted'])
                self.assertFalse(meta['reasoning_reservation_fields_omitted'])
                self.assertIsInstance(meta['prepared_at_unix_seconds'], float)
                seen.append(meta)
                params.reasoning_allowance = 9999  # Later observation must not rewrite preparation.
                return self.sdk_response()
            result, _, attempts = self.run_sdk_call(tmp, handle, params)
            [response] = self.metadata(Path(tmp, 'llm_responses.log'), 'RESPONSE')
        self.assertEqual(json.loads(request_tests.CORRECT), result)
        self.assertEqual(seen[0]['request_id'], response['request_id'])
        self.assertEqual(response['request_id'], attempts[0]['request_id'])
        self.assertEqual('synthetic', attempts[0]['response_id'])

    def test_reasoning_zero_estimate_and_missing_remain_distinct_and_trace_is_not_logged(self):
        cases = [(777, 'hidden trace', 777, 'provider'),
                 (0, 'hidden trace', 0, 'provider'),
                 (None, 'x' * 4000, 1000, 'estimated_from_reasoning_content'),
                 (None, None, None, 'unreported')]
        for reported, trace, expected, provenance in cases:
            with self.subTest(provenance=provenance, reported=reported), tempfile.TemporaryDirectory() as tmp:
                def handle(request):
                    data = self.sdk_response().json()
                    if trace is not None:
                        data['choices'][0]['message']['reasoning_content'] = trace
                    if reported is not None:
                        data['usage'] = {'prompt_tokens': 20, 'completion_tokens': 1000,
                                         'total_tokens': 1020,
                                         'completion_tokens_details': {'reasoning_tokens': reported}}
                    return httpx2.Response(200, json=data)
                result, _, attempts = self.run_sdk_call(tmp, handle)
                path = Path(tmp, 'llm_responses.log')
                [meta] = self.metadata(path, 'RESPONSE')
                self.assertEqual(expected, meta['reasoning_tokens'])
                self.assertEqual(provenance, meta['reasoning_tokens_source'])
                self.assertEqual(expected, attempts[0]['reasoning_tokens'])
                self.assertEqual(provenance, attempts[0]['reasoning_tokens_source'])
                if trace:
                    self.assertNotIn(trace, path.read_text())
                [block] = request_tests.blocks(path)
                self.assertEqual(result, json.loads(block.split('─' * 80 + '\n', 1)[1].strip()))

    def test_timeout_id_joins_intent_and_later_admission_does_not_inherit_it(self):
        seen = []
        def handle(request):
            seen.append(request)
            raise httpx2.ReadTimeout('synthetic timeout', request=request)
        params = gs.LLMGenParams(max_tokens=4096, temperature=.4, api_retry_limit=1)
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_effective_max_tokens',
                side_effect=[4096, gs.TokenBudgetError('synthetic later admission refusal')]):
            result, _, attempts = self.run_sdk_call(tmp, handle, params)
            [prepared] = self.metadata(Path(tmp, 'llm_responses.requests.log'), 'REQUEST')
        self.assertEqual([], result)
        self.assertEqual(1, len(seen))
        self.assertEqual(2, len(attempts))
        self.assertEqual(prepared['request_id'], attempts[0]['request_id'])
        self.assertIsNone(attempts[1]['request_id'])
        self.assertEqual('context_budget', attempts[1]['error_category'])

    def test_schema_fallback_keeps_logical_scope_and_next_preparation_uses_capability_cache(self):
        seen = []
        def handle(request):
            body = json.loads(request.content)
            seen.append(body)
            if 'response_format' in body:
                return httpx2.Response(400, json={'error': {
                    'message': 'response_format is unsupported', 'type': 'invalid_request_error'}})
            return self.sdk_response()
        params = gs.LLMGenParams(max_tokens=4096, temperature=.4, api_retry_limit=0,
            response_schema={'name': 'entries', 'schema': {'type': 'array'}})
        with tempfile.TemporaryDirectory() as tmp:
            first = self.run_sdk_call(tmp, handle, params)
            second = self.run_sdk_call(tmp, handle, params)
            prepared = self.metadata(Path(tmp, 'llm_responses.requests.log'), 'REQUEST')
            replies = self.metadata(Path(tmp, 'llm_responses.log'), 'RESPONSE')
        self.assertEqual(3, len(seen))
        self.assertEqual(2, len(prepared))
        self.assertEqual(seen[0]['response_format'], prepared[0]['response_format'])
        self.assertIsNone(prepared[1]['response_format'])
        self.assertNotEqual(prepared[0]['request_id'], prepared[1]['request_id'])
        self.assertEqual([row['request_id'] for row in prepared], [row['request_id'] for row in replies])
        self.assertEqual(json.loads(request_tests.CORRECT), first[0])
        self.assertEqual(first[0], second[0])

    def test_nested_unknown_options_are_omitted_without_changing_transport_or_inputs(self):
        extra = {'api_key': 'secret-body', 'headers': {'Authorization': 'secret-auth'},
                 'chat_template_kwargs': {'enable_thinking': {'api_key': 'secret-nested'},
                                          'password': 'secret-template'},
                 'seed': {'password': 'secret-seed'}, 'top_k': {'secret': 'secret-topk'},
                 'min_p': {'api_key': 'secret-minp'}, 'banned_tokens': [{'secret': 'secret-banned'}],
                 'reasoning_effort': {'password': 'secret-effort'}}
        reservation = {'entry_count': 2, 'raw': {'password': 'secret-raw'},
                       'scope': {'password': 'secret-scope'}, 'secret': 'secret-reservation'}
        original = copy.deepcopy((extra, reservation))
        params = gs.LLMGenParams(max_tokens=4096, temperature=.4,
                                provider_extra_body=extra, reasoning_reservation=reservation)
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            def handle(request):
                seen.append(json.loads(request.content))
                return self.sdk_response()
            result, _, _ = self.run_sdk_call(tmp, handle, params)
            path = Path(tmp, 'llm_responses.requests.log')
            [meta] = self.metadata(path, 'REQUEST')
            self.assertNotIn('secret-', path.read_text())
            self.assertTrue(meta['provider_options_omitted'])
            self.assertTrue(meta['reasoning_reservation_fields_omitted'])
            self.assertEqual({'chat_template_kwargs': {}}, meta['extra_body'])
            self.assertEqual({'entry_count': 2}, meta['reasoning_reservation'])
        self.assertTrue(result)
        self.assertEqual(original, (extra, reservation))
        for key, value in extra.items():
            self.assertEqual(value, seen[0][key])

    def test_configured_reasoning_adaptation_remains_later_than_logged_shared_arguments(self):
        seen, attempts = [], []
        with tempfile.TemporaryDirectory() as tmp, OpenAI(
                base_url='http://fixture.invalid/v1', api_key='synthetic', max_retries=0,
                http_client=httpx2.Client(transport=httpx2.MockTransport(
                    lambda request: seen.append(json.loads(request.content)) or self.sdk_response()))) as sdk:
            client = provider.ConfiguredOpenAI(sdk, {'profile_option': True})
            params = gs.LLMGenParams(max_tokens=4096, temperature=.4)
            with patch.object(gs, 'get_response_log_path', return_value=str(Path(tmp, 'reply.log'))), \
                    patch.dict(os.environ, {'ALEXANDRIA_RUN_ID': ''}), contextlib.redirect_stdout(io.StringIO()):
                result = gs.call_llm_for_entries(client, 'o3-mini', 'system', 'user', params,
                    'reply.log', 'ATTRIBUTE', max_retries=0, attempt_observer=attempts.append)
            [meta] = self.metadata(Path(tmp, 'reply.requests.log'), 'REQUEST')
        self.assertTrue(result)
        self.assertEqual('shared_completion_arguments_before_provider_adaptation', meta['scope'])
        self.assertEqual(.4, meta['sampling']['temperature'])
        self.assertNotIn('temperature', seen[0])
        self.assertEqual(meta['budget']['effective_max_tokens'], seen[0]['max_completion_tokens'])
        self.assertNotIn('max_tokens', seen[0])
        self.assertTrue(seen[0]['profile_option'])
        self.assertNotIn('profile_option', meta['extra_body'])

    def test_metadata_failures_keep_valid_response_without_provider_retry(self):
        for failure in ('request', 'response'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                sent, attempts = [], []
                reply = SimpleNamespace(id='synthetic',
                    choices=[SimpleNamespace(message=SimpleNamespace(content=request_tests.CORRECT),
                                             finish_reason='stop')], usage=None)
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                    create=lambda **kw: sent.append(kw) or reply)))
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(gs, 'get_response_log_path',
                                                    return_value=str(Path(tmp, 'reply.log'))))
                    stack.enter_context(patch.dict(os.environ, {'ALEXANDRIA_RUN_ID': ''}))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    if failure == 'request':
                        stack.enter_context(patch.object(gs, 'get_llm_request_log_metadata',
                                                        side_effect=TypeError('synthetic metadata refusal')))
                    else:
                        native_dumps = json.dumps
                        def dumping(value, *args, **kwargs):
                            if isinstance(value, dict) and 'reasoning_tokens_source' in value:
                                raise TypeError('synthetic response metadata refusal')
                            return native_dumps(value, *args, **kwargs)
                        stack.enter_context(patch.object(gs.json, 'dumps', side_effect=dumping))
                    classify = stack.enter_context(patch.object(gs, 'classify_llm_error', wraps=gs.classify_llm_error))
                    result = gs.call_llm_for_entries(client, 'fixture', 'system', 'user',
                        gs.LLMGenParams(temperature=.4, api_retry_limit=2), 'reply.log',
                        'ATTRIBUTE', max_retries=0, attempt_observer=attempts.append)
                self.assertEqual(json.loads(request_tests.CORRECT), result)
                self.assertEqual(1, len(sent))
                self.assertEqual(1, len(attempts))
                self.assertIn(failure + '_log_error', attempts[0])
                self.assertIsNotNone(attempts[0]['request_id'])
                classify.assert_not_called()

    def test_response_id_accessor_failures_keep_valid_response_without_retry(self):
        for error_type in (TypeError, OSError):
            with self.subTest(error_type=error_type.__name__), tempfile.TemporaryDirectory() as tmp:
                class Reply:
                    choices = [SimpleNamespace(message=SimpleNamespace(content=request_tests.CORRECT),
                                               finish_reason='stop')]
                    usage = None

                    @property
                    def id(self):
                        raise error_type('synthetic optional ID failure')

                sent, attempts = [], []
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                    create=lambda **kw: sent.append(kw) or Reply())))
                with patch.object(gs, 'get_response_log_path', return_value=str(Path(tmp, 'reply.log'))), \
                        patch.dict(os.environ, {'ALEXANDRIA_RUN_ID': ''}), \
                        patch.object(gs, 'classify_llm_error', wraps=gs.classify_llm_error) as classify, \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = gs.call_llm_for_entries(client, 'fixture', 'system', 'user',
                        gs.LLMGenParams(temperature=.4, api_retry_limit=2), 'reply.log',
                        'ATTRIBUTE', max_retries=0, attempt_observer=attempts.append)
                self.assertEqual(json.loads(request_tests.CORRECT), result)
                self.assertEqual(1, len(sent))
                [attempt] = attempts
                self.assertEqual('accepted', attempt['outcome'])
                self.assertIsNone(attempt['response_id'])
                self.assertEqual(request_tests.CORRECT, attempt['raw_response'])
                self.assertTrue(attempt['response_log_error'].startswith(error_type.__name__ + ':'))
                self.assertEqual('unreported', attempt['reasoning_tokens_source'])
                classify.assert_not_called()

    def test_non_string_response_id_is_explicitly_omitted(self):
        reply = SimpleNamespace(id={'api_key': 'synthetic-secret'}, usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=request_tests.CORRECT),
                                     finish_reason='stop')])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply)))
        attempts = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                return_value=str(Path(tmp, 'reply.log'))), patch.dict(os.environ, {'ALEXANDRIA_RUN_ID': ''}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = gs.call_llm_for_entries(client, 'fixture', 'system', 'user',
                gs.LLMGenParams(temperature=.4), 'reply.log', 'ATTRIBUTE',
                max_retries=0, attempt_observer=attempts.append)
            [metadata] = self.metadata(Path(tmp, 'reply.log'), 'RESPONSE')
            self.assertNotIn('synthetic-secret', Path(tmp, 'reply.log').read_text())
        self.assertEqual(json.loads(request_tests.CORRECT), result)
        self.assertIsNone(metadata['response_id'])
        self.assertTrue(metadata['response_id_omitted'])
        self.assertIsNone(attempts[0]['response_id'])
        self.assertNotIn('response_log_error', attempts[0])

    def test_rejected_attempt_id_survives_in_saved_source_failure_diagnostics(self):
        import three_pass_generate as three
        attempts = []
        client, _ = request_tests.make_client([request_tests.SHIFTED])
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                return_value=str(Path(tmp, 'reply.log'))), patch.dict(os.environ, {'ALEXANDRIA_RUN_ID': ''}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = gs.call_llm_for_entries(client, 'fixture', 'system', 'user',
                gs.LLMGenParams(temperature=.4), 'reply.log', 'ATTRIBUTE', max_retries=0,
                validate_entries=lambda rows: {'passed': False, 'findings': [
                    {'code': 'synthetic_rejection', 'message': 'Synthetic rejection.'}]},
                attempt_observer=attempts.append)
            failure = three.build_failure_record('attribute', 7, 'Synthetic source.', attempts[-1])
            output = str(Path(tmp, 'output.json'))
            three._write_manifest(output, {}, [], {}, 'failed', diagnostic_failures=[failure])
            saved = json.loads(Path(three.three_pass_manifest_path(output)).read_text())
            [prepared] = self.metadata(Path(tmp, 'reply.requests.log'), 'REQUEST')
            [response] = self.metadata(Path(tmp, 'reply.log'), 'RESPONSE')
        self.assertEqual([], result)
        [record] = saved['diagnostic_failures']
        self.assertEqual(7, record['entry'])
        self.assertEqual(prepared['request_id'], record['request_id'])
        self.assertEqual(response['request_id'], record['request_id'])
        self.assertEqual('unreported', record['reasoning_tokens_source'])
        self.assertNotIn('request_id', three.build_failure_record('attribute', 0, 'Legacy.'))

    def test_failover_rebinds_logged_model_budget_and_seed_to_new_profile(self):
        seen, attempts = [], []
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            def primary(request):
                seen.append(json.loads(request.content))
                raise httpx2.ReadTimeout('synthetic primary failure', request=request)
            def secondary(request):
                seen.append(json.loads(request.content))
                return self.sdk_response()
            sdks = [stack.enter_context(OpenAI(base_url='http://fixture.invalid/v1', api_key='synthetic',
                max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
                for handler in (primary, secondary)]
            client = provider.FailoverClient(sdks[0], 'primary', sdks[1], 'secondary',
                secondary_config={'base_url': 'http://secondary.invalid/v1', 'context_length': 8192,
                                  'max_output_tokens': 1024, 'provider_extra_body': {'seed': 22}})
            stack.enter_context(patch('lmstudio_settings.get_current_status', return_value={
                'available': True, 'loaded': True, 'context_length': 8192, 'parallel': 1}))
            stack.enter_context(patch.object(gs, 'get_response_log_path', return_value=str(Path(tmp, 'reply.log'))))
            stack.enter_context(patch.dict(os.environ, {'ALEXANDRIA_RUN_ID': ''}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            params = gs.LLMGenParams(max_tokens=4096, hard_max_tokens=4096, context_length=16384,
                                    temperature=.4, api_retry_limit=0, provider_extra_body={'seed': 11})
            result = gs.call_llm_for_entries(client, 'primary', 'system', 'user', params,
                'reply.log', 'ATTRIBUTE', max_retries=0, attempt_observer=attempts.append)
            prepared = self.metadata(Path(tmp, 'reply.requests.log'), 'REQUEST')
        self.assertTrue(result)
        self.assertTrue(client.switched)
        self.assertEqual(['primary', 'secondary'], [row['model'] for row in prepared])
        self.assertEqual([16384, 8192], [row['budget']['context_length'] for row in prepared])
        self.assertEqual([4096, 1024], [row['budget']['effective_max_tokens'] for row in prepared])
        self.assertEqual([11, 22], [row['extra_body']['seed'] for row in prepared])
        self.assertEqual([row['request_id'] for row in prepared], [row['request_id'] for row in attempts])
        self.assertEqual([11, 22], [row['seed'] for row in seen])


if __name__ == '__main__':
    unittest.main()
