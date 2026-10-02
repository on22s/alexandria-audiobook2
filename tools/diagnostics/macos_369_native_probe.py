"""CPU-only native macOS prerequisite probe; never imports the app or models.

Run with a native macOS Python: python3 macos_369_native_probe.py
The probe creates/removes one uniquely named transient user launchd job.
Kernel APIs are private and version dependent: errors are recorded, never
interpreted as an empty coalition. This is NOT the production #369 backend.
"""
import array
import fcntl
import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))
from macos_subprocess_kernel import (MacApi, UniqueInfo, CoalitionInfo, AuditToken,
                                     is_macos_coalition_reaped)


def save_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temporary, path)


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise TimeoutError('native prerequisite probe did not reach its expected state')


def receive_probe_lease(channel, path):
    """Receive one retained open-file description before admitting CPU children."""
    descriptors = []
    try:
        data, messages, flags, _ = channel.recvmsg(1, socket.CMSG_SPACE(4 * array.array('i').itemsize))
        unexpected = False
        for level, kind, payload in messages:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                unexpected = True
                continue
            values = array.array('i')
            values.frombytes(payload[:len(payload) - len(payload) % values.itemsize])
            descriptors.extend(values)
        if data != b'L' or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or unexpected or len(descriptors) != 1:
            raise ValueError('Expected exactly one untruncated probe lease descriptor')
        held = os.fstat(descriptors[0])
        expected = path.stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError('Received lease descriptor names another file')
        return descriptors.pop()
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def send_probe_lease(channel, descriptor):
    if channel.sendmsg([b'L'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                               array.array('i', [descriptor]))]) != 1:
        raise RuntimeError('Incomplete native lease handoff')


is_probe_coalition_reaped = is_macos_coalition_reaped


def is_probe_lease_free(path):
    with path.open('r+b') as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(probe, fcntl.LOCK_UN)
        return True


def await_lease_observation(root, stage):
    save_json(root / ('ready-' + stage + '.json'), {'stage': stage})
    wait_for(lambda: (root / ('ack-' + stage)).exists())


def get_probe_late_children(root):
    return [json.loads(path.read_text()) for path in root.glob('leaf-[0-9]*.json')
            if not path.name.endswith('-pulse.json')]


def run_child(root, *, fork_on_term=False):
    api = MacApi()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if fork_on_term:
        def spawn_late_children(*_):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            for _ in range(2):
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--leaf', str(root)],
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        signal.signal(signal.SIGTERM, spawn_late_children)
    prefix = 'child' if fork_on_term else 'leaf-' + str(os.getpid())
    save_json(root / (prefix + '.json'), api.get_info(os.getpid()))
    counter = 0
    while True:
        counter += 1
        save_json(root / ('pulse.json' if fork_on_term else prefix + '-pulse.json'), {'counter': counter})
        time.sleep(.02)


