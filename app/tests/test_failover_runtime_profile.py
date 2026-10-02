"""CPU-only SDK transport probe. A runtime enforces its known context budget."""
import json
import copy
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx
from openai import OpenAI
import generate_script as gs
import llm_provider as provider
import review_script as review
import find_nicknames as nick
import generate_personas as personas
import three_pass_generate as tp


class RuntimeTransport(httpx.BaseTransport):
    def __init__(self, context, fail=False):
        self.context = context
        self.fail = fail
        self.requests = []
        self.bodies = []

    def handle_request(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        prompt = sum(len(str(m['content'])) for m in body['messages'])
        footprint = (prompt + 2) // 3 + body['max_tokens'] + 512
        self.requests.append({'model': body['model'], 'prompt_chars': prompt,
                              'max_tokens': body['max_tokens'], 'footprint': footprint,
                              'context': self.context})
        if self.fail:
            return httpx.Response(429, json={'error': {'message': 'primary unavailable', 'type': 'rate_limit'}})
        if footprint > self.context:
            return httpx.Response(400, json={'error': {'message': 'context window exceeded', 'type': 'invalid_request_error'}})
        text = body['messages'][-1]['content'].rsplit('TARGET\n', 1)[-1]
        try:
            entries = json.loads(text)
        except ValueError:
            entries = [{'speaker': 'NARRATOR', 'text': text, 'instruct': ''}]
        return httpx.Response(200, json={'id': 'cpu-fixture', 'object': 'chat.completion',
            'created': 1, 'model': body['model'], 'choices': [{'index': 0,
            'message': {'role': 'assistant', 'content': json.dumps(entries)}, 'finish_reason': 'stop'}]})


class RuntimeBudgetProbe(unittest.TestCase):
    def run_case(self, source, review_mode=False):
        primary, secondary = RuntimeTransport(98304, True), RuntimeTransport(8192)
        clients = []
        for transport, model in ((primary, 'remote'), (secondary, 'local')):
            sdk = OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, {}))
        cfg = {'llm_failover': True, 'llm_mode': 'remote',
               'llm_remote': {'base_url': 'http://remote.invalid/v1', 'model_name': 'remote'},
               'llm_local': {'base_url': 'http://local.invalid/v1', 'model_name': 'local'}}
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client(cfg, cfg['llm_remote'], 30)
        params = gs.LLMGenParams(system_prompt='Return narration.', user_prompt_template='{chunk}',
            context_length=98304, max_tokens=12000, hard_max_tokens=12000,
            temperature=0, api_retry_limit=0)
        original_context = params.context_length
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch(
                'lmstudio_settings.get_current_status', side_effect=lambda mode, *args, **kwargs: {
                    'available': True, 'loaded': True, 'context_length': 98304 if mode == 'remote' else 8192,
                    'parallel': 2 if mode == 'remote' else 1, 'runtime': 'lmstudio'}):
            if review_mode:
                params.user_prompt_template = '{context}\nTARGET\n{batch}'
                entries = review.review_batch(client, 'remote', [
                    {'speaker': 'NARRATOR', 'text': source, 'instruct': ''}], 1, 1, params,
                    max_retries=0)
                split = None
            else:
                entries, split = gs.process_chunk_adaptively(client, 'remote', source, 1, 1, params)
        print('CPU_TRANSPORT_MEASUREMENTS', json.dumps({'source_chars': len(source),
            'primary_request_count': len(primary.requests), 'secondary_request_count': len(secondary.requests),
            'returned_entries': len(entries or []), 'adaptively_split': split}))
        self.assertTrue(entries, 'fallback must preserve source via safe budgeting and bounded splitting')
        self.assertEqual(source.split(), ' '.join(e['text'] for e in entries).split())
        self.assertTrue(all(r['footprint'] <= r['context'] for r in secondary.requests),
                        'no primary-sized request may reach the smaller runtime')
        self.assertEqual(original_context, params.context_length, 'caller parameters remain input-only')
        self.assertTrue(client.switched)

    def test_completion_budget_recalculates_for_smaller_secondary(self):
        self.run_case('The narrator waited quietly in the room.')

    def test_oversized_source_recovers_by_splitting_without_losing_text(self):
        self.run_case(' '.join(f'Sentence {i} describes a unique evening and its numbered doorway.' for i in range(500)))

    def test_review_preserves_long_single_entry_through_bounded_splitting(self):
        self.run_case(' '.join(f'Sentence {i} describes a unique evening and its numbered doorway.' for i in range(500)), True)


