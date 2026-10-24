"""A trusted, gated Win32 job supervisor; no worker runs before admission."""
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from json_metadata import get_unique_json_object

from windows_subprocess_job import (WindowsSubprocessJob, get_windows_job_api,
                                    get_windows_job_active_process_count)


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
            or (set(message) == {'error','errno'} and isinstance(message['error'],str)
                and 0 < len(message['error']) <= 2048
                and (message['errno'] is None or type(message['errno']) is int
                     and -(2**31) <= message['errno'] < 2**31)))
        if not valid:
            raise ValueError('invalid receipt')
    except (ValueError,UnicodeError,RecursionError):
        raise OSError('Windows subprocess owner returned an invalid admission receipt') from None
    return message


class WindowsOwnerControl:
    def __init__(self, job, process, directory, termination_grace=None):
        self.job = job
        self.process = process
        self.directory = directory
        from subprocess_ownership import SUBPROCESS_TERMINATE_GRACE_SECONDS
        self.termination_grace = (SUBPROCESS_TERMINATE_GRACE_SECONDS if termination_grace is None
                                  else termination_grace)

    def sendall(self, data):
        if int(data.strip()) != signal.SIGTERM:
            raise OSError('Windows owner supports termination only')
        self.job.request_termination(self.process.pid, self.termination_grace)

    def terminate(self):
        self.job.terminate()

    def close(self):
        if self.job.handle is None:
            return
        # Never hand back the task slot while kernel membership is unknown/live.
        # All API failures use the same policy: log, retain ownership, retry.
        reported = False
        while True:
            try:
                if self.job.get_active_process_count():
                    self.job.terminate()
                    self.job.ensure_stopped()
                self.job.close()
                break
            except OSError:
                if not reported:
                    logging.exception('Could not stop Windows job; retaining ownership')
                    reported = True
                time.sleep(.05)
        try:
            self.directory.cleanup()
        except OSError:
            # Kernel membership is already stopped and the handle closed.
            # A blocked temporary receipt must not mask admission failure or
            # prevent the caller from waiting/closing its process and pipes.
            logging.exception('Could not remove Windows owner temporary files after job shutdown')


def start_windows_owned_subprocess(command, *, exit_notice_path=None, termination_grace=None, inherited_handles=(), **kwargs):
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
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(gate), str(admission), *notice_args, *handle_args, *command],
            **kwargs)
        control = WindowsOwnerControl(job, process, directory, termination_grace)
        job.apply_process_membership(int(process._handle))
        assigned = True
        gate.touch(exist_ok=False)
        deadline = time.monotonic() + 30
        while True:
            try:
                message = read_windows_owner_message(admission)
                break
            except FileNotFoundError:
                if process.poll() is not None:
                    raise OSError('Windows subprocess owner exited before worker admission')
            except PermissionError:
                # A published Windows receipt can temporarily deny reads. Keep
                # the same admitted job and worker until the original deadline;
                # never respawn or release ownership while its receipt is blocked.
                pass
            if time.monotonic() >= deadline:
                raise OSError('Windows subprocess owner did not acknowledge worker admission')
            time.sleep(.01)
        if 'error' in message:
            raise OSError(message.get('errno'), message['error'])
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
    if inherited_handles:
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': list(inherited_handles)}
        process = subprocess.Popen(command, startupinfo=startup, close_fds=True)
    else:
        process = subprocess.Popen(command)
    save_windows_owner_message(admission, {'started': process.pid})
    notified = False
    while True:
        result = process.poll()
        if result is not None and not notified and exit_notice_path is not None:
            from subprocess_ownership import save_owned_exit_notice
            save_owned_exit_notice(exit_notice_path, result)
            notified = True
        if result is not None and get_windows_job_active_process_count(api) == 1:
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
            save_windows_owner_message(admission, {'error': str(error), 'errno': getattr(error, 'errno', None)})
        raise
    # Windows child exit codes are DWORDs; sys.exit()'s signed C-long conversion
    # can lose high-bit native failure statuses on Windows.
    get_windows_job_api().ExitProcess(result & 0xffffffff)
