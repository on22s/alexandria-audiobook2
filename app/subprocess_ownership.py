"""Shared descendant ownership and signal dispatch for supervised tasks."""
import ctypes
import json
import logging
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time


SUBPROCESS_TERMINATE_GRACE_SECONDS = 10.0
logger = logging.getLogger("AlexandriaUI")


def terminate_windows_process_tree(proc_or_pid, force=False, timeout=SUBPROCESS_TERMINATE_GRACE_SECONDS):
    """Terminate an owned Windows job, or dispatch a legacy tree operation."""
    owner = getattr(proc_or_pid, "_alexandria_windows_owner", None)
    if owner is not None:
        if force:
            owner.terminate()
        else:
            owner.sendall(f"{int(signal.SIGTERM)}\n".encode())
        return
    pid = proc_or_pid.pid if hasattr(proc_or_pid, "pid") else proc_or_pid
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        subprocess.run(command, check=True, capture_output=True, text=True,
                       timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        logger.warning("Windows taskkill failed for PID %s: %s", pid, error)
        raise OSError(f"Windows taskkill failed for PID {pid}") from error


def send_subprocess_signal(proc_or_pid, sig, termination_grace=SUBPROCESS_TERMINATE_GRACE_SECONDS) -> None:
    """Dispatch signals through the task owner or its native process group.

    Linux owned subprocesses use a dedicated reaper and control channel;
    Windows owned subprocesses dispatch through their Job Object controller.
    Other POSIX subprocesses are started with start_new_session=True
    (see _stream_subprocess_to_logs), which makes each the leader of its own
    process group (pgid == pid). Signalling only the direct child (proc.terminate/
    send_signal) misses grandchildren — e.g. Voice Lab's `train` stage runs
    batch_train_lora.py which spawns train_lora.py, and profiling spawns a llama
    process — so a cancel would leave the real GPU worker running orphaned and a
    pause would freeze only the idle wrapper. Kill the group instead.

    Accepts a Popen or a bare pid. Raises ProcessLookupError/OSError like
    proc.send_signal so existing callers' handlers still catch an already-exited
    process. Windows termination uses taskkill's tree operation; other Windows
    signals and unresolved POSIX groups retain direct-process dispatch."""
    control = getattr(proc_or_pid, "_alexandria_control", None)
    if control is not None:
        control.sendall(f"{int(sig)}\n".encode())
        return
    pid = proc_or_pid.pid if hasattr(proc_or_pid, "pid") else proc_or_pid
    if sys.platform == "win32" and sig == signal.SIGTERM:
        terminate_windows_process_tree(proc_or_pid, timeout=termination_grace)
        return
    if sys.platform != "win32":
        try:
            pgid = getattr(proc_or_pid, "_alexandria_pgid", None)
            os.killpg(pgid if pgid is not None else os.getpgid(pid), sig)
            return
        except ProcessLookupError:
            raise  # process/group already gone — let caller treat as exited
        except OSError:
            pass  # couldn't resolve/signal the group; fall back to direct
    if hasattr(proc_or_pid, "send_signal"):
        proc_or_pid.send_signal(sig)
    else:
        os.kill(pid, sig)


def is_subprocess_tree_running(process):
    windows_owner = getattr(process, '_alexandria_windows_owner', None)
    if windows_owner is not None:
        return windows_owner.job.handle is not None and bool(windows_owner.job.get_active_process_count())
    if getattr(process, '_alexandria_control', None) is not None or os.name != 'posix':
        return process.poll() is None
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_owned_subprocess(process, interrupt=False, timeout=5, *, force_after_grace=False):
    """Stop owned work, retaining the caller's grace and forced-tail policy."""
    graceful_signal = signal.SIGINT if interrupt and sys.platform != 'win32' else signal.SIGTERM
    deadline = time.monotonic() + timeout
    try:
        if is_subprocess_tree_running(process):
            try:
                send_subprocess_signal(process, graceful_signal, termination_grace=timeout)
            except ProcessLookupError:
                pass
            except OSError:
                if not force_after_grace:
                    raise
        if force_after_grace:
            deadline = time.monotonic() + timeout
        while is_subprocess_tree_running(process) and time.monotonic() < deadline:
            time.sleep(.05)
    finally:
        if force_after_grace or is_subprocess_tree_running(process):
            try:
                if sys.platform == 'win32':
                    terminate_windows_process_tree(process, force=True, timeout=timeout)
                else:
                    send_subprocess_signal(process, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                if not force_after_grace:
                    raise
    if force_after_grace:
        process.wait()
    else:
        process.wait(timeout=timeout)


def run_owned_capture(command, *, timeout, **kwargs):
    """Capture a bounded command, stopping and reaping its owned descendants."""
    process = start_owned_subprocess(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=(os.name == 'posix'),
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0),
        termination_grace=1, **kwargs)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            stop_owned_subprocess(process, timeout=1)
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        control = getattr(process, '_alexandria_control', None)
        if control is not None:
            control.close()
        process.stdout.close()
        process.stderr.close()


def save_owned_exit_notice(path, result):
    from utils import atomic_json_write
    atomic_json_write({'exit_code': result}, str(path))


def get_owned_exit_result(path):
    try:
        notice = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(notice, dict) or type(notice.get('exit_code')) is not int:
        raise ValueError('Malformed subprocess root-exit notice')
    return notice['exit_code']


def save_subprocess_stop_receipt(path):
    with open(path, 'x', encoding='utf-8') as receipt:
        receipt.write('stopped\n')
        receipt.flush()
        os.fsync(receipt.fileno())


def get_subprocess_lease_options(environment, task_lease_fd=None):
    """Read lease capabilities supported by the platform's outside task owner."""
    gpu_lease_fd = None
    if sys.platform not in ('linux', 'darwin'):
        return dict(gpu_lease_fd=None, task_lease_fd=None)
    if (environment.get('ALEXANDRIA_GPU_LOCK_HELD') == '1'
            and environment.get('ALEXANDRIA_GPU_LOCK_PID') == str(os.getpid())):
        gpu_lease_fd = int(environment.get('ALEXANDRIA_GPU_LOCK_FD', '9'))
    return dict(gpu_lease_fd=gpu_lease_fd, task_lease_fd=task_lease_fd)


def start_owned_subprocess(command, *, gpu_lease_fd=None, task_lease_fd=None,
                           stop_receipt_path=None, disconnect_signal=None, exit_notice_path=None,
                           termination_grace=None, **kwargs):
    """Start a command with a dedicated reaper, retaining its signal channel."""
    if sys.platform == "win32":
        from windows_subprocess_owner import start_windows_owned_subprocess
        return start_windows_owned_subprocess(
            command, exit_notice_path=exit_notice_path, termination_grace=termination_grace, **kwargs)
    if sys.platform == "darwin":
        from macos_subprocess_owner import start_macos_owned_subprocess
        return start_macos_owned_subprocess(
            command, gpu_lease_fd=gpu_lease_fd, task_lease_fd=task_lease_fd,
            stop_receipt_path=stop_receipt_path, disconnect_signal=disconnect_signal,
            exit_notice_path=exit_notice_path, termination_grace=termination_grace, **kwargs)
    if sys.platform != "linux":
        return subprocess.Popen(command, **kwargs)
    if disconnect_signal is None:
        disconnect_signal = signal.SIGKILL
    control, child_control = socket.socketpair()
    process = None
    try:
        retained = [child_control.fileno()]
        retained.extend(fd for fd in (gpu_lease_fd, task_lease_fd) if fd is not None)
        receipt_args = [] if stop_receipt_path is None else ['--stop-receipt', str(stop_receipt_path)]
        if disconnect_signal == signal.SIGTERM:
            receipt_args.append('--disconnect-term')
        elif disconnect_signal != signal.SIGKILL:
            raise ValueError('Unsupported subprocess disconnect signal')
        if exit_notice_path is not None:
            receipt_args.extend(['--exit-notice', str(exit_notice_path)])
        if receipt_args:
            receipt_args.append('--')
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             str(child_control.fileno()), str(gpu_lease_fd) if gpu_lease_fd is not None else "-", *receipt_args, *command],
            pass_fds=tuple(set(retained)), **kwargs)
        child_control.close()
        response = bytearray()
        while not response.endswith(b"\n"):
            part = control.recv(1)
            if not part:
                raise OSError("Subprocess owner exited before command admission")
            response.extend(part)
        admission = json.loads(response)
        if "error" in admission:
            raise OSError(admission.get("errno"), admission["error"])
        process._alexandria_control = control
        return process
    except BaseException:
        control.close()
        if process is not None:
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        raise
    finally:
        child_control.close()