class RuntimeProfileBindingTests(unittest.TestCase):
    def client(self, transport=None, config=None):
        secondary = OpenAI(base_url='http://fixture.invalid/v1', api_key='fixture', max_retries=0,
                           http_client=httpx.Client(transport=transport or RuntimeTransport(8192)))
        self.addCleanup(secondary.close)
        sdk = provider.ConfiguredOpenAI(secondary, {})
        return provider.FailoverClient(sdk, 'remote', sdk, 'local', secondary_label='local',
            secondary_config=config or {'base_url': 'http://local.invalid/v1', 'model_name': 'local'})

    def test_secondary_request_options_replace_primary_without_mutating_inputs(self):
        config = {'base_url': 'http://local.invalid/v1', 'model_name': 'local',
                  'provider_extra_body': {'secondary_only': False}, 'structured_output': 'off',
                  'api_retry_limit': 0}
        before = copy.deepcopy(config)
        client = self.client(config=config)
        params = gs.LLMGenParams(context_length=98304, provider_extra_body={'primary_only': True},
                                 structured_output='auto', api_retry_limit=7)
        client.failover({'category': 'rate_limited'})
        with patch('lmstudio_settings.get_current_status', return_value={
                'available': True, 'loaded': True, 'context_length': 8192, 'parallel': 1}) as status:
            effective = gs.ensure_run_request_params(client, params)
            variant = client.with_options(timeout=3)
            second = gs.ensure_run_request_params(variant, params)
        self.assertEqual(1, status.call_count)
        self.assertEqual(8192, effective.context_length)
        self.assertEqual({'secondary_only': False}, gs.build_extra_body(effective))
        self.assertEqual('off', effective.structured_output)
        self.assertEqual(0, effective.api_retry_limit)
        self.assertEqual(effective, second)
        self.assertEqual(98304, params.context_length)
        self.assertEqual({'primary_only': True}, params.provider_extra_body)
        self.assertEqual(before, config)
        effective.provider_extra_body['secondary_only'] = True
        self.assertEqual(False, client.ensure_active_runtime_profile()['config']['provider_extra_body']['secondary_only'])

    def test_unknown_secondary_runtime_is_explicit_and_cannot_inherit_large_primary(self):
        client = self.client()
        client.failover({'category': 'timeout'})
        with patch('lmstudio_settings.get_current_status', side_effect=OSError('status unavailable')), \
             patch('builtins.print') as diagnostics:
            effective = gs.ensure_run_request_params(client, gs.LLMGenParams(context_length=98304))
        self.assertEqual(4096, effective.context_length)
        self.assertIn('could not be verified', ' '.join(str(c) for c in diagnostics.call_args_list))
        self.assertIn('status unavailable', client.ensure_active_runtime_profile()['status']['runtime_error'])

    def test_actual_secondary_requests_obey_slot_cap_across_timeout_variants(self):
        class ConcurrentTransport(RuntimeTransport):
            def __init__(self):
                super().__init__(8192)
                self.lock = threading.Lock()
                self.live = self.peak = 0
            def handle_request(self, request):
                with self.lock:
                    self.live += 1
                    self.peak = max(self.peak, self.live)
                try:
                    time.sleep(0.02)
                    return super().handle_request(request)
                finally:
                    with self.lock:
                        self.live -= 1
        transport = ConcurrentTransport()
        client = self.client(transport)
        client.failover({'category': 'rate_limited'})
        with patch('lmstudio_settings.get_current_status', return_value={
                'available': True, 'loaded': True, 'context_length': 8192, 'parallel': 1}) as status:
            variants = [client.with_options(timeout=3) for _ in range(8)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                replies = list(executor.map(lambda variant: variant.chat.completions.create(
                    model='remote', messages=[{'role': 'user', 'content': 'Narration.'}], max_tokens=64), variants))
        self.assertEqual(1, status.call_count)
        self.assertEqual(1, transport.peak)
        self.assertEqual(8, len(replies))
        self.assertTrue(all(r['model'] == 'local' for r in transport.requests))


class RuntimeSwitchRaceTests(unittest.TestCase):
    def pair(self, primary=None, secondary_config=None):
        p, q = primary or RuntimeTransport(98304), RuntimeTransport(8192)
        configs = [
            {'base_url': 'http://remote.invalid/v1', 'model_name': 'remote',
             'provider_extra_body': {'primary_only': True}},
            {'base_url': 'http://127.0.0.1:1/v1', 'model_name': 'local',
             'provider_extra_body': {'secondary_only': False}, 'api_retry_limit': 0,
             **(secondary_config or {})}]
        clients = []
        for transport, config in zip((p, q), configs):
            sdk = OpenAI(base_url=config['base_url'], api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, config['provider_extra_body']))
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client({'llm_failover': True, 'llm_mode': 'remote',
                'llm_remote': configs[0], 'llm_local': configs[1]}, configs[0], 30)
        return client, p, q

    def params(self):
        return gs.LLMGenParams(system_prompt='Narrate.', user_prompt_template='{chunk}',
            context_length=98304, max_tokens=12000, api_retry_limit=0,
            provider_extra_body={'primary_only': True})

    def status(self):
        return patch('lmstudio_settings.get_current_status', return_value={
            'available': True, 'loaded': True, 'context_length': 8192, 'parallel': 1})

    def call(self, client, params, attempts=None):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)):
            return gs.call_llm_for_entries(client, 'remote', 'Narrate.', 'The room was quiet.',
                params, 'fixture.log', 'RACE', max_retries=0,
                attempt_observer=(attempts.append if attempts is not None else None))

    def test_switch_after_planning_rebinds_budget_and_wire_options_via_shared_variant(self):
        client, primary, secondary = self.pair(secondary_config={'structured_output': 'off'})
        variant = client.with_options(timeout=3)
        params = self.params()
        params.response_schema = {'schema': {'type': 'array', 'items': {'type': 'object'}}}
        real_create = gs.create_completion
        def switch_then_dispatch(*args, **kwargs):
            variant.failover({'category': 'rate_limited'})
            return real_create(*args, **kwargs)
        attempts = []
        with self.status(), patch.object(gs, 'create_completion', side_effect=switch_then_dispatch):
            entries = self.call(client, params, attempts)
        self.assertTrue(entries, 'the secondary must get a complete safe request')
        self.assertEqual('The room was quiet.', entries[0]['text'])
        self.assertTrue(client.switched)
        self.assertTrue(variant.switched)
        self.assertEqual([], primary.requests)
        self.assertEqual(1, len(secondary.requests))
        self.assertLessEqual(secondary.requests[0]['footprint'], 8192)
        body = secondary.bodies[0]
        self.assertNotIn('primary_only', body)
        self.assertEqual(False, body['secondary_only'])
        self.assertNotIn('response_format', body)
        self.assertNotIn('_run_profile_index', body)
        self.assertEqual('profile_changed', attempts[0]['error_category'])
        self.assertEqual('local', body['model'])
        self.assertEqual({'primary_only': True}, params.provider_extra_body)

    def test_primary_inflight_failure_after_another_switch_still_gets_secondary_chance(self):
        class Primary(RuntimeTransport):
            def handle_request(self, request):
                self.switch()
                return super().handle_request(request)
        primary = Primary(98304, True)
        client, primary, secondary = self.pair(primary)
        variant = client.with_options(timeout=3)
        primary.switch = lambda: client.failover({'category': 'server_error'})
        with self.status():
            entries = self.call(client, self.params())
        self.assertTrue(entries, 'the secondary must get a complete safe request')
        self.assertEqual('The room was quiet.', entries[0]['text'])
        self.assertEqual(1, len(primary.requests))
        self.assertEqual(1, len(secondary.requests))
        self.assertLessEqual(secondary.requests[0]['footprint'], 8192)

    def test_local_fallback_review_runs_real_admission_hook_before_transport(self):
        client, primary, secondary = self.pair(RuntimeTransport(98304, True))
        params = self.params()
        params.user_prompt_template = '{batch}'
        source = [{'speaker': 'NARRATOR', 'text': 'The room was quiet.', 'instruct': ''}]
        original = copy.deepcopy(source)
        attempts = []
        with self.status(), tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch.object(
                review, 'wait_for_vram_headroom', return_value=False) as admission:
            denied = review.review_batch(client, 'remote', source, 1, 1, params,
                max_retries=0, attempt_observer=attempts.append)
        self.assertIsNone(denied)
        self.assertEqual(1, admission.call_count)
        self.assertEqual([], secondary.requests)
        self.assertEqual('request_admission', attempts[-1]['error_category'])
        with self.status(), tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch.object(
                review, 'wait_for_vram_headroom', return_value=True) as admission:
            accepted = review.review_batch(client, 'remote', source, 1, 1, params, max_retries=0)
        self.assertEqual(source, accepted)
        self.assertEqual(original, source)
        self.assertEqual(1, admission.call_count)
        self.assertEqual(1, len(secondary.requests))

    def test_multi_entry_review_recombines_complete_source_or_reports_failure(self):
        for fail_second in (False, True):
            with self.subTest(fail_second=fail_second):
                client, primary, secondary = self.pair(RuntimeTransport(98304, True))
                original_handler = secondary.handle_request
                def handle(request):
                    response = original_handler(request)
                    if fail_second and len(secondary.requests) == 2:
                        return httpx.Response(400, json={'error': {
                            'message': 'fixture second review request rejected', 'type': 'invalid_request_error'}})
                    return response
                secondary.handle_request = handle
                source = [{'speaker': 'NARRATOR', 'text': f'Row{i} ' + 'The evening was quiet. ' * 25,
                           'instruct': '', 'uid': f'u{i}'} for i in range(50)]
                original = copy.deepcopy(source)
                params = self.params()
                params.user_prompt_template = '{context}\nTARGET\n{batch}'
                attempts = []
                with self.status(), tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                        side_effect=lambda name: str(Path(tmp) / name)), patch.object(
                        review, 'wait_for_vram_headroom', return_value=True):
                    output = review.review_batch(client, 'remote', source, 1, 1, params,
                        max_retries=0, attempt_observer=attempts.append)
                    if output is not None:
                        artifact = Path(tmp) / 'review.json'
                        artifact.write_text(json.dumps(output))
                        self.assertEqual(original, json.loads(artifact.read_text()))
                if fail_second:
                    self.assertIsNone(output, 'a successful first half cannot publish a partial review')
                    self.assertTrue(any(a.get('http_status') == 400 for a in attempts))
                else:
                    self.assertEqual(original, output)
                self.assertEqual(original, source)
                self.assertEqual(1, len(primary.requests))
                self.assertEqual(2, len(secondary.requests))
                self.assertTrue(all(r['footprint'] <= 8192 for r in secondary.requests))

    def test_context_rescue_skips_primary_sized_window_and_validates_only_target_text(self):
        client, primary, secondary = self.pair()
        client.failover({'category': 'rate_limited'})
        original_handler = secondary.handle_request
        def handle(request):
            response = original_handler(request)
            if response.is_error:
                return response
            target = json.loads(request.content)['messages'][-1]['content'].split('SOURCE TEXT:\n')[1]
            data = response.json()
            data['choices'][0]['message']['content'] = json.dumps([{'type': 'NARRATOR', 'text': target}])
            return httpx.Response(200, json=data)
        secondary.handle_request = handle
        target = 'The target room held an unbroken silence.'
        chunks = ['Earlier historical events. ' * 1500, target, 'Later future events. ' * 1500]
        original = list(chunks)
        params = self.params()
        params.segment_system_prompt = 'SEGMENT'
        resolutions = []
        with self.status(), tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch.object(
                tp, '_rescue_prompt_fits', wraps=tp._rescue_prompt_fits) as fits:
            output = tp.rescue_chunk_with_context(client, 'remote', chunks, 1, params,
                resolution_sink=resolutions, windows=(20000, 500), max_retries=0)
        self.assertEqual([{'type': 'NARRATOR', 'text': target}], output)
        self.assertEqual(['context_rescue:500'], resolutions)
        self.assertEqual([8192, 8192], [call.args[-1].context_length for call in fits.call_args_list])
        self.assertEqual([], primary.requests)
        self.assertEqual(1, len(secondary.requests))
        self.assertLessEqual(secondary.requests[0]['footprint'], 8192)
        self.assertEqual(original, chunks)
        self.assertEqual(98304, params.context_length)

    def test_primary_schema_rejection_during_switch_is_cached_only_for_primary(self):
        class Primary(RuntimeTransport):
            def handle_request(self, request):
                self.bodies.append(json.loads(request.content))
                self.switch()
                return httpx.Response(400, json={'error': {
                    'message': 'response_format json_schema is unsupported', 'type': 'invalid_request_error'}})
        primary = Primary(98304)
        client, _, secondary = self.pair(primary)
        primary.switch = lambda: client.failover({'category': 'server_error'})
        params = self.params()
        params.response_schema = {'schema': {'type': 'array', 'items': {'type': 'object'}}}
        with self.status():
            entries = self.call(client, params)
        self.assertTrue(entries)
        self.assertEqual({'http://remote.invalid/v1/'}, params.schema_rejected_by)
        self.assertNotIn('http://127.0.0.1:1/v1/', params.schema_rejected_by)
        self.assertIn('response_format', secondary.bodies[0])
        self.assertNotIn('primary_only', secondary.bodies[0])


