"""A trusted, gated Win32 job supervisor; no worker runs before admission."""
import json
import ctypes
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

from json_metadata import get_unique_json_object

from windows_subprocess_job import (WindowsSubprocessJob, get_windows_job_api,
                                    get_windows_job_active_process_count)


WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS = 10.0
WINDOWS_OWNER_RECOVERY_RETRY_SECONDS = 30.0
# Access denied, invalid handle and invalid parameter cannot recover by waiting.
# All other codes, including a missing code, retry within the same deadline:
# an unknown error is not evidence of a permanent failure or a stopped tree.
WINDOWS_OWNER_PERMANENT_ERRORS = frozenset({5, 6, 87})
_retained_windows_owners = set()


class WindowsOwnerCleanupError(OSError):
    def __init__(self, owner, error, *, deadline_exceeded=False):
        code = getattr(error, 'winerror', None) or error.errno
        reason = 'deadline exceeded' if deadline_exceeded else 'failed without retry'
        try:
            members = ', '.join(str(pid) for pid in owner.job.get_process_ids(max_queries=8))
            membership = f'Job member PIDs: {members or "none observed"}.'
        except OSError:
            membership = f'Job member PIDs unavailable; supervisor PID {owner.process.pid}.'
        super().__init__(code, f'Windows owner cleanup {reason} (Win32 error {code}); '
                         'owned process tree not proven dead; retaining ownership and task/GPU leases. '
                         f'{membership} End the owned processes in Task Manager or restart the app. '
                         'The retained locks release automatically once the job is confirmed empty.')
        self.owner = owner


