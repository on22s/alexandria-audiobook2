"""Content evidence uses native ordered text, never unique word timing coverage."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from word_timing import get_asr_transcript, read_asr_payload, run_timing_command, transcribe_asr_audio, MAX_RESULT_BYTES


class SpeechVerificationASRTests(unittest.TestCase):
    def test_raw_text_keeps_authored_repeat_and_ignores_unusable_timestamps(self):
        payload = {'transcription': [
            {'text': ' Go now.', 'offsets': {'from': -1, 'to': -1}},
            {'text': ' Go now.', 'offsets': {'from': -1, 'to': -1}}]}
        self.assertEqual('Go now. Go now.', get_asr_transcript(payload))
        self.assertEqual('', get_asr_transcript({'transcription': []}))

    def test_native_text_validation_never_returns_partial_evidence(self):
        for rows in [None, {}, [None], [{'text': 1}], [{'text': 'valid'}, {'text': None}],
                     [{'text': 'x' * 20001}], [{'text': '\ud800'}], [{'text': 'a'}] * 4097]:
            with self.subTest(kind=type(rows)), self.assertRaises(ValueError):
                get_asr_transcript({'transcription': rows})

    def test_full_text_limit_includes_segment_separators(self):
        self.assertEqual('x' * 20000, get_asr_transcript({'transcription': [{'text': 'x' * 20000}]}))
        with self.assertRaises(ValueError):
            get_asr_transcript({'transcription': [{'text': 'x' * 10000}, {'text': 'x' * 10000}]})

    def test_json_read_has_a_byte_bound_without_relying_on_stat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'asr.json'
            path.write_bytes(b' ' * MAX_RESULT_BYTES + b'{}')
            with self.assertRaisesRegex(ValueError, 'size limit'):
                read_asr_payload(path)
            path.write_text(json.dumps({'transcription': []}))
            self.assertEqual({'transcription': []}, read_asr_payload(path))
            path.write_text('[]')
            with self.assertRaisesRegex(ValueError, 'Malformed'):
                read_asr_payload(path)

    def test_command_retains_close_owned_lease_and_discards_unbounded_diagnostics(self):
        with patch('word_timing.start_owned_subprocess') as launch:
            launch.return_value.wait.return_value = 0
            launch.return_value.poll.return_value = 0
            run_timing_command(['fixture'], lambda: False, task_lease_fd=17)
            self.assertEqual(17, launch.call_args.kwargs['task_lease_fd'])
            self.assertEqual(subprocess.DEVNULL, launch.call_args.kwargs['stderr'])
            launch.return_value._alexandria_control.close.assert_called_once()

    def test_confirmation_cannot_reuse_primary_json_when_provider_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model'; model.write_bytes(b'fixture')
            result = root / 'asr.json'
            result.write_text(json.dumps({'transcription': [{'text': 'prior primary evidence'}]}))
            with patch('psutil.virtual_memory', return_value=SimpleNamespace(available=32 * 1024**3)):
                with self.assertRaises(FileNotFoundError):
                    transcribe_asr_audio(root / 'audio.wav', {'binary': 'fixture', 'model': str(model)},
                        lambda: False, directory, command=lambda *args: None)
            self.assertFalse(result.exists())


if __name__ == '__main__':
    unittest.main()
