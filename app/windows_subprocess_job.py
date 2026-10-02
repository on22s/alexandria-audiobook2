"""Win32 kernel ownership boundary for detached subprocess descendants."""
import ctypes
import subprocess
import time


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        'TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime',
        'ThisPeriodTotalKernelTime')] + [(name, ctypes.c_uint32) for name in (
            'TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses',
            'TotalTerminatedProcesses')]


def get_windows_job_api():
    """Load typed native operations only when running on Windows."""
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
        'SetInformationJobObject': ([ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        'AssignProcessToJobObject': ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        'QueryInformationJobObject': ([ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                      ctypes.c_uint32, ctypes.c_void_p], ctypes.c_int),
        'TerminateJobObject': ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        'CloseHandle': ([ctypes.c_void_p], ctypes.c_int),
        'OpenProcess': ([ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
        'IsProcessInJob': ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        'ExitProcess': ([ctypes.c_uint32], None),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = arguments
        function.restype = result
    return api


def raise_windows_job_error(operation):
    error = ctypes.get_last_error()
    raise OSError(error, f'{operation} failed: {ctypes.FormatError(error)}')


def get_windows_job_active_process_count(api, handle=None):
    """Read explicit job membership, or the calling supervisor's current job."""
    information = _Accounting()
    if not api.QueryInformationJobObject(
            handle, 1, ctypes.byref(information), ctypes.sizeof(information), None):
        raise_windows_job_error('QueryInformationJobObject')
    return information.ActiveProcesses


class WindowsSubprocessJob:
    """Own one command tree without permitting ordinary CreateProcess breakaway."""
    def __init__(self):
        self.api = get_windows_job_api()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise_windows_job_error('CreateJobObjectW')
        try:
            limits = _ExtendedLimits()
            # No breakaway, resource caps, or changes to existing termination grace.
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise_windows_job_error('SetInformationJobObject')
        except BaseException:
            self.close()
            raise

    def apply_process_membership(self, process_handle):
        """Admit a trusted bootstrap before allowing it to launch any worker."""
        if not self.api.AssignProcessToJobObject(self.handle, process_handle):
            raise_windows_job_error('AssignProcessToJobObject')

    def get_active_process_count(self):
        return get_windows_job_active_process_count(self.api, self.handle)

    def get_process_ids(self):
        capacity = 16
        while True:
            class ProcessIds(ctypes.Structure):
                _fields_ = [('assigned', ctypes.c_uint32), ('returned', ctypes.c_uint32),
                            ('ids', ctypes.c_size_t * capacity)]
            information = ProcessIds()
            success = self.api.QueryInformationJobObject(
                self.handle, 3, ctypes.byref(information), ctypes.sizeof(information), None)
            if not success and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise_windows_job_error('QueryInformationJobObject')
            if success and information.returned == information.assigned:
                if information.returned > capacity:
                    raise OSError('Invalid job process membership buffer')
                return list(information.ids[:information.returned])
            capacity = max(capacity * 2, information.assigned)

    def request_termination(self, owner_pid, timeout):
        """Request existing Windows graceful termination for exact job members."""
        deadline = time.monotonic() + timeout
        for pid in self.get_process_ids():
            if pid == owner_pid:
                continue  # Keep the supervisor alive until all members stop.
            process_handle = self.api.OpenProcess(0x1000, False, pid)
            if not process_handle:
                if ctypes.get_last_error() == 87:  # Process already exited.
                    continue
                raise_windows_job_error('OpenProcess')
            try:
                member = ctypes.c_int()
                if not self.api.IsProcessInJob(process_handle, self.handle, ctypes.byref(member)):
                    raise_windows_job_error('IsProcessInJob')
                if member.value:
                    # Retain the process object through taskkill so its PID cannot
                    # be recycled. No /T: membership already supplies the full tree.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OSError('Windows job termination request timed out')
                    try:
                        subprocess.run(['taskkill', '/PID', str(pid)], check=True,
                                       capture_output=True, text=True, timeout=remaining)
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                        raise OSError(f'Windows taskkill failed for job member {pid}') from error
            finally:
                if not self.api.CloseHandle(process_handle):
                    raise_windows_job_error('CloseHandle')

    def terminate(self, exit_code=1):
        """Force termination of all job members, including detached descendants."""
        if not self.api.TerminateJobObject(self.handle, exit_code):
            raise_windows_job_error('TerminateJobObject')

    def ensure_stopped(self):
        """Wait for kernel-confirmed zero membership before ownership release."""
        while self.get_active_process_count():
            time.sleep(.05)

    def close(self):
        """Close a job handle once; retain it if the kernel rejects close."""
        if self.handle is not None:
            if not self.api.CloseHandle(self.handle):
                raise_windows_job_error('CloseHandle')
            self.handle = None