class NicknameRuntimeRecoveryTests(unittest.TestCase):
    def test_real_collected_evidence_is_repacked_without_loss_and_registry_preserves_human_entries(self):
        class NicknameTransport(RuntimeTransport):
            def handle_request(self, request):
                response = super().handle_request(request)
                if response.is_error:
                    return response
                prompt = json.loads(request.content)['messages'][-1]['content']
                passages = prompt.split('CONTEXT PASSAGES (multiple names co-occur — alias evidence):\n')[1]
                lines = [line[2:] for line in passages.split('\nReturn the JSON now.')[0].splitlines()]
                key = hashlib.sha256(''.join(lines).encode()).hexdigest()
                payload = {'aliases': {'BETTY': 'ALICE'}, 'evidence': {key: lines}}
                data = response.json()
                data['choices'][0]['message']['content'] = json.dumps(payload)
                return httpx.Response(200, json=data)
        primary, secondary = RuntimeTransport(98304, True), NicknameTransport(8192)
        clients = []
        configs = [{'base_url': 'http://remote.invalid/v1', 'model_name': 'remote'},
                   {'base_url': 'http://127.0.0.1:1/v1', 'model_name': 'local', 'api_retry_limit': 0}]
        for transport, config in zip((primary, secondary), configs):
            sdk = OpenAI(base_url=config['base_url'], api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, {}))
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client({'llm_mode': 'remote', 'llm_failover': True,
                'llm_remote': configs[0], 'llm_local': configs[1]}, configs[0], 30)
        entries = [{'speaker': 'ALICE' if i % 2 else 'BETTY',
                    'text': f'Passage {i}: ALICE and BETTY ' + 'walked through the quiet evening. ' * 8}
                   for i in range(100)]
        original = copy.deepcopy(entries)
        expected = nick.collect_context(entries)[2]
        self.assertEqual(100, len(expected))
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch(
                'lmstudio_settings.get_current_status', return_value={'available': True, 'loaded': True,
                'context_length': 8192, 'parallel': 1}):
            aliases, evidence = nick.find_nicknames(client, 'remote', entries,
                context_length=98304, params=gs.LLMGenParams(api_retry_limit=0), concurrency=3)
            registry = Path(tmp) / 'aliases.json'
            registry.write_text(json.dumps({'HUMAN': 'ALICE'}))
            nick.save_discovered_aliases(str(registry), aliases, roster=['ALICE', 'BETTY', 'HUMAN'])
            reread = json.loads(registry.read_text())
            evidence_file = Path(tmp) / 'evidence.json'
            evidence_file.write_text(json.dumps(evidence))
            recorded = json.loads(evidence_file.read_text())
        self.assertEqual({'BETTY': 'ALICE'}, aliases)
        self.assertEqual({'HUMAN': 'ALICE', 'BETTY': 'ALICE'}, reread)
        self.assertEqual(expected, [line for lines in recorded.values() for line in lines])
        self.assertEqual(original, entries)
        self.assertGreater(len(secondary.requests), 1)
        self.assertEqual(1, len(primary.requests))
        self.assertTrue(all(r['footprint'] <= 8192 for r in secondary.requests))
        self.assertTrue(all('"ALICE"' in b['messages'][-1]['content'] and
                            '"BETTY"' in b['messages'][-1]['content'] for b in secondary.bodies))


