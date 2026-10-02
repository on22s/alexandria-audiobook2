import base64
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import benchmark_runner
import tts_benchmark


class TtsBenchmarkFailureProtocolTests(unittest.TestCase):
    def invoke_main(self, payload, directory):
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['tts_benchmark', '--payload', encoded,
                                       '--output-dir', directory]), contextlib.redirect_stdout(output):
            tts_benchmark.main()
        markers = [line for line in output.getvalue().splitlines()
                   if line.startswith('TTS_BENCHMARK_RESULT=')]
        self.assertEqual(1, len(markers))
        return json.loads(markers[0].split('=', 1)[1])

    def test_engine_and_output_directory_setup_failures_emit_failed_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'fixtures': [], 'repetitions': 1}
            with patch.object(tts_benchmark, 'TTSEngine', side_effect=RuntimeError('engine setup broke')):
                result = self.invoke_main(payload, tmp)
            self.assertEqual({'status': 'failed', 'metrics': {}, 'error': 'engine setup broke'}, result)
            file = Path(tmp, 'file'); file.write_text('keep')
            with patch.object(tts_benchmark, 'TTSEngine', return_value=object()):
                result = self.invoke_main(payload, str(file / 'audio'))
            self.assertEqual('failed', result['status'])
            self.assertTrue(result['error'])
            self.assertEqual('keep', file.read_text())

    def test_successful_and_per_case_failed_results_preserve_list_protocol(self):
        cases = [{'fixture_id': 'f', 'repetition': 1, 'status': 'failed',
                  'metrics': {}, 'error': 'generation failed'}]
        with patch.object(tts_benchmark, 'execute_payload', return_value=cases):
            self.assertEqual(cases, self.invoke_main({}, 'unused'))

    def test_runner_reports_worker_setup_error_instead_of_iterating_envelope(self):
        result = subprocess.CompletedProcess([], 0,
            'banner\nTTS_BENCHMARK_RESULT={"status":"failed","metrics":{},"error":"engine setup broke"}\n', '')
        with patch.object(benchmark_runner, 'run_benchmark_subprocess', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'engine setup broke'):
                benchmark_runner._run_tts_worker({}, 'local', {}, '/fixture', '/output', '')

    def test_native_worker_engine_failure_has_machine_readable_result(self):
        worker = Path(tts_benchmark.__file__).resolve()
        bootstrap = '''import runpy,sys,types
module=types.ModuleType('tts')
class Engine:
 def __init__(self,*args): raise RuntimeError('native engine setup broke')
module.TTSEngine=Engine
sys.modules['tts']=module
worker=sys.argv.pop(1)
runpy.run_path(worker,run_name='__main__')
'''
        payload = base64.b64encode(json.dumps({'fixtures': [], 'repetitions': 1}).encode()).decode()
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, '-c', bootstrap, str(worker),
                '--payload', payload, '--output-dir', tmp], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        markers = [line for line in result.stdout.splitlines() if line.startswith('TTS_BENCHMARK_RESULT=')]
        self.assertEqual(1, len(markers), result.stdout)
        self.assertEqual({'status': 'failed', 'metrics': {}, 'error': 'native engine setup broke'},
                         json.loads(markers[0].split('=', 1)[1]))
