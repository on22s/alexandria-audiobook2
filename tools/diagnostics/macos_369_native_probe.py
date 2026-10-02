"""CPU-only native macOS prerequisite probe; never imports the app or models.

Run with a native macOS Python: python3 macos_369_native_probe.py
The probe creates/removes one uniquely named transient user launchd job.
Kernel APIs are private and version dependent: errors are recorded, never
interpreted as an empty coalition. This is NOT the production #369 backend.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid


class UniqueInfo(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16), ('uniqueid', ctypes.c_uint64),
                ('parent_uniqueid', ctypes.c_uint64), ('pidversion', ctypes.c_int32),
                ('parent_pidversion', ctypes.c_int32), ('reserved', ctypes.c_uint64 * 2)]


class CoalitionInfo(ctypes.Structure):
    _fields_ = [('ids', ctypes.c_uint64 * 2), ('reserved', ctypes.c_uint64 * 3)]


class AuditToken(ctypes.Structure):
    _fields_ = [('values', ctypes.c_uint32 * 8)]


class MacApi:
    def __init__(self):
        if (ctypes.sizeof(UniqueInfo), ctypes.sizeof(CoalitionInfo), ctypes.sizeof(AuditToken)) != (56, 40, 32):
            raise RuntimeError('Unsupported native process-info structure layout')
        self.lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_listallpids.restype = ctypes.c_int
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                         ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self.lib.coalition_info_resource_usage.argtypes = [ctypes.c_uint64, ctypes.c_void_p,
                                                          ctypes.c_size_t]
        self.lib.coalition_info_resource_usage.restype = ctypes.c_int
        self.lib.proc_signal_with_audittoken.argtypes = [ctypes.POINTER(AuditToken), ctypes.c_int]
        self.lib.proc_signal_with_audittoken.restype = ctypes.c_int

    def get_info(self, pid):
        if type(pid) is not int or pid <= 0 or pid > 2147483647:
            raise ValueError('Expected a positive native PID')
        before, coalition, after = UniqueInfo(), CoalitionInfo(), UniqueInfo()
        for flavor, data in ((17, before), (20, coalition), (17, after)):
            ctypes.set_errno(0)
            result = self.lib.proc_pidinfo(pid, flavor, 0, ctypes.byref(data), ctypes.sizeof(data))
            if result != ctypes.sizeof(data):
                error = ctypes.get_errno() or errno.EPROTO
                raise OSError(error, f'proc_pidinfo({pid},{flavor}) returned {result}')
        if (before.uniqueid, before.pidversion) != (after.uniqueid, after.pidversion):
            raise OSError(errno.EAGAIN, f'PID {pid} identity changed during coalition observation')
        if not before.uniqueid or not coalition.ids[0]:
            raise OSError(errno.EPROTO, 'Missing native process identity or resource coalition')
        return {'pid': pid, 'pidversion': before.pidversion, 'uniqueid': before.uniqueid,
                'coalition': coalition.ids[0]}

    def get_pids(self):
        ctypes.set_errno(0)
        estimate = self.lib.proc_listallpids(None, 0)
        # libproc can turn a failed proc_listpids call into zero, not minus one.
        if estimate <= 0:
            raise OSError(ctypes.get_errno() or errno.EPROTO, 'Native process enumeration failed')
        capacity = estimate + 64
        for _ in range(8):
            # A diagnostic memory bound must refuse a truncated result, never certify it.
            if capacity > 131072:
                raise OSError(errno.ENOMEM, 'Native process enumeration exceeds probe buffer budget')
            pids = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = self.lib.proc_listallpids(pids, ctypes.sizeof(pids))
            if count <= 0 or count > capacity:
                raise OSError(ctypes.get_errno() or errno.EPROTO, 'Invalid native process enumeration')
            if count < capacity:
                return sorted({pid for pid in pids[:count] if pid > 0})
            capacity *= 2
        raise OSError(errno.EAGAIN, 'Native process enumeration kept filling its buffer')

    def get_coalition_members(self, coalition):
        if type(coalition) is not int or coalition <= 0 or coalition > 18446744073709551615:
            raise ValueError('Expected a positive native coalition ID')
        members = []
        for pid in self.get_pids():
            try:
                info = self.get_info(pid)
            except ProcessLookupError:
                continue  # A vanished snapshot PID contributes no current process identity.
            # Permission, layout and changing-identity errors must not hide an unknown member.
            if info['coalition'] == coalition:
                members.append(info)
        return members

    def get_active_count(self, coalition):
        if type(coalition) is not int or coalition <= 0 or coalition > 18446744073709551615:
            raise ValueError('Expected a positive native coalition ID')
        # XNU copies MIN(user size, struct size); the first two fields are uint64.
        counters = (ctypes.c_uint64 * 2)()
        ctypes.set_errno(0)
        result = self.lib.coalition_info_resource_usage(coalition, counters, ctypes.sizeof(counters))
        if result:
            raise OSError(ctypes.get_errno() or errno.EPROTO, f'coalition usage returned {result}')
        if counters[1] > counters[0]:
            raise ValueError('coalition exits exceed starts')
        return counters[0] - counters[1]

    def apply_signal(self, info, sig, *, stale=False):
        token = AuditToken()
        token.values[5] = info['pid']
        token.values[7] = (info['pidversion'] + int(stale)) & 0xffffffff
        return self.lib.proc_signal_with_audittoken(ctypes.byref(token), sig)


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


def run_child(root):
    api = MacApi()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    save_json(root / 'child.json', api.get_info(os.getpid()))
    counter = 0
    while True:
        counter += 1
        save_json(root / 'pulse.json', {'counter': counter})
        time.sleep(.02)


def run_job(root):
    child = None
    report = {'measurements': []}
    try:
        api = MacApi()
        owner = api.get_info(os.getpid())
        report['owner'] = owner
        initial = api.get_active_count(owner['coalition'])
        report['measurements'].append({'initial_active': initial})
        if initial != 1:
            raise ValueError('transient launchd job does not have an exclusive initial coalition')
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
        if api.apply_signal(child, signal.SIGKILL, stale=True) != errno.ESRCH:
            raise ValueError('wrong PID version was not rejected with ESRCH')
        if api.apply_signal(child, signal.SIGSTOP):
            raise OSError('audit-token SIGSTOP refused')
        time.sleep(.1)
        pulse = (root / 'pulse.json').read_bytes()
        time.sleep(.1)
        if pulse != (root / 'pulse.json').read_bytes():
            raise ValueError('SIGSTOP failed to freeze detached child')
        if api.apply_signal(child, signal.SIGCONT):
            raise OSError('audit-token SIGCONT refused')
        wait_for(lambda: pulse != (root / 'pulse.json').read_bytes())
        if api.apply_signal(child, signal.SIGTERM):
            raise OSError('audit-token SIGTERM refused')
        time.sleep(.2)
        if api.get_active_count(owner['coalition']) != 2:
            raise ValueError('TERM-refusing child did not retain membership')
        if api.apply_signal(child, signal.SIGKILL):
            raise OSError('audit-token SIGKILL refused')
        wait_for(lambda: api.get_active_count(owner['coalition']) == 1)
        report['measurements'].append({'after_forced_stop_active': 1})
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
        save_json(root / 'report.json', report)


def run_controller(output):
    if sys.platform != 'darwin':
        raise SystemExit('NOT RUN: requires native macOS; Linux cannot verify these prerequisites')
    label = 'org.alexandria.probe369.' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='alexandria-369-probe-') as tmp:
        root = Path(tmp)
        api = MacApi()
        sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(45)'])
        sentinel_info = api.get_info(sentinel.pid)
        try:
            admitted = subprocess.run(['/bin/launchctl', 'submit', '-l', label, '--',
                                       sys.executable, str(Path(__file__).resolve()), '--job', tmp],
                                      capture_output=True, text=True, timeout=10)
            if admitted.returncode:
                raise RuntimeError(f'launchctl submit refused: {admitted.stderr}')
            wait_for(lambda: (root / 'report.json').exists(), seconds=20)
            report = json.loads((root / 'report.json').read_text())
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
            # Remove exactly this UUID-labelled diagnostic job; no domain removal.
            subprocess.run(['/bin/launchctl', 'remove', label], capture_output=True, timeout=10)
            if (root / 'child.json').exists():
                info = json.loads((root / 'child.json').read_text())
                result = api.apply_signal(info, signal.SIGKILL)
                if result not in (0, errno.ESRCH):
                    raise OSError(result, 'probe child cleanup refused')
            sentinel.terminate()
            sentinel.wait(timeout=5)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--child']:
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
