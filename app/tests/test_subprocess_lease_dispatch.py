"""Platform dispatch must forward ownership capabilities without altering inputs."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import subprocess_ownership as ownership


class SubprocessLeaseDispatchTests(unittest.TestCase):
    def test_supported_owners_borrow_verified_caller_leases_without_changing_environment(self):
        environment = {'ALEXANDRIA_GPU_LOCK_HELD': '1', 'ALEXANDRIA_GPU_LOCK_PID': str(os.getpid()),
                       'ALEXANDRIA_GPU_LOCK_FD': '37'}
        original = dict(environment)
        for platform in ('linux', 'darwin'):
            with self.subTest(platform=platform), patch.object(ownership, 'sys', SimpleNamespace(platform=platform)):
                self.assertEqual({'gpu_lease_fd': 37, 'task_lease_fd': 38},
                                 ownership.get_subprocess_lease_options(environment, 38))
                self.assertEqual(original, environment)

    def test_foreign_or_unmarked_gpu_claim_cannot_be_borrowed(self):
        for environment in ({}, {'ALEXANDRIA_GPU_LOCK_HELD': '0'},
                            {'ALEXANDRIA_GPU_LOCK_HELD': '1', 'ALEXANDRIA_GPU_LOCK_PID': '-1',
                             'ALEXANDRIA_GPU_LOCK_FD': 'not-a-descriptor'}):
            with self.subTest(environment=environment), patch.object(ownership, 'sys', SimpleNamespace(platform='darwin')):
                self.assertEqual({'gpu_lease_fd': None, 'task_lease_fd': 38},
                                 ownership.get_subprocess_lease_options(environment, 38))

    def test_unsupported_platform_does_not_interpret_posix_descriptors(self):
        environment = {'ALEXANDRIA_GPU_LOCK_HELD': '1', 'ALEXANDRIA_GPU_LOCK_PID': str(os.getpid()),
                       'ALEXANDRIA_GPU_LOCK_FD': 'invalid'}
        for platform in ('win32', 'freebsd'):
            with self.subTest(platform=platform), patch.object(ownership, 'sys', SimpleNamespace(platform=platform)):
                self.assertEqual({'gpu_lease_fd': None, 'task_lease_fd': None},
                                 ownership.get_subprocess_lease_options(environment, 38))

    def test_bad_claim_descriptor_fails_before_worker_admission(self):
        environment = {'ALEXANDRIA_GPU_LOCK_HELD': '1', 'ALEXANDRIA_GPU_LOCK_PID': str(os.getpid()),
                       'ALEXANDRIA_GPU_LOCK_FD': 'invalid'}
        with patch.object(ownership, 'sys', SimpleNamespace(platform='darwin')), self.assertRaises(ValueError):
            ownership.get_subprocess_lease_options(environment, 38)

    def test_darwin_dispatch_forwards_all_owner_options_to_native_launcher(self):
        launcher = Mock(return_value=object())
        module = SimpleNamespace(start_macos_owned_subprocess=launcher)
        environment = {'FIXTURE': 'preserved'}
        with patch.object(ownership, 'sys', SimpleNamespace(platform='darwin')), \
             patch.dict(sys.modules, {'macos_subprocess_owner': module}):
            result = ownership.start_owned_subprocess(['python', 'worker.py'], gpu_lease_fd=37,
                task_lease_fd=38, stop_receipt_path='receipt', disconnect_signal=15,
                exit_notice_path='exit', termination_grace=2, env=environment,
                cwd='workdir', stdout=1, stderr=2, start_new_session=True)
        self.assertIs(result, launcher.return_value)
        launcher.assert_called_once_with(['python', 'worker.py'], gpu_lease_fd=37,
            task_lease_fd=38, stop_receipt_path='receipt', disconnect_signal=15,
            exit_notice_path='exit', termination_grace=2, env=environment,
            cwd='workdir', stdout=1, stderr=2, start_new_session=True)
        self.assertEqual({'FIXTURE': 'preserved'}, environment)
