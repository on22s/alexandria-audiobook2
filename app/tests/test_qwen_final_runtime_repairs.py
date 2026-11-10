"""Final #1844 runtime repairs: lease handoff and bounded punctuation planning.

No model or GPU is used. Descriptor tests capture the actual launch contract;
the planner cost assertion counts index comparisons rather than elapsed time.
"""
import builtins
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import qwen_align as qa
import qwen_runtime as qr
from tests.test_readalong_qwen import _CaptureTasks, _FixtureMixin


class FinalGpuLeaseTests(_FixtureMixin):
    def _route_environment(self, gpu):
        import core
        import readalong_export as export
        from routers.editor import ReadaloudEpubRequest

        tmp = self._mkdtmp()
        editor, _manager = self._fixture(tmp)
        capability = self._ready_capability(self._mkdtmp())
        capability['runtime_identity'] = capability.pop('runtime')
        if gpu:
            capability.update(device='cuda:0', dtype='bfloat16', devices=[dict(
                device='cuda:0', capability=[8, 6], free_bytes=6 * 1024**3,
                total_bytes=8 * 1024**3)])
            capability['provider_digest'] = qr.get_qwen_provider_digest(
                capability['model_dir'], runtime_identity=dict(
                    qwen_asr_version='0.0.6', torch_version=capability['torch_version'],
                    device='cuda:0', dtype='bfloat16', identity=capability['runtime_identity']))
        captured = {}
        environments = []
        native_environment = core.get_gpu_task_environment

        def environment(state, env=None):
            self.assertIs(state, core.process_state['readaloud_epub'])
            self.assertEqual({}, env)
            result = native_environment(state, env)
            environments.append(result)
            return result

        def build(_manager, _cancel, _progress, *, chunks, timing):
            captured.update(timing)
            return False, 'controlled fixture stop'

        with (Path(tmp) / 'gpu.lock').open('a') as handle:
            gpu_fd = handle.fileno()
            tasks = _CaptureTasks()
            with patch.object(qr, 'get_qwen_capability', return_value=capability), \
                    patch.object(core, 'acquire_gpu_lock', return_value=handle), \
                    patch.object(core.project_manager, 'ensure_idle_local_models_released', return_value=True), \
                    patch.object(core, 'get_gpu_task_environment', side_effect=environment), \
                    patch.object(export, 'build_epub_export', side_effect=build):
                editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(
                    timing_mode='qwen', timing_device='cuda:0' if gpu else 'cpu'))
                function, args, kwargs = tasks.registered[0]
                function(*args, **kwargs)
            self.assertEqual(1, len(environments))
            self.assertEqual(environments[0], captured['worker_env'])
            self.assertIsInstance(captured['task_lease_fd'], int)
            if gpu:
                self.assertEqual(str(gpu_fd), captured['worker_env']['ALEXANDRIA_GPU_LOCK_FD'])
                self.assertNotEqual(captured['task_lease_fd'], gpu_fd)
                self.assertEqual({'GPU_LOCK', 'ALEXANDRIA_GPU_LOCK_HELD',
                                  'ALEXANDRIA_GPU_LOCK_PID', 'ALEXANDRIA_GPU_LOCK_FD'},
                                 set(captured['worker_env']))
            else:
                self.assertEqual({}, captured['worker_env'])

    def test_gpu_route_captures_live_owned_lease_only(self):
        self._route_environment(True)

    def test_cpu_route_captures_no_gpu_lease(self):
        self._route_environment(False)

    def test_builder_forwards_captured_worker_environment(self):
        import readalong_export as export

        tmp = self._mkdtmp()
        _editor, manager = self._fixture(tmp)
        capability = self._ready_capability(self._mkdtmp())
        identity = capability['runtime']
        marker = {'GPU_LOCK': 'controlled-lease-path'}
        timing = dict(timing_mode='qwen', device='cpu', dtype='float32',
                      language='English', provider_digest=capability['provider_digest'],
                      qwen_asr_version='0.0.6', torch_version=capability['torch_version'],
                      capability=capability, runtime=identity, worker_env=marker)
        observed = []

        def collect(*args, **kwargs):
            observed.append(kwargs)
            raise ValueError('controlled collect stop')

        with patch.object(export, 'collect_aligned_rows', side_effect=collect):
            success, message = export.build_epub_export(manager, lambda: False, timing=timing)
        self.assertFalse(success)
        self.assertIn('controlled collect stop', message)
        self.assertEqual(1, len(observed))
        self.assertIs(marker, observed[0]['worker_env'])