class PersonaDiscoveryRuntimeRecoveryTests(unittest.TestCase):
    def run_discovery(self, entries, start):
        class DiscoveryTransport(RuntimeTransport):
            def handle_request(self, request):
                response = super().handle_request(request)
                if response.is_error:
                    return response
                prompt = json.loads(request.content)['messages'][-1]['content']
                lines = re.findall(r'^\[(\d+)\] ([^:]+): (.*)$', prompt.split('Script batch:\n')[1], re.M)
                payload = {'ALICE': {'features': ['observed calm voice'],
                    'sample_lines': [text for _, _, text in lines],
                    'evidence': [{'entry_index': int(index), 'quote': text} for index, _, text in lines]}}
                data = response.json()
                data['choices'][0]['message']['content'] = json.dumps(payload)
                return httpx.Response(200, json=data)
        primary, secondary = RuntimeTransport(98304, True), DiscoveryTransport(8192)
        clients = []
        configs = [{'base_url': 'http://remote.invalid/v1', 'model_name': 'remote'},
                   {'base_url': 'http://127.0.0.1:1/v1', 'model_name': 'local', 'api_retry_limit': 0}]
        for transport, config in zip((primary, secondary), configs):
            sdk = OpenAI(base_url=config['base_url'], api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, {}))
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client({'llm_mode': 'remote', 'llm_failover': True,
                'llm_remote': configs[0], 'llm_local': configs[1]}, configs[0], 30)
        original = copy.deepcopy(entries)
        prompt = personas._build_batch_discovery_prompt(start, entries, ['ALICE', 'BOB'])
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch(
                'lmstudio_settings.get_current_status', return_value={'available': True, 'loaded': True,
                'context_length': 8192, 'parallel': 1}):
            characters = personas._discover_batch_characters(client, 'remote', prompt, entries, 7,
                98304, {'api_retry_limit': 0}, batch_start=start, allowed_speakers=['ALICE', 'BOB'])
            personas._write_batch_character_refs(tmp, characters, ['ALICE', 'BOB'], 7)
            reference = json.loads(Path(personas._character_ref_path(tmp, 'ALICE')).read_text())
        self.assertEqual(original, entries)
        self.assertIn('observed calm voice', reference['features'])
        observations = reference['observations']
        self.assertTrue(all(item['batch'] == 7 for item in observations))
        evidence = [row for item in observations for row in item['evidence']]
        self.assertEqual(set(range(start, start + len(entries))), {e['entry_index'] for e in evidence})
        for offset, entry in enumerate(entries):
            parts = [e['quote'] for e in evidence if e['entry_index'] == start + offset]
            self.assertEqual(entry['text'].split(), ' '.join(parts).split())
        self.assertEqual(1, len(primary.requests))
        self.assertGreater(len(secondary.requests), 1)
        self.assertTrue(all(r['footprint'] <= 8192 for r in secondary.requests))
        self.assertTrue(all('Allowed speaker labels:\n- ALICE\n- BOB' in
                            b['messages'][-1]['content'] for b in secondary.bodies))

    def test_large_batch_preserves_global_source_indices_and_observed_features(self):
        entries = [{'speaker': 'ALICE', 'text': f'Original line {i}. ' +
                    'The evening was quiet and the street was empty. ' * 24} for i in range(40)]
        self.run_discovery(entries, 57)

    def test_single_long_entry_splits_text_while_retaining_its_original_index(self):
        self.run_discovery([{'speaker': 'ALICE', 'text': ' '.join(
            f'Sentence {i} describes a unique evening and its numbered doorway.' for i in range(500))}], 143)



