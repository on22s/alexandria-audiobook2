"""Production-path regressions for final retry-safety adjudication."""
import json
import unittest
from unittest.mock import patch

from speech_verification_receipt import get_speech_verification_chunk_view
from tests import test_speech_retry_fixes as fix_fixture
from tests.test_speech_verification_receipt import get_report


class SpeechRetryParentReviewTests(unittest.TestCase):
    def fixture(self):
        return fix_fixture.SpeechRetryFixTests().fixture()

    def test_default_batch_seed_honors_saved_voice_seed(self):
        with self.fixture() as (root, manager, before, prior, flags):
            fix_fixture.SpeechRetryFixTests().set_seed(root, manager, '1234567')
            original = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech',
                       side_effect=lambda path, text, *args, **kwargs: get_report(path, text)), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original) as provider:
                result = manager.generate_chunks_batch([0], batch_seed=-1)
            self.assertEqual([0], result['completed'])
            self.assertEqual(1234567, provider.call_args.args[3]['NARRATOR']['seed'])

    def test_verifier_memory_reason_is_not_provider_oom_retry(self):
        with self.fixture() as (root, manager, before, prior, flags):
            original = manager.engine.generate_voice_result
            with patch('speech_verification.verify_finished_speech', return_value={
                    'version': 1, 'status': 'unavailable', 'stage': 'primary_identity',
                    'reason': 'Cannot allocate memory'}), \
                    patch.object(manager.engine, 'generate_voice_result', wraps=original) as provider:
                result = manager.generate_chunks_parallel([0], max_workers=3)
            self.assertEqual([], result['completed'])
            self.assertEqual(1, provider.call_count)
            row = manager.load_chunks()[0]
            self.assertEqual(['verifier_unavailable'],
                             [entry['outcome'] for entry in row['speech_retry_attempts']])
            self.assertEqual(prior, (root / before['audio_path']).read_bytes())

    def test_batch_retention_qualification_precedes_serial_routing(self):
        with self.fixture() as (root, manager, before, prior, flags):
            with patch('project.get_take_history_policy', return_value={'enabled': True}), \
                    patch.object(manager, '_ensure_automatic_predecessor',
                                 side_effect=ValueError('retained metadata is full')), \
                    patch.object(manager, 'generate_chunks_parallel') as route:
                result = manager.generate_chunks_batch([0])
            route.assert_not_called()
            self.assertEqual([], result['completed'])
            self.assertEqual([(0, 'retained metadata is full')], result['failed'])

    def test_public_view_does_not_expose_raw_attempt_history(self):
        book = 'a' * 64
        chunk = {'uid': 'line-0', 'audio_revision': 'current', 'audio_path': None,
                 'speech_retry_attempts': [{'receipt': {'uid': 'line-0',
                     'book_token': book, 'audio_revision': 'current'},
                     'unvalidated_payload': 'not public evidence'}]}
        before = json.dumps(chunk, sort_keys=True)
        view = get_speech_verification_chunk_view(chunk, book, lambda path: None)
        self.assertNotIn('speech_retry_attempts', view)
        self.assertEqual(before, json.dumps(chunk, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