class FinalWorkerBridgeTests(unittest.TestCase):
    def test_collector_retains_distinct_gpu_and_task_descriptors(self):
        import core
        import readalong_export as export
        import subprocess_ownership as ownership

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcm = root / 'row.pcm'
            pcm.write_bytes(b'\0\0' * 24000)
            state = {'running': True}
            with (root / 'gpu.lock').open('a') as gpu, (root / 'task.lock').open('a') as task:
                with patch.object(core, 'process_state', {'readaloud_epub': state}), \
                        patch.object(core, '_gpu_leases', {'readaloud_epub': gpu}), \
                        patch.object(core, '_task_claims', {}):
                    environment = core.get_gpu_task_environment(state, env={})
                original = dict(environment)
                environment.update(PYTHONPATH='must-not-enter', HF_HUB_OFFLINE='0')
                captured = []

                def launch(command, **kwargs):
                    captured.append(kwargs)
                    job = json.loads(Path(command[-2]).read_text(encoding='utf-8'))
                    plan = job['rows'][0]['windows'][0]
                    token = dict(text='One.', start_time=0.0, end_time=.08)
                    window = dict(char_start=plan['char_start'], char_end=plan['char_end'],
                                  start_frame=0, end_frame=24000, offset_sec=0.0,
                                  slice_duration_sec=1.0, calls=1, tokens=[token])
                    result = dict(ok=True, tokens={'r0': [token]}, windows={'r0': [window]},
                                  provider=dict(qwen_asr_version='0.0.6', device='cuda:0',
                                                dtype='bfloat16', decoder='fixture', encoder_adapter='fixture'))
                    Path(command[-1]).write_text(json.dumps(result), encoding='utf-8')
                    return types.SimpleNamespace(wait=lambda timeout=None: 0, poll=lambda: 0)

                data = dict(layout=(1, 24000, 2), bounds=[(0, 24000)],
                            timeline=[({'text': 'One.'}, None, 0)], pcm_path=str(pcm))
                capability = dict(python=sys.executable, model_dir=tmp, device='cuda:0',
                                  dtype='bfloat16', qwen_asr_version='0.0.6')
                # Exercise the actual POSIX lease-option contract on every CI
                # host without launching, inheriting, or killing any process.
                with patch.object(ownership, 'sys', types.SimpleNamespace(platform='linux')), \
                        patch.object(qr, 'start_owned_subprocess', side_effect=launch):
                    rows, report = export.collect_aligned_rows(
                        data, 'English', capability, lambda: False, str(root / 'stage'),
                        task_lease_fd=task.fileno(), worker_env=environment)
                self.assertEqual(1, report['rows'])
                self.assertEqual('One.', rows[0]['tokens'][0]['text'])
                self.assertEqual(1, len(captured))
                self.assertEqual(gpu.fileno(), captured[0]['gpu_lease_fd'])
                self.assertEqual(task.fileno(), captured[0]['task_lease_fd'])
                self.assertNotEqual(captured[0]['gpu_lease_fd'], captured[0]['task_lease_fd'])
                child = captured[0]['env']
                self.assertEqual('1', child['HF_HUB_OFFLINE'])
                self.assertEqual('1', child['TRANSFORMERS_OFFLINE'])
                self.assertEqual('1', child['HF_HUB_DISABLE_TELEMETRY'])
                self.assertNotIn('PYTHONPATH', child)
                self.assertEqual(original['ALEXANDRIA_GPU_LOCK_FD'], child['ALEXANDRIA_GPU_LOCK_FD'])
                self.assertEqual('must-not-enter', environment['PYTHONPATH'])
                self.assertEqual('0', environment['HF_HUB_OFFLINE'])