class PersonaAliasRuntimeRecoveryTests(unittest.TestCase):
    def run_aliases(self, speakers):
        class AliasTransport(RuntimeTransport):
            def handle_request(self, request):
                response = super().handle_request(request)
                if response.is_error:
                    return response
                prompt = json.loads(request.content)['messages'][-1]['content']
                targets = re.findall(r"Speaker label: '([^']+)'", prompt)
                data = response.json()
                data['choices'][0]['message']['content'] = json.dumps({name: 'ALICE' for name in targets})
                return httpx.Response(200, json=data)
        primary, secondary = RuntimeTransport(98304, True), AliasTransport(8192)
        configs = [{'base_url': 'http://remote.invalid/v1', 'model_name': 'remote'},
                   {'base_url': 'http://127.0.0.1:1/v1', 'model_name': 'local', 'api_retry_limit': 0}]
        clients = []
        for transport, config in zip((primary, secondary), configs):
            sdk = OpenAI(base_url=config['base_url'], api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, {}))
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client({'llm_mode': 'remote', 'llm_failover': True,
                'llm_remote': configs[0], 'llm_local': configs[1]}, configs[0], 30)
        original = copy.deepcopy(speakers)
        initial = {'ALICE': {'seed': 17}, 'HUMAN': {'alias_of': 'ALICE', 'seed': 23}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch(
                'lmstudio_settings.get_current_status', return_value={'available': True, 'loaded': True,
                'context_length': 8192, 'parallel': 1}):
            aliases = personas._resolve_aliases_batch(client, 'remote', speakers, list(initial),
                                                      98304, {'api_retry_limit': 0})
            self.assertEqual({name: 'ALICE' for name in speakers}, aliases)
            voice_path = Path(tmp) / 'voice_config.json'
            voice_path.write_text(json.dumps(initial))
            generated = {**initial, **{name: {'seed': -1} for name in speakers}}
            personas.save_generated_voice_config(str(voice_path), generated, initial,
                                                  list(speakers) + list(initial), aliases)
            published = json.loads(voice_path.read_text())
            self.assertEqual(initial['HUMAN'], published['HUMAN'])
            self.assertEqual(initial['ALICE'], published['ALICE'])
            self.assertTrue(all(published[name]['alias_of'] == 'ALICE' for name in speakers))
        self.assertEqual(original, speakers)
        self.assertEqual(1, len(primary.requests))
        self.assertGreater(len(secondary.requests), 1)
        self.assertTrue(all(r['footprint'] <= 8192 for r in secondary.requests))
        texts = {name: {'sample_lines': [], 'narrator_context': []} for name in speakers}
        for body in secondary.bodies:
            prompt = body['messages'][-1]['content']
            roster = prompt.split('Comparison character labels (script and configured):\n')[1].split('\n\n')[0]
            self.assertEqual(set(speakers) | set(initial), set(line[2:] for line in roster.splitlines()))
            for name, block in re.findall(r"Speaker label: '([^']+)'\n(.*?)(?=\n\n---\n\n|$)",
                                          prompt.split('Speakers to analyze:\n\n')[1], re.S):
                context, samples = block.split('Sample spoken lines:\n')
                for kind, section in (('narrator_context', context.split('Nearby narrator context:\n')[1]),
                                      ('sample_lines', samples)):
                    texts[name][kind].extend(line[4:] for line in section.splitlines() if line.startswith('  - '))
        for name, info in speakers.items():
            for kind, limit in (('sample_lines', 3), ('narrator_context', 2)):
                self.assertEqual(' '.join(info[kind][:limit]).split(), ' '.join(texts[name][kind]).split())

    def test_large_alias_batch_preserves_selected_evidence_and_global_roster(self):
        speakers = {f'ALIAS{i}': {'sample_lines': [f'Alias{i} sample{j}. ' +
                    'The evening was quiet and the street was empty. ' * 35 for j in range(3)],
                    'narrator_context': [f'Alias{i} context{j}. ' + 'Alice entered the room. ' * 20
                                         for j in range(2)]} for i in range(10)}
        self.run_aliases(speakers)

    def test_single_long_alias_evidence_preserves_entire_selected_text(self):
        self.run_aliases({'ALIAS': {'sample_lines': [' '.join(
            f'Sentence {i} describes a unique evening and its numbered doorway.' for i in range(500))],
                                  'narrator_context': []}})


class PersonaEvidenceRuntimeRecoveryTests(unittest.TestCase):
    def run_case(self, mode, fail_child=False):
        class PersonaTransport(RuntimeTransport):
            def handle_request(self, request):
                response = super().handle_request(request)
                if response.is_error:
                    return response
                if fail_child and len(self.requests) == 2:
                    return httpx.Response(400, json={'error': {'message': 'fixture failed child'}})
                data = response.json()
                data['choices'][0]['message']['content'] = json.dumps({
                    'description': 'Observed fixture calm voice.', 'ref_text': 'Alice entered the room.'})
                return httpx.Response(200, json=data)
        primary, secondary = RuntimeTransport(98304, True), PersonaTransport(8192)
        configs = [{'base_url': 'http://remote.invalid/v1', 'model_name': 'remote', 'api_retry_limit': 0},
                   {'base_url': 'http://127.0.0.1:1/v1', 'model_name': 'local', 'api_retry_limit': 0}]
        clients = []
        for transport, config in zip((primary, secondary), configs):
            sdk = OpenAI(base_url=config['base_url'], api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, {}))
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client({'llm_mode': 'remote', 'llm_failover': True,
                'llm_remote': configs[0], 'llm_local': configs[1]}, configs[0], 30)
        evidence = [('sample_lines', ' '.join(f'Sentence {i} describes a quiet evening.' for i in range(500))),
                    ('narrator_context', ' '.join(f'Alice observation {i} is calm.' for i in range(300)))]
        original = copy.deepcopy(evidence)
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch(
                'lmstudio_settings.get_current_status', return_value={'available': True, 'loaded': True,
                'context_length': 8192, 'parallel': 1}), patch.object(personas.time, 'sleep'):
            published = Path(tmp) / 'persona.json'
            if mode == 'compile':
                ref_dir = Path(tmp) / 'refs'
                ref_dir.mkdir()
                ref = {'name': 'ALICE', 'features': ['Supported calm observation'],
                       'sample_lines': [evidence[0][1]]}
                ref_path = Path(personas._character_ref_path(str(ref_dir), 'ALICE'))
                ref_path.write_text(json.dumps(ref))
                before = ref_path.read_bytes()
                selected = personas._compile_character_prompt(ref, '{character_ref}')
                system = 'Use supported observations only. ' * 430
                template = 'CUSTOM COMPILE INSTRUCTION\n{character_ref}'
                def save_preview(root, engine, voices, speaker, description, ref_text):
                    published.write_text(json.dumps({'description': description, 'ref_text': ref_text}))
                    return True
                invoke = lambda: personas._compile_persona(client, 'remote', None, {}, tmp,
                    str(ref_dir), 'ALICE', {'ALICE': [evidence[0][1]]}, system, template,
                    98304, {'api_retry_limit': 0}, preview_saver=save_preview)
            else:
                system = 'Use supported observations only.' * (1000 if mode == 'fixed' else 1)
                def build_prompt(parts):
                    return 'CUSTOM SIMPLE INSTRUCTION\n' + json.dumps({
                        'speaker': 'ALICE', 'evidence': parts}, ensure_ascii=False)
                params = personas._persona_params(system, 98304, {'api_retry_limit': 0}, 400, 0.3)
                invoke = lambda: personas.request_persona_with_evidence(client, 'remote', system,
                    build_prompt, evidence, params, 'PERSONA ALICE')
                if mode == 'cli':
                    script_path = Path(tmp) / 'annotated_script.json'
                    source = [{'speaker': 'NARRATOR', 'text': evidence[1][1]},
                              {'speaker': 'ALICE', 'text': evidence[0][1]}]
                    script_path.write_text(json.dumps(source))
                    voice_path = Path(tmp) / 'voice_config.json'
                    original_voices = {'HUMAN': {'seed': 23}}
                    voice_path.write_text(json.dumps(original_voices))
                    config = {'llm_mode': 'remote', 'llm_failover': True,
                              'llm_remote': configs[0], 'llm_local': configs[1],
                              'prompts': {'persona_system_prompt': system}}
                    config['prompts']['persona_user_prompt'] = (
                        'CUSTOM SIMPLE INSTRUCTION\n' + '{{"speaker":"{speaker}",'
                        '"sample_lines":"{sample_lines}","narrator_context":"{narrator_context}"}}')
                    def save_cli_preview(root, engine, voices, speaker, description, ref_text, **kwargs):
                        voices[speaker] = {'description': description, 'ref_text': ref_text, 'seed': 17}
                        published.write_text(json.dumps(voices[speaker]))
                        return True
                    def invoke():
                        with patch.object(personas, 'get_runtime_data_dir', return_value=tmp), patch.object(
                                personas, 'get_app_config_path', return_value=str(Path(tmp) / 'config.json')), patch.object(
                                personas, 'load_app_config', return_value=config), patch.object(
                                personas, 'ensure_ideal_settings', return_value=(None, {'context_length': 98304}, 'CPU fixture')), patch.object(
                                personas, 'make_run_client', return_value=client), patch.object(
                                personas, 'TTSEngine', return_value=object()), patch.object(
                                personas, '_save_generated_preview', side_effect=save_cli_preview), patch(
                                'sys.argv', ['generate_personas.py', '--speakers', 'ALICE']):
                            personas.main()
                        self.assertEqual(json.loads(script_path.read_text()), source)
                        saved = json.loads(voice_path.read_text())
                        self.assertEqual(original_voices['HUMAN'], saved['HUMAN'])
                        self.assertEqual('Observed fixture calm voice.', saved['ALICE']['description'])
            if fail_child or mode == 'fixed':
                with self.assertRaises(personas.PersonaContextRecoveryError):
                    invoke()
                self.assertFalse(published.exists())
            else:
                result = invoke()
                if mode not in ('compile', 'cli'):
                    published.write_text(json.dumps(result))
                self.assertEqual('Observed fixture calm voice.', json.loads(published.read_text())['description'])
                if mode == 'compile':
                    self.assertEqual(before, ref_path.read_bytes())
            evidence_bodies = [body for body in secondary.bodies if
                               'Supported partial persona drafts (not new source text):' not in
                               body['messages'][-1]['content']]
            if not fail_child and mode != 'fixed':
                self.assertGreater(len(evidence_bodies), 1)
                if mode == 'compile':
                    fragments = []
                    for body in evidence_bodies:
                        prompt = body['messages'][-1]['content']
                        self.assertTrue(prompt.startswith('CUSTOM COMPILE INSTRUCTION\n'))
                        fragment = json.loads(prompt.split('\n', 1)[1])
                        fragments.extend(fragment['selected_reference_fragments'])
                    self.assertEqual(selected.split(), ' '.join(fragments).split())
                else:
                    delivered = []
                    for body in evidence_bodies:
                        prompt = body['messages'][-1]['content']
                        self.assertTrue(prompt.startswith('CUSTOM SIMPLE INSTRUCTION\n'))
                        payload = json.loads(prompt.split('\n', 1)[1])
                        self.assertEqual('ALICE', payload['speaker'])
                        if mode == 'cli':
                            delivered.extend((kind, payload[kind]) for kind in
                                             ('sample_lines', 'narrator_context') if payload[kind] and
                                             payload[kind] != '(No nearby narrator intro lines found.)')
                        else:
                            delivered.extend(payload['evidence'])
                    for kind, text in evidence:
                        self.assertEqual(text.split(), ' '.join(t for k, t in delivered if k == kind).split())
        self.assertEqual(original, evidence)
        self.assertEqual(1, len(primary.requests))
        self.assertTrue(all(r['footprint'] <= 8192 for r in secondary.requests))
        self.assertTrue(all(body['messages'][0]['content'] == system for body in secondary.bodies))

    def test_simple_selected_samples_and_narrator_context_are_fully_recovered(self):
        self.run_case('simple')

    def test_native_compile_publishes_recovered_persona_preserving_selected_preview(self):
        self.run_case('compile')

    def test_failed_evidence_child_cannot_publish_partial_persona(self):
        self.run_case('compile', fail_child=True)

    def test_oversized_fixed_instructions_report_failure(self):
        self.run_case('fixed')

    def test_actual_simple_cli_publishes_recovered_persona_and_preserves_human_voice(self):
        self.run_case('cli')

