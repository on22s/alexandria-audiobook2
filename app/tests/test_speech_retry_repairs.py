"""Fail-first lifecycle checks for the reviewed speech-retry integration."""
import copy
import gc
import json
import unittest
import uuid
import weakref
from unittest.mock import patch

from tests import test_speech_retry_integration as integration_fixture
from tests.test_speech_verification_receipt import get_report


class SpeechRetryRepairTests(unittest.TestCase):
    def fixture(self):
        return integration_fixture.SpeechRetryIntegrationTests().retry_fixture()

    def test_generic_audio_replacement_does_not_keep_displaced_attempt_history(self):
        from take_retention import get_captured_take
        with self.fixture() as (root, manager, before, prior, flags):
            with patch('speech_verification.verify_finished_speech',
                       side_effect=lambda path, text, *args, **kwargs: get_report(path, text)):
                self.assertTrue(manager.generate_chunk_audio(0)[0])
            current = manager.load_chunks()[0]
            captured = get_captured_take(current)
            self.assertTrue(captured['speech_retry_attempts'])
            source = root / 'restored.wav'
            source.write_bytes(prior)
            revision = uuid.uuid4().hex
            with manager._ensure_chunk_generation_publication(current):
                result = manager._update_chunk_fields_by_uid(current['uid'], expected_chunk=current,
                    audio_source=str(source), audio_revision=revision, status='done',
                    speech_verification=None, speech_verification_failure=None)
            self.assertEqual(revision, result['audio_revision'])
            self.assertIsNone(result.get('speech_retry_attempts'))
            self.assertNotEqual(revision, captured['speech_retry_attempts'][-1]['receipt']['audio_revision'])

    def test_quality_retry_uses_one_pronunciation_snapshot_and_next_request_refreshes(self):
        with self.fixture() as (root, manager, before, prior, flags):
            lexicon = root / 'retry-lexicon.json'
            lexicon.write_text('{}')
            texts = []
            def verify(path, text, *args, **kwargs):
                texts.append(text)
                if len(texts) == 1:
                    lexicon.write_text(json.dumps({'guide': 'helper'}))
                    return get_report(path, text, 'The calm guide closed.',
                                      status='corroborated_content_difference')
                return get_report(path, text)
            with patch('pronunciation.DEFAULT_PATH', str(lexicon)), \
                    patch('speech_verification.verify_finished_speech', side_effect=verify):
                self.assertTrue(manager.generate_chunk_audio(0)[0])
                self.assertEqual(2, len(texts))
                self.assertEqual(texts[0], texts[1])
                self.assertTrue(manager.generate_chunk_audio(0)[0])
            self.assertEqual(3, len(texts))
            self.assertIn('helper', texts[2])

    def test_engine_policy_accessor_is_used_for_a_real_engine_proxy(self):
        class Proxy:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def get_speech_retry_policy(self):
                return self.wrapped.get_speech_retry_policy()
        with self.fixture() as (root, manager, before, prior, flags):
            manager.engine = Proxy(manager.engine)
            with patch('speech_verification.verify_finished_speech',
                       side_effect=lambda path, text, *args, **kwargs: get_report(path, text)):
                result = manager.generate_chunk_audio(0)
            self.assertTrue(result[0], result[1])
            self.assertEqual('verified', manager.load_chunks()[0]['speech_retry_attempts'][-1]['outcome'])

    def test_completed_budgets_are_released_before_the_parallel_call_finishes(self):
        with self.fixture() as (root, manager, before, prior, flags):
            rows = manager.load_chunks()
            second = {**copy.deepcopy(rows[0]), 'id': 1, 'uid': 'line-1',
                      'audio_revision': 'prior-second', 'speech_verification': None}
            manager.save_chunks(rows + [second])
            references = {}
            original_create = manager._create_speech_retry_budget
            def create(chunk):
                budget = original_create(chunk)
                references[chunk['uid']] = weakref.ref(budget)
                return budget
            checkpoints = []
            def progress(completed, failed, total):
                gc.collect()
                released = sum(reference() is None for reference in references.values())
                checkpoints.append(released)
                self.assertGreaterEqual(released, completed)
            with patch.object(manager, '_create_speech_retry_budget', side_effect=create), \
                    patch('project._export_audio_segment', side_effect=RuntimeError), \
                    patch('speech_verification.verify_finished_speech',
                          side_effect=lambda path, text, *args, **kwargs: get_report(path, text)):
                result = manager.generate_chunks_parallel([0, 1], max_workers=1, progress_callback=progress)
            self.assertEqual([0, 1], sorted(result['completed']))
            self.assertEqual(2, checkpoints[-1])

    def test_cancelled_budget_admission_is_not_counted_as_provider_failure(self):
        from audio_generation_owner import AudioGenerationCancelled
        with self.fixture() as (root, manager, before, prior, flags):
            original_provider = manager.engine.generate_voice_result
            with patch.object(manager, '_create_speech_retry_budget', side_effect=AudioGenerationCancelled()), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider:
                result = manager.generate_chunks_parallel([0], max_workers=1)
            provider.assert_not_called()
            self.assertEqual([], result['failed'])
            self.assertEqual([], result['completed'])
            self.assertEqual(1, result['cancelled'])
            self.assertEqual(before['audio_revision'], manager.load_chunks()[0]['audio_revision'])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_callback_cancellation_during_audio_install_keeps_selected_prior_take(self):
        import project
        with self.fixture() as (root, manager, before, prior, flags):
            cancelled = {'value': False}
            original_sync = project.sync_adapter_directory
            def sync_then_cancel(path):
                original_sync(path)
                if str(path) == str(manager.voicelines_dir):
                    cancelled['value'] = True
            with patch('project.sync_adapter_directory', side_effect=sync_then_cancel), \
                    patch('speech_verification.verify_finished_speech',
                          side_effect=lambda path, text, *args, **kwargs: get_report(path, text)):
                result = manager.generate_chunks_parallel([0], max_workers=1,
                    cancel_check=lambda: cancelled['value'])
            self.assertTrue(cancelled['value'])
            self.assertEqual([], result['completed'])
            self.assertEqual([], result['failed'])
            self.assertEqual(1, result['cancelled'])
            self.assertEqual(before['audio_revision'], manager.load_chunks()[0]['audio_revision'])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_unowned_opted_in_callback_cancellation_is_not_a_provider_failure(self):
        from audio_generation_owner import ensure_audio_generation_owner
        with self.fixture() as (root, manager, before, prior, flags):
            original_provider = manager.engine.generate_voice_result
            with ensure_audio_generation_owner(None), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original_provider) as provider:
                result = manager.generate_chunks_parallel([0], max_workers=1, cancel_check=lambda: True)
            provider.assert_not_called()
            self.assertEqual([], result['failed'])
            self.assertEqual(1, result['cancelled'])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())
