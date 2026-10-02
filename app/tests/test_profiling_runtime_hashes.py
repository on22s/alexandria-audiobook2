"""Actual fixture files must stay bound before load and before results return."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
import profiling_benchmark as worker
import benchmark_runner as runner


class ProfilingRuntimeHashTests(unittest.TestCase):
    def fixture(self, root):
        archive, model = root / 'dataset.zip', root / 'model.gguf'
        archive.write_bytes(b'fixture dataset bytes')
        model.write_bytes(b'fixture model bytes')
        fixture = {'zip_path': archive.name, 'model_path': model.name,
                   'zip_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                   'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
                   'dataset_id': 'fixture_book', 'seed': 42}
        fixture['sha256'] = runner._hash_entries(fixture)
        return {'fixture': fixture, 'root_dir': str(root), 'zip_path': str(archive), 'model_path': str(model)}

    def modules(self, payload, changed=None):
        voice = ModuleType('voice_profiler')
        voice.get_ref_wav = Mock(return_value=b'known WAV fixture')
        voice.get_ref_text = Mock(return_value='Known source text.')
        features = {key: value for key, value in zip(
            ('mean_f0', 'std_f0', 'mean_rms', 'speaking_rate', 'mean_centroid', 'smoothness', 'flatness'),
            (180., 15., .2, 3., 500., .5, .1))}
        voice.analyze_ref_wav = Mock(return_value=features)
        voice.interpret_features = Mock(return_value='Fixture feature summary.')
        voice.parse_book_title = Mock(return_value='Fixture book')
        voice.parse_narrator_name = Mock(return_value='Fixture narrator')
        def describe(*args, **kwargs):
            if changed:
                Path(payload[changed]).write_bytes(b'changed during worker execution')
            return 'Known fixture voice profile.'
        voice.llm_describe = Mock(side_effect=describe)
        llama = ModuleType('llama_cpp')
        llama.Llama = Mock(return_value=object())
        return voice, llama

    def test_changes_after_runner_preflight_are_rejected_before_heavy_import_or_load(self):
        for key in ('zip_path', 'model_path'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                payload = self.fixture(Path(tmp))
                runner._validate_profiling_fixture(payload['fixture'], tmp)
                Path(payload[key]).write_bytes(b'changed after preflight')
                voice, llama = self.modules(payload)
                with patch.dict(sys.modules, {'voice_profiler': voice, 'llama_cpp': llama}), \
                     self.assertRaisesRegex(ValueError, 'changed at execution'):
                    worker.execute_payload(payload)
                llama.Llama.assert_not_called()
                voice.get_ref_wav.assert_not_called()

    def test_changed_files_during_execution_cannot_return_a_passed_result(self):
        for key in ('zip_path', 'model_path'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                payload = self.fixture(Path(tmp))
                voice, llama = self.modules(payload, changed=key)
                with patch.dict(sys.modules, {'voice_profiler': voice, 'llama_cpp': llama}), \
                     patch.object(sys, 'path', list(sys.path)), \
                     self.assertRaisesRegex(ValueError, 'changed at execution'):
                    worker.execute_payload(payload)
                self.assertEqual(1, llama.Llama.call_count)
                self.assertEqual(1, voice.llm_describe.call_count)

    def test_unchanged_runtime_files_produce_fixture_bound_metrics_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.fixture(Path(tmp))
            original = copy.deepcopy(payload)
            files = {key: Path(payload[key]).read_bytes() for key in ('zip_path', 'model_path')}
            voice, llama = self.modules(payload)
            with patch.dict(sys.modules, {'voice_profiler': voice, 'llama_cpp': llama}), \
                 patch.object(sys, 'path', list(sys.path)):
                result = worker.execute_payload(payload)
            self.assertEqual('passed', result['status'])
            self.assertEqual(180., result['voice_features']['mean_f0'])
            self.assertEqual('Known fixture voice profile.', result['voice_profile'])
            self.assertEqual(str(Path(tmp) / 'model.gguf'), llama.Llama.call_args.kwargs['model_path'])
            self.assertEqual(original, payload)
            self.assertEqual(files, {key: Path(payload[key]).read_bytes() for key in files})
            artifact = Path(tmp) / 'metrics.json'
            artifact.write_text(json.dumps(result))
            self.assertEqual(result, json.loads(artifact.read_text()))