class ThreePassInstructionRuntimeRecoveryTests(unittest.TestCase):
    def run_case(self, kind):
        class InstructionTransport(RuntimeTransport):
            def handle_request(self, request):
                response = super().handle_request(request)
                if response.is_error:
                    return response
                rows = json.loads(json.loads(request.content)['messages'][-1]['content'].rsplit('TARGET\n', 1)[-1])
                output = [{'n': row['n'], **({'instruct': 'Fixture delivery for ' + row['text'].split()[0]}
                          if kind == 'instruct' else {'speaker': 'ALICE'})} for row in rows]
                data = response.json()
                data['choices'][0]['message']['content'] = json.dumps(output)
                return httpx.Response(200, json=data)
        primary, secondary = RuntimeTransport(98304, True), InstructionTransport(8192)
        clients = []
        configs = [{'base_url': 'http://remote.invalid/v1', 'model_name': 'remote'},
                   {'base_url': 'http://127.0.0.1:1/v1', 'model_name': 'local', 'api_retry_limit': 0}]
        for transport, config in zip((primary, secondary), configs):
            sdk = OpenAI(base_url=config['base_url'], api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, {}))
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client({'llm_mode': 'remote', 'llm_failover': True,
                'llm_remote': configs[0], 'llm_local': configs[1]}, configs[0], 30)
        params = gs.LLMGenParams(system_prompt='Return indexed delivery notes.', user_prompt_template='{batch}',
                                context_length=98304, max_tokens=12000, api_retry_limit=0)
        entries = [{'speaker': 'ALICE', 'text': f'Row{i} ' + 'The evening was quiet. ' * 50,
                    'uid': f'u{i}', 'pause_after': 0.3} for i in range(40)]
        if kind == 'attribute':
            entries = [{**{k: v for k, v in e.items() if k != 'speaker'}, 'type': 'SPOKEN'} for e in entries]
            params.user_prompt_template = 'ROSTER {roster}\nTARGET\n{batch}'
        original = copy.deepcopy(entries)
        contexts = [{'previous_context': {'speaker': 'BOB', 'text': f'Previous {i}'},
                     'next_context': {'speaker': 'BOB', 'text': f'Next {i}'}} for i in range(40)]
        original_contexts = copy.deepcopy(contexts)
        exhausted = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch(
                'lmstudio_settings.get_current_status', return_value={'available': True, 'loaded': True,
                'context_length': 8192, 'parallel': 1}):
            if kind == 'instruct':
                output = tp.instruct_batch(client, 'remote', entries, params, max_retries=0,
                                          neighbor_contexts=contexts, exhaustion_sink=exhausted)
            else:
                output = tp.attribute_batch(client, 'remote', entries, params, ['ALICE', 'BOB'],
                    max_retries=0, neighbor_contexts=contexts, exhaustion_sink=exhausted, on_exhaustion='fallback')
            artifact = Path(tmp) / 'instructed.json'
            artifact.write_text(json.dumps(output))
            recorded = json.loads(artifact.read_text())
        self.assertEqual([], exhausted)
        if kind == 'instruct':
            self.assertEqual([f'Fixture delivery for Row{i}' for i in range(40)],
                             [e['instruct'] for e in recorded])
            self.assertEqual(original, [{k: v for k, v in e.items() if k != 'instruct'} for e in recorded])
        else:
            self.assertEqual(['ALICE'] * 40, [e['speaker'] for e in recorded])
            self.assertEqual([{**{k: v for k, v in e.items() if k != 'type'}, 'speaker': 'ALICE'}
                              for e in original], recorded)
            self.assertTrue(all('ROSTER ALICE, BOB' in body['messages'][-1]['content'] for body in secondary.bodies))
        self.assertEqual(original, entries)
        self.assertEqual(original_contexts, contexts)
        self.assertEqual(98304, params.context_length)
        self.assertEqual(1, len(primary.requests))
        self.assertGreater(len(secondary.requests), 1)
        self.assertTrue(all(r['footprint'] <= 8192 for r in secondary.requests))
        presented = [row for body in secondary.bodies for row in json.loads(body['messages'][-1]['content'].rsplit('TARGET\n', 1)[-1])]
        self.assertEqual([e['text'] for e in original], [row['text'] for row in presented])
        self.assertEqual([c['previous_context'] for c in contexts], [row['previous_context'] for row in presented])
        self.assertEqual([c['next_context'] for c in contexts], [row['next_context'] for row in presented])

    def test_smaller_secondary_recovers_full_instruction_batch_without_default_delivery(self):
        self.run_case('instruct')

    def test_smaller_secondary_recovers_attribution_before_unknown_speaker_fallback(self):
        self.run_case('attribute')


