"""Actual macOS production-owner checks, CPU only; no model imports."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))
from macos_subprocess_kernel import MacApi, is_macos_coalition_reaped
from subprocess_ownership import start_owned_subprocess as start_macos_owned_subprocess
from subprocess_ownership import send_subprocess_signal


def wait_for(predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(message)


def is_lease_free(path):
    with path.open('r+b') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(stream, fcntl.LOCK_UN)
        return True


LEAF = r"""
import json, os, signal, sys, time
from pathlib import Path
from macos_subprocess_kernel import MacApi
pulse, identity, late = map(Path, sys.argv[1:])
os.setsid()
def refuse(sig, frame):
    if not late.exists():
        late.write_text('forked')
        for _ in range(2):
            child = os.fork()
            if child == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                while True:
                    time.sleep(.1)
signal.signal(signal.SIGTERM, refuse)
identity.write_text(json.dumps(MacApi().get_info(os.getpid())))
i = 0
while True:
    i += 1
    pulse.write_text(str(i))
    time.sleep(.03)
"""


def check_detached_scope(tmp, inherited_pipes):
    prefix = tmp / ('inherited' if inherited_pipes else 'closed')
    identity = prefix.with_suffix('.identity')
    pulse = prefix.with_suffix('.pulse')
    late = prefix.with_suffix('.late')
    lease = prefix.with_suffix('.lease')
    receipt = prefix.with_suffix('.receipt')
    notice = prefix.with_suffix('.exit')
    command = [sys.executable, '-c', LEAF, str(pulse), str(identity), str(late)]
    wrapper = "import subprocess,sys; subprocess.Popen(sys.argv[1:]" + (')' if inherited_pipes else ', stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)') + "; print('root done',flush=True)"
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'app')}
    with lease.open('w+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        process = start_macos_owned_subprocess([sys.executable, '-c', wrapper, *command],
            gpu_lease_fd=held.fileno(), stop_receipt_path=receipt, exit_notice_path=notice,
            termination_grace=1, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        held.close()
        try:
            wait_for(lambda: identity.exists() and pulse.exists() and notice.exists(), 'worker/root admission missing')
            info = json.loads(identity.read_text())
            assert json.loads(notice.read_text()) == {'exit_code': 0}
            assert process.poll() is None, 'root exit released the task owner'
            assert not is_lease_free(lease), 'root exit released the real lease'
            send_subprocess_signal(process, signal.SIGSTOP)
            time.sleep(.3)
            frozen = pulse.read_text()
            time.sleep(.3)
            assert frozen == pulse.read_text(), 'detached worker kept running while paused'
            assert not is_lease_free(lease)
            send_subprocess_signal(process, signal.SIGCONT)
            wait_for(lambda: pulse.read_text() != frozen, 'detached worker failed to resume')
            send_subprocess_signal(process, signal.SIGTERM)
            wait_for(late.exists, 'TERM refusal did not fork late descendants')
            assert not is_lease_free(lease), 'lease released during TERM grace'
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, (process.returncode, stderr)
            assert stdout == 'root done\n', stdout
            assert receipt.read_text() == 'stopped\n'
            assert is_macos_coalition_reaped(MacApi(), info['coalition'])
            assert is_lease_free(lease), 'reaped task did not release lease'
            return {'pipes': 'inherited' if inherited_pipes else 'closed', 'coalition': info['coalition'], 'root_exit': process.returncode, 'late_children': 2, 'reaped': True}
        finally:
            if process.poll() is None:
                send_subprocess_signal(process, signal.SIGKILL)
                process.wait(timeout=20)
            process._alexandria_control.close()
            process.stdout.close()
            process.stderr.close()


def check_app_death(tmp):
    lease, identity, pulse, late, ready = [tmp / name for name in ('app.lease', 'app.identity', 'app.pulse', 'app.late', 'app.ready')]
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'app')}
    child = [sys.executable, '-c', LEAF, str(pulse), str(identity), str(late)]
    app_code = "import fcntl,json,pathlib,sys,time; from subprocess_ownership import start_owned_subprocess as start_macos_owned_subprocess; held=open(sys.argv[1],'w+b'); fcntl.flock(held,fcntl.LOCK_EX); p=start_macos_owned_subprocess(json.loads(sys.argv[3]),gpu_lease_fd=held.fileno()); held.close(); pathlib.Path(sys.argv[2]).write_text(str(p.pid)); time.sleep(60)"
    app = subprocess.Popen([sys.executable, '-c', app_code, str(lease), str(ready), json.dumps(child)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        wait_for(lambda: ready.exists() and identity.exists(), 'outside app did not admit real worker')
        info = json.loads(identity.read_text())
        assert not is_lease_free(lease)
        app.kill()  # Verified Popen PID, no command-line matching.
        app.wait(timeout=5)
        wait_for(lambda: is_macos_coalition_reaped(MacApi(), info['coalition']), 'app death left its private scope alive')
        wait_for(lambda: is_lease_free(lease), 'app death cleanup retained lease after kernel reap')
        return {'coalition': info['coalition'], 'app_death': True, 'reaped': True}
    finally:
        if app.poll() is None:
            app.kill()
            app.wait()
        app.stderr.close()


def main():
    result = {'platform': sys.platform, 'checks_passed': False}
    destination = Path('macos_369_owner_result.json')
    sentinel = None
    try:
        assert sys.platform == 'darwin', 'These checks require the actual Apple kernel'
        sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        with tempfile.TemporaryDirectory(prefix='alexandria-native-owner-') as temporary:
            tmp = Path(temporary)
            environment = {**os.environ, 'OWNER_TEST': 'native'}
            process = start_macos_owned_subprocess([sys.executable, '-c', "import os,sys; print(os.environ['OWNER_TEST']); print(os.getcwd()); sys.exit(7)"], cwd=tmp, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                output, errors = process.communicate(timeout=20)
                assert process.returncode == 7, (process.returncode, errors)
                assert output == f'native\n{tmp.resolve()}\n', output
            finally:
                process._alexandria_control.close()
            result['scope_checks'] = [check_detached_scope(tmp, False), check_detached_scope(tmp, True), check_app_death(tmp)]
            assert sentinel.poll() is None, 'unrelated sentinel was signalled'
            result['unrelated_sentinel_alive'] = True
            result['checks_passed'] = True
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        destination.write_text(json.dumps(result, indent=2) + '\n')
        if sentinel is not None and sentinel.poll() is None:
            sentinel.terminate()
            sentinel.wait(timeout=5)


if __name__ == '__main__':
    main()
