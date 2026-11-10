"""Final #1844 runtime repairs: lease handoff and bounded punctuation planning.

No model or GPU is used. Descriptor tests capture the actual launch contract;
the planner cost assertion counts consumed candidates rather than elapsed time.
"""
import builtins
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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


class FinalPunctuationPlannerTests(unittest.TestCase):
    def test_punctuation_fold_does_not_rescan_all_spoken_candidates(self):
        text = 'w. ! ' * 32
        ranges = qa.cover_ranges(text, qa.split_sentences(text))
        sentences = [dict(start=start, end=end, words=qa.count_units(text[start:end]))
                     for start, end in ranges]
        original = [dict(sentence) for sentence in sentences]
        consumed = [0]

        def counted(function):
            def run(*args, **kwargs):
                if len(args) == 1 and isinstance(args[0], types.GeneratorType):
                    def values():
                        for value in args[0]:
                            consumed[0] += 1
                            yield value
                    return function(values(), **kwargs)
                return function(*args, **kwargs)
            return run

        with patch.object(qa, 'max', counted(builtins.max), create=True), \
                patch.object(qa, 'min', counted(builtins.min), create=True):
            windows = qa.get_aligner_windows(60.0, sentences, text=text)
        self.assertLessEqual(consumed[0], 4 * len(sentences),
                             'punctuation folding repeatedly consumes all spoken candidates')
        self.assertEqual(original, sentences, 'planning must not mutate sentence inputs')
        self.assertEqual(0, windows[0]['char_start'])
        self.assertEqual(len(text), windows[-1]['char_end'])
        for previous, current in zip(windows, windows[1:]):
            self.assertEqual(previous['char_end'], current['char_start'])


if __name__ == '__main__':
    unittest.main()
