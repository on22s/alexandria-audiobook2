"""Actual API input proof follows completed bytes across isolated composition."""
from contextvars import copy_context
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from synthesis_input import (ensure_speech_input_capture, capture_speech_input,
    get_captured_speech_input, promote_speech_input, capture_composite_speech_input)


class SynthesisInputCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.audio = self.root / 'audio.wav'; self.audio.write_bytes(b'completed synthetic artifact')

    def test_exact_text_and_completed_bytes_are_copied_without_repreparation(self):
        text = 'LOO-ee\r\n greeted Jose\u0301. Go, go!'
        with ensure_speech_input_capture():
            capture_speech_input(self.audio, text, 'English', 'local_custom_api')
            first = get_captured_speech_input(self.audio)
            self.assertEqual(text, first['text'])
            first['text'] = 'caller mutation'
            self.assertEqual(text, get_captured_speech_input(self.audio)['text'])
        self.assertIsNone(get_captured_speech_input(self.audio))

    def test_disabled_unknown_and_failed_outputs_supply_no_guessed_expectation(self):
        with patch('synthesis_input._get_artifact', side_effect=AssertionError('disabled capture read output')):
            with ensure_speech_input_capture(False):
                capture_speech_input(self.audio, 'Expected argument', 'English', 'fixture')
                self.assertIsNone(get_captured_speech_input(self.audio))
        with ensure_speech_input_capture():
            self.assertIsNone(get_captured_speech_input(self.audio))
            self.assertIsNone(capture_speech_input(self.root / 'missing', 'Actual argument', 'English', 'fixture'))

    def test_changed_output_discards_previously_captured_input(self):
        with ensure_speech_input_capture():
            capture_speech_input(self.audio, 'Actual speech.', 'English', 'fixture')
            self.audio.write_bytes(b'replaced output')
            self.assertIsNone(get_captured_speech_input(self.audio))

    def test_copied_context_worker_is_isolated_until_accepted_promotion(self):
        target = self.root / 'selected.wav'
        with ensure_speech_input_capture():
            def worker():
                with ensure_speech_input_capture():
                    capture_speech_input(self.audio, 'Actual worker input.', 'English', 'external_custom_api')
                    return get_captured_speech_input(self.audio)
            receipt = copy_context().run(worker)
            self.assertIsNone(get_captured_speech_input(self.audio))
            self.assertIsNone(promote_speech_input(receipt, target))
            target.write_bytes(self.audio.read_bytes())
            promote_speech_input(receipt, target)
            self.assertEqual('Actual worker input.', get_captured_speech_input(target)['text'])
            receipt['text'] = 'late mutation'
            self.assertEqual('Actual worker input.', get_captured_speech_input(target)['text'])

    def test_windows_preserve_exact_span_whitespace_and_require_all_children(self):
        second = self.root / 'second.wav'; second.write_bytes(b'second artifact')
        parent = self.root / 'parent.wav'; parent.write_bytes(b'assembled parent')
        with ensure_speech_input_capture():
            capture_speech_input(self.audio, 'First.\r\n', 'English', 'local_custom_api')
            self.assertIsNone(capture_composite_speech_input([self.audio,second], parent, 'First.\r\nSecond.', 'windows'))
            capture_speech_input(second, 'Second.', 'English', 'local_custom_api')
            self.assertIsNone(capture_composite_speech_input([self.audio,second], parent, 'First. Second.', 'windows'))
            row = capture_composite_speech_input([self.audio,second], parent, 'First.\r\nSecond.', 'windows')
            self.assertEqual('First.\r\nSecond.', row['text'])

    def test_ensemble_has_one_script_and_rejects_mismatched_member_inputs(self):
        second = self.root / 'second.wav'; second.write_bytes(b'second member')
        parent = self.root / 'parent.wav'; parent.write_bytes(b'mixed ensemble')
        with ensure_speech_input_capture():
            capture_speech_input(self.audio, 'Go now.', 'English', 'local_custom_api')
            capture_speech_input(second, 'Go now.', 'English', 'local_clone_api')
            row = capture_composite_speech_input([self.audio,second], parent, 'Go now.', 'ensemble')
            self.assertEqual('Go now.', row['text'])
            capture_speech_input(second, 'Go later.', 'English', 'local_clone_api')
            self.assertIsNone(capture_composite_speech_input([self.audio,second], parent, 'Go now.', 'ensemble'))
            self.assertIsNone(get_captured_speech_input(parent))

    def test_failed_promotion_clears_older_same_path_proof_even_if_bytes_match(self):
        with ensure_speech_input_capture():
            capture_speech_input(self.audio, 'Older input.', 'English', 'fixture')
            self.assertIsNotNone(get_captured_speech_input(self.audio))
            self.assertIsNone(promote_speech_input(None, self.audio))
            self.assertIsNone(get_captured_speech_input(self.audio))

    def test_collector_and_text_limits_are_explicit_absence_instead_of_partial_proof(self):
        with patch('synthesis_input.MAX_ARTIFACTS',2), ensure_speech_input_capture():
            for index in range(3):
                path = self.root / str(index); path.write_bytes(b'artifact')
                capture_speech_input(path, 'Actual speech.', 'English', 'fixture')
            self.assertIsNone(get_captured_speech_input(self.root/'0'))
            self.assertIsNone(get_captured_speech_input(self.root/'2'))
        with ensure_speech_input_capture():
            for text in ['', 'x'*20001, 'bad\ud800']:
                self.assertIsNone(capture_speech_input(self.audio, text, 'English', 'fixture'))


if __name__ == '__main__':
    unittest.main()