def get_process_record(pid):
    """Read kernel parenthood and birth time from one process observation."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[1]), fields[19]


def get_process_identity(pid):
    """Read the kernel birth time used to reject recycled PID observations."""
    return get_process_record(pid)[1]


def get_owned_descendants():
    """Walk this reaper's descendants, rejecting stale parent/child identities."""
    root = os.getpid()
    pending = [(root, get_process_identity(root))]
    found = {}
    while pending:
        parent, parent_identity = pending.pop()
        try:
            if get_process_identity(parent) != parent_identity:
                continue
            threads = list(Path(f"/proc/{parent}/task").iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        for thread in threads:
            try:
                children = (thread / "children").read_text().split()
                if get_process_identity(parent) != parent_identity:
                    break
            except (FileNotFoundError, ProcessLookupError):
                continue
            for raw in children:
                pid = int(raw)
                if pid in found:
                    continue
                try:
                    current_parent, identity = get_process_record(pid)
                    if current_parent != parent or get_process_identity(parent) != parent_identity:
                        continue
                except (FileNotFoundError, ProcessLookupError):
                    continue
                found[pid] = identity
                pending.append((pid, identity))
    return found


def ensure_pidfd_support():
    """Verify the kernel handle operations before admitting any worker."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = (ctypes.c_int, ctypes.c_uint)
    libc.pidfd_open.restype = ctypes.c_int
    libc.pidfd_send_signal.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    libc.pidfd_send_signal.restype = ctypes.c_int
    descriptor = libc.pidfd_open(os.getpid(), 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Linux subprocess ownership requires pidfd support")
    os.close(descriptor)
    return libc


def open_owned_process(libc, pid):
    """Open a stable kernel handle before signalling an observed descendant."""
    descriptor = libc.pidfd_open(pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Could not open owned process")
    return descriptor


def apply_descendant_signal(libc, sig, delivered):
    """Signal owned identities through pidfds, never recycled numeric PIDs."""
    for pid, identity in get_owned_descendants().items():
        key = (pid, identity, sig)
        if key in delivered:
            continue
        descriptor = None
        try:
            descriptor = open_owned_process(libc, pid)
            if get_process_identity(pid) != identity:
                continue
            if libc.pidfd_send_signal(descriptor, sig, None, 0) < 0:
                raise OSError(ctypes.get_errno(), "Could not signal owned process")
            delivered.add(key)
        except (ProcessLookupError, FileNotFoundError):
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)


def ensure_owned_descendants_stopped(libc):
    """Retain ownership while exceptional shutdown reaps all remaining work."""
    delivered = set()
    reported = False
    while True:
        try:
            apply_descendant_signal(libc, signal.SIGCONT, delivered)
            apply_descendant_signal(libc, signal.SIGKILL, delivered)
        except OSError as error:
            if not reported:
                print(f"ERROR: Could not stop owned subprocess: {error}; retaining ownership", file=sys.stderr, flush=True)
                reported = True
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                break
        select.select([], [], [], .05)


def run_owned_command(control, command, *, disconnect_signal=None, stop_timeout=None, exit_notice_path=None):
    """Retain the command's lifecycle until every adopted descendant exits."""
    if disconnect_signal is None:
        disconnect_signal = signal.SIGKILL
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Could not establish subprocess ownership")
    libc = ensure_pidfd_support()
    received = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda value, frame: received.append(value))
    process = subprocess.Popen(command, start_new_session=True)
    buffer = b""
    root_result = None
    stopping = None
    stopping_started = None
    paused = False
    delivered = set()
    connected = True
    try:
        control.sendall(json.dumps({"started": process.pid}).encode() + b"\n")
        while True:
            while True:
                try:
                    pid, result = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return root_result
                if pid == 0:
                    break
                if pid == process.pid:
                    root_result = os.waitstatus_to_exitcode(result)
                    process.returncode = root_result
                    if exit_notice_path is not None:
                        save_owned_exit_notice(exit_notice_path, root_result)
            readable, _, _ = select.select([control] if connected else [], [], [], .05)
            if readable:
                data = control.recv(4096)
                if not data:
                    connected = False
                    received.append(disconnect_signal)
                buffer += data
                while b"\n" in buffer:
                    message, buffer = buffer.split(b"\n", 1)
                    received.append(int(message))
            for sig in received:
                if sig == signal.SIGSTOP:
                    paused = True
                    delivered.clear()
                elif sig == signal.SIGCONT:
                    paused = False
                    delivered.clear()
                    apply_descendant_signal(libc, sig, delivered)
                elif sig == signal.SIGKILL or stopping != signal.SIGKILL:
                    stopping = sig
                    delivered.clear()
            received.clear()
            if stopping is not None:
                if stopping_started is None:
                    stopping_started = time.monotonic()
                if (stop_timeout is not None and stopping != signal.SIGKILL
                        and time.monotonic() - stopping_started >= stop_timeout):
                    stopping = signal.SIGKILL
                    delivered.clear()
                apply_descendant_signal(libc, signal.SIGCONT, delivered)
                apply_descendant_signal(libc, stopping, delivered)
            elif paused:
                apply_descendant_signal(libc, signal.SIGSTOP, delivered)

    except BaseException:
        ensure_owned_descendants_stopped(libc)
        raise


