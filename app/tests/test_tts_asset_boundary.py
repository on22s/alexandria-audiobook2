import base64
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import benchmark_runner as runner
import tts_benchmark as worker


class PcmEngine:
    def _init_local_clone(self): pass
    def _get_clone_prompt(self, *args, **kwargs): return 'prompt'
    def _init_local_lora(self, *args, **kwargs): return 'model'
    def _ensure_lora_prompt(self, *args, **kwargs): return 'prompt'
    def generate_clone_voice(self, text, speaker, config, path): return self.write(path)
    def generate_lora_voice(self, text, instruct, config, path): return self.write(path)
    def write(self, path):
        with wave.open(path, 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
            stream.writeframes(b'\x01\x00' * 2400)
        return True


class TtsAssetBoundaryTests(unittest.TestCase):
    def clone_fixture(self, name):
        return {'id': 'clone', 'voice_type': 'clone', 'text': 'Hello', 'speaker': 'ALICE',
                'seed': 1, 'ref_audio': name, 'ref_audio_sha256': hashlib.sha256(b'reference').hexdigest(),
                'ref_text': 'Reference words'}

    def test_outside_clone_reference_refuses_before_hash_prompt_or_generation(self):
        for mode in ('absolute', 'parent', 'symlink'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); fixtures = root / 'fixtures'; fixtures.mkdir()
                reference = root / 'private.wav'; reference.write_bytes(b'reference')
                (fixtures / 'link.wav').symlink_to(reference)
                name = {'absolute': str(reference), 'parent': '../private.wav', 'symlink': 'link.wav'}[mode]
                fixture = self.clone_fixture(name); before = copy.deepcopy(fixture)
                with patch.object(worker, 'get_file_sha256', wraps=worker.get_file_sha256) as read, \
                        patch.object(worker, '_run_with_utilization_sampling', side_effect=lambda fn: (fn(), None)):
                    with self.assertRaises(ValueError):
                        worker.run_clone_voice_case(PcmEngine(), fixture, str(fixtures / 'out.wav'), str(fixtures))
                    read.assert_not_called()
                self.assertFalse((fixtures / 'out.wav').exists()); self.assertEqual(before, fixture)
                self.assertEqual(b'reference', reference.read_bytes())

    def test_outside_adapter_and_artifact_refuse_before_snapshot(self):
        for mode in ('adapter-absolute', 'adapter-parent', 'artifact-parent', 'artifact-absolute', 'artifact-link'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); fixtures = root / 'fixtures'; fixtures.mkdir(); adapter = fixtures / 'adapter'; adapter.mkdir()
                outside = root / 'outside'; outside.mkdir(); (outside / 'training_meta.json').write_text('{}')
                (adapter / 'training_meta.json').write_text('{}')
                secret = fixtures / 'private'; secret.write_bytes(b'weights'); (adapter / 'link').symlink_to(secret)
                name = {'artifact-parent': '../private', 'artifact-absolute': str(secret), 'artifact-link': 'link'}.get(mode)
                fixture = {'text': 'Hello', 'seed': 1, 'adapter_path':
                    str(outside) if mode == 'adapter-absolute' else '../outside' if mode == 'adapter-parent' else 'adapter',
                    'adapter_artifact_sha256': {name: hashlib.sha256(b'weights').hexdigest()} if name else {}}
                @contextlib.contextmanager
                def snapshot(path): yield path, 'fixture-generation'
                with patch.object(worker, 'ensure_adapter_generation_snapshot', side_effect=snapshot) as capture, \
                        patch.object(worker, '_run_with_utilization_sampling', side_effect=lambda fn: (fn(), None)), \
                        patch.dict(sys.modules, torch=SimpleNamespace(manual_seed=lambda seed: None)):
                    with self.assertRaises(ValueError):
                        worker.run_lora_voice_case(PcmEngine(), fixture, str(fixtures / 'out.wav'), str(fixtures))
                    capture.assert_not_called()
                self.assertFalse((fixtures / 'out.wav').exists()); self.assertEqual(b'weights', secret.read_bytes())

    def test_explicit_staging_root_works_and_fixture_root_fields_cannot_override_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); reference = root / 'reference.wav'; reference.write_bytes(b'reference')
            payload = {'fixtures': [self.clone_fixture(str(reference))], 'repetitions': 1, '_asset_root': str(root), 'asset_root': str(root)}
            before = copy.deepcopy(payload)
            with patch.object(worker, 'TTSEngine', return_value=PcmEngine()), \
                    patch.object(worker, '_run_with_utilization_sampling', side_effect=lambda fn: (fn(), None)):
                rejected = worker.execute_payload(payload, str(root / 'default-output'))
                accepted = worker.execute_payload(payload, str(root / 'staged-output'), asset_root=str(root))
            self.assertEqual('failed', rejected[0]['status']); self.assertEqual('passed', accepted[0]['status'])
            self.assertEqual(.1, accepted[0]['metrics']['duration_seconds']); self.assertEqual(before, payload)

    def test_remote_dispatch_passes_created_root_as_argument_and_removes_payload_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'reference.wav').write_bytes(b'reference')
            payload = {'fixtures': [self.clone_fixture('reference.wav')], 'repetitions': 1, '_asset_root': '/untrusted'}
            before = copy.deepcopy(payload); commands = []
            created = '/tmp/alexandria-tts-benchmark-assets.abcdefghij'
            def transport(command, **kwargs):
                commands.append(command)
                stdout = created + '\n' if len(commands) == 1 else 'TTS_BENCHMARK_RESULT=[]\n'
                return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
            with patch.object(runner, 'run_benchmark_subprocess', side_effect=transport), \
                    patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as cleanup:
                self.assertEqual([], runner._run_tts_worker(payload, 'remote',
                    {'remote_root': '/remote checkout', 'remote_python': '/remote python'}, tmp, '/output', 'fixture-host'))
            args = shlex.split(commands[-1][2]); self.assertEqual(created, args[args.index('--asset-root') + 1])
            decoded = json.loads(base64.b64decode(args[args.index('--payload') + 1]))
            self.assertNotIn('_asset_root', decoded)
            self.assertTrue(decoded['fixtures'][0]['ref_audio'].startswith(created + '/'))
            self.assertEqual(before, payload)
            cleanup.assert_called_once()
