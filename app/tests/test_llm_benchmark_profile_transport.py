"""Native SDK wire requests, worker cleanup, and SSH stdin envelope contracts."""
import contextlib
import hashlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import benchmark_runner as runner
import llm_benchmark_worker as worker
import llm_provider

for env, name in [('WORKER_SOURCE', 'worker'), ('RUNNER_SOURCE', 'runner')]:
    if os.environ.get(env):
        spec = importlib.util.spec_from_file_location(name+'_saved', os.environ[env])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        globals()[name] = module

PROFILE = {'base_url': 'http://localhost:1234/v1', 'api_key': 'env:BENCH_FIXTURE_KEY',
           'model_name': 'gpt-5', 'provider_headers': {'X-Benchmark-Tenant': 'reader'},
           'provider_extra_body': {'provider_flag': 'fixture', 'reasoning_effort': 'high'},
           'request_timeout_seconds': 17, 'connect_timeout_seconds': 3,
           'request_interval_seconds': 0.1}


class BenchmarkProfileTransportTests(unittest.TestCase):
    def test_all_four_worker_stages_send_profile_on_native_sdk_requests_and_close(self):
        stages = {'script_generation': '_run_script_generation_case',
                  'script_review': '_run_script_review_case',
                  'persona_generation': '_run_persona_case',
                  'nickname_detection': '_run_nickname_case'}
        for stage, function in stages.items():
            with self.subTest(stage=stage):
                requests, clients = [], []

                def handler(request):
                    requests.append((dict(request.headers), json.loads(request.content)))
                    return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion',
                        'created': 1, 'model': 'gpt-5', 'choices': [{'index': 0,
                        'message': {'role': 'assistant', 'content': '[]'}, 'finish_reason': 'stop'}]})

                def construct(**kwargs):
                    raw = OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
                    clients.append(raw)
                    return raw

                def run_case(*args):
                    client = next(value for value in args if hasattr(value, 'chat'))
                    client.chat.completions.create(model='gpt-5', messages=[{'role': 'user', 'content': 'cue'}],
                        temperature=0.4, top_p=0.8, max_tokens=40, extra_body={'explicit_flag': True})
                    return {'status': 'passed'}

                payload = {'llm_config': copy.deepcopy(PROFILE), 'base_url': PROFILE['base_url'],
                           'api_key': PROFILE['api_key'], 'model_name': 'gpt-5', 'params': {},
                           'max_retries': 0, 'word_ratio_min': 0.95, 'word_ratio_max': 1.05,
                           'fixtures': [{'id': 'f', 'text': 'cue', 'original': [], 'repetition_numbers': [1, 2]}]}
                before = copy.deepcopy(payload)
                shared_sleep, shared_monotonic = time.sleep, time.monotonic
                with patch.dict(os.environ, {'BENCH_FIXTURE_KEY': 'fixture-secret'}), \
                     patch.object(llm_provider, 'OpenAI', construct), \
                     patch.object(worker, 'OpenAI', construct, create=True), \
                     patch.object(worker.benchmark_runner, function, run_case), \
                     patch.object(llm_provider, 'time') as provider_clock:
                    self.assertIs(time.sleep, shared_sleep)
                    self.assertIs(time.monotonic, shared_monotonic)
                    provider_clock.monotonic.return_value = 10
                    cases = worker.execute_payload(stage, payload)
                self.assertEqual(2, len(cases))
                self.assertEqual(before, payload)
                self.assertEqual(2, len(requests))
                for headers, body in requests:
                    self.assertEqual('reader', headers.get('x-benchmark-tenant'))
                    self.assertEqual('Bearer fixture-secret', headers['authorization'])
                    self.assertEqual('fixture', body['provider_flag'])
                    self.assertEqual('high', body['reasoning_effort'])
                    self.assertTrue(body['explicit_flag'])
                    self.assertEqual(40, body['max_completion_tokens'])
                    self.assertNotIn('max_tokens', body)
                    self.assertNotIn('temperature', body)
                    self.assertNotIn('top_p', body)
                self.assertEqual(17, clients[0].timeout.read)
                self.assertEqual(3, clients[0].timeout.connect)
                self.assertTrue(clients[0].is_closed())
                provider_clock.sleep.assert_called_once()
                self.assertAlmostEqual(0.1, provider_clock.sleep.call_args.args[0])

    def test_all_orchestrators_pass_full_selected_profile_to_shared_factory(self):
        import benchmark_core
        environment_details = {'hostname': 'fixture', 'gpu_name': 'fixture',
                               'backend': 'cpu', 'python_version': '3.10', 'git_commit': 'fixture'}
        for stage in ('script_generation', 'script_review', 'persona_generation', 'nickname_detection'):
            for target in ('local', 'thunder'):
                with self.subTest(stage=stage, target=target), tempfile.TemporaryDirectory() as tmp:
                    profiles = {mode: {**PROFILE, 'provider_headers': {'X-Mode': mode}}
                                for mode in ('local', 'remote')}
                    config = {'llm_local': profiles['local'], 'llm_remote': profiles['remote'],
                              'llm_remote_ssh': 'fixture-host'}
                    entries = [{'speaker': 'ALICE', 'text': 'one two three four five'}]
                    if stage == 'script_generation':
                        source = Path(tmp) / 'source.txt'
                        source.write_text('one two three four five')
                        fixture = {'id': 'f', 'path': str(source),
                                   'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
                    elif stage == 'script_review':
                        source = Path(tmp) / 'source.json'
                        source.write_text(json.dumps(entries))
                        fixture = {'id': 'f', 'path': str(source), 'entry_start': 1, 'entry_count': 1,
                                   'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                                   'sha256': runner._hash_entries(entries)}
                    else:
                        content = ({'entries': entries, 'speakers': ['ALICE'], 'batch_size': 1}
                                   if stage == 'persona_generation' else
                                   {'entries': entries, 'expected_aliases': {}, 'existing_aliases': {}})
                        fixture = {'id': 'f', **content, 'sha256': runner._hash_entries(content)}
                    manifest = {'schema_version': 1, 'stage': stage, 'targets': [target],
                                'repetitions': 1, 'fixtures': [fixture]}
                    environment = benchmark_core.build_environment_fingerprint(target, environment_details)
                    state = {'cancel': False, 'logs': [], 'tasks': [{'fixture_id': 'f', 'status': 'pending'}]}
                    report_path = str(Path(tmp) / 'report.json')
                    report = benchmark_core.build_benchmark_report(manifest, environment)
                    prior_case = {'fixture_id': 'f', 'repetition': 1, 'status': 'passed', 'cue': 'prior evidence'}
                    report['cases'] = [prior_case]
                    benchmark_core.save_benchmark_report(report_path, report)
                    status = {'available': True, 'loaded': True, 'context_length': 8192}
                    with patch.object(runner, 'load_app_config', return_value=config), \
                         patch.object(runner, 'get_lmstudio_status', return_value=status), \
                         patch.object(runner, 'get_remote_lmstudio_status', return_value=status), \
                         patch.object(runner, 'make_llm_client', create=True) as factory, \
                         patch.object(runner, 'OpenAI', create=True), \
                         patch.object(runner, '_measure_llm_network_rtt', return_value=0):
                        getattr(runner, 'run_'+stage+'_benchmark')(
                            manifest, environment, report_path, state, 'config.json', tmp)
                    factory.assert_called_once()
                    self.assertEqual(profiles['local' if target == 'local' else 'remote'],
                                     factory.call_args.args[0])
                    self.assertEqual('complete', state['status'])
                    self.assertEqual([prior_case], json.loads(Path(report_path).read_text())['cases'])

    def test_case_failure_closes_client_without_hiding_original_error(self):
        raw = SimpleNamespace(close=lambda: closed.append(True))
        closed = []
        payload = {'base_url': PROFILE['base_url'], 'model_name': 'm',
                   'fixtures': [{'id': 'f'}]}
        with patch.object(worker, 'make_llm_client', return_value=raw, create=True), \
             patch.object(worker, 'OpenAI', return_value=raw, create=True), \
             patch.object(worker.benchmark_runner, '_run_persona_case', side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                worker.execute_payload('persona_generation', payload)
        self.assertEqual([True], closed)

    def test_ssh_arguments_exclude_payload_and_full_json_is_sent_by_stdin(self):
        payload = {'llm_config': {**PROFILE, 'api_key': 'fixture-secret'},
                   'fixtures': [{'text': 'big passage ' * 10000}]}
        before = copy.deepcopy(payload)
        response = SimpleNamespace(returncode=0, stderr='',
            stdout='banner\nLLM_BENCHMARK_RESULT=[{"status":"passed"}]\n')
        with patch.object(runner, 'run_benchmark_subprocess', return_value=response) as run:
            result = runner._run_llm_worker('script_generation', payload,
                {'remote_root': '/remote with spaces', 'remote_python': '/venv/python'}, 'fixture-host')
        command = run.call_args.args[0]
        remote = shlex.split(command[2])
        self.assertEqual(['ssh', 'fixture-host'], command[:2])
        self.assertEqual(['/venv/python', '/remote with spaces/app/llm_benchmark_worker.py',
                          '--stage', 'script_generation', '--payload-stdin'], remote)
        self.assertNotIn('fixture-secret', command[2])
        self.assertEqual(payload, json.loads(run.call_args.kwargs['input']))
        self.assertEqual(before, payload)
        self.assertEqual([{'status': 'passed'}], result)
        self.assertLess(len(command[2]), 200)

    def test_remote_profile_preserves_options_resolves_local_reference_and_keeps_input(self):
        profile = copy.deepcopy(PROFILE)
        profile['base_url'] = 'https://forward.example:4321/v1'
        before = copy.deepcopy(profile)
        with patch.dict(os.environ, {'BENCH_FIXTURE_KEY': 'fixture-secret'}):
            result = runner.get_remote_llm_worker_profile(profile)
        self.assertEqual('http://localhost:4321/v1', result['base_url'])
        self.assertEqual('fixture-secret', result['api_key'])
        self.assertEqual(before, profile)
        for key in profile.keys() - {'base_url', 'api_key'}:
            self.assertEqual(profile[key], result[key])

    def test_cli_reads_stdin_and_preserves_result_marker(self):
        payload = {'llm_config': PROFILE, 'fixtures': [], 'model_name': 'm'}
        output = io.StringIO()
        with patch('sys.argv', ['worker', '--stage', 'persona_generation', '--payload-stdin']), \
             patch('sys.stdin', io.StringIO(json.dumps(payload))), \
             patch.object(worker, 'execute_payload', return_value=[{'status': 'passed'}]) as execute, \
             contextlib.redirect_stdout(output):
            worker.main()
        execute.assert_called_once_with('persona_generation', payload)
        self.assertEqual('LLM_BENCHMARK_RESULT=[{"status":"passed"}]\n', output.getvalue())