class ThreePassPipelineRuntimeRecoveryTests(unittest.TestCase):
    def test_full_pipeline_preserves_source_checkpoint_and_secondary_model_binding(self):
        primary, secondary = RuntimeTransport(98304, True), RuntimeTransport(8192)
        original_handler = secondary.handle_request
        def handle(request):
            response = original_handler(request)
            if response.is_error:
                return response
            body = json.loads(request.content)
            system = body['messages'][0]['content']
            user = body['messages'][-1]['content']
            if system.startswith('SEGMENT'):
                payload = [{'type': 'NARRATOR', 'text': line.strip()}
                           for line in user.split('SOURCE\n', 1)[1].splitlines() if line.strip()]
            elif system == 'INSTRUCT':
                payload = [{'n': row['n'], 'instruct': 'Fixture calm narration.'} for row in json.loads(user)]
            else:
                raise AssertionError('fixture expected deterministic narrator attribution')
            data = response.json()
            data['choices'][0]['message']['content'] = json.dumps(payload)
            return httpx.Response(200, json=data)
        secondary.handle_request = handle
        clients = []
        configs = [{'base_url': 'http://remote.invalid/v1', 'model_name': 'remote'},
                   {'base_url': 'http://127.0.0.1:1/v1', 'model_name': 'local', 'api_retry_limit': 0}]
        for transport, config in zip((primary, secondary), configs):
            sdk = OpenAI(base_url=config['base_url'], api_key='fixture', max_retries=0,
                         http_client=httpx.Client(transport=transport))
            self.addCleanup(sdk.close)
            clients.append(provider.ConfiguredOpenAI(sdk, {}))
        with patch.object(provider, 'make_llm_client', side_effect=clients):
            client = provider.make_run_client({'llm_mode': 'remote', 'llm_failover': True,
                'llm_remote': configs[0], 'llm_local': configs[1]}, configs[0], 30)
        source = '\n'.join(f'Sentence {i} describes a unique evening and its numbered doorway.' for i in range(500))
        params = gs.LLMGenParams(context_length=98304, max_tokens=12000, api_retry_limit=0,
            segment_system_prompt='SEGMENT', segment_user_prompt_template='SOURCE\n{chunk}',
            instruct_system_prompt='INSTRUCT', instruct_user_prompt_template='{batch}')
        with tempfile.TemporaryDirectory() as tmp, patch.object(gs, 'get_response_log_path',
                side_effect=lambda name: str(Path(tmp) / name)), patch(
                'lmstudio_settings.get_current_status', return_value={'available': True, 'loaded': True,
                'context_length': 8192, 'parallel': 1}):
            output = str(Path(tmp) / 'script.json')
            entries = tp.run_three_pass(client, 'remote', source, params, len(source) + 1, output_path=output)
            checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
            # Match the CLI's atomic publication after the pipeline returns.
            tp.atomic_json_write(entries, output)
            artifact = json.loads(Path(output).read_text())
            count = len(secondary.requests)
            resumed = tp.run_three_pass(client, 'remote', source, params, len(source) + 1, output_path=output)
            self.assertEqual(count, len(secondary.requests), 'completed checkpoint resumes without another request')
        self.assertEqual(artifact, resumed)
        self.assertEqual(source.split(), ' '.join(e['text'] for e in artifact).split())
        self.assertTrue(all(e['speaker'] == 'NARRATOR' and e['instruct'] == 'Fixture calm narration.' for e in artifact))
        self.assertEqual('done', checkpoint['stage'])
        self.assertEqual(1, checkpoint['chunks_done'])
        self.assertEqual(artifact, checkpoint['annotated'])
        self.assertEqual('complete', manifest['status'])
        self.assertEqual([], manifest['diagnostic_failures'])
        self.assertEqual({'primary_model': 'remote', 'failover_model': 'local', 'failover_used': True},
                         manifest['model_binding'])
        self.assertEqual(manifest['model_binding'], checkpoint['model_binding'])
        self.assertEqual(1, len(primary.requests))
        self.assertTrue(all(r['footprint'] <= 8192 for r in secondary.requests))
        self.assertGreater(len(secondary.requests), 2)
        self.assertEqual(98304, params.context_length)


