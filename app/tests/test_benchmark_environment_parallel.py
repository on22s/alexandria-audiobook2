"""Concurrent read-only probe barriers; no SSH, GPU or model inference."""
from contextlib import ExitStack
import copy
import importlib.util
import os
import subprocess
import threading
import unittest
from unittest.mock import patch

import benchmark_environment as environment

if os.environ.get('BENCHMARK_ENVIRONMENT_SOURCE'):
    spec = importlib.util.spec_from_file_location('saved_environment', os.environ['BENCHMARK_ENVIRONMENT_SOURCE'])
    environment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(environment)


class BenchmarkEnvironmentParallelTests(unittest.TestCase):
    def execute(self, *, remote_root='/remote', failure=None):
        count = 4 if remote_root else 3
        barrier = threading.Barrier(count)
        called = []
        lock = threading.Lock()
        runtime = {'revision': 'a' * 40, 'platform': {'system': 'Linux'}, 'packages': {'local': '1'}}
        remote = {'hostname': 'thunder', 'python_version': '3.11', 'git_commit': 'a' * 40,
                  'packages': {'remote': '2'}, 'platform': {'system': 'Linux'},
                  'worktree': {'dirty': False, 'sha256': 'remote-tree'}}
        status = {'available': True, 'loaded': True, 'parallel': 1, 'context_length': 4096}
        before = copy.deepcopy((runtime, remote, status))
        def probe(name, result):
            def run(*args):
                with lock:
                    called.append((name, args))
                barrier.wait(timeout=1)
                if name == failure:
                    raise subprocess.TimeoutExpired('fixture-probe', 20)
                return result
            return run
        with ExitStack() as patches:
            patches.enter_context(patch.object(environment, 'get_runtime_info', return_value=runtime))
            patches.enter_context(patch.object(environment, '_get_local_worktree_identity',
                return_value={'dirty': False, 'sha256': 'local-tree'}))
            patches.enter_context(patch.object(environment, '_verify_remote_checkout',
                side_effect=probe('checkout', 'a' * 40)))
            patches.enter_context(patch.object(environment, '_get_remote_runtime_observations',
                side_effect=probe('runtime', remote)))
            patches.enter_context(patch.object(environment, 'get_remote_gpu_name_and_backend',
                side_effect=probe('gpu', ('A6000', 'cuda'))))
            patches.enter_context(patch.object(environment, 'get_remote_lmstudio_status',
                side_effect=probe('lmstudio', status)))
            if failure:
                with self.assertRaises(subprocess.TimeoutExpired):
                    environment.collect_thunder_environment('/repo', 'fixture-host', 'model',
                        remote_root=remote_root, remote_python='/python with spaces')
                result = None
            else:
                result = environment.collect_thunder_environment('/repo', 'fixture-host', 'model',
                    remote_root=remote_root, remote_python='/python with spaces')
        expected_calls = [('runtime', ('fixture-host', '/python with spaces', remote_root)),
                          ('gpu', ('fixture-host',)), ('lmstudio', ('fixture-host', 'model'))]
        if remote_root:
            expected_calls.append(('checkout', ('/repo', 'fixture-host', remote_root)))
        self.assertCountEqual(expected_calls, called)
        self.assertEqual(before, (runtime, remote, status))
        if result:
            expected = {'hostname': 'thunder', 'gpu_name': 'A6000', 'backend': 'cuda',
                'python_version': '3.11', 'git_commit': 'a' * 40,
                'worktree': remote['worktree'] if remote_root else {'dirty': False, 'sha256': 'local-tree'},
                'orchestrator_platform': runtime['platform'], 'packages': remote['packages'],
                'remote_platform': remote['platform'], 'python_executable': '/python with spaces',
                'orchestrator_git_commit': 'a' * 40,
                'orchestrator_worktree': {'dirty': False, 'sha256': 'local-tree'},
                'orchestrator_packages': runtime['packages'],
                'lmstudio': {'model_name': 'model', 'model_loaded': True,
                             'context_length': 4096, 'parallel': 1}}
            if remote_root:
                expected['remote_checkout_commit'] = 'a' * 40
            self.assertEqual(environment.build_environment_fingerprint('thunder', expected), result)

    def test_all_four_probes_overlap_and_preserve_exact_fingerprint(self):
        self.execute()

    def test_without_remote_root_only_three_read_only_probes_run(self):
        self.execute(remote_root=None)

    def test_probe_timeout_is_not_converted_into_a_valid_fingerprint(self):
        self.execute(failure='runtime')


class BenchmarkConcurrentProbeFailureTests(unittest.TestCase):
    def test_invalid_observations_cannot_publish_a_fingerprint(self):
        runtime = {'revision': 'a' * 40, 'platform': {}, 'packages': {}}
        remote = {'hostname': 'thunder', 'python_version': '3.11', 'git_commit': 'a' * 40,
                  'packages': {}, 'platform': {}, 'worktree': {'dirty': False, 'sha256': 'tree'}}
        for failure in ('checkout', 'gpu', 'lmstudio'):
            with self.subTest(failure=failure), ExitStack() as patches:
                patches.enter_context(patch.object(environment, 'get_runtime_info', return_value=runtime))
                patches.enter_context(patch.object(environment, '_get_local_worktree_identity', return_value={}))
                checkout = patches.enter_context(patch.object(environment, '_verify_remote_checkout',
                    return_value='a' * 40,
                    side_effect=ValueError('remote checkout must be clean and match the local git revision')
                        if failure == 'checkout' else None))
                patches.enter_context(patch.object(environment, '_get_remote_runtime_observations', return_value=remote))
                patches.enter_context(patch.object(environment, 'get_remote_gpu_name_and_backend',
                    return_value=(None, None) if failure == 'gpu' else ('A6000', 'cuda')))
                patches.enter_context(patch.object(environment, 'get_remote_lmstudio_status',
                    return_value={'available': failure != 'lmstudio', 'loaded': True}))
                fingerprint = patches.enter_context(patch.object(environment, 'build_environment_fingerprint'))
                with self.assertRaisesRegex(ValueError, 'must be clean' if failure == 'checkout' else
                        'GPU/backend' if failure == 'gpu' else 'status is unavailable'):
                    environment.collect_thunder_environment('/repo', 'fixture-host', 'model', '/remote')
                fingerprint.assert_not_called()
                checkout.assert_called_once_with('/repo', 'fixture-host', '/remote')
