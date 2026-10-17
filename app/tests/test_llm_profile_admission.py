"""Selected manual/CPU profiles reach native workflows without GPU admission."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import httpx2
from openai import OpenAI

import core
import find_nicknames as nick
import generate_script as generation
import llm_bench
import llm_provider as provider
import lmstudio_settings
import review_script as review
from completed_review_receipt import get_review_receipt_path
from utils import atomic_json_write


SOURCE = [{'speaker': 'ALICE', 'text': 'Alice waited in the room.', 'instruct': '', 'pause_after': 700},
          {'speaker': 'BOB', 'text': 'Bob answered Alice quietly.', 'instruct': ''}]
STATUS = {'available': True, 'loaded': True, 'optimized': True,
          'parallel': 2, 'context_length': 8192, 'runtime': 'lmstudio'}


def get_config(profile):
    return {'llm_mode': 'local', 'llm_local': {
        'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'fixture',
        'model_name': 'fixture-model', 'api_retry_limit': 0, 'structured_output': 'off',
        **profile}, 'generation': {'review_batch_size': 1, 'max_tokens': 1000},
        'prompts': {'review_system_prompt': 'Review the fixture.',
                    'review_user_prompt': '{context}\nTARGET\n{batch}'}}


def get_reply(messages):
    user = messages[-1]['content']
    if user.startswith('Continue this scene'):
        return 'A synthetic scene continuation.'
    if 'TARGET\n' in user:
        return user.rsplit('TARGET\n', 1)[-1]
    return '{}'


def run_workflow(profile, *, target='review', context=0, dedupe=False,
                 usage=(95, 100), benchmark_real=False, failover=False, secondary_on_gpu=False,
                 secondary_transport='http'):
    """Real main/client/validators/publication; only hardware/HTTP are fixtures."""
    requests, clients, responder_errors = [], [], []
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        config = get_config(profile)
        if failover:
            config['llm_failover'] = True
            config['llm_remote'] = {**config['llm_local'], 'model_name': 'fixture-secondary',
                                    'on_this_gpu': secondary_on_gpu, 'transport': secondary_transport}
        config_path = root / 'config.json'
        config_path.write_text(json.dumps(config))
        book = root / 'book.json'
        book.write_text(json.dumps(SOURCE))
        aliases = root / 'aliases.json'
        before_config = config_path.read_bytes()
        stack.enter_context(patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(root)}))
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(review, 'ensure_ideal_settings', return_value=(False, STATUS, 'fixture')))
        stack.enter_context(patch.object(nick, 'ensure_ideal_settings', return_value=(False, STATUS, 'fixture')))
        stack.enter_context(patch.object(review, 'get_current_status', return_value=STATUS))
        vram = stack.enter_context(patch.object(review, 'get_vram_usage', side_effect=(
            usage if callable(usage) else lambda: usage)))
        stack.enter_context(patch.object(lmstudio_settings, 'get_current_status', return_value=STATUS))
        sleep = stack.enter_context(patch.object(review.time, 'sleep'))
        gpu = stack.enter_context(patch.object(llm_bench.lmstudio_settings, 'get_gpu_name_and_backend',
                                               return_value=('fixture-GPU', 'fixture-backend')))
        stack.enter_context(patch.object(llm_bench.platform, 'node', return_value='fixture-host'))
        bench = stack.enter_context(patch.object(llm_bench, 'find_optimal_concurrency',
            **({'wraps': llm_bench.find_optimal_concurrency} if benchmark_real else {'return_value': 1})))

        def handle(request):
            body = json.loads(request.content)
            requests.append(copy.deepcopy(body))
            if failover and body['model'] == 'fixture-model':
                return httpx2.Response(429, json={'error': {'message': 'fixture primary unavailable',
                                                          'type': 'rate_limit'}})
            return httpx2.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': get_reply(body['messages'])}}]})

        def make_sdk(**kwargs):
            client = OpenAI(**kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
            clients.append(client)
            stack.callback(client.close)
            return client
        stack.enter_context(patch.object(provider, 'OpenAI', side_effect=make_sdk))

        stop = threading.Event()
        if profile.get('transport') == 'manual' or (failover and secondary_transport == 'manual'):
            # Native files/owner lifecycle, answered by a bounded synthetic person.
            pending, response = root / 'manual_llm/pending.json', root / 'manual_llm/response.json'
            stack.enter_context(patch.object(provider.ManualClient, 'POLL_SECONDS', .005))
            # Review's watchdog sleep is globally patched; manual polling must yield.
            sleep.side_effect = lambda seconds: stop.wait(min(seconds, .005))
            def answer():
                answered = set()
                while not stop.wait(.001):
                    try:
                        row = json.loads(pending.read_text())
                        if row['id'] not in answered:
                            answered.add(row['id'])
                            requests.append(copy.deepcopy(row))
                            atomic_json_write({'id': row['id'], 'content': get_reply(row['messages'])}, str(response))
                    except (FileNotFoundError, json.JSONDecodeError):
                        continue
                    except Exception as error:
                        responder_errors.append(str(error))
                        return
            person = threading.Thread(target=answer, daemon=True)
            person.start()
            def close_person():
                stop.set()
                person.join(timeout=1)
                if person.is_alive() or responder_errors:
                    raise AssertionError(responder_errors or 'manual responder did not stop')
            stack.callback(close_person)

        argv = ['fixture', '--input', str(book)]
        if target == 'review':
            if context:
                argv += ['--context-window', str(context)]
            if dedupe:
                argv += ['--dedupe-speakers', '--alias-registry', str(aliases)]
        else:
            argv += ['--aliases-file', str(aliases)]
        stack.enter_context(patch.object(sys, 'argv', argv))
        (review if target == 'review' else nick).main()
        receipt = Path(get_review_receipt_path(str(book)))
        checkpoint = Path(str(book) + '.review_checkpoint.json')
        return {'requests': copy.deepcopy(requests), 'output': json.loads(book.read_text()),
                'receipt': receipt.exists(), 'checkpoint': checkpoint.exists(),
                'receipt_document': json.loads(receipt.read_text()) if receipt.exists() else None,
                'checkpoint_document': json.loads(checkpoint.read_text()) if checkpoint.exists() else None,
                'aliases': json.loads(aliases.read_text()) if aliases.exists() else None,
                'config_unchanged': before_config == config_path.read_bytes(),
                'benchmark_calls': bench.call_count, 'gpu_probe_calls': gpu.call_count,
                'sleep_calls': sleep.call_count, 'vram_probe_calls': vram.call_count,
                'http_clients': len(clients),
                'pending': (root / 'manual_llm/pending.json').exists(),
                'response': (root / 'manual_llm/response.json').exists()}


class LlmProfileAdmissionTests(unittest.TestCase):
    def test_manual_bound_snapshot_returns_one_before_any_probe_or_cache_side_effect(self):
        profile = {'transport': 'manual', 'on_this_gpu': True}
        original = copy.deepcopy(profile)
        with patch.object(llm_bench, 'load_app_config', side_effect=AssertionError('unexpected config read')), \
             patch.object(lmstudio_settings, 'get_current_status') as status, \
             patch.object(lmstudio_settings, 'get_gpu_name_and_backend') as gpu, \
             patch.object(llm_bench, 'find_optimal_concurrency') as measure:
            result = llm_bench.get_cached_or_benchmarked_concurrency('unused', 'local',
                'http://127.0.0.1:1234/v1', 'fixture', object(), status=STATUS, llm_config=profile)
        self.assertEqual(1, result)
        self.assertEqual(original, profile)
        status.assert_not_called(); gpu.assert_not_called(); measure.assert_not_called()

    def test_manual_mode_ignores_machine_cache_and_preserves_its_exact_bytes(self):
        for mode, url in (('local', 'http://remote.invalid/v1'), ('remote', 'http://127.0.0.1:1234/v1')):
            for cached in (None, 8):
                with self.subTest(mode=mode, cached=cached), tempfile.TemporaryDirectory() as directory:
                    config = get_config({'transport': 'manual', 'concurrency': cached,
                                         'concurrency_for': url + '::fixture'})
                    config['llm_' + mode] = config.pop('llm_local')
                    path = Path(directory) / 'config.json'
                    path.write_text(json.dumps(config))
                    before = path.read_bytes()
                    with patch.object(lmstudio_settings, 'get_current_status') as status, \
                         patch.object(llm_bench, 'find_optimal_concurrency') as measure:
                        result = llm_bench.get_cached_or_benchmarked_concurrency(str(path), mode,
                            url, 'fixture', object(), status=STATUS)
                    self.assertEqual(1, result)
                    self.assertEqual(before, path.read_bytes())
                    status.assert_not_called(); measure.assert_not_called()

    def test_cpu_profile_reaches_real_review_in_both_modes_and_dedupe(self):
        for context in (0, 1):
            with self.subTest(context=context):
                result = run_workflow({'on_this_gpu': False}, context=context, dedupe=True)
                self.assertEqual(SOURCE, result['output'])
                self.assertTrue(result['receipt']); self.assertFalse(result['checkpoint'])
                self.assertEqual(3, len(result['requests']))
                self.assertIsNone(result['aliases'])
                self.assertEqual(0, result['sleep_calls'])
                self.assertEqual(0, result['vram_probe_calls'])

    def test_gpu_owned_profile_still_refuses_saturation_before_any_review_request(self):
        for profile in ({}, {'on_this_gpu': True}):
            for context in (0, 1):
                with self.subTest(profile=profile, context=context):
                    result = run_workflow(profile, context=context, dedupe=True)
                    self.assertEqual([], result['requests'])
                    self.assertEqual(SOURCE, result['output'])
                    self.assertFalse(result['receipt']); self.assertTrue(result['checkpoint'])
                    self.assertEqual(0, result['sleep_calls'])
                    self.assertEqual(1, result['vram_probe_calls'])

    def test_later_batches_and_dedupe_follow_actual_cpu_failover_in_both_modes(self):
        for context in (0, 1):
            with self.subTest(context=context):
                readings = iter([(50, 100), (95, 100)])
                result = run_workflow({'on_this_gpu': True}, context=context, dedupe=True,
                                      usage=lambda: next(readings), failover=True)
                self.assertEqual(SOURCE, result['output'])
                self.assertTrue(result['receipt']); self.assertFalse(result['checkpoint'])
                self.assertEqual(['fixture-model'] * 3 + ['fixture-secondary'] * 3,
                                 [row['model'] for row in result['requests']])
                self.assertEqual(1, result['vram_probe_calls'])

    def test_cpu_primary_cannot_bypass_gpu_owned_secondary_headroom(self):
        for context in (0, 1):
            with self.subTest(context=context):
                result = run_workflow({'on_this_gpu': False}, context=context,
                                      failover=True, secondary_on_gpu=True)
                self.assertEqual(['fixture-model'] * 3, [row['model'] for row in result['requests']])
                self.assertFalse(result['receipt']); self.assertTrue(result['checkpoint'])
                self.assertEqual(SOURCE, result['output'])
                self.assertGreater(result['vram_probe_calls'], 0)

    def test_gpu_owned_dedupe_gate_still_waits_and_refuses_new_calls(self):
        calls = 0
        def usage():
            nonlocal calls
            calls += 1
            return (50, 100) if calls <= 2 else (95, 100)
        result = run_workflow({'on_this_gpu': True}, dedupe=True, usage=usage)
        self.assertEqual(2, len(result['requests']))
        self.assertFalse(result['receipt'])
        self.assertGreater(result['sleep_calls'], 0)
        self.assertIsNone(result['aliases'])

    def test_manual_native_review_and_nickname_queues_only_request_book_work(self):
        code = ('import faulthandler; faulthandler.dump_traceback_later(10, exit=True); '
                'from tests.test_llm_profile_admission import LlmProfileAdmissionTests; '
                'LlmProfileAdmissionTests()._run_manual_workflows()')
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join(os.path.abspath(path) for path in sys.path)}
        result = subprocess.run([sys.executable, '-c', code], env=env,
                                capture_output=True, text=True, timeout=12)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def _run_manual_workflows(self):
        for target, context, dedupe in (('review', 0, True), ('review', 1, True), ('nicknames', 0, False)):
            result = run_workflow({'transport': 'manual', 'on_this_gpu': True},
                                  target=target, context=context, dedupe=dedupe, benchmark_real=True)
            self.assertEqual(3 if target == 'review' else 1, len(result['requests']))
            self.assertFalse(any(row['messages'][-1]['content'].startswith('Continue this scene')
                                 for row in result['requests']))
            self.assertEqual(0, result['benchmark_calls']); self.assertEqual(0, result['gpu_probe_calls'])
            self.assertEqual(0, result['http_clients'])
            self.assertTrue(result['config_unchanged'])
            self.assertFalse(result['pending']); self.assertFalse(result['response'])
            self.assertEqual(SOURCE, result['output'])
            if target == 'review':
                self.assertTrue(result['receipt']); self.assertFalse(result['checkpoint'])
            self.assertEqual(None if target == 'review' else {}, result['aliases'])
        result = run_workflow({'on_this_gpu': False}, dedupe=True, failover=True,
                              secondary_on_gpu=True, secondary_transport='manual')
        self.assertEqual(['fixture-model'] * 3 + ['fixture-secondary'] * 3,
                         [row['model'] for row in result['requests']])
        self.assertEqual(1, result['http_clients'])
        self.assertEqual(0, result['vram_probe_calls'])
        self.assertTrue(result['receipt']); self.assertFalse(result['checkpoint'])
        self.assertFalse(result['pending']); self.assertFalse(result['response'])
        self.assertEqual(SOURCE, result['output'])

    def test_shared_core_ownership_remains_conservative_for_missing_or_invalid_profiles(self):
        for config in ({}, None, {'llm_mode': 'local', 'llm_local': {'on_this_gpu': None}}):
            with self.subTest(config=config):
                self.assertTrue(core.is_llm_on_this_gpu(config))
        config = get_config({'transport': 'manual', 'on_this_gpu': True})
        self.assertFalse(core.is_llm_on_this_gpu(config))
        config = get_config({'on_this_gpu': False})
        self.assertFalse(core.is_llm_on_this_gpu(config))
        with patch.object(core, 'load_app_config', return_value=config), \
             patch.object(core, 'process_state', {'audio': {'running': True}}), \
             patch.object(core, '_task_claims', {}):
            core.check_global_gpu_lock('review')
        config['llm_local']['on_this_gpu'] = True
        with patch.object(core, 'load_app_config', return_value=config), \
             patch.object(core, 'process_state', {'audio': {'running': True}}), \
             patch.object(core, '_task_claims', {}):
            with self.assertRaises(core.HTTPException):
                core.check_global_gpu_lock('review')

    def test_secondary_admission_uses_selected_runtime_profile_and_retains_caller_gate(self):
        from tests.test_failover_runtime_profile import RuntimeSwitchRaceTests, RuntimeTransport
        fixture = RuntimeSwitchRaceTests()
        for profile, expected in (({'on_this_gpu': False}, True), ({'transport': 'manual'}, True),
                                  ({'on_this_gpu': True}, False), ({}, False)):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                client, primary, secondary = fixture.pair(RuntimeTransport(98304, True), profile)
                self.addCleanup(client.close)
                params = fixture.params(); params.user_prompt_template = '{context}\nTARGET\n{batch}'
                with fixture.status(), patch.object(review, 'get_vram_usage', return_value=(95, 100)), \
                     patch.object(review.time, 'sleep'), patch.object(generation, 'get_response_log_path',
                        side_effect=lambda name: str(Path(directory) / name)), redirect_stdout(io.StringIO()):
                    output = review.review_batch(client, 'remote', SOURCE, 1, 1, params, max_retries=0)
                self.assertEqual(SOURCE if expected else None, output)
                self.assertEqual(1, len(primary.requests))
                self.assertEqual(1 if expected else 0, len(secondary.requests))
                if expected:
                    client, _, secondary = fixture.pair(RuntimeTransport(98304, True), profile)
                    self.addCleanup(client.close)
                    params.request_admission = Mock(return_value=False)
                    with fixture.status(), patch.object(generation, 'get_response_log_path',
                            side_effect=lambda name: str(Path(directory) / name)), redirect_stdout(io.StringIO()):
                        output = review.review_batch(client, 'remote', SOURCE, 1, 1, params, max_retries=0)
                    self.assertIsNone(output); self.assertEqual([], secondary.requests)
                    params.request_admission.assert_called_once()


if __name__ == '__main__':
    unittest.main()