class RunFingerprintResumeIdentityTests(unittest.TestCase):
    def test_execution_history_can_change_but_configured_identity_and_hashes_stay_strict(self):
        source = 'Complete source.'
        entries = [{'speaker': 'NARRATOR', 'text': source, 'instruct': 'Neutral.'}]
        params = gs.LLMGenParams(system_prompt='Narrate.', user_prompt_template='{chunk}')
        fingerprint = gs.get_generation_fingerprint(source, [source], 'remote', 'http://remote', params, 3000)
        fingerprint['model_binding'] = {'primary_model': 'remote', 'failover_model': 'local', 'failover_used': False}
        accepted = [{'source_sha256': fingerprint['chunk_sha256'][0], 'entries': entries,
            'quality': gs.validate_chunk_quality(source, entries),
            'model_binding': {**fingerprint['model_binding'], 'failover_used': True}}]
        original = copy.deepcopy(fingerprint)
        switched = copy.deepcopy(fingerprint)
        switched['model_binding']['failover_used'] = True
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / 'book.json')
            gs.save_generation_checkpoint(output, fingerprint, accepted)
            path = Path(gs.get_generation_checkpoint_path(output))
            before = path.read_bytes()
            self.assertEqual(accepted, gs.load_generation_checkpoint(output, switched))
            for field, value in (('primary_model', 'other'), ('failover_model', 'other'), ('failover_used', 'yes')):
                changed = copy.deepcopy(switched)
                changed['model_binding'][field] = value
                self.assertEqual([], gs.load_generation_checkpoint(output, changed))
            for field in ('source_sha256', 'settings_sha256'):
                changed = copy.deepcopy(switched)
                changed[field] = 'changed'
                self.assertEqual([], gs.load_generation_checkpoint(output, changed))
            self.assertEqual(before, path.read_bytes())
        self.assertEqual(original, fingerprint)
        self.assertEqual(True, switched['model_binding']['failover_used'])


if __name__ == '__main__':
    unittest.main()
