"""Quick fixtures own a private native lease; full inference keeps the shared one."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from experiments import gpu_guard
import run_isolated_api_tests as runner
from tests import test_api as client


class IsolatedApiLockTests(unittest.TestCase):
    def run_fixture(self, arguments, failure=None):
        before = dict(os.environ)
        original_arguments = list(arguments)
        observed = {}
        server = MagicMock()
        server.poll.return_value = None
        server._alexandria_control = MagicMock()
        error = RuntimeError('fixture ' + str(failure))

        def check_context():
            env = observed['server_env']
            self.assertTrue(Path(env['ALEXANDRIA_DATA_DIR']).is_dir())
            if not client.parse_api_test_arguments(arguments).full:
                self.assertTrue(Path(env['GPU_LOCK']).parent.is_dir())
                self.assertEqual(env['GPU_LOCK'], os.environ['GPU_LOCK'])

        def launch(command, **kwargs):
            observed['server_env'] = dict(kwargs['env'])
            self.assertFalse((Path(kwargs['env']['ALEXANDRIA_DATA_DIR']) / 'uploads/test.wav').exists())
            check_context()
            if failure == 'launch':
                raise error
            return server

        def suite(command, **kwargs):
            check_context()
            observed['client_env'] = dict(kwargs['env'])
            observed['client_args'] = command[5:]
            if failure == 'suite':
                raise error
            return SimpleNamespace(returncode=7)

        def stop(process, **kwargs):
            self.assertIs(server, process)
            check_context()
            if failure == 'stop':
                raise error

        server._alexandria_control.close.side_effect = check_context
        with patch.object(runner, 'start_owned_subprocess', side_effect=launch), \
             patch.object(runner, 'get_isolated_server_port', return_value=18765), \
             patch.object(runner, 'urlopen', side_effect=error if failure == 'readiness' else None,
                          return_value=MagicMock()), \
             patch.object(runner.subprocess, 'run', side_effect=suite), \
             patch.object(runner, 'stop_owned_subprocess', side_effect=stop) as stopped:
            if failure:
                with self.assertRaises(RuntimeError) as caught:
                    runner.main(arguments)
                self.assertIs(error, caught.exception)
            else:
                self.assertEqual(7, runner.main(arguments))
            self.assertEqual(0 if failure == 'launch' else 1, stopped.call_count)
        self.assertEqual(before, dict(os.environ))
        self.assertEqual(original_arguments, arguments)
        self.assertFalse(Path(observed['server_env']['ALEXANDRIA_DATA_DIR']).exists())
        if not client.parse_api_test_arguments(arguments).full:
            self.assertFalse(Path(observed['server_env']['GPU_LOCK']).parent.exists())
        return observed

    def test_quick_mode_replaces_only_fixture_lease_and_restores_environment(self):
        with patch.dict(os.environ, {'GPU_LOCK': '/fixture/shared.lock',
                                     'ALEXANDRIA_GPU_LOCK_HELD': '1',
                                     'ALEXANDRIA_GPU_LOCK_PID': '12345',
                                     'ALEXANDRIA_GPU_LOCK_FD': '9'}):
            observed = self.run_fixture([])
        env = observed['server_env']
        self.assertNotEqual('/fixture/shared.lock', env['GPU_LOCK'])
        self.assertEqual('0', env['ALEXANDRIA_GPU_LOCK_HELD'])
        self.assertEqual(env, observed['client_env'])

    def test_full_and_abbreviated_full_preserve_shared_ownership_exactly(self):
        fields = {'GPU_LOCK': '/fixture/shared.lock', 'ALEXANDRIA_GPU_LOCK_HELD': '1',
                  'ALEXANDRIA_GPU_LOCK_PID': '12345', 'ALEXANDRIA_GPU_LOCK_FD': '9'}
        for option in ('--full', '--ful', '--f'):
            with self.subTest(option=option), patch.dict(os.environ, fields), \
                 patch.object(runner, 'use_private_cpu_fixture_lock',
                              side_effect=AssertionError('full must keep shared lease')):
                observed = self.run_fixture([option])
                for key, value in fields.items():
                    self.assertEqual(value, observed['server_env'][key])
                    self.assertEqual(value, observed['client_env'][key])

    def test_cli_forwards_urls_and_summary_without_reinterpreting_full_mode(self):
        arguments = ['--url', 'http://first.invalid', '--url', 'http://last.invalid',
                     '--json-summary', 'summary with spaces.json', '--ful']
        parsed = client.parse_api_test_arguments(arguments)
        self.assertEqual('http://last.invalid', parsed.url)
        self.assertEqual('summary with spaces.json', parsed.json_summary)
        self.assertTrue(parsed.full)
        observed = self.run_fixture(arguments)
        self.assertEqual(arguments, observed['client_args'])
        self.assertIs(client.parse_api_test_arguments, runner.parse_api_test_arguments)

    def test_invalid_arguments_and_help_exit_before_starting_a_server(self):
        for arguments, status in [(['--full=false'], 2), (['--unknown'], 2), (['--help'], 0)]:
            with self.subTest(arguments=arguments), \
                 patch.object(runner, 'start_owned_subprocess') as launch, \
                 patch.object(runner, 'use_private_cpu_fixture_lock') as lease, \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    runner.main(arguments)
                self.assertEqual(status, caught.exception.code)
                launch.assert_not_called()
                lease.assert_not_called()

    def test_failure_paths_restore_private_context_and_keep_original_error(self):
        for failure in ('launch', 'readiness', 'suite', 'stop'):
            with self.subTest(failure=failure):
                self.run_fixture([], failure)

    def test_native_quick_server_returns_404_while_original_lease_stays_held(self):
        probe = '''
import os
from experiments import gpu_guard
from tests import test_api as client
client.BASE_URL = os.environ['FIXTURE_URL']
client.test_lora_delete_dataset_404()
client.test_lora_delete_model_404()
assert os.environ['GPU_LOCK'] != os.environ['FIXTURE_ORIGINAL_LOCK']
assert os.environ['ALEXANDRIA_GPU_LOCK_HELD'] == '0'
assert gpu_guard.gpu_is_busy(os.environ['FIXTURE_ORIGINAL_LOCK'])
handle = gpu_guard.acquire_gpu_lock()
assert handle is not None
try:
    try: gpu_guard.acquire_gpu_lock()
    except RuntimeError: pass
    else: raise AssertionError('private native lease allowed a second owner')
finally: gpu_guard.release_gpu_lock(handle)
print('NATIVE_PRIVATE_LEASE_AND_BOTH_404_VERIFIED')
'''
        actual_start = runner.start_owned_subprocess
        actual_run = subprocess.run
        observed = {}
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile() as server_log, \
             patch.dict(os.environ, {'GPU_LOCK': str(Path(tmp) / 'original.lock'),
                                     'ALEXANDRIA_GPU_LOCK_HELD': '0'}):
            original = os.environ['GPU_LOCK']
            lease = gpu_guard.acquire_gpu_lock()
            try:
                self.assertTrue(gpu_guard.gpu_is_busy(original))

                def launch(command, **kwargs):
                    observed['data'] = kwargs['env']['ALEXANDRIA_DATA_DIR']
                    kwargs.update(stdout=server_log, stderr=subprocess.STDOUT)
                    return actual_start(command, **kwargs)

                def run_probe(command, **kwargs):
                    url = command[command.index('--url') + 1]
                    result = actual_run([sys.executable, '-c', probe], cwd=kwargs['cwd'],
                                        env=dict(kwargs['env'], FIXTURE_URL=url,
                                                 FIXTURE_ORIGINAL_LOCK=original),
                                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('NATIVE_PRIVATE_LEASE_AND_BOTH_404_VERIFIED', result.stdout)
                    return result

                with patch.object(runner, 'start_owned_subprocess', side_effect=launch), \
                     patch.object(runner.subprocess, 'run', side_effect=run_probe):
                    self.assertEqual(0, runner.main([]))
                self.assertTrue(gpu_guard.gpu_is_busy(original))
                self.assertFalse(Path(observed['data']).exists())
            finally:
                gpu_guard.release_gpu_lock(lease)
            self.assertFalse(gpu_guard.gpu_is_busy(original))

    def test_quick_preparer_missing_fixture_cannot_dispatch_a_subprocess(self):
        from fastapi import BackgroundTasks
        from routers import preparer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            state = {'running': False, 'cancel': False}
            workers = []

            def register(*arguments):
                workers.append(arguments[-1])

            with patch.object(preparer, 'UPLOADS_DIR', str(uploads)), \
                 patch.object(preparer, 'PREPARER_OUTPUT_DIR', str(root / 'outputs')), \
                 patch.object(preparer, 'process_state', {'batch_preparer': state}), \
                 patch.object(preparer, '_resolve_preparer_interpreter', return_value=sys.executable), \
                 patch.object(preparer, '_revalidate_voicelab_paths', return_value=None), \
                 patch.object(preparer, 'check_global_gpu_lock'), \
                 patch.object(preparer, 'check_disk_space', return_value=(True, 10)), \
                 patch.object(preparer, 'reserve_background_task', return_value='cpu-fixture'), \
                 patch.object(preparer, 'register_claimed_background_task', side_effect=register), \
                 patch.object(preparer, '_stream_subprocess_to_logs') as dispatch:
                request = preparer.BatchPreparerRequest(tasks=[
                    {'audio_filename': 'test.wav', 'output_filename': 'test.zip'}])
                result = asyncio.run(preparer._start_preparer_batch(request, BackgroundTasks()))
                self.assertEqual('started', result['status'])
                self.assertEqual(1, len(workers))
                workers[0]()
                self.assertFalse((uploads / 'test.wav').exists())
                self.assertEqual('failed', state['tasks'][0]['status'])
                self.assertFalse(state['running'])
                dispatch.assert_not_called()
