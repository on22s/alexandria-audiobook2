"""Outside Darwin task broker; leases survive until the private scope is reaped."""
import array
import errno
import fcntl
import json
import os
import re
from pathlib import Path
import select
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from macos_subprocess_kernel import MacApi, is_macos_coalition_reaped

_JOB_REMOVAL_RETRY_SECONDS = 0.5
_MAX_JOB_LIST_BYTES = 4 * 1024 * 1024


def send_macos_owner_event(channel, message):
    channel.sendall(json.dumps(message).encode() + b'\n')


def receive_macos_owner_line(channel):
    data = bytearray()
    while not data.endswith(b'\n'):
        part = channel.recv(1)
        if not part:
            raise OSError('macOS task owner disconnected before admission')
        data.extend(part)
    return json.loads(data)


def receive_macos_owner_bytes(channel, count):
    data = bytearray()
    while len(data) < count:
        part = channel.recv(min(count - len(data), 65536))
        if not part:
            raise OSError('Incomplete macOS owner configuration')
        data.extend(part)
    return data


def send_macos_owner_descriptor(channel, descriptor):
    if channel.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                               array.array('i', [descriptor]))]) != 1:
        raise OSError('Incomplete macOS owner descriptor transfer')


# macOS installs every descriptor in a message even when the receive buffer
# truncates it, and reports only those that fit; the rest leak, holding any lock
# they carry. Room for Linux's per-message maximum (SCM_MAX_FD) lets a malformed
# packet from the parent be seen and closed in full.
MAX_RECEIVED_DESCRIPTORS = 253


def receive_macos_owner_descriptor(channel):
    received = []
    try:
        data, messages, flags, _ = channel.recvmsg(
            1, socket.CMSG_SPACE(array.array('i').itemsize * MAX_RECEIVED_DESCRIPTORS))
        unexpected = False
        for level, kind, payload in messages:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                unexpected = True
                continue
            values = array.array('i')
            values.frombytes(payload[:len(payload) - len(payload) % values.itemsize])
            received.extend(values)
        if data != b'F' or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or unexpected or len(received) != 1:
            raise ValueError('Expected one complete macOS owner descriptor')
        os.set_inheritable(received[0], False)
        return received.pop()
    finally:
        for descriptor in received:
            os.close(descriptor)


def start_macos_owned_subprocess(command, *, gpu_lease_fd=None, task_lease_fd=None,
                                 stop_receipt_path=None, disconnect_signal=None,
                                 exit_notice_path=None, termination_grace=None, **kwargs):
    if kwargs.pop('preexec_fn', None) is not None:
        raise ValueError('A Python preexec_fn cannot cross the macOS task-job boundary')
    if kwargs.pop('close_fds', True) is not True:
        raise ValueError('macOS task ownership requires explicit pass_fds rather than close_fds=False')
    if disconnect_signal is None:
        disconnect_signal = signal.SIGKILL
    if disconnect_signal not in (signal.SIGTERM, signal.SIGKILL):
        raise ValueError('Unsupported subprocess disconnect signal')
    from subprocess_ownership import SUBPROCESS_TERMINATE_GRACE_SECONDS
    worker = {key: kwargs.pop(key) for key in ('shell', 'executable', 'restore_signals',
              'umask', 'user', 'group', 'extra_groups', 'start_new_session') if key in kwargs}
    extra = tuple(kwargs.pop('pass_fds', ()))
    worker['pass_fds'] = list(extra)
    if 'executable' in worker:
        worker['executable'] = os.fsdecode(worker['executable'])
    arguments = [os.fsdecode(arg) for arg in ([command] if isinstance(command, (str, bytes, os.PathLike)) else command)]
    spec = {'command': arguments, 'worker': worker,
            'stop_receipt': None if stop_receipt_path is None else os.fspath(stop_receipt_path),
            'exit_notice': None if exit_notice_path is None else os.fspath(exit_notice_path),
            'disconnect_signal': int(disconnect_signal),
            'grace': SUBPROCESS_TERMINATE_GRACE_SECONDS if termination_grace is None else termination_grace}
    control, child_control = socket.socketpair()
    process = None
    try:
        retained = {child_control.fileno(), *extra}
        retained.update(fd for fd in (gpu_lease_fd, task_lease_fd) if fd is not None)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--broker',
                                    str(child_control.fileno()), json.dumps(spec)],
                                   pass_fds=tuple(retained), start_new_session=True, **kwargs)
        child_control.close()
        control.settimeout(30)
        admission = receive_macos_owner_line(control)
        if 'error' in admission:
            raise OSError(admission.get('errno'), admission['error'])
        if type(admission.get('started')) is not int:
            raise ValueError('Malformed macOS worker admission')
        control.settimeout(None)
        process._alexandria_control = control
        return process
    except BaseException:
        control.close()
        if process is not None:
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise
    finally:
        child_control.close()


