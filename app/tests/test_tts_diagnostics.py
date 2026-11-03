"""Operational observations are bounded and never accept caller content."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
import tts
import tts_diagnostics as diagnostics


class TtsDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        with diagnostics._LOCK:
            saved = list(diagnostics._RECORDS)
            diagnostics._RECORDS.clear()
        def restore():
            with diagnostics._LOCK:
                diagnostics._RECORDS.clear()
                diagnostics._RECORDS.extend(saved)
        self.addCleanup(restore)

    def records(self):
        return diagnostics.get_tts_diagnostics_bundle()['sections']['tts']['records']

    def test_native_render_retains_audio_and_excludes_every_caller_field(self):
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        secret = 'PRIVATE_BOOK_SPEAKER_sk-' + 'q' * 25
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (secret + '.wav')
            def render(*_args, **_kwargs):
                with diagnostics.measure_tts_stage_time('inference_decode'):
                    audio = np.full(2400, .1)
                engine._save_wav(audio, 24000, path)
                return True
            with patch.object(engine, '_local_generate_custom', side_effect=render):
                result = engine.generate_voice_result(secret, secret, secret,
                    {secret: {'type': 'custom', 'voice': secret}}, str(path))
            self.assertTrue(result.success)
            self.assertEqual(2400, sf.info(path).frames)
        bundle = diagnostics.get_tts_diagnostics_bundle()
        encoded = json.dumps(bundle)
        self.assertNotIn(secret, encoded)
        self.assertNotIn(directory, encoded)
        self.assertNotIn('sk-', encoded)
        record = self.records()[0]
        self.assertEqual('completed', record['outcome'])
        self.assertAlmostEqual(.1, record['audio_seconds'])
        self.assertEqual(1, record['audio_files'])
        self.assertIsNone(record['stages_seconds']['external_request'])
        self.assertGreaterEqual(record['stages_seconds']['audio_validation_save'], 0)
        self.assertEqual(1, len(self.records()))  # nested renders are one observation

    def test_measured_stage_clock_and_exception_do_not_change_the_failure(self):
        engine = tts.TTSEngine({'tts': {'mode': 'external'}})
        error = RuntimeError('PRIVATE_BOOK /home/private/secret.txt api_key=private')
        @diagnostics.capture_tts_operation('generate_voice')
        def render(self):
            with diagnostics.measure_tts_stage_time('external_request'):
                raise error
        with patch.object(diagnostics.time, 'monotonic', side_effect=[10, 11, 14, 15]):
            with self.assertRaises(RuntimeError) as caught:
                render(engine)
        self.assertIs(error, caught.exception)
        record = self.records()[0]
        self.assertEqual(5, record['elapsed_seconds'])
        self.assertEqual(3, record['stages_seconds']['external_request'])
        self.assertEqual('failed', record['outcome'])
        self.assertIsNone(record['audio_seconds'])
        self.assertNotIn('PRIVATE_BOOK', json.dumps(self.records()))

    def test_unobserved_and_closed_stages_do_not_retain_exceptions(self):
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        contexts = [copy_context()]
        @diagnostics.capture_tts_operation('generate_voice')
        def render(self):
            contexts.append(copy_context())
            return True
        self.assertTrue(render(engine))
        before = self.records()
        def fail():
            error = tts.GeneratedAudioError('PRIVATE_UNTRACKED_AUDIO')
            with patch.object(tts, 'get_tts_failure_classification', side_effect=AssertionError('unobserved classification')):
                with self.assertRaises(tts.GeneratedAudioError) as caught:
                    with diagnostics.measure_tts_stage_time('audio_validation_save'):
                        raise error
            self.assertIs(error, caught.exception)
            self.assertIsNone(diagnostics._ACTIVE_STAGE.get())
            self.assertIsNone(diagnostics._ESCAPED_STAGE.get())
        for context in contexts:
            context.run(fail)
        self.assertEqual(before, self.records())

    def test_unwound_failure_keeps_specific_category_and_innermost_stage(self):
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        @diagnostics.capture_tts_operation('generate_voice')
        def render(self, error, handled):
            try:
                with diagnostics.measure_tts_stage_time('model_preparation'):
                    with diagnostics.measure_tts_stage_time('inference_decode'):
                        raise error
            except Exception as caught:
                if not handled:
                    raise
                tts.apply_tts_failure(caught)
                return False
        for error, category in ((RuntimeError('CUDA out of memory PRIVATE_BOOK'), 'out_of_memory'),
                                (TimeoutError('PRIVATE_TIMEOUT'), 'timeout'),
                                (tts.GeneratedAudioError('PRIVATE_AUDIO'), 'invalid_audio'),
                                (RuntimeError('PRIVATE_GENERIC'), 'generation_error')):
            for handled in (False, True):
                with self.subTest(category=category, handled=handled):
                    if handled:
                        self.assertFalse(render(engine, error, True))
                    else:
                        with self.assertRaises(type(error)) as caught:
                            render(engine, error, False)
                        self.assertIs(error, caught.exception)
                    record = self.records()[-1]
                    self.assertEqual(category, record['failure_category'])
                    self.assertEqual('inference_decode', record['failure_stage'])
                    self.assertEqual([{'category': category, 'stage': 'inference_decode'}], record['failure_groups'])
                    self.assertNotIn('PRIVATE_', json.dumps(record))
                    self.assertIsNone(diagnostics._ESCAPED_STAGE.get())
        class UnprintableError(Exception):
            def __str__(self):
                raise ValueError('PRIVATE_UNPRINTABLE')
        error = UnprintableError()
        with self.assertRaises(UnprintableError) as caught:
            render(engine, error, False)
        self.assertIs(error, caught.exception)
        self.assertEqual('generation_error', self.records()[-1]['failure_category'])
        self.assertEqual('inference_decode', self.records()[-1]['failure_stage'])

    def test_parallel_calls_isolate_outcomes_and_late_updates_are_ignored(self):
        engine = tts.TTSEngine({'tts': {'mode': 'external'}})
        barrier = threading.Barrier(2)
        contexts = []
        @diagnostics.capture_tts_operation('generate_voice')
        def render(self, success):
            contexts.append(copy_context())
            diagnostics.record_tts_cache(success)
            barrier.wait(timeout=3)
            return success
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda success: render(engine, success), [True, False]))
        self.assertEqual([True, False], results)
        before = self.records()
        contexts[0].run(diagnostics.record_tts_failure, 'timeout')
        contexts[0].run(diagnostics.record_tts_cache, True)
        contexts[0].run(diagnostics.record_tts_reference_cache, {'hits': 99}, {'hits': 99})
        self.assertEqual(before, self.records())
        outcomes = {record['outcome']: record for record in before}
        self.assertEqual(1, outcomes['completed']['reference_cache_hits'])
        self.assertEqual(1, outcomes['failed']['reference_cache_misses'])

    def test_batch_counts_and_record_bounds_do_not_retain_failed_text(self):
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        @diagnostics.capture_tts_operation('generate_batch')
        def render(self):
            return {'completed': [0], 'failed': [(1, 'PRIVATE_TEXT')], 'extra': 'PRIVATE_TEXT'}
        for _ in range(55):
            result = render(engine)
        self.assertEqual('PRIVATE_TEXT', result['extra'])
        self.assertEqual(50, len(self.records()))
        record = self.records()[-1]
        self.assertEqual(('partial', 1, 1), (record['outcome'], record['completed_items'], record['failed_items']))
        payload = json.dumps(diagnostics.get_tts_diagnostics_bundle())
        self.assertLessEqual(len(payload.encode()), 65536)
        self.assertNotIn('PRIVATE_TEXT', payload)

    def test_endpoint_reads_no_observations_without_loading_engine(self):
        from routers.system import get_tts_diagnostics
        with patch.object(tts.TTSEngine, '__init__', side_effect=AssertionError('model initialization')):
            result = get_tts_diagnostics()
        self.assertEqual([], result['sections']['tts']['records'])

    def test_export_projects_only_allowlisted_values_even_in_known_fields(self):
        private = 'PRIVATE_BOOK sk-abcdefghijklmnopqrst /home/user/book C:\\Users\\private\\ref.wav https://private.invalid/token'
        runtime = {'revision': private, 'branch': private, 'python': private,
                   'dirty': private, 'platform': {'system': private, 'machine': private, 'release': private},
                   'packages': {'torch': private, 'other': private}, 'error': private}
        residency = {'state': 'observed', 'mode': private, 'configured_device': private,
                     'residents': [{'family': private, 'device': private,
                                    'load_capture': {'scope': private, 'error': private}, 'dtype': private}],
                     'release_events': [private], 'error': private}
        record = {name: private for name in ('operation', 'backend', 'outcome', 'failure_category',
                                            'failure_stage', 'elapsed_seconds', 'audio_seconds')}
        record.update(stages_seconds={private: private}, reference_cache={private: private}, error=private)
        with diagnostics._LOCK:
            diagnostics._RECORDS.append(record)
        bundle = diagnostics.get_tts_diagnostics_bundle(runtime=runtime, residency=residency,
            assets=[{'model_id': private, 'state': private}], device={'state': private, 'gpu_backend': private})
        payload = json.dumps(bundle)
        for secret in ('PRIVATE_BOOK', 'sk-', '/home/user', 'private.invalid', 'ref.wav'):
            self.assertNotIn(secret, payload)
        self.assertIsNone(bundle['sections']['tts']['runtime']['revision'])
        self.assertIsNone(bundle['sections']['tts']['residency']['residents'][0]['dtype'])
        self.assertEqual('unknown', self.records()[0]['operation'])
        self.assertLessEqual(len(payload.encode()), 65536)
        self.assertIsNone(diagnostics._get_number(10**1000))

    def test_nested_private_errors_preserve_identity_and_have_stable_safe_fingerprints(self):
        engine = tts.TTSEngine({'tts': {'mode': 'external'}})
        @diagnostics.capture_tts_operation('generate_voice')
        def render(self, error):
            with diagnostics.measure_tts_stage_time('external_request'):
                raise error
        for text in ('PRIVATE_BOOK /Users/private/book token=secret', 'C:\\Users\\private\\voice.wav PRIVATE_PROMPT'):
            cause = ValueError(text)
            error = RuntimeError(text)
            error.__cause__ = cause
            with self.assertRaises(RuntimeError) as caught:
                render(engine, error)
            self.assertIs(error, caught.exception)
            self.assertIs(cause, caught.exception.__cause__)
        first, second = self.records()
        self.assertEqual(first['error_fingerprint'], second['error_fingerprint'])
        self.assertRegex(first['error_fingerprint'], r'^tts-v1-[0-9a-f]{16}$')
        self.assertEqual('external_request', first['failure_stage'])
        self.assertEqual('generation_error', first['failure_category'])
        self.assertNotIn('PRIVATE_', json.dumps(self.records()))
        moved = dict(first, failure_stage='model_preparation',
                     failure_groups=[{'category': 'generation_error', 'stage': 'model_preparation'}])
        self.assertNotEqual(first['error_fingerprint'], diagnostics._get_report_record(moved)['error_fingerprint'])
        changed = dict(first, failure_category='timeout')
        self.assertNotEqual(first['error_fingerprint'], diagnostics._get_report_record(changed)['error_fingerprint'])

    def test_collection_failure_is_explicit_and_does_not_hide_saved_error(self):
        from routers import system
        with diagnostics._LOCK:
            diagnostics._RECORDS.append({'operation': 'generate_voice', 'backend': 'local',
                                        'outcome': 'failed', 'failure_category': 'timeout'})
        with patch.object(system, 'get_runtime_info', side_effect=RuntimeError('PRIVATE_COLLECTION_ERROR')), \
             patch.object(system.project_manager, 'get_model_residency', return_value={'state': 'not_initialized'}), \
             patch.object(tts.TTSEngine, 'get_local_model_inventory', return_value=[]), \
             patch('device_utils.get_loaded_torch_device_observation', return_value={'state': 'runtime_not_loaded'}), \
             patch.object(tts.TTSEngine, '__init__', side_effect=AssertionError('must not load')):
            bundle = system.get_tts_diagnostics()
        report = bundle['sections']['tts']
        self.assertEqual('unavailable', report['collection']['runtime'])
        self.assertEqual('collected', report['collection']['residency'])
        self.assertEqual('timeout', report['records'][0]['failure_category'])
        self.assertIsNone(report['runtime']['revision'])
        self.assertNotIn('PRIVATE_', json.dumps(bundle))

    def test_missing_dependencies_and_checkout_identity_are_qualified(self):
        import runtime_info
        runtime_info.get_runtime_info.cache_clear()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ', {'ALEXANDRIA_BUILD_COMMIT': 'a' * 40}), \
             patch.object(runtime_info.metadata, 'version', side_effect=runtime_info.metadata.PackageNotFoundError):
            runtime = runtime_info.get_runtime_info(directory)
        report = diagnostics.get_tts_diagnostics_bundle(runtime=runtime)['sections']['tts']['runtime']
        self.assertEqual('a' * 40, report['revision'])
        self.assertEqual('build', report['revision_scope'])
        self.assertIsNone(report['dirty'])
        self.assertTrue(all(value is None for value in report['packages'].values()))
        runtime_info.get_runtime_info.cache_clear()

    def test_platform_versions_and_safe_build_identifiers_survive_projection(self):
        for release, machine in (('6.8.0-45-generic', 'x86_64'), ('6.11.4-301.fc41.x86_64', 'riscv64'), ('10', 'ARM64')):
            report = diagnostics._get_report_runtime({'platform': {'system': 'Linux', 'release': release, 'machine': machine},
                'revision': 'release-2026-07', 'revision_source': 'environment', 'revision_scope': 'build',
                'packages': {'torch': '2.10.0a0+git8f3a1c0', 'transformers': '4.46.0.dev0', 'peft': '2.1.0.post1'}})
            self.assertEqual(release.split('-')[0], report['platform']['release'])
            self.assertEqual(machine, report['platform']['machine'])
            self.assertEqual('release-2026-07', report['revision'])
            self.assertEqual('2.10.0a0', report['packages']['torch'])
            self.assertEqual('4.46.0.dev0', report['packages']['transformers'])
            self.assertEqual('2.1.0.post1', report['packages']['peft'])
        self.assertEqual('2.9.0+rocmsdk20250901', diagnostics._get_version('2.9.0+rocmsdk20250901'))
        self.assertEqual('2.10.0+xpu', diagnostics._get_version('2.10.0+xpu'))
        self.assertEqual('2.9.0', diagnostics._get_version('2.9.0+PRIVATE_BOOK'))
        rejected = diagnostics._get_report_runtime({'revision': 'PRIVATE_BOOK', 'revision_source': 'environment', 'revision_scope': 'build'})
        self.assertEqual('unavailable', rejected['revision_source'])
        self.assertIsNone(rejected['revision_scope'])

    def test_cancellations_remain_interrupted_without_error_fingerprints(self):
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        @diagnostics.capture_tts_operation('generate_voice')
        def render(self, error):
            with diagnostics.measure_tts_stage_time('inference_decode'):
                raise error
        for error_type in (tts.AudioGenerationCancelled, tts.ExportCancelled):
            error = error_type('PRIVATE_CANCELLATION')
            with self.assertRaises(error_type) as caught:
                render(engine, error)
            self.assertIs(error, caught.exception)
            record = self.records()[-1]
            self.assertEqual('interrupted', record['outcome'])
            self.assertIsNone(record['error_fingerprint'])
            self.assertIsNone(record['failure_category'])
            self.assertIsNone(record['failure_stage'])

    def test_parallel_failures_have_order_independent_category_stage_groups(self):
        engine = tts.TTSEngine({'tts': {'mode': 'external'}})
        @diagnostics.capture_tts_operation('generate_batch')
        def render(self, reverse):
            first_done = threading.Event()
            def worker(category, stage, first):
                if not first and not first_done.wait(3):
                    raise AssertionError('first worker did not complete')
                try:
                    with diagnostics.measure_tts_stage_time(stage):
                        raise TimeoutError('PRIVATE_TIMEOUT') if category == 'timeout' else tts.GeneratedAudioError('PRIVATE_AUDIO')
                except Exception as error:
                    tts.apply_tts_failure(error)
                if first:
                    first_done.set()
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(copy_context().run, worker, category, stage, first)
                    for category, stage, first in (('timeout', 'external_request', not reverse),
                                                   ('invalid_audio', 'audio_validation_save', reverse))]
                for future in futures:
                    future.result(timeout=4)
            return {'completed': [0], 'failed': [(1, 'PRIVATE_TEXT')]}
        render(engine, False)
        render(engine, True)
        first, second = self.records()
        self.assertEqual(first['error_fingerprint'], second['error_fingerprint'])
        self.assertEqual('multiple_failures', first['failure_category'])
        self.assertIsNone(first['failure_stage'])
        self.assertEqual([{'category': 'invalid_audio', 'stage': 'audio_validation_save'},
                          {'category': 'timeout', 'stage': 'external_request'}], first['failure_groups'])
        changed = dict(first, failure_groups=[{'category': 'timeout', 'stage': 'model_preparation'}])
        self.assertNotEqual(first['error_fingerprint'], diagnostics._get_report_record(changed)['error_fingerprint'])

    def test_maximum_failure_groups_stay_bounded_without_dropping_report(self):
        groups = [{'category': category, 'stage': stage} for category in diagnostics._FAILURES for stage in (*diagnostics._STAGES, None)]
        record = {'operation': 'generate_voice', 'backend': 'local', 'outcome': 'failed', 'failure_groups': groups,
                  'reference_cache': {tier: {name: 10**15 for name in ('hits', 'misses', 'evictions', 'bypasses',
                    'cleared_entries', 'entries', 'charged_bytes', 'max_entries', 'max_bytes')}
                    for tier in ('prepared_audio', 'clone_prompts')}}
        with diagnostics._LOCK:
            diagnostics._RECORDS.extend(dict(record) for _ in range(50))
        bundle = diagnostics.get_tts_diagnostics_bundle()
        self.assertLessEqual(len(json.dumps(bundle).encode()), 65536)
        report = bundle['sections']['tts']
        self.assertGreater(report['records_omitted'], 0)
        self.assertEqual(50, report['records_omitted'] + len(report['records']))
        self.assertGreater(len(report['records']), 0)

    def test_configuration_is_read_without_loading_or_mutating_a_book(self):
        from project import ProjectManager
        from routers import system
        import socket
        with tempfile.TemporaryDirectory() as directory:
            manager = ProjectManager(directory)
            config_path = Path(manager.config_path)
            config_path.write_text(json.dumps({'tts': {'mode': 'external', 'device': ' ROCm ',
                'url': 'https://PRIVATE_ENDPOINT', 'api_key': 'PRIVATE_TOKEN'}}), encoding='utf-8')
            before = config_path.read_bytes()
            files_before = sorted(path.relative_to(directory).as_posix() for path in Path(directory).rglob('*'))
            with patch.object(system, 'project_manager', manager), \
                 patch.object(manager, 'get_engine', side_effect=AssertionError('no engine')), \
                 patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value=None), \
                 patch.object(socket.socket, 'connect', side_effect=AssertionError('no network')), \
                 manager._engine_lock:
                bundle = system.get_tts_diagnostics()
            self.assertEqual(before, config_path.read_bytes())
            self.assertEqual(files_before, sorted(path.relative_to(directory).as_posix() for path in Path(directory).rglob('*')))
            self.assertIsNone(manager.engine)
            report = bundle['sections']['tts']
            self.assertEqual('busy', report['residency']['state'])
            self.assertEqual('external', report['configuration']['mode'])
            self.assertEqual('cuda', report['configuration']['device'])
            self.assertNotIn('PRIVATE_', json.dumps(bundle))
            config_path.write_text('not JSON PRIVATE_TEXT', encoding='utf-8')
            with patch.object(system, 'project_manager', manager):
                damaged = system.get_tts_diagnostics()['sections']['tts']
            self.assertEqual('unavailable', damaged['collection']['configuration'])
            self.assertIsNone(damaged['configuration']['mode'])

    def test_all_collectors_can_fail_without_erasing_recorded_failures(self):
        from routers import system
        with diagnostics._LOCK:
            diagnostics._RECORDS.append({'operation': 'generate_voice', 'outcome': 'failed', 'failure_category': 'timeout'})
        with patch.object(system.project_manager, 'get_model_residency', side_effect=RuntimeError('PRIVATE')), \
             patch.object(system, 'get_runtime_info', side_effect=RuntimeError('PRIVATE')), \
             patch.object(tts.TTSEngine, 'get_local_model_inventory', side_effect=RuntimeError('PRIVATE')), \
             patch('device_utils.get_loaded_torch_device_observation', side_effect=RuntimeError('PRIVATE')), \
             patch('config_settings.load_app_config_result', side_effect=RuntimeError('PRIVATE')):
            report = system.get_tts_diagnostics()['sections']['tts']
        self.assertEqual({'unavailable'}, set(report['collection'].values()))
        self.assertEqual('timeout', report['records'][0]['failure_category'])
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_allowlists_cover_actual_operation_and_model_producers(self):
        import ast
        tree = ast.parse(Path(tts.__file__).read_text(encoding='utf-8'))
        operations = {node.args[0].value for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == 'capture_tts_operation'}
        self.assertEqual(operations, diagnostics._OPERATIONS)
        with patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value=None):
            assets = tts.TTSEngine.get_local_model_inventory()
        projected = diagnostics.get_tts_diagnostics_bundle(assets=assets)['sections']['tts']['assets']
        self.assertEqual(set(tts.TTSEngine._LOCAL_MODEL_IDS.values()), {asset['model_id'] for asset in projected})

    def test_native_clone_cache_hits_misses_and_save_failure_are_observed(self):
        from tests.test_clone_reference_cache import Prompt
        import torch
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / 'reference.wav'
            sf.write(reference, np.full(2400, .1), 24000)
            class Model:
                def create_voice_clone_prompt(self, **kwargs):
                    return [Prompt(None, torch.ones(4), ref_text=kwargs['ref_text'])]
            config = {'S': {'type': 'clone', 'ref_audio': str(reference), 'ref_text': 'Private reference'}}
            @diagnostics.capture_tts_operation('generate_clone_voice')
            def render(self):
                with patch.object(engine, '_init_local_clone', return_value=Model()):
                    first = engine._get_clone_prompt('S', config)
                    second = engine._get_clone_prompt('S', config)
                return first is not second and torch.equal(first[0].ref_spk_embedding,
                                                           second[0].ref_spk_embedding)
            self.assertTrue(render(engine))
            self.assertEqual((1, 1), (self.records()[0]['reference_cache_hits'], self.records()[0]['reference_cache_misses']))
            snapshot = self.records()[0]['reference_cache']
            self.assertEqual(1, snapshot['clone_prompts']['entries'])
            self.assertEqual(1, snapshot['prepared_audio']['entries'])
            with patch.object(engine, '_local_generate_custom', side_effect=OSError('private disk failure')):
                result = engine.generate_voice_result('Private text', '', 'S', {'S': {'type':'custom'}}, str(Path(directory)/'out.wav'))
            self.assertFalse(result.success)
            self.assertEqual('failed', self.records()[-1]['outcome'])

    def test_reference_cache_observations_remain_bounded_and_content_free(self):
        from tests.test_clone_reference_cache import Prompt
        import torch
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / 'PRIVATE_REFERENCE.wav'
            sf.write(reference, np.full(2400, .1), 24000)
            class Model:
                def create_voice_clone_prompt(self, **kwargs):
                    return [Prompt(None, torch.ones(4), ref_text=kwargs['ref_text'])]
            model = Model()
            @diagnostics.capture_tts_operation('generate_clone_voice')
            def render(self):
                self._get_clone_prompt('PRIVATE_SPEAKER', {'PRIVATE_SPEAKER': {
                    'ref_audio': str(reference), 'ref_text': 'PRIVATE_REFERENCE_TEXT'}})
                return True
            with patch.object(engine, '_init_local_clone', return_value=model):
                for _ in range(55):
                    self.assertTrue(render(engine))
        records = self.records()
        payload = json.dumps(diagnostics.get_tts_diagnostics_bundle())
        self.assertEqual(50, len(records))
        self.assertLessEqual(len(payload.encode()), 65536)
        self.assertNotIn('PRIVATE_', payload)
        self.assertNotIn(directory, payload)
        for record in records:
            for tier in record['reference_cache'].values():
                self.assertLessEqual(tier['entries'], tier['max_entries'])
                self.assertLessEqual(tier['charged_bytes'], tier['max_bytes'])

    def test_legacy_boolean_failure_still_records_a_safe_failure_category(self):
        engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        def fail(*_args, **_kwargs):
            tts.apply_tts_failure(MemoryError('PRIVATE_TEXT'))
            return False
        with patch.object(engine, '_local_generate_custom', side_effect=fail):
            self.assertFalse(engine.generate_voice('Private', '', 'S', {'S': {'type': 'custom'}}, 'unused.wav'))
        self.assertEqual('out_of_memory', self.records()[0]['failure_category'])
        self.assertNotIn('PRIVATE_TEXT', json.dumps(self.records()))
