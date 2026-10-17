"""Run the exact notebook lifecycle code without installers, models or signals."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import requests
import socket
import subprocess
import sys
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


class ColabOllamaTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent.parent.parent
        self.notebook = json.loads((root / 'alexandria_colab.ipynb').read_text())
        source = ''.join(self.notebook['cells'][12]['source'])
        self.stop_source = ''.join(self.notebook['cells'][14]['source'])
        self.final_source = ''.join(self.notebook['cells'][18]['source'])
        functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)]
        self.events = []
        self.checked = Mock(side_effect=lambda argv: self.events.append(('checked', argv)))
        self.start = Mock()
        self.stop = Mock(side_effect=self.retire)
        self.namespace = {'os': os, 'subprocess': subprocess, 'time': time, 'requests': requests,
                          'APP_DIR': '/synthetic/app', 'run_checked_install_command': self.checked,
                          'start_owned_subprocess': self.start, 'stop_owned_subprocess': self.stop,
                          'is_subprocess_tree_running': lambda process: process.poll() is None}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'colab-ollama-functions', 'exec'),
             self.namespace)
        self.output = io.StringIO()

    def process(self):
        process = NS(returncode=None, _alexandria_control=Mock())
        process.poll = lambda: process.returncode
        return process

    def retire(self, process, timeout):
        self.events.append(('retired', process))
        self.assertEqual(10, timeout)
        process.returncode = 0

    def test_stop_retires_original_handle_then_clears_it_and_is_idempotent(self):
        process = self.process()
        self.namespace['ollama_process'] = process
        with contextlib.redirect_stdout(self.output):
            self.assertTrue(self.namespace['_stop_ollama']())
            self.assertFalse(self.namespace['_stop_ollama']())
        self.stop.assert_called_once_with(process, timeout=10)
        process._alexandria_control.close.assert_called_once_with()
        self.assertIsNone(self.namespace['ollama_process'])
        self.assertEqual(1, self.output.getvalue().count('tree retired'))

    def test_missing_or_foreign_handle_never_dispatches_a_signal(self):
        self.assertFalse(self.namespace['_stop_ollama']())
        foreign = NS(pid=123, poll=lambda: None)
        self.namespace['ollama_process'] = foreign
        with self.assertRaisesRegex(RuntimeError, 'ownership is unknown'):
            self.namespace['_stop_ollama']()
        self.assertIs(foreign, self.namespace['ollama_process'])
        self.stop.assert_not_called()

    def test_cleanup_errors_and_interruptions_preserve_handle_and_do_not_claim_success(self):
        for error in (OSError('denied'), subprocess.TimeoutExpired('owned', 10), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                process = self.process()
                self.namespace['ollama_process'] = process
                self.stop.side_effect = error
                with contextlib.redirect_stdout(self.output), self.assertRaises(type(error)) as caught:
                    self.namespace['_stop_ollama']()
                self.assertIs(error, caught.exception)
                self.assertIs(process, self.namespace['ollama_process'])
                process._alexandria_control.close.assert_not_called()
        self.assertNotIn('retired', self.output.getvalue())

    def test_cleanup_return_without_retirement_is_a_failure(self):
        process = self.process()
        self.namespace['ollama_process'] = process
        self.stop.side_effect = None
        with self.assertRaisesRegex(RuntimeError, 'has not retired'):
            self.namespace['_stop_ollama']()
        self.assertIs(process, self.namespace['ollama_process'])
        process._alexandria_control.close.assert_not_called()

    def test_readiness_retries_only_connection_and_timeout_with_same_policy(self):
        ready = Mock()
        ready.json.return_value = {'version': 'test'}
        responses = [requests.ConnectionError('starting'), requests.ReadTimeout('discovering'), ready]
        with patch.object(requests, 'get', side_effect=responses) as get, \
             patch.object(time, 'sleep') as sleep:
            self.namespace['_wait_ollama_ready'](self.process())
        self.assertEqual(3, get.call_count)
        self.assertTrue(all(call.kwargs == {'timeout': 2} for call in get.call_args_list))
        self.assertEqual([((1,), {}), ((1,), {})], sleep.call_args_list)

    def test_readiness_timeout_is_finite_and_owned_exit_refuses_before_request(self):
        with patch.object(requests, 'get', side_effect=requests.ReadTimeout('starting')) as get, \
             patch.object(time, 'sleep') as sleep:
            with self.assertRaises(TimeoutError):
                self.namespace['_wait_ollama_ready'](self.process())
            self.assertEqual(60, get.call_count)
            self.assertEqual(60, sleep.call_count)
        process = self.process()
        process.returncode = 1
        with patch.object(requests, 'get') as get, self.assertRaisesRegex(RuntimeError, 'exited'):
            self.namespace['_wait_ollama_ready'](process)
        get.assert_not_called()

    def test_HTTP_errors_and_malformed_readiness_do_not_become_transient(self):
        for failure in ('HTTP', 'version', 'exit'):
            with self.subTest(failure=failure):
                response = Mock()
                response.json.return_value = {'version': None if failure == 'version' else 'test'}
                process = self.process()
                if failure == 'HTTP':
                    response.raise_for_status.side_effect = requests.HTTPError('503')
                if failure == 'exit':
                    response.json.side_effect = lambda: setattr(process, 'returncode', 1) or {'version': 'test'}
                expected = requests.HTTPError if failure == 'HTTP' else RuntimeError
                with patch.object(requests, 'get', return_value=response) as get, \
                     patch.object(time, 'sleep') as sleep, self.assertRaises(expected):
                    self.namespace['_wait_ollama_ready'](process)
                self.assertEqual(1, get.call_count)
                sleep.assert_not_called()

    def test_restart_retires_previous_tree_before_replacing_handle_and_binds_pull(self):
        old, new = self.process(), self.process()
        self.namespace['ollama_process'] = old
        self.start.side_effect = lambda *args, **kwargs: self.events.append(('start', kwargs)) or new
        wait = Mock(side_effect=lambda process: self.events.append(('ready', process)))
        self.namespace['_wait_ollama_ready'] = wait
        with patch.object(socket, 'socket'), \
             contextlib.redirect_stdout(self.output):
            self.namespace['start_notebook_ollama']('model; literal')
        self.assertEqual('retired', self.events[0][0])
        self.assertEqual('start', self.events[1][0])
        self.assertIs(new, self.namespace['ollama_process'])
        self.start.assert_called_once()
        self.assertEqual(['ollama', 'serve'], self.start.call_args.args[0])
        self.assertTrue(self.start.call_args.kwargs['start_new_session'])
        self.assertEqual('127.0.0.1:11434', self.start.call_args.kwargs['env']['OLLAMA_HOST'])
        self.checked.assert_called_once_with(['env', 'OLLAMA_HOST=127.0.0.1:11434',
                                             'ollama', 'pull', 'model; literal'])

    def test_existing_listener_refuses_before_start_or_model_pull(self):
        with patch.object(socket, 'socket') as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError('in use')
            with self.assertRaises(OSError):
                self.namespace['start_notebook_ollama']('model')
        self.start.assert_not_called()
        self.checked.assert_not_called()

    def test_startup_or_pull_failure_retires_only_the_new_owner_and_never_prints_running(self):
        for site in ('ready', 'pull'):
            with self.subTest(site=site):
                process = self.process()
                self.start.return_value = process
                error = subprocess.CalledProcessError(9, ['ollama', 'pull'])
                self.checked.side_effect = error if site == 'pull' else None
                wait = Mock(side_effect=error if site == 'ready' else None)
                self.namespace['_wait_ollama_ready'] = wait
                with patch.object(socket, 'socket'), \
                     contextlib.redirect_stdout(self.output), self.assertRaises(type(error)) as caught:
                    self.namespace['start_notebook_ollama']('model')
                self.assertIs(error, caught.exception)
                self.assertIsNone(self.namespace['ollama_process'])
                process._alexandria_control.close.assert_called_once_with()
        self.assertNotIn('is running with', self.output.getvalue())

    def test_primary_start_failure_keeps_cleanup_refusal_and_original_handle(self):
        process = self.process()
        self.start.return_value = process
        primary, cleanup = RuntimeError('readiness failed'), OSError('cleanup refused')
        self.stop.side_effect = cleanup
        self.namespace['_wait_ollama_ready'] = Mock(side_effect=primary)
        with patch.object(socket, 'socket'), \
             self.assertRaises(RuntimeError) as caught:
            self.namespace['start_notebook_ollama']('model')
        self.assertIs(primary, caught.exception)
        self.assertIs(cleanup, caught.exception.__cause__)
        self.assertIs(process, self.namespace['ollama_process'])

    def test_stop_cell_uses_helper_and_keeps_VRAM_readback_separate(self):
        stop = Mock(return_value=True)
        usage = NS(stdout='9000 MiB, 15000 MiB\n')
        with patch.object(subprocess, 'run', return_value=usage) as run, \
             contextlib.redirect_stdout(self.output):
            exec(self.stop_source, {'_stop_ollama': stop})
        stop.assert_called_once_with()
        self.assertEqual('nvidia-smi', run.call_args.args[0][0])
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertIn(usage.stdout, self.output.getvalue())
        self.assertNotIn('memory freed', self.output.getvalue())
        with patch.object(subprocess, 'run') as run, self.assertRaises(RuntimeError):
            exec(self.stop_source, {})
        run.assert_not_called()

    def test_final_cleanup_attempts_both_services_when_tunnel_or_server_fails(self):
        for site in ('tunnel', 'server'):
            with self.subTest(site=site):
                events = []
                error = RuntimeError(site)
                def cleanup(name):
                    events.append(name)
                    if name == site:
                        raise error
                ngrok = NS(kill=lambda: cleanup('tunnel'))
                namespace = {'_stop_server': lambda: cleanup('server'),
                             '_stop_ollama': lambda: cleanup('ollama')}
                with patch.dict(sys.modules, pyngrok=NS(ngrok=ngrok)), \
                     contextlib.redirect_stdout(self.output), self.assertRaises(RuntimeError) as caught:
                    exec(self.final_source, namespace)
                self.assertIs(error, caught.exception)
                self.assertEqual(['tunnel', 'server', 'ollama'], events)

    def test_checked_install_failures_stop_before_start_and_pull_failure_retires_owner(self):
        source = ''.join(self.notebook['cells'][12]['source'])
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at):
                calls = []
                process = self.process()
                self.start.reset_mock()
                self.stop.reset_mock()
                self.start.return_value = process
                error = subprocess.CalledProcessError(9, ['synthetic command'])
                def checked(argv):
                    calls.append(argv)
                    if len(calls) == fail_at:
                        raise error
                response = Mock()
                response.json.return_value = {'version': 'test'}
                ownership = NS(start_owned_subprocess=self.start, stop_owned_subprocess=self.stop,
                               is_subprocess_tree_running=lambda p: p.poll() is None)
                namespace = {'ALEXANDRIA_DIR': '/synthetic', 'run_checked_install_command': checked}
                with patch.dict(sys.modules, subprocess_ownership=ownership), \
                     patch.object(sys, 'path', sys.path[:]), patch.object(socket, 'socket'), \
                     patch.object(requests, 'get', return_value=response), \
                     contextlib.redirect_stdout(self.output), self.assertRaises(type(error)) as caught:
                    exec(source, namespace)
                self.assertIs(error, caught.exception)
                self.assertEqual(fail_at, len(calls))
                self.assertEqual(int(fail_at == 3), self.start.call_count)
                self.assertEqual(int(fail_at == 3), self.stop.call_count)
                if fail_at >= 2:
                    self.assertEqual(['bash', '-o', 'pipefail'], calls[1][:3])
        self.assertNotIn('is running with', self.output.getvalue())

    def test_final_cleanup_refuses_a_legacy_handle_without_signalling_it(self):
        foreign = NS(terminate=Mock())
        namespace = {'ollama_process': foreign}
        with patch.dict(sys.modules, pyngrok=NS(ngrok=NS(kill=Mock()))), \
             contextlib.redirect_stdout(self.output), self.assertRaisesRegex(RuntimeError, 'ownership is unknown'):
            exec(self.final_source, namespace)
        foreign.terminate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