def run_job(root):
    child = None
    late_children = []
    lease = None
    report = {'measurements': []}
    try:
        api = MacApi()
        owner = api.get_info(os.getpid())
        report['owner'] = owner
        initial = api.get_active_count(owner['coalition'])
        report['measurements'].append({'initial_active': initial})
        if initial != 1:
            raise ValueError('transient launchd job does not have an exclusive initial coalition')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(10)
            channel.connect(str(root / 'lease.sock'))
            lease = receive_probe_lease(channel, root / 'lease.lock')
        (root / 'lease-received').write_text('ready')
        await_lease_observation(root, 'admission')
        wrapper_code = "import subprocess,sys;subprocess.Popen([sys.executable,sys.argv[1],'--child',sys.argv[2]],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
        wrapper = subprocess.Popen([sys.executable, '-c', wrapper_code, str(Path(__file__).resolve()), str(root)])
        wrapper.wait(timeout=10)
        wait_for(lambda: (root / 'child.json').exists() and (root / 'pulse.json').exists())
        child = json.loads((root / 'child.json').read_text())
        if child['coalition'] != owner['coalition']:
            raise ValueError('setsid child departed the isolated coalition')
        if os.getpgid(child['pid']) == os.getpgid(os.getpid()):
            raise ValueError('fixture did not actually escape the owner process group')
        wait_for(lambda: api.get_active_count(owner['coalition']) == 2)
        members = api.get_coalition_members(owner['coalition'])
        if {(item['pid'], item['uniqueid']) for item in members} != {
                (owner['pid'], owner['uniqueid']), (child['pid'], child['uniqueid'])}:
            raise ValueError('Native coalition enumeration disagrees with its two known members')
        report['measurements'].append({'after_wrapper_exit_active': 2, 'child': child,
                                       'enumerated_members': members})
        await_lease_observation(root, 'detached')
        if api.apply_signal(child, signal.SIGKILL, stale=True) != errno.ESRCH:
            raise ValueError('wrong PID version was not rejected with ESRCH')
        if api.apply_signal(child, signal.SIGSTOP):
            raise OSError('audit-token SIGSTOP refused')
        time.sleep(.1)
        pulse = (root / 'pulse.json').read_bytes()
        time.sleep(.1)
        if pulse != (root / 'pulse.json').read_bytes():
            raise ValueError('SIGSTOP failed to freeze detached child')
        await_lease_observation(root, 'paused')
        if api.apply_signal(child, signal.SIGCONT):
            raise OSError('audit-token SIGCONT refused')
        wait_for(lambda: pulse != (root / 'pulse.json').read_bytes())
        if api.apply_signal(child, signal.SIGTERM):
            raise OSError('audit-token SIGTERM refused')
        wait_for(lambda: len(get_probe_late_children(root)) == 2)
        late_children = get_probe_late_children(root)
        if len(late_children) != 2 or any(item['coalition'] != owner['coalition'] for item in late_children):
            raise ValueError('Late TERM children did not inherit the owned coalition')
        wait_for(lambda: api.get_active_count(owner['coalition']) == 4)
        report['measurements'].append({'after_term_fork_active': 4, 'late_children': late_children})
        await_lease_observation(root, 'term')
        if api.apply_signal(child, signal.SIGKILL):
            raise OSError('audit-token SIGKILL refused')
        for item in late_children:
            if api.apply_signal(item, signal.SIGKILL):
                raise OSError('audit-token late-child SIGKILL refused')
        wait_for(lambda: api.get_active_count(owner['coalition']) == 1)
        report['measurements'].append({'after_forced_stop_active': 1})
        await_lease_observation(root, 'cleaned')
        report['prerequisites_passed'] = True
    except BaseException as error:
        report.update(prerequisites_passed=False, error=repr(error))
    finally:
        if child is None and (root / 'child.json').exists():
            child = json.loads((root / 'child.json').read_text())
        if child is not None:
            # Exact audit identity only: never an unguarded numeric PID/tree kill.
            result = api.apply_signal(child, signal.SIGKILL)
            if result not in (0, errno.ESRCH):
                report.update(prerequisites_passed=False, cleanup_error=result)
        for info in get_probe_late_children(root):
            result = api.apply_signal(info, signal.SIGKILL)
            if result not in (0, errno.ESRCH):
                report.update(prerequisites_passed=False, late_child_cleanup_error=result)
        save_json(root / 'report.json', report)
        if lease is not None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                    channel.settimeout(10)
                    channel.connect(str(root / 'lease.sock'))
                    send_probe_lease(channel, lease)
                    if channel.recv(1) != b'R':
                        raise RuntimeError('Controller did not retain the returned probe lease')
            finally:
                os.close(lease)


