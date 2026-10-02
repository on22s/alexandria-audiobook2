"""Native app/benchmark ownership checks with real kernel leases; CPU only."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from macos_369_owner_checks import LEAF, wait_for, is_lease_free
from macos_subprocess_kernel import MacApi, is_macos_coalition_reaped
from subprocess_ownership import send_subprocess_signal


@contextmanager
def ensure_test_claim(core, root, name):
    from task_ownership import acquire_task_lease
    from experiments.gpu_guard import acquire_gpu_lock
    gpu = acquire_gpu_lock(root / (name + '.gpu'))
    task = acquire_task_lease(root, name, set())
    state = {'running': True, 'logs': [], 'process': None, 'pid': None,
             'cancel': False, 'paused': False, 'processes': []}
    paths = [Path(gpu.name), Path(task.name)]
    try:
        with patch.dict(core.process_state, {name: state}), \
             patch.dict(core._gpu_leases, {name: gpu}), \
             patch.dict(core._task_claims, {name: {'lease': task, 'phase': 'started'}}):
            yield state, (gpu, task), paths
    finally:
        gpu.close()
        task.close()


def check_stream(core, root, inherited, fail_reader=False):
    prefix = root / ('stream_' + str(inherited) + '_' + str(fail_reader))
    pulse, identity, late = [prefix.with_suffix('.' + ext) for ext in ('pulse', 'identity', 'late')]
    leaf = [sys.executable, '-c', LEAF, str(pulse), str(identity), str(late)]
    wrapper = "import subprocess,sys;subprocess.Popen(sys.argv[1:]" + (')' if inherited else ',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)') + ";print('root done',flush=True)"
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'app')}
    with ensure_test_claim(core, root, prefix.name) as (state, leases, paths):
        outcome = []
        class ReaderFailureQueue(core.queue.Queue):
            def get(self, *args, **kwargs):
                if identity.exists():
                    raise ValueError('controlled app log failure')
                return super().get(*args, **kwargs)
        def run():
            try:
                outcome.append(core._stream_subprocess_to_logs(
                    [sys.executable, '-c', wrapper, *leaf], str(root), state, env=environment))
            except BaseException as error:
                outcome.append(error)
        worker = threading.Thread(target=run)
        with patch.object(core, 'CANCEL_TERMINATE_GRACE_SECONDS', .5):
            queue_patch = patch.object(core.queue, 'Queue', ReaderFailureQueue) if fail_reader else nullcontext()
            with queue_patch:
                worker.start()
                info = None
                try:
                    wait_for(lambda: identity.exists() and pulse.exists(), 'actual app caller did not admit worker')
                    info = json.loads(identity.read_text())
                    if not fail_reader:
                        wait_for(lambda: state['process'] is not None and any('root done' in x for x in state['logs']), 'root output missing')
                        # Drop original application descriptors: only the outside owner may retain these locks.
                        for lease in leases:
                            lease.close()
                        assert all(not is_lease_free(path) for path in paths), 'app owner failed to borrow both leases'
                        assert worker.is_alive(), 'root exit completed app while detached child remained'
                        send_subprocess_signal(state['process'], signal.SIGSTOP)
                        time.sleep(.3)
                        frozen = pulse.read_text()
                        time.sleep(.3)
                        assert pulse.read_text() == frozen, 'app pause missed detached child'
                        send_subprocess_signal(state['process'], signal.SIGCONT)
                        wait_for(lambda: pulse.read_text() != frozen, 'app resume missed detached child')
                        state['cancel'] = True
                    worker.join(timeout=20)
                    assert not worker.is_alive(), 'app did not finish owned cleanup'
                    assert is_macos_coalition_reaped(MacApi(), info['coalition']), 'app returned before strict native reap'
                    assert state['process'] is None and state['pid'] is None, 'app retained stale process state'
                    if fail_reader:
                        assert len(outcome) == 1 and isinstance(outcome[0], ValueError), repr(outcome)
                        assert str(outcome[0]) == 'controlled app log failure'
                        for lease in leases:
                            lease.close()
                    else:
                        assert len(outcome) == 1 and outcome[0][0] == 0, repr(outcome)
                        assert late.exists(), 'app cancellation did not reach TERM-refusing child'
                    assert all(is_lease_free(path) for path in paths), 'reaped app owner retained lease'
                    return {'pipes': 'inherited' if inherited else 'closed', 'exception': fail_reader,
                            'coalition': info['coalition'], 'reaped': True, 'leases': 2}
                finally:
                    state['cancel'] = True
                    process = state.get('process')
                    if process is not None and process.poll() is None:
                        send_subprocess_signal(process, signal.SIGKILL)
                    worker.join(timeout=20)
                    assert not worker.is_alive(), 'fixture left app thread alive'


def check_benchmark(core, root):
    import benchmark_execution as execution
    state = {'cancel': False}
    token = execution.BENCHMARK_STATE.set(state)
    try:
        payload = '音声🙂-' * 30000
        result = execution.run_benchmark_subprocess([sys.executable, '-c',
            'import sys,time;time.sleep(.2);data=sys.stdin.read();print(data,end="");print("stderr-marker",file=sys.stderr)'],
            input=payload, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0 and result.stdout == payload and result.stderr == 'stderr-marker\n'
        assert state['processes'] == []
    finally:
        execution.BENCHMARK_STATE.reset(token)
    pulse, identity, late = [root / ('bench.' + ext) for ext in ('pulse', 'identity', 'late')]
    wrapper = "import subprocess,sys;subprocess.Popen(sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print('bench root done',flush=True)"
    leaf = [sys.executable, '-c', LEAF, str(pulse), str(identity), str(late)]
    with ensure_test_claim(core, root, 'native_benchmark') as (state, leases, paths):
        monitor_errors = []
        def cancel():
            try:
                wait_for(identity.exists, 'benchmark detached child not admitted')
                for lease in leases:
                    lease.close()
                assert all(not is_lease_free(path) for path in paths), 'benchmark failed to borrow both leases'
                state['cancel'] = True
            except BaseException as error:
                monitor_errors.append(error)
                state['cancel'] = True
        monitor = threading.Thread(target=cancel)
        token = execution.BENCHMARK_STATE.set(state)
        monitor.start()
        try:
            with patch.object(core, 'CANCEL_TERMINATE_GRACE_SECONDS', .5):
                try:
                    execution.run_benchmark_subprocess([sys.executable, '-c', wrapper, *leaf],
                        capture_output=True, text=True, timeout=20,
                        env={**os.environ, 'PYTHONPATH': str(ROOT / 'app')})
                except execution.BenchmarkCancelled:
                    pass
                else:
                    raise AssertionError('benchmark returned while detached worker was live')
            monitor.join(timeout=20)
            assert not monitor.is_alive() and not monitor_errors, repr(monitor_errors)
            info = json.loads(identity.read_text())
            assert is_macos_coalition_reaped(MacApi(), info['coalition'])
            assert all(is_lease_free(path) for path in paths)
            assert state['processes'] == []
            return {'stdio_verified': True, 'cancel_reaped': True, 'coalition': info['coalition'], 'leases': 2}
        finally:
            state['cancel'] = True
            monitor.join(timeout=20)
            execution.BENCHMARK_STATE.reset(token)


def main():
    result = {'platform': sys.platform, 'passed': False}
    sentinel = None
    try:
        assert sys.platform == 'darwin', 'Actual macOS required'
        with tempfile.TemporaryDirectory(prefix='alexandria-native-callers-') as temporary:
            root = Path(temporary)
            os.environ['ALEXANDRIA_DATA_DIR'] = str(root)
            project = types.ModuleType('project')
            project.ProjectManager = lambda *args, **kwargs: None
            sys.modules['project'] = project  # Only model construction is outside this CPU ownership check.
            import core
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'])
            result['stream_checks'] = [check_stream(core, root, False), check_stream(core, root, True),
                                      check_stream(core, root, True, fail_reader=True)]
            result['benchmark'] = check_benchmark(core, root)
            assert sentinel.poll() is None, 'unrelated sentinel was signalled'
            result.update(passed=True, unrelated_sentinel_alive=True)
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        Path('macos_369_callers_result.json').write_text(json.dumps(result, indent=2) + '\n')
        if sentinel is not None and sentinel.poll() is None:
            sentinel.terminate()
            sentinel.wait(timeout=5)


if __name__ == '__main__':
    main()
