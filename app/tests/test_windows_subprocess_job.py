"""Typed Win32 boundary contracts; these fixtures do not execute Windows APIs."""
import ctypes
import unittest
from unittest.mock import patch

import windows_subprocess_job as jobs


class JobApiFixture:
    def __init__(self):
        self.handle = 0x100000001
        self.calls = []
        self.fail = None
        self.counts = [0]
        self.pids = [100, 101, 102]
        self.nonmember = 102

    def CreateJobObjectW(self, attributes, name):
        self.calls.append(('create', attributes, name))
        return 0 if self.fail == 'create' else self.handle

    def SetInformationJobObject(self, handle, kind, address, size):
        limits = ctypes.cast(address, ctypes.POINTER(jobs._ExtendedLimits)).contents
        self.calls.append(('limits', handle, kind, size,
                           limits.BasicLimitInformation.LimitFlags,
                           limits.ProcessMemoryLimit, limits.JobMemoryLimit))
        return self.fail != 'limits'

    def AssignProcessToJobObject(self, handle, process_handle):
        self.calls.append(('assign', handle, process_handle))
        return self.fail != 'assign'

    def QueryInformationJobObject(self, handle, kind, address, size, returned):
        self.calls.append(('query', handle, kind, size, returned))
        if kind == 3:
            header = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32 * 2)).contents
            capacity = (size - 8) // ctypes.sizeof(ctypes.c_size_t)
            header[0] = len(self.pids)
            header[1] = min(capacity, len(self.pids))
            data = ctypes.cast(ctypes.addressof(header) + 8,
                               ctypes.POINTER(ctypes.c_size_t * capacity)).contents
            for index, pid in enumerate(self.pids[:capacity]):
                data[index] = pid
            return self.fail != 'query'
        information = ctypes.cast(address, ctypes.POINTER(jobs._Accounting)).contents
        information.ActiveProcesses = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        return self.fail != 'query'

    def TerminateJobObject(self, handle, exit_code):
        self.calls.append(('terminate', handle, exit_code))
        return self.fail != 'terminate'

    def CloseHandle(self, handle):
        self.calls.append(('close', handle))
        return self.fail != 'close'

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(('open_process', access, inherit, pid))
        return pid + 0x100000000

    def IsProcessInJob(self, process_handle, job_handle, address):
        self.calls.append(('is_member', process_handle, job_handle))
        ctypes.cast(address, ctypes.POINTER(ctypes.c_int)).contents.value = (
            process_handle - 0x100000000 != self.nonmember)
        return True


class WindowsJobBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.api = JobApiFixture()
        self.loader = patch.object(jobs, 'get_windows_job_api', return_value=self.api)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        # ctypes Windows error helpers do not exist on the Linux test host.
        self.errors = patch.object(jobs, 'raise_windows_job_error',
                                   side_effect=lambda name: (_ for _ in ()).throw(OSError(name)))
        self.errors.start()
        self.addCleanup(self.errors.stop)

    def test_limits_contain_children_without_changing_resource_caps(self):
        job = jobs.WindowsSubprocessJob()
        self.assertEqual(('limits', self.api.handle, 9, ctypes.sizeof(jobs._ExtendedLimits),
                          0x2000, 0, 0), self.api.calls[1])
        job.apply_process_membership(0x200000002)
        self.assertEqual(('assign', self.api.handle, 0x200000002), self.api.calls[-1])
        job.close()
        job.close()
        self.assertEqual(1, sum(call[0] == 'close' for call in self.api.calls))

    def test_failed_configuration_closes_unadmitted_job(self):
        self.api.fail = 'limits'
        with self.assertRaisesRegex(OSError, 'SetInformationJobObject'):
            jobs.WindowsSubprocessJob()
        self.assertEqual(('close', self.api.handle), self.api.calls[-1])
        self.assertFalse(any(call[0] == 'assign' for call in self.api.calls))

    def test_failed_create_does_not_configure_or_close_invalid_handle(self):
        self.api.fail = 'create'
        with self.assertRaisesRegex(OSError, 'CreateJobObjectW'):
            jobs.WindowsSubprocessJob()
        self.assertEqual([('create', None, None)], self.api.calls)

    def test_membership_termination_and_query_errors_are_not_success(self):
        job = jobs.WindowsSubprocessJob()
        for operation, callback, error in (
                ('assign', lambda: job.apply_process_membership(999), 'AssignProcessToJobObject'),
                ('terminate', job.terminate, 'TerminateJobObject'),
                ('query', job.get_active_process_count, 'QueryInformationJobObject')):
            with self.subTest(operation=operation):
                self.api.fail = operation
                with self.assertRaisesRegex(OSError, error):
                    callback()
                self.assertEqual(self.api.handle, job.handle)
        self.api.fail = None
        job.close()

    def test_stopped_proof_waits_for_zero_membership(self):
        job = jobs.WindowsSubprocessJob()
        self.api.counts = [3, 2, 1, 0]
        with patch.object(jobs.time, 'sleep') as sleep:
            job.terminate(7)
            job.ensure_stopped()
        self.assertEqual(3, sleep.call_count)
        self.assertIn(('terminate', self.api.handle, 7), self.api.calls)
        self.assertFalse(any(call[0] == 'close' for call in self.api.calls))
        job.close()

    def test_failed_close_retains_the_live_handle_for_retry(self):
        job = jobs.WindowsSubprocessJob()
        self.api.fail = 'close'
        with self.assertRaisesRegex(OSError, 'CloseHandle'):
            job.close()
        self.assertEqual(self.api.handle, job.handle)
        self.api.fail = None
        job.close()
        self.assertIsNone(job.handle)

    def test_native_signatures_preserve_pointer_width(self):
        from types import SimpleNamespace
        names = ('CreateJobObjectW', 'SetInformationJobObject',
                 'AssignProcessToJobObject', 'QueryInformationJobObject',
                 'TerminateJobObject', 'CloseHandle', 'OpenProcess', 'IsProcessInJob',
                 'ExitProcess')
        library = SimpleNamespace(**{name: SimpleNamespace() for name in names})
        with patch.object(ctypes, 'WinDLL', return_value=library, create=True):
            self.assertIs(library, self.real_api_loader())
        self.assertIs(ctypes.c_void_p, library.CreateJobObjectW.restype)
        self.assertIs(ctypes.c_void_p, library.AssignProcessToJobObject.argtypes[1])
        self.assertIs(ctypes.c_uint32, library.QueryInformationJobObject.argtypes[3])

    def test_known_windows_x64_structure_offsets(self):
        # Explicit SDK layout values catch a self-consistent but wrong fake API.
        self.assertEqual(40, jobs._Accounting.ActiveProcesses.offset)
        self.assertEqual(48, ctypes.sizeof(jobs._Accounting))
        self.assertEqual(16, jobs._BasicLimits.LimitFlags.offset)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(64, ctypes.sizeof(jobs._BasicLimits))
            self.assertEqual(144, ctypes.sizeof(jobs._ExtendedLimits))

    def test_graceful_termination_preserves_owner_and_nonmembers(self):
        job = jobs.WindowsSubprocessJob()
        def terminate(argv, **kwargs):
            self.assertEqual(['taskkill', '/PID', '101'], argv)
            self.assertNotIn('/T', argv)
            self.assertNotIn('/F', argv)
            self.assertFalse(any(call == ('close', 101 + 0x100000000)
                                 for call in self.api.calls))
        with patch.object(jobs.subprocess, 'run', side_effect=terminate) as run:
            job.request_termination(100, 10)
        self.assertEqual(1, run.call_count)
        self.assertIn(('close', 101 + 0x100000000), self.api.calls)
        self.assertIn(('close', 102 + 0x100000000), self.api.calls)
        self.assertFalse(any(call[0] == 'open_process' and call[-1] == 100
                             for call in self.api.calls))
        job.close()

    def test_membership_buffer_grows_without_discarding_detached_members(self):
        self.api.pids = list(range(1, 51))
        job = jobs.WindowsSubprocessJob()
        self.assertEqual(self.api.pids, job.get_process_ids())
        self.assertEqual(2, sum(call[0] == 'query' for call in self.api.calls))
        job.close()

    def test_failed_taskkill_closes_member_handle_and_retains_job(self):
        job = jobs.WindowsSubprocessJob()
        with patch.object(jobs.subprocess, 'run', side_effect=jobs.subprocess.CalledProcessError(5, ['taskkill'])):
            with self.assertRaisesRegex(OSError, 'taskkill failed for job member 101'):
                job.request_termination(100, 10)
        self.assertIn(('close', 101 + 0x100000000), self.api.calls)
        self.assertEqual(self.api.handle, job.handle)
        job.close()

    def test_grace_request_budget_is_shared_across_all_members(self):
        job = jobs.WindowsSubprocessJob()
        self.api.nonmember = None
        with patch.object(jobs.time, 'monotonic', side_effect=[0, 1, 11]), \
             patch.object(jobs.subprocess, 'run') as run:
            with self.assertRaisesRegex(OSError, 'termination request timed out'):
                job.request_termination(100, 10)
        self.assertEqual(1, run.call_count)
        self.assertEqual(9, run.call_args.kwargs['timeout'])
        self.assertEqual(self.api.handle, job.handle)
        job.close()

    def real_api_loader(self):
        self.loader.stop()
        try:
            return jobs.get_windows_job_api()
        finally:
            self.loader.start()
