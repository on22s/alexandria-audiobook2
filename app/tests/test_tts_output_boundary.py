import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import tts_benchmark as worker


class TtsOutputBoundaryTests(unittest.TestCase):
    def generate(self, engine, fixture, path, **kwargs):
        self.paths.append(path)
        with wave.open(path, 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
            stream.writeframes(b'\x01\x00' * 2400)
        return worker.measure_wav(path, 1)

    def execute(self, payload, output):
        self.paths = []
        with patch.object(worker, 'TTSEngine', return_value=object()), \
                patch.object(worker, 'run_custom_voice_case', side_effect=self.generate):
            return worker.execute_payload(payload, str(output))

    def test_bad_ids_and_repetitions_fail_before_wav_generation(self):
        cases = [(value, 1) for value in ('../victim', '/victim', 'a/b', 'a\\b', 'C:out', '', '.', '..', 'a\x00')]
        cases += [('safe', value) for value in ('../victim', '/victim', '1', True, 0, -1, 1.5)]
        for fixture_id, repetition in cases:
            with self.subTest(fixture_id=repr(fixture_id), repetition=repr(repetition)), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'output'; output.mkdir()
                payload = {'fixtures': [{'id': fixture_id, 'repetition_numbers': [repetition]}], 'repetitions': 1}
                before = copy.deepcopy(payload)
                cases = self.execute(payload, output)
                self.assertEqual('failed', cases[0]['status'])
                self.assertEqual([], self.paths)
                self.assertEqual([], list(output.iterdir()))
                self.assertEqual(before, payload)

    def test_escaping_output_symlink_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / 'output'; output.mkdir()
            sentinel = root / 'private.wav'; sentinel.write_bytes(b'private original')
            (output / 'safe-1.wav').symlink_to(sentinel)
            cases = self.execute({'fixtures': [{'id': 'safe'}], 'repetitions': 1}, output)
            self.assertEqual('failed', cases[0]['status'])
            self.assertEqual([], self.paths)
            self.assertEqual(b'private original', sentinel.read_bytes())
            self.assertTrue((output / 'safe-1.wav').is_symlink())

    def test_normal_unicode_cases_still_publish_measured_pcm_inside_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'output'
            payload = {'fixtures': [{'id': 'Café 猫'}], 'repetitions': 2}
            before = copy.deepcopy(payload); cases = self.execute(payload, output)
            self.assertEqual(['passed', 'passed'], [case['status'] for case in cases])
            self.assertEqual(['Café 猫-1.wav', 'Café 猫-2.wav'], sorted(path.name for path in output.iterdir()))
            for case in cases:
                self.assertEqual(.1, case['metrics']['duration_seconds'])
                self.assertEqual(24000, case['metrics']['sample_rate'])
            self.assertEqual(before, payload)