def save_windows_owner_message(path, message):
    # Even escaped astral characters fit the bounded receipt. Keep a useful
    # error prefix rather than publishing a startup error the reader refuses.
    if isinstance(message.get('error'),str) and len(message['error']) > 256:
        message = {**message,'error':message['error'][:253]+'...'}
    temporary = path.with_suffix('.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(message, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_windows_owner_message(path):
    """Read one bounded, complete supervisor receipt; leave I/O retry to its owner."""
    with path.open('rb') as stream:
        raw = stream.read(4097)
    try:
        if len(raw) > 4096:
            raise ValueError('oversized receipt')
        message = json.loads(raw.decode('utf-8'),object_pairs_hook=get_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite receipt')))
        valid = isinstance(message,dict) and (
            (set(message) == {'started'} and type(message['started']) is int
             and 0 < message['started'] <= 0xffffffff)
            or (set(message) - {'worker_not_launched'} == {'error','errno'}
                and ('worker_not_launched' not in message or message['worker_not_launched'] is True)
                and isinstance(message['error'],str)
                and 0 < len(message['error']) <= 2048
                and (message['errno'] is None or type(message['errno']) is int
                     and -(2**31) <= message['errno'] < 2**31)))
        if not valid:
            raise ValueError('invalid receipt')
    except (ValueError,UnicodeError,RecursionError):
        raise OSError('Windows subprocess owner returned an invalid admission receipt') from None
    return message


def _serialize_owner_close(method):
    def guarded(self, *args, **kwargs):
        with self._close_lock:
            return method(self, *args, **kwargs)
    return guarded


class WindowsOwnerControl:
    def __init__(self, job, process, directory, termination_grace=None):
        self.job = job
        self.process = process
        self.directory = directory
        self._close_lock = threading.RLock()
        self.cleanup_error = None
        self._cleanup_retry_at = 0
        self.cancel_directory = None
        from subprocess_ownership import SUBPROCESS_TERMINATE_GRACE_SECONDS
        self.termination_grace = (SUBPROCESS_TERMINATE_GRACE_SECONDS if termination_grace is None
                                  else termination_grace)

    def sendall(self, data):
        if int(data.strip()) != signal.SIGTERM:
            raise OSError('Windows owner supports termination only')
        self.job.request_termination(self.process.pid, self.termination_grace)

    def terminate(self):
        self.job.terminate()

    @_serialize_owner_close
    def retry_cleanup(self):
        """Observe membership each time; serialize close and force retries on one schedule."""
        if self.job.handle is None or self.cleanup_error is None:
            return
        active = self.job.get_active_process_count()
        if (time.monotonic() < self._cleanup_retry_at
                or (active and self.cleanup_error.errno == 5)):
            return
        self._cleanup_retry_at = time.monotonic() + WINDOWS_OWNER_RECOVERY_RETRY_SECONDS
        if not active:
            self.close(deadline=time.monotonic())
            return
        try:
            self.job.terminate()
        except OSError as error:
            failure = WindowsOwnerCleanupError(self, error)
            self.cleanup_error = failure
            logging.error('%s', failure)
            raise failure from error

    @_serialize_owner_close
    def close(self, *, deadline=None):
        if self.job.handle is None:
            return
        if deadline is None:
            deadline = time.monotonic() + WINDOWS_OWNER_CLOSE_TIMEOUT_SECONDS
        backoff = .05
        reported = False
        while True:
            try:
                if self.job.get_active_process_count():
                    self.job.terminate()
                    self.job.ensure_stopped(deadline=deadline)
                self.job.close()
                self.cleanup_error = None
                _retained_windows_owners.discard(self)
                break
            except OSError as error:
                if not reported:
                    logging.exception('Could not stop Windows job; retaining ownership')
                    reported = True
                remaining = deadline - time.monotonic()
                code = getattr(error, 'winerror', None) or error.errno
                if code in WINDOWS_OWNER_PERMANENT_ERRORS or remaining <= 0:
                    from review_startup import record_failure
                    failure = WindowsOwnerCleanupError(self, error, deadline_exceeded=remaining <= 0)
                    self.cleanup_error = failure
                    self._cleanup_retry_at = time.monotonic() + WINDOWS_OWNER_RECOVERY_RETRY_SECONDS
                    _retained_windows_owners.add(self)
                    logging.error('%s', failure)
                    record_failure('windows_owner_cleanup_failed', failure, task='subprocess_owner')
                    raise failure from error
                time.sleep(min(backoff, remaining))
                backoff = min(backoff * 2, .5)
        try:
            self.directory.cleanup()
        except OSError:
            # Kernel membership is already stopped and the handle closed.
            # A blocked temporary receipt must not mask admission failure or
            # prevent the caller from waiting/closing its process and pipes.
            logging.exception('Could not remove Windows owner temporary files after job shutdown')
        if self.cancel_directory is not None:
            try:
                self.cancel_directory.cleanup()
            except OSError:
                logging.exception('Could not remove Windows cancel files after job shutdown')


def _inherited_windows_environment():
    # os.environ omits hidden per-drive current-directory entries. Preserve
    # the real inherited block when adding CPython's private bootstrap value.
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetEnvironmentStringsW.argtypes = []
    api.GetEnvironmentStringsW.restype = ctypes.c_void_p
    api.FreeEnvironmentStringsW.argtypes = [ctypes.c_void_p]
    api.FreeEnvironmentStringsW.restype = ctypes.c_int
    block = api.GetEnvironmentStringsW()
    if not block:
        raise ctypes.WinError(ctypes.get_last_error())
    environment = {}
    try:
        offset = 0
        while True:
            entry = ctypes.wstring_at(block + offset)
            if not entry:
                break
            # Python characters and native UTF-16 code units differ for
            # supplementary characters and may include lone surrogates.
            offset += len(entry.encode('utf-16-le', 'surrogatepass')) + 2
            split = entry.find('=', 1)
            if split < 0:
                raise OSError('Malformed native Windows environment entry')
            environment.setdefault(entry[:split], entry[split + 1:])
    finally:
        if not api.FreeEnvironmentStringsW(block):
            raise ctypes.WinError(ctypes.get_last_error())
    return environment


def _popen_owned_python(command, **kwargs):
    # CPython's Windows multiprocessing bootstrap bypasses the venv redirector
    # this way. The redirector can start Python before its own job assignment.
    base = getattr(sys, '_base_executable', None)
    if (sys.platform == 'win32' and base and command
            and os.path.normcase(str(command[0])) == os.path.normcase(sys.executable)
            and os.path.normcase(sys.executable) != os.path.normcase(base)):
        command = [base, *command[1:]]
        environment = (_inherited_windows_environment() if kwargs.get('env') is None
                       else dict(kwargs['env']))
        # Windows environment names are case-insensitive. Do not leave a
        # second spelling competing with the private bootstrap value.
        for name in tuple(environment):
            if os.fsdecode(name).casefold() == '__pyvenv_launcher__':
                del environment[name]
        environment['__PYVENV_LAUNCHER__'] = sys.executable
        kwargs = dict(kwargs, env=environment)
        if (kwargs.get('executable') is not None
                and os.path.normcase(str(kwargs['executable'])) == os.path.normcase(sys.executable)):
            kwargs['executable'] = base
    return subprocess.Popen(command, **kwargs)


def _trusted_supervisor_executable(executable):
    try:
        executable = os.fsdecode(executable)
    except TypeError as error:
        raise ValueError('Windows owner requires its trusted Python supervisor executable') from error
    for trusted in (sys.executable, getattr(sys, '_base_executable', None) or sys.executable):
        if os.path.normcase(executable) == os.path.normcase(trusted):
            return trusted
        try:
            if os.path.samefile(executable, trusted):
                # Launch through the trusted runtime path, never through an
                # alias that could change after this identity observation.
                return trusted
        except (OSError, ValueError):
            pass
    raise ValueError('Windows owner requires its trusted Python supervisor executable')


def start_windows_owned_subprocess(command, *, exit_notice_path=None, termination_grace=None, inherited_handles=(), **kwargs):
    from subprocess_ownership import get_worker_launch_error, mark_unlaunched_exec_errors

    # Popen options configure the trusted supervisor. The worker interpreter
    # is selected by command[0], including an explicitly requested base Python.
    try:
        inherited_handles = tuple(() if inherited_handles is None else inherited_handles)
    except TypeError as error:
        raise ValueError('Expected positive Windows handles') from error
    if any(type(value) is not int or value <= 0 for value in inherited_handles):
        raise ValueError('Expected positive Windows handles')
    if kwargs.get('executable') is not None:
        kwargs = dict(kwargs, executable=_trusted_supervisor_executable(kwargs['executable']))
    job = WindowsSubprocessJob()
    try:
        directory = tempfile.TemporaryDirectory(prefix='alexandria-windows-owner-')
    except BaseException:
        job.close()
        raise
    root = Path(directory.name)
    gate, admission = root / 'admitted', root / 'worker.json'
    process = None
    control = None
    assigned = False
    try:
        notice_args = [] if exit_notice_path is None else ['--exit-notice', str(exit_notice_path), '--']
        handle_args = []
        if inherited_handles:
            if kwargs.get('startupinfo') is not None:
                raise ValueError('Explicit inherited handles require their own startup info')
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {'handle_list': list(inherited_handles)}
            kwargs = dict(kwargs, startupinfo=startup, close_fds=True)
            handle_args = ['--inherit-handles', json.dumps(list(inherited_handles)), '--']
        with mark_unlaunched_exec_errors():
            process = _popen_owned_python(
                [sys.executable, str(Path(__file__).resolve()), str(gate), str(admission), *notice_args, *handle_args, *command],
                **kwargs)
        control = WindowsOwnerControl(job, process, directory, termination_grace)
        job.apply_process_membership(int(process._handle))
        assigned = True
        gate.touch(exist_ok=False)
        deadline = time.monotonic() + 30
        owner_exit_observed = False
        while True:
            try:
                message = read_windows_owner_message(admission)
                break
            except FileNotFoundError:
                if owner_exit_observed:
                    raise OSError('Windows subprocess owner exited before worker admission')
                if process.poll() is not None:
                    # Publication can race the first missing-file observation.
                    # Make a final read after observing process exit.
                    owner_exit_observed = True
                    continue
            except PermissionError:
                # A published Windows receipt can temporarily deny reads. Keep
                # the same admitted job and worker until the original deadline;
                # never respawn or release ownership while its receipt is blocked.
                pass
            if time.monotonic() >= deadline:
                raise OSError('Windows subprocess owner did not acknowledge worker admission')
            time.sleep(.01)
        if 'error' in message:
            raise get_worker_launch_error(message)
        process._alexandria_control = control
        process._alexandria_windows_owner = control
        return process
    except BaseException:
        if control is not None:
            if not assigned:
                # A failed assignment cannot have admitted any real worker.
                process.kill()
                process.wait()
            control.close()
            process.wait()
        else:
            job.close()
            directory.cleanup()
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise


def run_windows_owned_command(gate, admission, command, exit_notice_path=None, inherited_handles=()):
    from subprocess_ownership import mark_unlaunched_exec_errors

    deadline = time.monotonic() + 30
    while not gate.exists():
        if time.monotonic() >= deadline:
            raise OSError('Windows supervisor was not admitted; worker was not launched')
        time.sleep(.01)
    api = get_windows_job_api()
    # NULL queries the calling process's innermost job; no child job handle
    # survives here to defeat parent's KILL_ON_JOB_CLOSE protection.
    if get_windows_job_active_process_count(api) != 1:
        raise OSError('Windows supervisor has no exclusive initial job membership')
    options = {}
    if inherited_handles:
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': list(inherited_handles)}
        options = {'startupinfo': startup, 'close_fds': True}
    with mark_unlaunched_exec_errors():
        process = _popen_owned_python(command, **options)
    save_windows_owner_message(admission, {'started': process.pid})
    notified = False
    query_reported = False
    while True:
        result = process.poll()
        if result is not None and not notified and exit_notice_path is not None:
            from subprocess_ownership import save_owned_exit_notice
            save_owned_exit_notice(exit_notice_path, result)
            notified = True
        if result is not None:
            try:
                active = get_windows_job_active_process_count(api)
            except OSError:
                if not query_reported:
                    logging.exception('Could not query Windows job; retaining supervisor ownership')
                    query_reported = True
            else:
                if active == 1:
                    return result
        time.sleep(.05)


if __name__ == '__main__':
    gate, admission = map(Path, sys.argv[1:3])
    command = sys.argv[3:]
    exit_notice_path = None
    if command[:1] == ['--exit-notice']:
        if len(command) < 4 or command[2] != '--':
            raise ValueError('Invalid Windows root-exit notice arguments')
        exit_notice_path, command = command[1], command[3:]
    inherited_handles = ()
    if command[:1] == ['--inherit-handles']:
        if len(command) < 4 or command[2] != '--':
            raise ValueError('Invalid Windows inherited-handle arguments')
        inherited_handles, command = json.loads(command[1]), command[3:]
        if (not isinstance(inherited_handles, list)
                or any(type(value) is not int or value <= 0 for value in inherited_handles)):
            raise ValueError('Expected positive Windows handles')
    try:
        result = run_windows_owned_command(gate, admission, command, exit_notice_path, inherited_handles)
    except (OSError, RuntimeError) as error:
        if not admission.exists():
            from subprocess_ownership import get_worker_launch_error_message
            save_windows_owner_message(admission, get_worker_launch_error_message(error))
        raise
    # Windows child exit codes are DWORDs; sys.exit()'s signed C-long conversion
    # can lose high-bit native failure statuses on Windows.
    get_windows_job_api().ExitProcess(result & 0xffffffff)
