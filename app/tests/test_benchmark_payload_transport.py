"""Large synthetic payloads cross real process boundaries without entering argv."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import benchmark_runner as runner
import benchmark_execution as execution
from tests.test_benchmark_worker_protocol import WORKERS, METRICS
from benchmark_fixtures import build_tts_generation_manifest, build_dataset_builder_manifest


class BenchmarkPayloadTransportTests(unittest.TestCase):
    def prepare_workers(self, root):
        app = root / 'app'
        app.mkdir()
        original = Path(__file__).resolve().parent.parent
        for name in ('benchmark_remote_command.py', 'subprocess_ownership.py'):
            (app / name).symlink_to(original / name)
        for name in WORKERS:
            # Replace stage execution only; exercise the real CLI and protocol.
            (app / (name + '.py')).write_text(
                f'import sys;sys.path.insert(0, {str(original)!r})\n'
                'import hashlib\n'
                f'import {name} as worker\n'
                'def execute(value, *args, **kwargs):\n'
                '    fixture = value.get("fixture", value)\n'
                '    text = (fixture["entries"][0] if "entries" in fixture else fixture["samples"][0]["text"] if "samples" in fixture else fixture["fixtures"][0]["text"])\n'
                '    return {"digest": hashlib.sha256(text.encode()).hexdigest()}\n'
                f'worker.{"execute_fixture" if name in METRICS else "execute_payload"} = execute\n'
                'worker.main()\n')

    def test_all_local_dispatchers_decode_large_payload_in_native_workers(self):
        text = 'synthetic 音声🙂 ' * 20000
        fixture = {'entries': [text], 'zip_path': 'fixture.zip', 'model_path': 'fixture.gguf'}
        fixture['sha256'] = runner._hash_entries({'entries': fixture['entries']})
        expected = hashlib.sha256(text.encode()).hexdigest()
        tts_payload = build_tts_generation_manifest([{'text': text}])
        builder_fixture = build_dataset_builder_manifest([{'samples': [{'text': text}]}])['fixtures'][0]
        with tempfile.TemporaryDirectory() as tmp:
            self.prepare_workers(Path(tmp))
            settings = {'local_python': sys.executable}
            calls = {
                'tts': lambda: runner._run_tts_worker(tts_payload, 'local', settings, tmp, tmp, None),
                'lora_training': lambda: runner._run_lora_training_worker(fixture, 'local', settings, tmp, None),
                'preparer': lambda: runner._run_preparer_worker(fixture, 'local', settings, tmp, None),
                'dedup': lambda: runner._run_dedup_worker(fixture, 'local', settings, tmp, None),
                'profiling': lambda: runner._run_profiling_worker(fixture, 'local', settings, tmp, None),
                'naming': lambda: runner._run_naming_worker(fixture, 'local', settings, tmp, None),
                'export': lambda: runner._run_export_worker('audacity_export', fixture, 'local', settings, tmp, None),
                'dataset_builder': lambda: runner._run_dataset_builder_worker(builder_fixture, 'local', settings, tmp, None, {}),
            }
            def transport(command, **kwargs):
                self.assertIn('--payload-stdin', command)
                self.assertNotIn('--payload', command)
                self.assertLess(max(len(arg.encode()) for arg in command), 4096)
                self.assertGreater(len(kwargs['input'].encode()), 128 * 1024)
                return execution.run_benchmark_subprocess(command, **kwargs)
            state = {'cancel': False}
            token = execution.BENCHMARK_STATE.set(state)
            try:
                with patch.object(runner, 'run_benchmark_subprocess', side_effect=transport), \
                        patch.object(runner, '_validate_export_fixture'):
                    for name, call in calls.items():
                        with self.subTest(worker=name):
                            value = call()
                            if name + '_benchmark' in METRICS:
                                value = value['metrics']
                            self.assertEqual({'digest': expected}, value)
                            self.assertEqual([], state['processes'])
            finally:
                execution.BENCHMARK_STATE.reset(token)

    @unittest.skipUnless(sys.platform == 'linux', 'native Linux SSH ownership transport')
    def test_remote_tts_and_builder_use_owned_stdin_without_payload_in_ssh_argv(self):
        text = 'synthetic 音声🙂 ' * 20000
        fixture = {'entries': [text]}
        expected = hashlib.sha256(text.encode()).hexdigest()
        tts_payload = build_tts_generation_manifest([{'text': text}])
        builder_fixture = build_dataset_builder_manifest([{'samples': [{'text': text}]}])['fixtures'][0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_workers(root)
            ssh = root / 'ssh'
            record = root / 'argv.jsonl'
            ssh.write_text('#!' + sys.executable + '\n'
                'import json,os,pathlib,shlex,sys\n'
                f'with pathlib.Path({str(record)!r}).open("a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                'args=shlex.split(sys.argv[2]);os.execv(args[0],args)\n')
            ssh.chmod(0o700)
            settings = {'remote_python': sys.executable, 'remote_root': tmp}
            state = {'cancel': False}
            token = execution.BENCHMARK_STATE.set(state)
            try:
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH']), \
                        patch.object(runner, '_stage_remote_tts_assets', side_effect=lambda payload, *args: dict(payload)):
                    self.assertEqual({'digest': expected}, runner._run_tts_worker(
                        tts_payload, 'thunder', settings, tmp, tmp, 'fixture-host'))
                    self.assertEqual({'digest': expected}, runner._run_dataset_builder_worker(
                        builder_fixture, 'thunder', settings, tmp, 'fixture-host', {}))
                self.assertEqual([], state['processes'])
                for line in record.read_text().splitlines():
                    argv = json.loads(line)
                    self.assertLess(max(len(arg.encode()) for arg in argv), 4096)
                    self.assertNotIn('synthetic', line)
            finally:
                execution.BENCHMARK_STATE.reset(token)
