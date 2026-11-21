"""Review benchmarks execute the selected schema and refuse cross-mode reuse."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx2
from openai import OpenAI

import benchmark_core as core
from benchmark_fixtures import build_script_review_manifest
import benchmark_runner as runner
from config_settings import get_review_labels_only, load_app_config
import llm_benchmark_worker as worker
import llm_provider
import review_script as review
from routers import benchmark as routes


SOURCE = [{'speaker': 'BOB', 'text': 'And yet— Just turn your head.', 'instruct': '',
           'pause_after': 1500, 'provenance': {'span': [4, 9]}},
          {'speaker': 'NARRATOR', 'text': 'said Alice.', 'instruct': ''}]
LABELS = [{'n': 1, 'speaker': 'NARRATOR', 'instruct': ''},
          {'n': 0, 'speaker': 'ALICE', 'instruct': 'Gently.'}]
ENVIRONMENT = {'hostname': 'fixture', 'gpu_name': 'fixture', 'backend': 'cpu',
               'python_version': '3.10', 'git_commit': 'fixture'}


def get_config(mode):
    config = {'llm_mode': 'remote', 'llm_remote_ssh': 'fixture-host',
              'llm_local': {'model_name': 'fixture-local', 'base_url': 'http://fixture.invalid/v1',
                            'api_key': 'fixture'},
              'llm_remote': {'model_name': 'fixture-remote', 'base_url': 'http://fixture.invalid:1234/v1',
                             'api_key': 'fixture'},
              'generation': {'max_tokens': 1000, 'temperature': .4, 'top_p': .8, 'top_k': 5},
              'prompts': {'review_system_prompt': 'Classic fixture reviewer.',
                          'review_user_prompt': '{context}\nTARGET\n{batch}'}}
    if mode is not None:
        config['prompts']['review_labels_only'] = mode
    return config


def run_native_benchmark(target='local', mode=True, reply=None, runner_module=runner, config=None):
    """Use actual runner, review, worker and SDK request bodies with CPU fixtures."""
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        source = root / 'fixture.json'
        source.write_text(json.dumps(SOURCE))
        config_path = root / 'config.json'
        config_path.write_text(json.dumps(config or get_config(mode)))
        manifest = build_script_review_manifest(
            [{'path': str(source), 'entry_starts': [1]}], directory, targets=[target])
        manifest['settings'] = {'remote_root': '/fixture worker',
                                'remote_python': '/fixture python', 'max_retries': 0}
        environment = core.get_benchmark_execution_environment(manifest,
            core.build_environment_fingerprint(target, ENVIRONMENT), load_app_config(str(config_path)))
        response_rows = (LABELS if mode else [
            {'speaker': 'ALICE', 'text': SOURCE[0]['text'], 'instruct': 'Gently.'}, SOURCE[1]]) if reply is None else reply
        requests, outputs, params_seen, envelopes = [], [], [], []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx2.Response(200, json={'id': 'fixture', 'object': 'chat.completion',
                'created': 1, 'model': 'fixture', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': json.dumps(response_rows)},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 20,
                                                  'total_tokens': 40}})

        def construct(**kwargs):
            client = OpenAI(**kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
            stack.callback(client.close)
            return client

        def run_review(*args, **kwargs):
            params_seen.append(args[5])
            output = review.review_batch(*args, **kwargs)
            outputs.append(output)
            return output

        def run_remote(command, marker, message, **kwargs):
            payload = json.loads(kwargs['input'])
            envelopes.append({'command': command, 'payload': copy.deepcopy(payload)})
            return worker.execute_payload('script_review', payload)

        stack.enter_context(patch.object(llm_provider, 'OpenAI', construct))
        stack.enter_context(patch.object(runner_module, 'review_batch', side_effect=run_review))
        stack.enter_context(patch.object(runner_module, 'get_lmstudio_status', return_value={
            'available': True, 'loaded': True, 'context_length': 8192}))
        stack.enter_context(patch.object(runner_module, 'get_remote_lmstudio_status', return_value={
            'available': True, 'loaded': True, 'context_length': 8192}))
        stack.enter_context(patch.object(runner_module, '_measure_llm_network_rtt', return_value=0))
        stack.enter_context(patch.object(runner_module, 'run_benchmark_worker', side_effect=run_remote))
        stack.enter_context(redirect_stdout(io.StringIO()))
        state = {'cancel': False, 'logs': [], 'tasks': [
            {'fixture_id': fixture['id'], 'status': 'pending'} for fixture in manifest['fixtures']]}
        report_path = root / 'report.json'
        report = runner_module.run_script_review_benchmark(
            manifest, environment, str(report_path), state, str(config_path), directory)
        saved_report = json.loads(report_path.read_text())
        assert source.read_text() == json.dumps(SOURCE)
        return {'report': report, 'saved_report': saved_report, 'requests': requests,
                'outputs': outputs, 'params': params_seen, 'envelopes': envelopes, 'state': state}


class BenchmarkReviewModeTests(unittest.TestCase):
    def test_basic_benchmark_contracts_still_import_without_site_packages(self):
        result = subprocess.run([sys.executable, '-S', '-c',
            "import benchmark_core as core; assert core.get_stage_registry()['script_review']['gpu'] is True"],
            cwd=Path(__file__).parent.parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_thunder_preflight_and_worker_share_the_keyless_loopback_profile(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'fixture.json')
            source.write_text(json.dumps(SOURCE))
            manifest = build_script_review_manifest(
                [{'path': str(source), 'entry_starts': [1]}], directory, targets=['thunder'])
            config = {'llm_mode': 'local', 'llm_remote_ssh': 'fixture-host',
                      'llm_remote': {'base_url': 'https://instance.thundercompute.net:1234/v1',
                                     'model_name': 'fixture-remote'},
                      'generation': {'max_tokens': 1000}}
            with patch.dict('os.environ', {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False), \
                    patch.object(routes, 'check_global_gpu_lock'), \
                    patch.object(routes, 'collect_thunder_environment',
                                 return_value=core.build_environment_fingerprint(
                                     'thunder', ENVIRONMENT)):
                result = routes._build_benchmark_preflight(SimpleNamespace(manifest=manifest),
                                                           config=config)
            self.assertEqual('ready', result['benchmark_state'])
            worker = runner.get_remote_llm_worker_profile(
                core.get_benchmark_llm_config(config, 'thunder'))
            self.assertEqual('http://localhost:1234/v1', worker['base_url'])
            self.assertEqual('local', worker['api_key'],
                             'the worker and preflight agree on the loopback placeholder')
            self.assertNotIn('sk-synthetic', json.dumps(result))

    def test_thunder_blank_key_still_dispatches_the_loopback_worker(self):
        config = get_config(True)
        config['llm_remote']['base_url'] = 'https://instance.thundercompute.net:1234/v1'
        config['llm_remote']['api_key'] = ''
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
            result = run_native_benchmark(target='thunder', config=config)
        self.assertEqual(1, len(result['envelopes']))
        envelope = result['envelopes'][0]
        worker_profile = envelope['payload']['llm_config']
        self.assertEqual('http://localhost:1234/v1', worker_profile['base_url'])
        self.assertEqual('local', worker_profile['api_key'])
        self.assertEqual(1, len(result['requests']), 'the mocked worker must issue a real request')
        self.assertEqual('passed', result['report']['cases'][0]['status'])
        self.assertNotIn('sk-synthetic', json.dumps(envelope))

    def test_rtt_probe_uses_safe_profile_and_skips_when_unavailable(self):
        llm = {'base_url': 'https://instance.thundercompute.net:1234/v1',
               'model_name': 'fixture-remote', 'api_key': ''}
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
            profile = runner._rtt_probe_profile(llm)
        self.assertEqual('local', profile['api_key'])
        self.assertEqual(llm['base_url'], profile['base_url'],
                         'the probe must still measure the public URL')
        self.assertNotIn('sk-synthetic', json.dumps(profile))
        with patch.object(runner, 'make_llm_client',
                          side_effect=llm_provider.MissingAPIKeyError('unavailable')):
            self.assertIsNone(runner._rtt_probe_seconds(llm))

    def assert_labels_request(self, result):
        self.assertEqual('passed', result['report']['cases'][0]['status'])
        self.assertEqual(result['report'], result['saved_report'])
        self.assertTrue(result['params'][0].review_labels_only)
        self.assertEqual(1000, result['params'][0].max_tokens)
        self.assertEqual(8192, result['params'][0].context_length)
        self.assertEqual([{**SOURCE[0], 'speaker': 'ALICE', 'instruct': 'Gently.'}, SOURCE[1]], result['outputs'][0])
        schema = result['requests'][0]['response_format']['json_schema']['schema']
        properties = schema['items']['properties']
        required = {'n', 'speaker', 'instruct'}
        annotations = {'type', 'spoken', 'speaker_gender', 'speaker_age_group', 'speaker_ageless'}
        self.assertEqual(required | annotations, set(properties))
        self.assertEqual(required, set(schema['items']['required']))
        self.assertFalse(schema['items']['additionalProperties'])
        self.assertEqual({'type': ['boolean', 'null']}, properties['spoken'])
        self.assertEqual({'type': ['boolean', 'null']}, properties['speaker_ageless'])
        self.assertEqual(['NARRATOR', 'SPOKEN', None], properties['type']['enum'])
        self.assertIn('female', properties['speaker_gender']['enum'])
        self.assertIn('adult', properties['speaker_age_group']['enum'])
        self.assertNotIn('text', properties)
        self.assertEqual([0, 1], schema['items']['properties']['n']['enum'])
        self.assertEqual((2, 2), (schema['minItems'], schema['maxItems']))
        self.assertIn('Never return text', result['requests'][0]['messages'][0]['content'])
        self.assertEqual(0, result['report']['cases'][0]['changes']['text_changed'])
        self.assertEqual(1, result['report']['cases'][0]['changes']['speaker_changed'])

    def test_local_selected_mode_reaches_real_sdk_schema_and_source_binding(self):
        result = run_native_benchmark()
        self.assert_labels_request(result)
        self.assertEqual('fixture-local', result['requests'][0]['model'])
        self.assertEqual([], result['envelopes'])

    def test_remote_stdin_replay_keeps_selected_mode_and_actual_worker_schema(self):
        result = run_native_benchmark('thunder')
        self.assert_labels_request(result)
        self.assertEqual('fixture-remote', result['requests'][0]['model'])
        envelope = result['envelopes'][0]
        self.assertTrue(envelope['payload']['params']['review_labels_only'])
        self.assertEqual(SOURCE, envelope['payload']['fixtures'][0]['original'])
        self.assertEqual('http://localhost:1234/v1', envelope['payload']['llm_config']['base_url'])
        self.assertNotIn('And yet', ' '.join(envelope['command']))
        self.assertIn('--payload-stdin', envelope['command'][-1])

    def test_optional_annotations_survive_benchmark_transports_and_source_binding(self):
        annotations = {'type': 'SPOKEN', 'spoken': True, 'speaker_gender': 'female',
                       'speaker_age_group': 'adult', 'speaker_ageless': False}
        for target in ('local', 'thunder'):
            for indexed in (False, True):
                with self.subTest(target=target, indexed=indexed):
                    corrected = {'speaker': 'ALICE', 'instruct': 'Gently.', **annotations}
                    corrected.update({'n': 0} if indexed else {'text': SOURCE[0]['text']})
                    unchanged = LABELS[0] if indexed else SOURCE[1]
                    result = run_native_benchmark(target, indexed, [corrected, unchanged])
                    self.assertEqual('passed', result['report']['cases'][0]['status'])
                    self.assertEqual([{**SOURCE[0], 'speaker': 'ALICE', 'instruct': 'Gently.',
                                       **annotations}, SOURCE[1]], result['outputs'][0])
                    self.assertEqual('accepted', result['report']['cases'][0]['attempts'][0]['outcome'])

    def test_invalid_annotations_are_rejected_by_benchmark_transports(self):
        for target in ('local', 'thunder'):
            for indexed in (False, True):
                with self.subTest(target=target, indexed=indexed):
                    corrected = {'speaker': 'ALICE', 'instruct': 'Gently.', 'spoken': 'true'}
                    corrected.update({'n': 0} if indexed else {'text': SOURCE[0]['text']})
                    unchanged = LABELS[0] if indexed else SOURCE[1]
                    result = run_native_benchmark(target, indexed, [corrected, unchanged])
                    self.assertEqual('failed', result['report']['cases'][0]['status'])
                    self.assertEqual([None], result['outputs'])

    def test_default_and_explicit_false_still_request_classic_review(self):
        for target in ('local', 'thunder'):
            for mode in (None, False):
                with self.subTest(target=target, mode=mode):
                    result = run_native_benchmark(target, mode)
                    self.assertEqual('passed', result['report']['cases'][0]['status'])
                    self.assertFalse(result['params'][0].review_labels_only)
                    body = result['requests'][0]
                    self.assertEqual('Classic fixture reviewer.\n\n' + review.REVIEW_CLASSIC_RESPONSE_RULE,
                                     body['messages'][0]['content'])
                    rows = json.loads(body['messages'][1]['content'].split('TARGET\n', 1)[1])
                    self.assertEqual(SOURCE, rows)
                    self.assertNotIn('n', rows[0])

    def test_classic_worker_and_local_benchmark_reject_unverified_boundary_partitions(self):
        source = [{'speaker': 'NARRATOR', 'text': 'Rain fell. Please open the gate.',
                   'instruct': '', 'spoken': False, 'source_span': [0, 32], 'pause_after': 700},
                  {'speaker': 'ALICE', 'text': 'Take the blue cup.', 'instruct': '',
                   'spoken': True, 'source_span': [34, 52]}]
        reply = [{'speaker': 'NARRATOR', 'text': 'Rain fell.', 'instruct': ''},
                 {'speaker': 'ALICE', 'text': 'Please open the gate. Take the blue cup.', 'instruct': ''}]
        for target in ('local', 'thunder'):
            with self.subTest(target=target), patch.object(sys.modules[__name__], 'SOURCE', source):
                result = run_native_benchmark(target, False, reply)
                self.assertEqual('failed', result['report']['cases'][0]['status'])
                self.assertEqual(0, result['report']['cases'][0]['entry_count'])
                if target == 'local':
                    self.assertIsNone(result['outputs'][0])
                attempt = result['report']['cases'][0]['attempts'][0]
                self.assertEqual('quality_rejected', attempt['outcome'])
                self.assertIn('review_source_span_unverified', attempt['failure_codes'])

    def test_selected_mode_keeps_index_and_label_validation(self):
        for reply, code in ((LABELS[:1], 'review_label_alignment'),
                            ([LABELS[0], LABELS[0]], 'review_label_alignment'),
                            ([{**LABELS[0], 'speaker': ''}, LABELS[1]], 'review_label_value')):
            with self.subTest(code=code):
                result = run_native_benchmark(reply=reply)
                case = result['report']['cases'][0]
                self.assertEqual('failed', case['status'])
                self.assertIn(code, case['attempts'][0]['failure_codes'])
                self.assertEqual([None], result['outputs'])

    def test_cache_revision_does_not_change_execution_or_preflight_identity(self):
        for target in ("local", "thunder"):
            for stage in ("script_generation", "script_review", "persona_generation", "nickname_detection"):
                with self.subTest(target=target, stage=stage):
                    manifest = {"schema_version": 1, "stage": stage, "targets": [target],
                                "fixtures": [{"id": "fixture", "sha256": "fixture"}]}
                    base = core.build_environment_fingerprint(target, ENVIRONMENT)
                    config = get_config(False)
                    original = core.get_benchmark_execution_environment(manifest, base, config)
                    profile = config["llm_remote" if target == "thunder" else "llm_local"]
                    for revision in ("1" * 32, "2" * 32):
                        profile["concurrency_revision"] = revision
                        current = core.get_benchmark_execution_environment(manifest, base, config)
                        self.assertEqual(original, current)
                        self.assertEqual(core.get_benchmark_preflight_id(manifest, {target: original}),
                                         core.get_benchmark_preflight_id(manifest, {target: current}))

    def test_mode_changes_local_and_remote_execution_and_preflight_identity(self):
        manifest = {'schema_version': 1, 'stage': 'script_review', 'targets': ['local'],
                    'fixtures': [{'id': 'fixture', 'sha256': 'fixture'}]}
        for target in ('local', 'thunder'):
            with self.subTest(target=target):
                manifest['targets'] = [target]
                base = core.build_environment_fingerprint(target, ENVIRONMENT)
                config = get_config(False)
                before = copy.deepcopy(config)
                classic = core.get_benchmark_execution_environment(manifest, base, config)
                config['prompts']['review_labels_only'] = True
                labels = core.get_benchmark_execution_environment(manifest, base, config)
                self.assertNotEqual(classic['details']['execution_sha256'], labels['details']['execution_sha256'])
                self.assertNotEqual(core.get_benchmark_preflight_id(manifest, {target: classic}),
                                    core.get_benchmark_preflight_id(manifest, {target: labels}))
                config['prompts']['review_labels_only'] = False
                self.assertEqual(classic, core.get_benchmark_execution_environment(manifest, base, config))
                self.assertEqual(before, config)
                self.assertNotIn('execution_sha256', base['details'])

    def test_classic_identity_is_stable_for_absent_false_and_nonreview_stages(self):
        for target in ('local', 'thunder'):
            for stage in ('script_review', 'script_generation', 'persona_generation'):
                with self.subTest(target=target, stage=stage):
                    manifest = {'stage': stage}
                    base = core.build_environment_fingerprint(target, ENVIRONMENT)
                    absent = core.get_benchmark_execution_environment(manifest, base, get_config(None))
                    off = core.get_benchmark_execution_environment(manifest, base, get_config(False))
                    self.assertEqual(absent, off)
                    if stage != 'script_review':
                        self.assertEqual(off, core.get_benchmark_execution_environment(manifest, base, get_config(True)))

    def test_report_resume_refuses_completed_cases_from_the_other_mode_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = {'schema_version': 1, 'stage': 'script_review', 'targets': ['local'],
                        'fixtures': [{'id': 'fixture', 'sha256': 'fixture'}]}
            base = core.build_environment_fingerprint('local', ENVIRONMENT)
            classic = core.get_benchmark_execution_environment(manifest, base, get_config(False))
            labels = core.get_benchmark_execution_environment(manifest, base, get_config(True))
            path = Path(directory, 'report.json')
            report = core.build_benchmark_report(manifest, classic)
            report['cases'] = [{'fixture_id': 'fixture', 'repetition': 1, 'status': 'passed'}]
            core.save_benchmark_report(str(path), report)
            before = path.read_bytes()
            self.assertEqual(report, core.load_resumable_benchmark_report(str(path), manifest, classic))
            with self.assertRaisesRegex(ValueError, 'environment changed'):
                core.load_resumable_benchmark_report(str(path), manifest, labels)
            self.assertEqual(before, path.read_bytes())

    def test_actual_start_route_refuses_mode_changed_since_preflight_before_claim(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            config_path = Path(directory, 'config.json')
            config_path.write_text(json.dumps(get_config(False)))
            manifest = {'schema_version': 1, 'stage': 'script_review', 'targets': ['local'],
                        'fixtures': [{'id': 'fixture', 'sha256': 'fixture'}]}
            stack.enter_context(patch.object(routes, 'CONFIG_PATH', str(config_path)))
            stack.enter_context(patch.object(routes, 'check_global_gpu_lock'))
            stack.enter_context(patch.object(routes, 'collect_local_environment',
                return_value=core.build_environment_fingerprint('local', ENVIRONMENT)))
            claim = stack.enter_context(patch.object(routes, 'reserve_background_task'))
            register = stack.enter_context(patch.object(routes, 'register_claimed_background_task'))
            api = FastAPI(); api.include_router(routes.router)
            client = stack.enter_context(TestClient(api))
            preflight = client.post('/api/benchmark/preflight', json={'manifest': manifest})
            self.assertEqual(200, preflight.status_code, preflight.text)
            config_path.write_text(json.dumps(get_config(True)))
            response = client.post('/api/benchmark/start', json={
                'manifest': manifest, 'preflight_id': preflight.json()['preflight_id']})
            self.assertEqual(409, response.status_code, response.text)
            claim.assert_not_called(); register.assert_not_called()

    def test_production_cli_keeps_configured_mode_and_explicit_cli_override(self):
        for mode, arguments, expected in ((True, [], True), (False, ['--labels-only'], True),
                                          (False, [], False)):
            with self.subTest(mode=mode, args=arguments), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                path = Path(directory, 'fixture.json'); path.write_text(json.dumps(SOURCE))
                config = get_config(mode)
                observed = []
                def run_review(_client, _model, entries, _batch, _total, params, **kwargs):
                    observed.append(params.review_labels_only)
                    return entries
                stack.enter_context(patch.object(review.sys, 'argv', ['review', '--input', str(path)] + arguments))
                for name, value in (('get_runtime_data_dir', directory), ('load_app_config', config),
                                    ('ensure_ideal_settings', (True, {}, 'fixture')),
                                    ('get_active_llm_config', config['llm_remote']),
                                    ('get_current_status', {'loaded': False}), ('make_run_client', object()),
                                    ('ensure_cached_or_benchmarked_concurrency', 1)):
                    stack.enter_context(patch.object(review, name, return_value=value))
                stack.enter_context(patch.object(review, 'review_batch', side_effect=run_review))
                stack.enter_context(patch.object(review, 'wait_for_vram_headroom', side_effect=AssertionError('No GPU probes')))
                stack.enter_context(redirect_stdout(io.StringIO()))
                review.main()
                self.assertEqual([expected], observed)

    def test_shared_mode_read_does_not_mutate_or_treat_raw_truthy_values_as_true(self):
        for mode in (None, False, True, 'true', 1):
            with self.subTest(mode=mode):
                config = get_config(mode); before = copy.deepcopy(config)
                self.assertEqual(mode is True, get_review_labels_only(config))
                self.assertEqual(before, config)


class ThunderLegacyReportResumeTests(unittest.TestCase):
    """An old Thunder report may be resumed only when dispatch did not change."""

    def _pre_fix_digest(self, manifest, config):
        """The report digest as the pre-fix code hashed it (public profile)."""
        from completed_review_receipt import (REVIEW_ALGORITHM_VERSION,
                                              REVIEW_SOURCE_SPAN_ALGORITHM_VERSION)
        from llm_provider import resolve_historical_api_key
        public = core.get_benchmark_llm_config(config, 'thunder')
        omitted = {'concurrency', 'concurrency_for', 'concurrency_environment', 'concurrency_revision'}
        identity = {'version': 1, 'stage': manifest['stage'],
                    'ssh_alias': config.get('llm_remote_ssh'),
                    'profile': {key: value for key, value in public.items() if key not in omitted},
                    'resolved_api_key': resolve_historical_api_key(public.get('api_key'))}
        generation = config.get('generation') or {}
        identity['generation'] = {key: generation.get(key) for key in (
            'max_tokens', 'temperature', 'top_p', 'top_k', 'min_p', 'presence_penalty', 'banned_tokens')}
        prompts = config.get('prompts') or {}
        identity['prompts'] = {key: prompts.get(key) for key in (
            'review_system_prompt', 'review_user_prompt')}
        if get_review_labels_only(config):
            identity['prompts']['review_labels_only'] = True
        identity['review_algorithm'] = REVIEW_ALGORITHM_VERSION
        identity['review_source_span_algorithm'] = REVIEW_SOURCE_SPAN_ALGORITHM_VERSION
        return core._stable_hash(identity)

    def _case(self, config):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'fixture.json'
            source.write_text(json.dumps(SOURCE))
            manifest = build_script_review_manifest(
                [{'path': str(source), 'entry_starts': [1]}], tmp, targets=['thunder'])
            base = core.build_environment_fingerprint('thunder', ENVIRONMENT)
            return manifest, core.get_benchmark_execution_environment(
                manifest, base, config)

    def test_literal_key_thunder_report_resumes_without_mutating_canonical_identity(self):
        config = get_config(True)
        manifest, current = self._case(config)
        self.assertNotIn('legacy_sha256', current['details'],
                         'the compatibility digest must stay out of canonical identity')
        old = copy.deepcopy(current)
        old['details']['execution_sha256'] = self._pre_fix_digest(manifest, config)
        old['sha256'] = core._stable_hash({'target': 'thunder', 'details': old['details']})
        self.assertEqual(current['legacy_sha256'], old['sha256'],
                         'the offered legacy digest must be the historical one')
        report = core.build_benchmark_report(manifest, current)
        report['environment'] = old
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            core.save_benchmark_report(path, report)
            resumed = core.load_resumable_benchmark_report(path, manifest, current)
        self.assertEqual([], resumed['cases'])
        self.assertEqual(old['sha256'], resumed['environment']['sha256'])

    def test_changed_profile_and_keyless_public_profile_refuse_legacy_resume(self):
        config = get_config(True)
        manifest, current = self._case(config)
        old = copy.deepcopy(current)
        old['details']['execution_sha256'] = self._pre_fix_digest(manifest, config)
        old['sha256'] = core._stable_hash({'target': 'thunder', 'details': old['details']})
        report = core.build_benchmark_report(manifest, current)
        report['environment'] = old
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            core.save_benchmark_report(path, report)
            changed = copy.deepcopy(config)
            changed['llm_remote']['api_key'] = 'changed-fixture'
            _, changed_environment = self._case(changed)
            with self.assertRaisesRegex(ValueError, 'environment changed'):
                core.load_resumable_benchmark_report(path, manifest, changed_environment)
            keyless = copy.deepcopy(config)
            keyless['llm_remote']['api_key'] = ''
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-synthetic'}, clear=False):
                _, keyless_environment = self._case(keyless)
            self.assertNotIn('legacy_sha256', keyless_environment,
                             'a keyless public profile changed the worker credential')
            with self.assertRaisesRegex(ValueError, 'environment changed'):
                core.load_resumable_benchmark_report(path, manifest, keyless_environment)
