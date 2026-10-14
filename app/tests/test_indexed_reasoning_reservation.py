"""CPU SDK/orchestrator controls for fixed-window learned reasoning budgets."""
import contextlib
import io
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import generate_script as gs
import llm_provider as provider
import three_pass_generate as tp
from lmstudio_settings import TokenBudgetError


class IndexedTransport(httpx.BaseTransport):
    def __init__(self, first_reasoning=0, truncated_calls=0, fail_after=None, reasoning_only=False):
        self.first_reasoning = first_reasoning
        self.truncated_calls = truncated_calls
        self.fail_after = fail_after
        self.reasoning_only = reasoning_only
        self.bodies = []

    def handle_request(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.fail_after is not None and len(self.bodies) > self.fail_after:
            return httpx.Response(429, json={'error': {'message': 'unavailable'}})
        rows = json.loads(body['messages'][-1]['content'])
        truncated = len(self.bodies) <= self.truncated_calls
        content = ('' if self.reasoning_only else '[{"n":') if truncated else json.dumps([
            {'n': row['n'], 'head': ' '.join(row['text'].split()[:3]),
             **({'speaker': 'UNKNOWN'} if 'type' in row else {'instruct': 'Natural.'})}
            for row in rows])
        return httpx.Response(200, json={'id': 'cpu-indexed', 'object': 'chat.completion',
            'created': 1, 'model': body['model'], 'choices': [{'index': 0,
            'message': {'role': 'assistant', 'content': content},
            'finish_reason': 'length' if truncated else 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': self.first_reasoning if truncated else 30,
                      'total_tokens': 1, 'completion_tokens_details': {
                          'reasoning_tokens': self.first_reasoning if truncated else 0}}})


class IndexedReasoningReservationTests(unittest.TestCase):
    def get_sdk(self, transport):
        sdk = OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                     http_client=httpx.Client(transport=transport))
        self.addCleanup(sdk.close)
        return provider.ConfiguredOpenAI(sdk, {})

    def get_params(self, **changes):
        return gs.LLMGenParams(max_tokens=10000, hard_max_tokens=16384, context_length=16384,
            temperature=0, structured_output='off', api_retry_limit=0,
            attribute_system_prompt='ATTRIBUTE', user_prompt_template='{batch}',
            instruct_system_prompt='INSTRUCT', instruct_user_prompt_template='{batch}', **changes)

    def run_pipeline(self, transport, count=4, words=30, params=None, client=None, **options):
        params = params or self.get_params()
        texts = [f'Line {i} quietly ' + ('walks ' * words) for i in range(count)]
        source = ' '.join(texts)
        segmented = [{'type': 'SPOKEN', 'text': text} for text in texts]
        client = client or self.get_sdk(transport)
        measured = []
        native_resolver = getattr(tp, 'get_indexed_reasoning_request_params', None)

        def measure(*args, **kwargs):
            result = native_resolver(*args, **kwargs)
            measured.append({'metadata': dict(result.reasoning_reservation),
                             'max_tokens': result.max_tokens, 'hard_max_tokens': result.hard_max_tokens,
                             'context_length': result.context_length})
            return result

        options = {'attribute_target_chars': 1000000, 'instruct_target_chars': 1000000,
                   'on_exhaustion': 'fallback', **options}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output, \
             patch.object(gs, 'get_response_log_path', side_effect=lambda name: str(Path(tmp) / name)), \
             (patch.object(tp, 'get_indexed_reasoning_request_params', side_effect=measure)
              if native_resolver is not None else contextlib.nullcontext()):
            path = str(Path(tmp) / 'out.json')
            fingerprint = tp.three_pass_fingerprint(source, 'fixture', 30000, params, **options)
            binding = tp.get_run_model_binding(client, 'fixture')
            if binding['failover_model'] is not None:
                fingerprint['model_binding'] = binding
            chunks = tp.get_segment_chunk_records(source, 30000, params)
            tp._save_three_pass_checkpoint(path, fingerprint, 'segment', segmented, len(chunks), [], [],
                                           ['fixture_segment'] * len(chunks), model_binding=binding)
            result = tp.run_three_pass(client, 'fixture', source, params, 30000,
                                       output_path=path, **options)
            self.assertEqual(texts, [row['text'] for row in result])
            self.assertTrue(all(row['instruct'] == 'Natural.' for row in result))
            checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(path)).read_text())
            self.assertEqual(result, checkpoint['annotated'])
        return result, measured, output.getvalue()

    def test_full_pipeline_recovers_after_context_saturating_truncation(self):
        transport = IndexedTransport(first_reasoning=9489, truncated_calls=2, reasoning_only=True)
        params = self.get_params()
        result, measured, _ = self.run_pipeline(transport, params=params, on_exhaustion='fail')
        self.assertEqual(4, len(result))
        self.assertEqual(18978, params.reasoning_allowance, 'raw learner must remain uncapped')
        bounded = [row['metadata'] for row in measured if row['metadata'].get('raw') == 18978]
        self.assertTrue(bounded)
        self.assertTrue(all(row['applied'] < row['raw'] for row in bounded))
        self.assertGreater(len(transport.bodies), 2, 'successful requests must follow the bad responses')
        for body in transport.bodies:
            prompt = sum(len(message['content']) for message in body['messages'])
            self.assertLessEqual((prompt + 2) // 3 + body['max_tokens'] + 512, 16384)

    def test_ordinary_truncation_keeps_learning_and_subdivides_next_window(self):
        params = replace(self.get_params(), max_tokens=1000, hard_max_tokens=8192, context_length=8192)
        transport = IndexedTransport(first_reasoning=900, truncated_calls=1)
        _, measured, output = self.run_pipeline(transport, count=16, words=385, params=params,
                                                attribute_batch_size=8, attribute_target_chars=0)
        self.assertEqual(1800, params.reasoning_allowance)
        self.assertIn('Attribution request exceeds budget; subdividing 8 entries', output)
        warm = [row['metadata'] for row in measured if row['metadata'].get('raw') == 1800
                and row['metadata']['per_entry_tokens'] == tp.ATTRIBUTE_TOKENS_PER_ENTRY]
        self.assertTrue(warm)
        self.assertTrue(all(row['applied'] == 1800 for row in warm))
        batches = [json.loads(body['messages'][-1]['content']) for body in transport.bodies
                   if body['messages'][0]['content'] == 'ATTRIBUTE']
        self.assertEqual(8, len(batches[0]))
        self.assertTrue(all(len(rows) < 8 for rows in batches[2:]))
        control = IndexedTransport()
        self.run_pipeline(control, count=16, words=385, params=replace(params, reasoning_allowance=0,
            observed_calls=0), attribute_batch_size=8, attribute_target_chars=0)
        control_batches = [json.loads(body['messages'][-1]['content']) for body in control.bodies
                           if body['messages'][0]['content'] == 'ATTRIBUTE']
        self.assertEqual([8, 8], [len(rows) for rows in control_batches])

    def test_cold_non_reasoning_pipeline_never_evaluates_anchor_budgets(self):
        transport = IndexedTransport()
        self.run_pipeline(transport)
        self.assertEqual([4, 4], [len(json.loads(body['messages'][-1]['content']))
                                 for body in transport.bodies])
        self.assertEqual([10000, 1024], [body['max_tokens'] for body in transport.bodies])
        source = self.get_params()
        source.observed_calls = 1
        request = replace(source, request_params_resolver=lambda p: tp.get_indexed_reasoning_request_params(
            p, source, lambda p: self.fail('raw0 must not build anchors'), True),
            reasoning_reservation={'entry_count': 1, 'per_entry_tokens': 64})
        resolved = gs.ensure_run_request_params(None, request)
        self.assertEqual(10000, resolved.max_tokens)
        self.assertEqual((0, 0, None), tuple(resolved.reasoning_reservation[key]
                                           for key in ('raw', 'applied', 'limit')))
        self.assertEqual(1024, tp.bound_index_request_params(resolved, 1, 64).max_tokens)

    def test_unknown_context_and_explicit_stage_initial_preserved(self):
        source = replace(self.get_params(), context_length=None, max_tokens=500,
                         task_max_tokens=10000, request_initial_max_tokens=300,
                         reasoning_allowance=18978, observed_calls=1)
        anchors = lambda p: [('', 'Synthetic entry')]
        request = replace(source, request_params_resolver=lambda p: tp.get_indexed_reasoning_request_params(
            p, source, anchors, True), reasoning_reservation={'entry_count': 1, 'per_entry_tokens': 64})
        resolved = gs.ensure_run_request_params(None, request)
        self.assertEqual(300, resolved.max_tokens)
        self.assertEqual('output_only', resolved.reasoning_reservation['scope'])
        self.assertEqual(15360, resolved.reasoning_allowance)
        attempts = []
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
             patch.object(gs, 'get_response_log_path', return_value=str(Path(tmp) / 'responses.log')):
            transport = IndexedTransport()
            gs.call_llm_for_entries(self.get_sdk(transport), 'fixture', 'ATTRIBUTE',
                '[{"n":0,"type":"SPOKEN","text":"Synthetic entry"}]', resolved,
                'responses.log', 'ATTRIBUTE', max_retries=0, attempt_observer=attempts.append)
        self.assertEqual(300, transport.bodies[0]['max_tokens'])
        self.assertEqual(resolved.reasoning_reservation, attempts[0]['reasoning_reservation'])
        self.assertEqual(500, source.max_tokens)

    def test_actual_sdk_failover_refreshes_bound_and_secondary_hard_ceiling(self):
        primary = IndexedTransport(first_reasoning=9489, truncated_calls=1, fail_after=1)
        secondary = IndexedTransport()
        client = provider.FailoverClient(self.get_sdk(primary), 'primary', self.get_sdk(secondary),
            'secondary', secondary_label='local', secondary_config={
                'base_url': 'http://fixture.invalid/v1', 'model_name': 'secondary',
                'max_output_tokens': 2048, 'api_retry_limit': 0, 'structured_output': 'off'})
        with patch('lmstudio_settings.get_current_status', return_value={
                'available': True, 'loaded': True, 'context_length': 4096, 'parallel': 1}):
            _, measured, _ = self.run_pipeline(primary, client=client)
        self.assertTrue(client.switched)
        self.assertEqual(2, len(primary.bodies), 'one truncation then one API failure; no reset retries')
        self.assertTrue(secondary.bodies)
        secondary_params = [row for row in measured if row['context_length'] == 4096]
        self.assertTrue(secondary_params)
        self.assertTrue(all(row['metadata']['raw'] == 18978 for row in secondary_params))
        self.assertTrue(all(row['metadata']['applied'] <= 1024 for row in secondary_params))
        self.assertTrue(all(row['hard_max_tokens'] == 2048 for row in secondary_params))
        for body in secondary.bodies:
            self.assertEqual('secondary', body['model'])
            self.assertLessEqual(body['max_tokens'], 2048)
            prompt = sum(len(message['content']) for message in body['messages'])
            self.assertLessEqual((prompt + 2) // 3 + body['max_tokens'] + 512, 4096)

    def test_native_primary_profile_cap_does_not_poison_next_secondary_window(self):
        original = tp.get_three_pass_run_params(
            {'llm': {'max_output_tokens': 2000}, 'generation': {'max_tokens': 10000}},
            {'context_length': 16384})
        self.assertEqual((2000, 10000), (original.max_tokens, original.task_max_tokens))
        params = replace(original, attribute_system_prompt='ATTRIBUTE', user_prompt_template='{batch}',
            instruct_system_prompt='INSTRUCT', instruct_user_prompt_template='{batch}',
            structured_output='off', reasoning_allowance=4000, observed_calls=1)
        transport = IndexedTransport()
        client = provider.FailoverClient(self.get_sdk(IndexedTransport()), 'primary',
            self.get_sdk(transport), 'secondary', secondary_label='local', secondary_config={
                'base_url': 'http://fixture.invalid/v1', 'model_name': 'secondary',
                'structured_output': 'off'})
        client.failover({'category': 'rate_limited'})
        with patch('lmstudio_settings.get_current_status', return_value={
                'available': True, 'loaded': True, 'context_length': 16384, 'parallel': 1}):
            self.run_pipeline(transport, count=25, words=3, params=params, client=client)
        self.assertEqual(5600, transport.bodies[0]['max_tokens'],
                         '1600 visible +4000 reasoning; fresh secondary has room')

    def test_fixed_window_reservation_is_not_a_current_request_room_clamp(self):
        source = replace(self.get_params(), context_length=4096, reasoning_allowance=18978,
                         observed_calls=1)
        request = replace(source, reasoning_reservation={'entry_count': 1, 'per_entry_tokens': 64})
        resolved = tp.get_indexed_reasoning_request_params(
            request, source, lambda p: [('', 'x' * 600)], True)
        self.assertEqual(2360, resolved.reasoning_allowance)
        self.assertTrue(tp.does_request_fit_context('', 'x' * 600, resolved, 256, True))
        self.assertFalse(tp.does_request_fit_context('', 'x' * 3000, resolved, 256, True))
        blanket = replace(resolved, reasoning_allowance=2328)
        self.assertTrue(tp.does_request_fit_context('', 'x' * 3000, blanket, 256, True))

    def test_real_oversized_singleton_refuses_before_sdk(self):
        transport = IndexedTransport()
        params = replace(self.get_params(), context_length=4096)
        with self.assertRaises(TokenBudgetError):
            self.run_pipeline(transport, count=1, words=3000, params=params)
        self.assertEqual([], transport.bodies)

    def test_callback_state_stays_out_of_serialization_and_fingerprints(self):
        import benchmark_runner as benchmark
        params = self.get_params()
        hooked = replace(params, request_params_resolver=lambda p: p,
                         reasoning_reservation={'raw': 9999, 'applied': 1000})
        wire = json.loads(json.dumps(benchmark.get_llm_worker_params(hooked)))
        self.assertNotIn('request_params_resolver', wire)
        self.assertNotIn('reasoning_reservation', wire)
        restored = gs.LLMGenParams(**wire)
        self.assertIsNone(restored.request_params_resolver)
        self.assertEqual(params, hooked)
        self.assertEqual(tp.three_pass_fingerprint('Synthetic.', 'fixture', 3000, params),
                         tp.three_pass_fingerprint('Synthetic.', 'fixture', 3000, hooked))

    def test_legacy_params_preserve_early_guards_and_sdk_attempt_records(self):
        for legacy in (object(), SimpleNamespace(max_tokens=500)):
            self.assertIs(legacy, gs.ensure_run_request_params(None, legacy))
        legacy_ceiling = SimpleNamespace(observed_calls=0)
        self.assertIs(legacy_ceiling, tp.bound_index_request_params(legacy_ceiling, 1, 64))
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp, \
                 contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(gs, 'get_response_log_path', return_value=str(Path(tmp) / 'response.log')):
                values = vars(self.get_params()).copy()
                values.pop('request_params_resolver')
                values.pop('reasoning_reservation')
                legacy = SimpleNamespace(**values)
                transport = IndexedTransport(fail_after=0 if fail else None)
                attempts = []
                result = gs.call_llm_for_entries(self.get_sdk(transport), 'fixture', 'ATTRIBUTE',
                    '[{"n":0,"type":"SPOKEN","text":"Known synthetic words"}]', legacy,
                    'response.log', 'ATTRIBUTE', max_retries=0, attempt_observer=attempts.append)
                self.assertEqual(1, len(transport.bodies))
                self.assertEqual(1, len(attempts))
                self.assertNotIn('reasoning_reservation', attempts[0])
                if fail:
                    self.assertEqual([], result)
                    self.assertEqual('rate_limited', attempts[0]['error_category'])
                else:
                    self.assertTrue(result)
                    self.assertIsNone(attempts[0]['error'])