class FinalAmbientLeaseTests(unittest.TestCase):
    def _assert_launch_contract(self, kind, ambient_present=True):
        import subprocess_ownership as ownership

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job, result = root / 'job.json', root / 'result.json'
            job.write_text(json.dumps({'device': 'cpu', 'dtype': 'float32', 'rows': []}))
            with (root / 'ambient.lock').open('a') as ambient, \
                    (root / 'admitted.lock').open('a') as admitted, \
                    (root / 'task.lock').open('a') as task:
                def gpu_environment(handle):
                    return dict(GPU_LOCK=str(handle.name), ALEXANDRIA_GPU_LOCK_HELD='1',
                                ALEXANDRIA_GPU_LOCK_PID=str(os.getpid()),
                                ALEXANDRIA_GPU_LOCK_FD=str(handle.fileno()))

                ambient_env = gpu_environment(ambient)
                worker_env = (None if kind == 'legacy' else
                              gpu_environment(admitted) if kind == 'gpu' else
                              dict(PYTHONPATH='controlled-override', HF_HUB_OFFLINE='0')
                              if kind == 'unsafe_cpu' else {})
                captured = []

                def launch(command, **kwargs):
                    captured.append(kwargs)
                    Path(command[-1]).write_text(json.dumps(
                        dict(ok=True, tokens={}, windows={}, provider={})))
                    return types.SimpleNamespace(wait=lambda timeout=None: 0, poll=lambda: 0)

                with patch.dict(os.environ, ambient_env, clear=False), \
                        patch.object(ownership, 'sys', types.SimpleNamespace(platform='linux')), \
                        patch.object(qr, 'start_owned_subprocess', side_effect=launch):
                    if not ambient_present:
                        for key in ambient_env:
                            os.environ.pop(key, None)
                    qr.run_qwen_alignment_full(['controlled-not-executed'], job, result,
                        lambda: False, task_lease_fd=task.fileno(), worker_env=worker_env)
                self.assertEqual(1, len(captured))
                actual = captured[0]
                expected = (ambient.fileno() if kind == 'legacy' else
                            admitted.fileno() if kind == 'gpu' else None)
                self.assertEqual(expected, actual['gpu_lease_fd'])
                self.assertEqual(task.fileno(), actual['task_lease_fd'])
                child = actual['env']
                for key in ('HF_HUB_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'TRANSFORMERS_OFFLINE'):
                    self.assertEqual('1', child[key])
                self.assertNotIn('PYTHONPATH', child)
                if kind in ('legacy', 'gpu'):
                    expected_env = ambient_env if kind == 'legacy' else gpu_environment(admitted)
                    for key, value in expected_env.items():
                        self.assertEqual(value, child[key])
                else:
                    for key in ambient_env:
                        self.assertNotIn(key, child)

    def test_explicit_cpu_clears_ambient_gpu_capability(self):
        self._assert_launch_contract('cpu')

    def test_explicit_cpu_override_stays_isolated(self):
        self._assert_launch_contract('unsafe_cpu')

    def test_legacy_none_retains_ambient_gpu_capability(self):
        self._assert_launch_contract('legacy')

    def test_captured_gpu_replaces_ambient_capability(self):
        self._assert_launch_contract('gpu')

    def test_explicit_cpu_without_ambient_gpu_keeps_task_lease(self):
        self._assert_launch_contract('cpu', ambient_present=False)


class FinalOwnedCaptureCancellationTests(unittest.TestCase):
    def _cancel_child(self, *, identity=False, timeout=60, callback_error=False):
        import subprocess_ownership as owned

        with tempfile.TemporaryDirectory() as tmp:
            ready, completed = Path(tmp) / 'ready', Path(tmp) / 'completed'
            code = ("import json,os,pathlib,sys,time\n"
                    "pathlib.Path(%r).write_text(str(os.getpid()))\n"
                    "time.sleep(5)\n"
                    "pathlib.Path(%r).touch()\n"
                    "print(json.dumps({'python_version':sys.version.split()[0],"
                    "'packages':{},'prefix':sys.prefix}))\n") % (str(ready), str(completed))
            processes, stopped = [], []
            native_start, native_stop = owned.start_owned_subprocess, owned.stop_owned_subprocess

            def start(*args, **kwargs):
                process = native_start(*args, **kwargs)
                processes.append(process)
                return process

            def stop(process, *args, **kwargs):
                result = native_stop(process, *args, **kwargs)
                self.assertIsNotNone(process.poll())
                self.assertFalse(owned.is_subprocess_tree_running(process))
                stopped.append(process)
                return result

            def cancelled():
                if ready.exists() and callback_error:
                    raise RuntimeError('controlled callback failure')
                return ready.exists()

            with patch.object(owned, 'start_owned_subprocess', side_effect=start), \
                    patch.object(owned, 'stop_owned_subprocess', side_effect=stop):
                error = RuntimeError if callback_error else ValueError
                message = 'controlled callback failure' if callback_error else 'cancelled'
                with self.assertRaisesRegex(error, message):
                    if identity:
                        with patch.object(qr, '_IDENTITY_SOURCE', code):
                            qr.get_qwen_runtime_identity(sys.executable, cancel_check=cancelled,
                                                         timeout=timeout)
                    else:
                        owned.run_owned_capture([sys.executable, '-c', code], timeout=timeout,
                                                cancel_check=cancelled)
            self.assertTrue(ready.exists(), 'cancellation must occur after real child admission')
            self.assertFalse(completed.exists(), 'capture waited for natural completion before cancelling')
            self.assertEqual(processes, stopped)
            self.assertEqual(1, len(processes))
            self.assertTrue(processes[0].stdout.closed)
            self.assertTrue(processes[0].stderr.closed)

    def test_identity_cancellation_stops_and_reaps_metadata_child(self):
        self._cancel_child(identity=True)

    def test_capture_timeout_none_can_cancel_and_reap(self):
        self._cancel_child(timeout=None)

    def test_callback_exception_stops_and_reaps_child(self):
        self._cancel_child(callback_error=True)

    def test_false_callback_preserves_outputs_and_nonzero_exit(self):
        import subprocess_ownership as owned

        command = [sys.executable, '-c',
                   "import sys;print('output');print('error',file=sys.stderr);sys.exit(7)"]
        for options in ({}, {'cancel_check': lambda: False}):
            with self.subTest(callback=bool(options)):
                result = owned.run_owned_capture(command, timeout=5, **options)
                self.assertEqual(7, result.returncode)
                self.assertEqual(b'output\r\n' if os.name == 'nt' else b'output\n', result.stdout)
                self.assertEqual(b'error\r\n' if os.name == 'nt' else b'error\n', result.stderr)

    def test_already_cancelled_capture_does_not_start_child(self):
        import subprocess_ownership as owned

        process = types.SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO())
        with patch.object(owned, 'start_owned_subprocess', return_value=process) as start, \
                patch.object(owned, 'stop_owned_subprocess'):
            with self.assertRaisesRegex(ValueError, 'Owned capture cancelled'):
                owned.run_owned_capture(['controlled-not-executed'], timeout=1,
                                        cancel_check=lambda: True)
        start.assert_not_called()

    def test_prelaunch_callback_exception_does_not_start_child(self):
        import subprocess_ownership as owned

        def cancelled():
            raise RuntimeError('controlled prelaunch callback failure')

        process = types.SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO())
        with patch.object(owned, 'start_owned_subprocess', return_value=process) as start, \
                patch.object(owned, 'stop_owned_subprocess'):
            with self.assertRaisesRegex(RuntimeError, 'controlled prelaunch callback failure'):
                owned.run_owned_capture(['controlled-not-executed'], timeout=1, cancel_check=cancelled)
        start.assert_not_called()

    def test_default_capture_uses_exact_single_communicate_call(self):
        import subprocess_ownership as owned

        for timeout in (3, None):
            process = types.SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO(),
                                            returncode=7, communicate=Mock(return_value=(b'out', b'err')))
            with patch.object(owned, 'start_owned_subprocess', return_value=process):
                result = owned.run_owned_capture(['controlled'], timeout=timeout)
            process.communicate.assert_called_once_with(timeout=timeout)
            self.assertEqual((7, b'out', b'err'), (result.returncode, result.stdout, result.stderr))
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)

    def test_polling_deadline_preserves_original_timeout_and_partial_bytes(self):
        import subprocess_ownership as owned

        command = ['controlled-child']
        process = types.SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO(), returncode=None,
            communicate=Mock(side_effect=[
                subprocess.TimeoutExpired(command, .1, output=b'first', stderr=b'error1'),
                subprocess.TimeoutExpired(command, .1, output=b'first+second', stderr=b'error1+2')]))
        with patch.object(owned, 'start_owned_subprocess', return_value=process), \
                patch.object(owned, 'stop_owned_subprocess') as stop, \
                patch.object(owned.time, 'monotonic', side_effect=[10., 10.05, 10.10, 10.15, 10.26]):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                owned.run_owned_capture(command, timeout=.25, cancel_check=lambda: False)
        self.assertEqual(command, caught.exception.cmd)
        self.assertEqual(.25, caught.exception.timeout)
        self.assertEqual(b'first+second', caught.exception.output)
        self.assertEqual(b'error1+2', caught.exception.stderr)
        self.assertEqual(2, process.communicate.call_count)
        stop.assert_called_once_with(process, timeout=1)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_actual_deadline_timeout_reaps_before_pipe_cleanup(self):
        import subprocess_ownership as owned

        processes, stopped = [], []
        native_start, native_stop = owned.start_owned_subprocess, owned.stop_owned_subprocess

        def start(*args, **kwargs):
            process = native_start(*args, **kwargs)
            processes.append(process)
            return process

        def stop(process, *args, **kwargs):
            result = native_stop(process, *args, **kwargs)
            self.assertIsNotNone(process.poll())
            self.assertFalse(owned.is_subprocess_tree_running(process))
            stopped.append(process)
            return result

        with patch.object(owned, 'start_owned_subprocess', side_effect=start), \
                patch.object(owned, 'stop_owned_subprocess', side_effect=stop):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                owned.run_owned_capture([sys.executable, '-c', 'import time;time.sleep(5)'],
                                        timeout=.2, cancel_check=lambda: False)
        self.assertEqual(.2, caught.exception.timeout)
        self.assertEqual(processes[0].args, caught.exception.cmd)
        self.assertEqual(processes, stopped)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)