if __name__ == "__main__":
    command = sys.argv[3:]
    stop_receipt_path = None
    disconnect_signal = signal.SIGKILL
    exit_notice_path = None
    has_options = False
    while command[:1] in (['--stop-receipt'], ['--disconnect-term'], ['--exit-notice']):
        has_options = True
        option = command.pop(0)
        if option == '--disconnect-term':
            disconnect_signal = signal.SIGTERM
        elif option == '--stop-receipt':
            stop_receipt_path = command.pop(0)
        else:
            exit_notice_path = command.pop(0)
    if has_options:
        if len(command) < 2 or command[0] != '--':
            raise ValueError('Invalid subprocess ownership arguments')
        command = command[1:]
    with socket.socket(fileno=int(sys.argv[1])) as control:
        try:
            if sys.argv[2] != "-":
                from experiments.gpu_guard import is_queue_lock_held_by_us
                descriptor = int(sys.argv[2])
                if os.environ.get("ALEXANDRIA_GPU_LOCK_FD") != str(descriptor):
                    raise OSError("GPU lease descriptor does not match task environment")
                if not is_queue_lock_held_by_us():
                    raise OSError("GPU lease has no verified ancestor owner")
                os.environ["ALEXANDRIA_GPU_LOCK_PID"] = str(os.getpid())
            result = run_owned_command(control, command, disconnect_signal=disconnect_signal,
                                       exit_notice_path=exit_notice_path)
            if stop_receipt_path is not None:
                # run_owned_command returns only after waitpid proves there are
                # no remaining descendants, including adopted detached children.
                save_subprocess_stop_receipt(stop_receipt_path)
        except (OSError, RuntimeError) as error:
            control.sendall(json.dumps({"error": str(error), "errno": getattr(error, "errno", None)}).encode() + b"\n")
            raise
    if result < 0:
        if -result != signal.SIGKILL:
            signal.signal(-result, signal.SIG_DFL)
        os.kill(os.getpid(), -result)
    sys.exit(result)