def run_macos_task_job(address):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.connect(address)
        api = MacApi()
        owner = api.get_info(os.getpid())
        if api.get_active_count(owner['coalition']) != 1:
            raise OSError('macOS task job lacks exclusive initial membership')
        send_macos_owner_event(channel, {'owner': owner})
        size = int.from_bytes(receive_macos_owner_bytes(channel, 8), 'big')
        configuration = json.loads(receive_macos_owner_bytes(channel, size))
        options = configuration['worker']
        targets = options.get('pass_fds', [])
        # Every received descriptor is moved above worker targets before mapping,
        # so an explicit target can never overwrite another received capability.
        minimum = max([2, *targets]) + 1
        safe_control = fcntl.fcntl(channel.fileno(), fcntl.F_DUPFD_CLOEXEC, minimum)
        channel.close()  # Its low-numbered FD may be an explicit worker target.
        with socket.socket(fileno=safe_control) as stable:
            received = []
            try:
                for _ in range(3 + len(targets)):
                    descriptor = receive_macos_owner_descriptor(stable)
                    try:
                        received.append(fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, minimum))
                    finally:
                        os.close(descriptor)
                for target, descriptor in zip(targets, received[3:]):
                    os.dup2(descriptor, target)
                options = {**options, 'pass_fds': tuple(targets)}
                process = subprocess.Popen(configuration['command'], cwd=configuration['cwd'],
                                           env=configuration['env'], stdin=received[0],
                                           stdout=received[1], stderr=received[2], **options)
                for target in targets:
                    os.close(target)
                send_macos_owner_event(stable, {'started': process.pid})
            except BaseException as error:
                send_macos_owner_event(stable, {'error': str(error), 'errno': getattr(error, 'errno', None)})
                raise
            finally:
                for descriptor in received:
                    os.close(descriptor)
            notified = False
            commands = b''
            while True:
                result = process.poll()
                if result is not None and not notified:
                    send_macos_owner_event(stable, {'root_exit': result})
                    notified = True
                readable, _, _ = select.select([stable], [], [], .05)
                if readable:
                    data = stable.recv(4096)
                    if not data:
                        return
                    commands += data
                    while b'\n' in commands:
                        line, commands = commands.split(b'\n', 1)
                        if line == b'finish':
                            return
                        raise ValueError('Unknown macOS task-job command')


def apply_macos_scope_signal(api, coalition, sig):
    for member in api.get_coalition_members(coalition):
        result = api.apply_signal(member, sig)
        if result not in (0, errno.ESRCH):
            raise OSError(result, 'Could not signal macOS task member')


def get_verified_macos_task_scope(api, candidate, broker):
    if (api.get_info(candidate['pid']) != candidate
            or candidate['coalition'] == broker['coalition']
            or api.get_active_count(candidate['coalition']) != 1):
        raise OSError('Could not verify private macOS task admission')
    return candidate