class FinalPunctuationPlannerTests(unittest.TestCase):
    def test_punctuation_fold_does_not_rescan_all_spoken_candidates(self):
        text = 'w. ! ' * 32
        ranges = qa.cover_ranges(text, qa.split_sentences(text))
        sentences = [dict(start=start, end=end, words=qa.count_units(text[start:end]))
                     for start, end in ranges]
        original = [dict(sentence) for sentence in sentences]
        comparisons = [0]

        class CountedIndex(int):
            def __lt__(self, other):
                comparisons[0] += 1
                return int.__lt__(self, other)

            def __gt__(self, other):
                comparisons[0] += 1
                return int.__gt__(self, other)

        def enumerate_indexes(values, start=0):
            for index, value in builtins.enumerate(values, start):
                yield CountedIndex(index), value

        with patch.object(qa, 'enumerate', enumerate_indexes, create=True):
            windows = qa.get_aligner_windows(60.0, sentences, text=text)
        self.assertGreater(comparisons[0], 0, 'cost instrumentation must observe actual index work')
        self.assertLessEqual(comparisons[0], 4 * len(sentences),
                             'punctuation folding repeatedly compares all spoken candidates')
        self.assertEqual(original, sentences, 'planning must not mutate sentence inputs')
        self.assertEqual(0, windows[0]['char_start'])
        self.assertEqual(len(text), windows[-1]['char_end'])
        for previous, current in zip(windows, windows[1:]):
            self.assertEqual(previous['char_end'], current['char_start'])


if __name__ == '__main__':
    unittest.main()
