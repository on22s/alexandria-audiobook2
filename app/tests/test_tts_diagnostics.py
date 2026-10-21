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