def _bounded_command_output(command, *, limit, timeout):
    """Capture a private probe with hard time and byte limits; reap it on failure."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={**os.environ, 'LC_ALL': 'C'})
    deadline = time.monotonic() + timeout
    data = bytearray()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if not selector.select(min(remaining, 0.5)):
                    continue
                try:
                    chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(data)))
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            if process.wait(timeout=remaining) != 0:
                return None
        except subprocess.TimeoutExpired:
            return None
        return bytes(data)
    finally:
        try:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
        finally:
            process.stdout.close()


def _macos_job_is_absent(label):
    """Prove absence only from a successful, complete list in this bootstrap context."""
    raw = _bounded_command_output(['/bin/launchctl', 'list'], limit=_MAX_JOB_LIST_BYTES, timeout=30)
    if raw is None or not raw.endswith(b'\n'):
        return False
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeDecodeError:
        return False
    if not lines or lines[0].split('\t') != ['PID', 'Status', 'Label']:
        return False
    present = False
    for line in lines[1:]:
        fields = line.split('\t')
        if (len(fields) != 3 or not fields[2]
                or not (fields[0] == '-' or re.fullmatch(r'[0-9]+', fields[0]))
                or not (fields[1] in ('-', '???') or re.fullmatch(r'-?[0-9]+', fields[1]))):
            return False
        present |= fields[2] == label
    return not present


def ensure_macos_job_removed(label):
    """Ask launchd to drop the task job; True only when launchctl reports success.

    A `launchctl submit` job is KeepAlive, so until this succeeds launchd may
    start a replacement actor inside the private scope (#1646)."""
    try:
        result = subprocess.run(['/bin/launchctl', 'remove', label], capture_output=True, timeout=30)
        if result.returncode == 0:
            return True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        return _macos_job_is_absent(label)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def ensure_macos_unadmitted_job_removed(label):
    """Do not leave a submitted KeepAlive job behind after failed admission."""
    reported = False
    while not ensure_macos_job_removed(label):
        if not reported:
            print('ERROR: macOS task job removal failed; retaining ownership',
                  file=sys.stderr, flush=True)
            reported = True
        time.sleep(_JOB_REMOVAL_RETRY_SECONDS)


def ensure_macos_scope_reaped(api, coalition, label, *, removal_confirmed=False):
    """Retain the broker's leases through failures, until the kernel removes CID."""
    reported = False
    while True:
        try:
            reaped = is_macos_coalition_reaped(api, coalition)
            if not reaped:
                apply_macos_scope_signal(api, coalition, signal.SIGCONT)
                apply_macos_scope_signal(api, coalition, signal.SIGKILL)
            if not removal_confirmed:
                removal_confirmed = ensure_macos_job_removed(label)
                if not removal_confirmed and not reported:
                    print('ERROR: macOS task job removal failed; retaining ownership',
                          file=sys.stderr, flush=True)
                    reported = True
            if reaped and removal_confirmed:
                return
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            if not reported:
                print(f'ERROR: macOS cleanup failed; retaining ownership: {error}',
                      file=sys.stderr, flush=True)
                reported = True
        time.sleep(_JOB_REMOVAL_RETRY_SECONDS)


def run_macos_owner_broker(control, specification):
    api = MacApi()
    broker = api.get_info(os.getpid())
    received = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda value, frame: received.append(value))
    with tempfile.TemporaryDirectory(prefix='alexandria-mac-', dir='/tmp') as directory:
        address = str(Path(directory) / 'task.sock')
        label = 'org.alexandria.task.' + uuid.uuid4().hex
        owner = None
        actor = None
        admitted = False
        reaped = False
        root_result = None
        actor_buffer = b''
        control_buffer = b''
        stopping = None
        stopping_at = None
        paused = False
        resume = False
        connected = True
        removing = False
        submission_impossible = False
        next_remove = 0.0
        removal_reported = False
        finished = False
        reported = False
        def request_job_removal():
            nonlocal removing, next_remove, removal_reported
            if removing or time.monotonic() < next_remove:
                return
            next_remove = time.monotonic() + _JOB_REMOVAL_RETRY_SECONDS
            try:
                removing = ensure_macos_job_removed(label)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                removing = False
            if not removing and not removal_reported:
                print('ERROR: macOS task job removal failed; retaining ownership',
                      file=sys.stderr, flush=True)
                removal_reported = True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(address)
            listener.listen(1)
            listener.settimeout(30)
            try:
                try:
                    launched = subprocess.run(['/bin/launchctl', 'submit', '-l', label, '--',
                        sys.executable, str(Path(__file__).resolve()), '--job', address],
                        capture_output=True, text=True, timeout=30)
                except (FileNotFoundError, PermissionError, NotADirectoryError):
                    # These executable-start failures cannot have submitted a job.
                    submission_impossible = True
                    raise
                if launched.returncode:
                    raise OSError('launchctl task admission failed: ' + launched.stderr)
                actor = listener.accept()[0]
                actor.settimeout(30)
                candidate = receive_macos_owner_line(actor)['owner']
                owner = get_verified_macos_task_scope(api, candidate, broker)
                payload = json.dumps({**specification, 'cwd': os.getcwd(), 'env': dict(os.environ)}).encode()
                actor.sendall(len(payload).to_bytes(8, 'big') + payload)
                for descriptor in (0, 1, 2, *specification['worker']['pass_fds']):
                    send_macos_owner_descriptor(actor, descriptor)
                actor.settimeout(None)
                while True:
                    try:
                        reaped = is_macos_coalition_reaped(api, owner['coalition'])
                    except (OSError, ValueError) as error:
                        if not reported:
                            print(f'ERROR: macOS scope observation failed; retaining ownership: {error}',
                                  file=sys.stderr, flush=True)
                            reported = True
                    if reaped:
                        request_job_removal()
                        if removing:
                            if root_result is not None:
                                return root_result
                            return 128 + stopping if stopping is not None else 1
                        time.sleep(.05)
                        continue
                    channels = ([control] if connected else []) + ([actor] if actor is not None else [])
                    ready, _, _ = select.select(channels, [], [], .05)
                    if control in ready:
                        data = control.recv(4096)
                        if not data:
                            connected = False
                            received.append(specification['disconnect_signal'])
                        control_buffer += data
                        while b'\n' in control_buffer:
                            line, control_buffer = control_buffer.split(b'\n', 1)
                            received.append(int(line))
                    if actor is not None and actor in ready:
                        data = actor.recv(4096)
                        if not data:
                            actor.close()
                            actor = None
                            if root_result is None and stopping is None:
                                stopping = signal.SIGKILL
                        actor_buffer += data
                        while b'\n' in actor_buffer:
                            line, actor_buffer = actor_buffer.split(b'\n', 1)
                            message = json.loads(line)
                            if 'started' in message:
                                send_macos_owner_event(control, message)
                                admitted = True
                            elif 'root_exit' in message:
                                root_result = message['root_exit']
                                if specification['exit_notice'] is not None:
                                    from subprocess_ownership import save_owned_exit_notice
                                    save_owned_exit_notice(specification['exit_notice'], root_result)
                            elif 'error' in message:
                                raise OSError(message.get('errno'), message['error'])
                    for sig in received:
                        if sig == signal.SIGSTOP:
                            paused = True
                        elif sig == signal.SIGCONT:
                            paused = False
                            resume = True
                        elif sig not in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGKILL):
                            raise ValueError('Unsupported macOS owner signal')
                        elif stopping != signal.SIGKILL:
                            stopping = sig
                    received.clear()
                    try:
                        if stopping is not None:
                            if stopping_at is None:
                                stopping_at = time.monotonic()
                            if time.monotonic() - stopping_at >= specification['grace']:
                                stopping = signal.SIGKILL
                            apply_macos_scope_signal(api, owner['coalition'], signal.SIGCONT)
                            apply_macos_scope_signal(api, owner['coalition'], stopping)
                        elif paused:
                            apply_macos_scope_signal(api, owner['coalition'], signal.SIGSTOP)
                        elif resume:
                            apply_macos_scope_signal(api, owner['coalition'], signal.SIGCONT)
                            resume = False
                        if (root_result is not None and actor is not None and not paused
                                and api.get_active_count(owner['coalition']) == 1):
                            actor.sendall(b'finish\n')
                            actor.close()
                            actor = None
                            finished = True
                        # `launchctl submit` jobs are KeepAlive: launchd restarts an
                        # actor that exits after running past its 10 s throttle, and
                        # the restarted actor waits forever for a configuration, so
                        # the scope would never reach zero members (#1646). Once the
                        # actor-only scope has been told to finish, or the whole scope
                        # is being SIGKILLed anyway (a graceful stop keeps its grace),
                        # remove the job without waiting to observe it empty, retrying
                        # until launchctl succeeds. Reaping is still proven only by
                        # ESRCH above; the leases are held until then.
                        if (actor is None and not removing
                                and (finished or stopping == signal.SIGKILL
                                     or api.get_active_count(owner['coalition']) == 0)):
                            request_job_removal()
                    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                        if not reported:
                            print(f'ERROR: Could not control macOS task; retaining ownership: {error}',
                                  file=sys.stderr, flush=True)
                            reported = True
            except BaseException as error:
                if actor is not None:
                    actor.close()
                    actor = None
                if owner is not None:
                    ensure_macos_scope_reaped(api, owner['coalition'], label,
                                              removal_confirmed=removing)
                    reaped = True
                if not admitted:
                    try:
                        send_macos_owner_event(control, {'error': str(error), 'errno': getattr(error, 'errno', None)})
                    except OSError:
                        pass
                raise
            finally:
                if actor is not None:
                    actor.close()
                if owner is None and not submission_impossible:
                    # No worker was admitted before a private coalition was proven.
                    ensure_macos_unadmitted_job_removed(label)
                if reaped and specification['stop_receipt'] is not None:
                    from subprocess_ownership import save_subprocess_stop_receipt
                    save_subprocess_stop_receipt(specification['stop_receipt'])


if __name__ == '__main__':
    if sys.argv[1] == '--job':
        run_macos_task_job(sys.argv[2])
    else:
        with socket.socket(fileno=int(sys.argv[2])) as control:
            result = run_macos_owner_broker(control, json.loads(sys.argv[3]))
        sys.exit(128 - result if result < 0 else result)
