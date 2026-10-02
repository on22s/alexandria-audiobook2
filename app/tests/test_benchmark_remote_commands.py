import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark_runner as runner


class BenchmarkRemoteCommandTests(unittest.TestCase):
    def test_profiling_hashes_literal_model_path_with_shell_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp, "model space; 'quoted' $literal.gguf")
            model.write_bytes(b'model fixture')
            fixture = dict(zip_path='audio.zip', zip_sha256='a'*64,
                           model_sha256=hashlib.sha256(model.read_bytes()).hexdigest())
            settings = dict(remote_root='/remote', remote_python='/remote/python',
                            remote_model_path=str(model))
            original = copy.deepcopy(fixture)
            calls = []
            def transport(command, **kwargs):
                calls.append(command)
                if command[0] == 'scp':
                    return subprocess.CompletedProcess(command, 0, '', '')
                if len(calls) == 2:
                    result = subprocess.run(['bash', '-c', ' '.join(command[2:])],
                                            capture_output=True, text=True)
                    result.stdout = 'Fixture decorative banner\n' + result.stdout
                    return result
                return subprocess.CompletedProcess(command, 0,
                    'PROFILING_BENCHMARK_RESULT={"status":"passed"}\n', '')
            with patch.object(runner, 'run_benchmark_subprocess', side_effect=transport):
                result = runner._run_profiling_worker(fixture, 'thunder', settings, tmp, 'fixture-host')
            self.assertEqual({'status': 'passed'}, result)
            self.assertEqual(3, len(calls))
            self.assertEqual(original, fixture)
            self.assertEqual(b'model fixture', model.read_bytes())

    def test_training_mkdir_receives_exact_literal_dataset_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp, 'argv.jsonl')
            fixture = dict(sha256='a'*64, dataset_path='dataset',
                           audio_sha256={"audio space; 'quoted' $literal/clip.wav": 'b'*64})
            original = copy.deepcopy(fixture)
            settings = dict(remote_root='/remote', remote_python='/remote/python')
            recorder = 'import json,sys;open(sys.argv[1],"a").write(json.dumps(sys.argv[2:])+"\\n")'
            definition = 'mkdir() { ' + shlex.join([sys.executable, '-c', recorder, str(record)]) + ' "$@"; }; '
            calls = []
            def transport(command, **kwargs):
                calls.append(command)
                if command[0] == 'scp':
                    return subprocess.CompletedProcess(command, 0, '', '')
                if 'mkdir' in ' '.join(command[2:]):
                    return subprocess.run(['bash', '-c', definition + ' '.join(command[2:])],
                                          capture_output=True, text=True)
                return subprocess.CompletedProcess(command, 0,
                    'LORA_TRAINING_BENCHMARK_RESULT={"status":"passed"}\n', '')
            with patch.object(runner, 'run_benchmark_subprocess', side_effect=transport):
                result = runner._run_lora_training_worker(fixture, 'thunder', settings, tmp, 'fixture-host')
            self.assertEqual({'status': 'passed'}, result)
            base = '/tmp/alexandria-lora-training-' + fixture['sha256']
            received = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertEqual([['-p', '--', base], ['-p', '--', base],
                              ['-p', '--', base + "/audio space; 'quoted' $literal"]], received)
            self.assertEqual(original, fixture)
            self.assertEqual(6, len(calls))