def run_controller(output):
    if sys.platform != 'darwin':
        raise SystemExit('NOT RUN: requires native macOS; Linux cannot verify these prerequisites')
    label = 'org.alexandria.probe369.' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='alexandria-369-', dir='/tmp') as tmp:
        root = Path(tmp)
        api = MacApi()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, (root / 'lease.lock').open('w+b') as lease:
            listener.bind(str(root / 'lease.sock'))
            listener.listen(1)
            listener.settimeout(10)
            fcntl.flock(lease, fcntl.LOCK_EX)
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(45)'])
            try:
                sentinel_info = api.get_info(sentinel.pid)
                admitted = subprocess.run(['/bin/launchctl', 'submit', '-l', label, '--',
                                           sys.executable, str(Path(__file__).resolve()), '--job', tmp],
                                          capture_output=True, text=True, timeout=10)
                if admitted.returncode:
                    raise RuntimeError(f'launchctl submit refused: {admitted.stderr}')
                with listener.accept()[0] as channel:
                    send_probe_lease(channel, lease.fileno())
                wait_for(lambda: (root / 'lease-received').exists())
                lease.close()
                observed = []
                for stage in ('admission', 'detached', 'paused', 'term', 'cleaned'):
                    wait_for(lambda: (root / ('ready-' + stage + '.json')).exists())
                    if stage == 'admission' and (root / 'child.json').exists():
                        raise RuntimeError('Child admitted before retained lease observation')
                    if is_probe_lease_free(root / 'lease.lock'):
                        raise RuntimeError('Probe lease released during ' + stage)
                    observed.append(stage)
                    (root / ('ack-' + stage)).write_text('observed')
                with listener.accept()[0] as channel:
                    keeper = receive_probe_lease(channel, root / 'lease.lock')
                    try:
                        wait_for(lambda: (root / 'report.json').exists(), seconds=20)
                        report = json.loads((root / 'report.json').read_text())
                        owner = report['owner']
                        if api.get_info(owner['pid']) != owner or api.get_active_count(owner['coalition']) != 1:
                            raise RuntimeError('Outside guardian could not observe the live owner scope')
                        report['outside_live_scope_observed'] = True
                        channel.sendall(b'R')
                        removed = subprocess.run(['/bin/launchctl', 'remove', label],
                                                 capture_output=True, text=True, timeout=10)
                        wait_for(lambda: is_probe_coalition_reaped(api, owner['coalition']))
                        if is_probe_lease_free(root / 'lease.lock'):
                            raise RuntimeError('Guardian lease released before the reap receipt')
                        report['coalition_reaped'] = True
                        report['lease_retained_until_reap'] = True
                        report['launchctl_remove_status'] = removed.returncode
                    finally:
                        os.close(keeper)
                wait_for(lambda: is_probe_lease_free(root / 'lease.lock'))
                report['lease_retained_stages'] = observed
                report['lease_released_after_cleanup'] = True
                report['kernel'] = {key: getattr(os.uname(), key) for key in ('sysname', 'release', 'version', 'machine')}
                report['unrelated_sentinel_alive'] = sentinel.poll() is None
                report['sentinel'] = sentinel_info
                if not report['unrelated_sentinel_alive']:
                    report['prerequisites_passed'] = False
                if report.get('owner', {}).get('coalition') == sentinel_info['coalition']:
                    report.update(prerequisites_passed=False, isolation_error='sentinel shares job coalition')
                save_json(output, report)
                print(output)
                return 0 if report['prerequisites_passed'] else 1
            finally:
                try:
                    # Remove exactly this UUID-labelled diagnostic job; no domain removal.
                    subprocess.run(['/bin/launchctl', 'remove', label], capture_output=True, timeout=10)
                    children = ([json.loads((root / 'child.json').read_text())] if (root / 'child.json').exists() else [])
                    children.extend(get_probe_late_children(root))
                    for info in children:
                        result = api.apply_signal(info, signal.SIGKILL)
                        if result not in (0, errno.ESRCH):
                            raise OSError(result, 'probe child cleanup refused')
                finally:
                    sentinel.terminate()
                    sentinel.wait(timeout=5)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--child']:
        run_child(Path(sys.argv[2]), fork_on_term=True)
    elif sys.argv[1:2] == ['--leaf']:
        run_child(Path(sys.argv[2]))
    elif sys.argv[1:2] == ['--job']:
        run_job(Path(sys.argv[2]))
    else:
        output = Path.cwd() / 'macos_369_native_probe_result.json'
        try:
            result = run_controller(output)
        except Exception as error:
            save_json(output, {'prerequisites_passed': False,
                               'controller_error': repr(error),
                               'platform': sys.platform})
            raise
        raise SystemExit(result)
