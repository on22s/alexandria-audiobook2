"""Fail-first regressions for the reviewed speech-retry defects.

Each test here fails on the frozen pre-fix staging and passes with the repair:

* C01  the app stores voice seeds as JSON strings (VoiceConfigItem.seed is
       Optional[str]); a digit-shaped saved seed must render and retry, a
       non-numeric or out-of-bound (uint32) seed must render unchanged once and
       refuse the retry as ``seed_unbound`` - never refuse the render.
* C04  a persistence failure while writing the retry outcome must not escape
       the worker and leave the row ``generating``.
* C11  an exhausted budget keeps the last attempt's reason in the message.
* C16  a take-invalidating text edit clears displaced retry history.
* C06  the public view serves retry history only when it belongs to the
       current take, detached from the stored row.
"""
import copy
import json
import unittest
from unittest.mock import patch

from speech_verification_receipt import get_speech_verification_chunk_view
from tests import test_speech_retry_integration as integration_fixture
from tests.test_speech_verification_receipt import get_report
from tts import TTSFailure, TTSGenerationResult


class SpeechRetryFixTests(unittest.TestCase):
    def fixture(self):
        return integration_fixture.SpeechRetryIntegrationTests().retry_fixture()

    def verifier(self, root, before, prior, reports, actions=None):
        return integration_fixture.SpeechRetryIntegrationTests().sequence_verifier(
            root, before, prior, reports, actions=actions)

    def provider_seeds(self, spy):
        return [call[0][3]['NARRATOR']['seed'] for call in spy.call_args_list]

    def set_seed(self, root, manager, seed):
        voices_path = root / manager.voice_config_path
        voices = json.loads(voices_path.read_text())
        voices['NARRATOR']['seed'] = seed
        voices_path.write_text(json.dumps(voices))

    def test_digit_string_seed_renders_and_retries_with_distinct_seeds(self):
        with self.fixture() as (root, manager, before, prior, flags):
            self.set_seed(root, manager, "1234567")
            verify, state = self.verifier(root, before, prior, ['mismatch', 'good'])
            original = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original) as spy:
                self.assertTrue(manager.generate_chunk_audio(0)[0])
            seeds = self.provider_seeds(spy)
            self.assertEqual(2, spy.call_count)
            self.assertEqual(1234567, seeds[0])
            self.assertEqual(2, len(set(seeds)))
            row = manager.load_chunks()[0]
            self.assertEqual('done', row['status'])
            self.assertEqual([1234567, seeds[1]], [e['requested_seed'] for e in row['speech_retry_attempts']])

    def test_nonnumeric_seed_renders_once_and_refuses_the_retry(self):
        with self.fixture() as (root, manager, before, prior, flags):
            self.set_seed(root, manager, "not-a-seed")
            verify, state = self.verifier(root, before, prior, ['mismatch', 'good'])
            original = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original) as spy:
                self.assertFalse(manager.generate_chunk_audio(0)[0])
            self.assertEqual(1, spy.call_count)
            self.assertEqual(["not-a-seed"], self.provider_seeds(spy))
            row = manager.load_chunks()[0]
            self.assertEqual('error', row['status'])
            self.assertEqual([(1, 'provider_failure', None)],
                             [(e['attempt'], e['outcome'], e['requested_seed'])
                              for e in row['speech_retry_attempts']])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_uint32_seed_keeps_the_first_render_and_refuses_the_retry(self):
        with self.fixture() as (root, manager, before, prior, flags):
            self.set_seed(root, manager, 3000000000)
            verify, state = self.verifier(root, before, prior, ['mismatch'])
            original = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original) as spy:
                self.assertFalse(manager.generate_chunk_audio(0)[0])
            self.assertEqual(1, spy.call_count)
            self.assertEqual([3000000000], self.provider_seeds(spy))
            row = manager.load_chunks()[0]
            self.assertEqual('error', row['status'])
            self.assertEqual(1, len(row['speech_retry_attempts']))
            self.assertIsNone(row['speech_retry_attempts'][0]['requested_seed'])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_failure_write_error_returns_instead_of_escaping_or_sticking(self):
        with self.fixture() as (root, manager, before, prior, flags):
            original_save = manager._save_chunks_locked

            def save_boom(rows, *args, **kwargs):
                if any(isinstance(row, dict) and row.get('status') == 'error' for row in rows):
                    raise ValueError('synthetic persistence failure')
                return original_save(rows, *args, **kwargs)

            verify, state = self.verifier(root, before, prior, ['mismatch', 'mismatch'])
            with patch.object(manager, '_save_chunks_locked', side_effect=save_boom), \
                    patch('speech_verification.verify_finished_speech', side_effect=verify):
                success, message = manager.generate_chunk_audio(0)
            self.assertFalse(success)
            self.assertIn('Speech verification confirmed', message)
            row = manager.load_chunks()[0]
            self.assertNotEqual('generating', row['status'])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_exhausted_budget_keeps_the_last_attempt_reason(self):
        with self.fixture() as (root, manager, before, prior, flags):
            def provider(*args, **kwargs):
                return TTSGenerationResult(False, TTSFailure(
                    'out_of_memory', 'CUDA out of memory', 'Free memory.'))

            with patch.object(manager.engine, 'generate_voice_result', side_effect=provider) as spy:
                result = manager.generate_chunks_parallel([0], max_workers=3)
            self.assertEqual(2, spy.call_count)
            self.assertEqual([], result['completed'])
            row = manager.load_chunks()[0]
            self.assertEqual('error', row['status'])
            self.assertIn('out of memory', str(row['error']).lower())
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_text_edit_clears_displaced_retry_history(self):
        with self.fixture() as (root, manager, before, prior, flags):
            verify, state = self.verifier(root, before, prior, ['good'])
            with patch('speech_verification.verify_finished_speech', side_effect=verify):
                self.assertTrue(manager.generate_chunk_audio(0)[0])
            self.assertTrue(manager.load_chunks()[0]['speech_retry_attempts'])
            manager.update_chunk(0, {'text': 'New edited words.'})
            row = manager.load_chunks()[0]
            self.assertIsNone(row.get('speech_retry_attempts'))
            self.assertIsNone(row.get('audio_path'))

    def test_first_render_with_review_verdict_ends_in_error_without_audio(self):
        with self.fixture() as (root, manager, before, prior, flags):
            manager._update_chunk_fields_by_uid('line-0', audio_path=None, audio_revision=None,
                                                status='pending', speech_verification=None,
                                                speech_verification_failure=None)
            verify, state = self.verifier(root, before, prior, ['review'])
            original = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', side_effect=verify), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original) as spy:
                self.assertFalse(manager.generate_chunk_audio(0)[0])
            self.assertEqual(1, spy.call_count)
            row = manager.load_chunks()[0]
            self.assertEqual('error', row['status'])
            self.assertIsNone(row.get('audio_path'))
            self.assertIsNone(row.get('speech_verification_failure'))
            self.assertEqual([(1, 'acoustic_suspicion')],
                             [(e['attempt'], e['outcome']) for e in row['speech_retry_attempts']])
            self.assertEqual([], list((root / 'voicelines').glob('.render-*')))

    def test_view_serves_retry_history_only_for_the_selected_take(self):
        book = 'a' * 64
        receipt = {'uid': 'line-0', 'book_token': book, 'audio_revision': 'new'}
        chunk = {'uid': 'line-0', 'id': 0, 'audio_revision': 'new', 'audio_path': None,
                 'speech_retry_attempts': [
                     {'attempt': 1, 'attempt_id': 'a', 'outcome': 'corroborated_content_difference',
                      'receipt': {'uid': 'line-0', 'book_token': book, 'audio_revision': 'old'}},
                     {'attempt': 2, 'attempt_id': 'b', 'outcome': 'verified', 'receipt': receipt}]}
        view = get_speech_verification_chunk_view(chunk, book, lambda path: None)
        self.assertNotIn('speech_retry_attempts', view)
        self.assertEqual('a', chunk['speech_retry_attempts'][0]['attempt_id'])
        stale = {**copy.deepcopy(chunk), 'audio_revision': 'newer'}
        stale_view = get_speech_verification_chunk_view(stale, book, lambda path: None)
        self.assertNotIn('speech_retry_attempts', stale_view)
        wrong_book = get_speech_verification_chunk_view(
            copy.deepcopy(chunk), 'b' * 64, lambda path: None)
        self.assertNotIn('speech_retry_attempts', wrong_book)


if __name__ == '__main__':
    unittest.main()
