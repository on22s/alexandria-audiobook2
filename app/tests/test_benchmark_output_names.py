"""Output allocation preserves native names and refuses destination redirects."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import tts_benchmark
from benchmark_validation import get_benchmark_artifact_name, get_benchmark_output_path


class BenchmarkOutputNameTests(unittest.TestCase):
    def test_shared_policy_refuses_path_components_and_preserves_literal_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('', '.', '..', '../outside', '/outside', 'nested/name',
                         'nested\\name', 'drive:name', 'null\x00name', None, 42):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    get_benchmark_output_path(tmp, name)
            for name in ('voice-1', '語音', 'sample_000.wav', 'manifest.json'):
                self.assertEqual(get_benchmark_artifact_name(name), name)
                self.assertEqual(get_benchmark_output_path(tmp, name), str(Path(tmp, name)))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_tts_refuses_inside_outside_and_dangling_output_links_before_generation(self):
        for target_kind in ('inside', 'outside', 'dangling'):
            with self.subTest(target=target_kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / 'output'
                output.mkdir()
                target = (output if target_kind == 'inside' else root) / 'prior.wav'
                if target_kind != 'dangling':
                    target.write_bytes(b'preserve prior audio')
                leaf = output / 'voice-1.wav'
                leaf.symlink_to(target)
                payload = {'fixtures': [{'id': 'voice'}], 'repetitions': 1}
                with patch.object(tts_benchmark, 'TTSEngine'),\
                     patch.object(tts_benchmark, 'run_custom_voice_case') as generate:
                    result = tts_benchmark.execute_payload(payload, str(output))
                generate.assert_not_called()
                self.assertEqual(result[0]['status'], 'failed')
                self.assertIn('symlink', result[0]['error'])
                self.assertTrue(leaf.is_symlink())
                if target_kind != 'dangling':
                    self.assertEqual(target.read_bytes(), b'preserve prior audio')
                else:
                    self.assertFalse(target.exists())

    def test_native_tts_output_keeps_existing_names_and_pcm_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'fixtures': [{'id': '語音'}], 'repetitions': 2}
            names = []
            def generate(engine, fixture, output_path, load_model=False):
                names.append(Path(output_path).name)
                with wave.open(output_path, 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(b'\x10\x00' * 2400)
                return tts_benchmark.measure_wav(output_path, .1)
            with patch.object(tts_benchmark, 'TTSEngine'),\
                 patch.object(tts_benchmark, 'run_custom_voice_case', side_effect=generate):
                result = tts_benchmark.execute_payload(payload, tmp)
            self.assertEqual(names, ['語音-1.wav', '語音-2.wav'])
            self.assertEqual([case['status'] for case in result], ['passed', 'passed'])
            for name, case in zip(names, result):
                with wave.open(str(Path(tmp, name))) as audio:
                    self.assertEqual((audio.getframerate(), audio.getnframes()), (24000, 2400))
                self.assertEqual(case['metrics']['duration_seconds'], .1)
                self.assertEqual(case['metrics']['sha256'], result[0]['metrics']['sha256'])
