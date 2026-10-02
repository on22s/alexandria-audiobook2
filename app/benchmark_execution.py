"""Per-invocation benchmark state and owned, cancellable local commands."""
import contextvars
import os
import locale
import subprocess
import sys
import time
import tempfile
import shlex

from subprocess_ownership import start_owned_subprocess, get_subprocess_lease_options
from benchmark_remote_command import is_llm_benchmark_command

BENCHMARK_STATE = contextvars.ContextVar('benchmark_execution_state', default=None)


class BenchmarkCancelled(BaseException):
    pass


def get_benchmark_process_options(state, options):
    from core import get_gpu_task_environment, get_task_lease_descriptor
    options = dict(options)
    environment = get_gpu_task_environment(state, options.pop('env', None))
    return dict(options, env=environment,
                **get_subprocess_lease_options(environment, get_task_lease_descriptor(state)),
                start_new_session=True)


def run_benchmark_subprocess(command, *, timeout=None, check=False, input=None,
                             capture_output=False, **kwargs):
    """Retain local command ownership until cancellation/timeout cleanup finishes."""
    state = BENCHMARK_STATE.get()
    if state is None:
        return subprocess.run(command, timeout=timeout, check=check, input=input,
                              capture_output=capture_output, **kwargs)
    if state.get('cancel'):
        raise BenchmarkCancelled('Benchmark cancellation requested')
    if os.path.basename(command[0]) == 'ssh' and len(command) == 3:
        worker = shlex.split(command[2])
        if len(worker) >= 2 and (worker[1].endswith('_benchmark.py')
                                 or is_llm_benchmark_command(worker)):
            from benchmark_remote_execution import run_remote_benchmark_subprocess
            return run_remote_benchmark_subprocess(command, worker, state, timeout=timeout,
                check=check, input=input, capture_output=capture_output, **kwargs)
    # Asset preparation and transfers retain their existing bounded operation.
    if os.path.basename(command[0]) in ('ssh', 'scp'):
        return subprocess.run(command, timeout=timeout, check=check, input=input,
                              capture_output=capture_output, **kwargs)
    from core import ensure_failed_subprocess_stopped
    if capture_output:
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.PIPE
    input_stream = None
    if input is not None:
        text_mode = kwargs.get('text') or kwargs.get('universal_newlines') or kwargs.get('encoding') or kwargs.get('errors')
        if text_mode:
            encoding = kwargs.get('encoding') or ('utf-8' if sys.flags.utf8_mode else locale.getpreferredencoding(False))
            input = input.encode(encoding, kwargs.get('errors') or 'strict')
        input_stream = tempfile.TemporaryFile()
        try:
            input_stream.write(input)
            input_stream.seek(0)
        except BaseException:
            input_stream.close()
            raise
        kwargs['stdin'] = input_stream
    process = None
    started = time.monotonic()
    try:
        process = start_owned_subprocess(command, **get_benchmark_process_options(state, kwargs))
        state.setdefault('processes', []).append(process)
        while True:
            if state.get('cancel'):
                raise BenchmarkCancelled('Benchmark cancellation requested')
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(.1, remaining) if remaining is not None else .1)
                break
            except subprocess.TimeoutExpired:
                continue
        if state.get('cancel'):
            raise BenchmarkCancelled('Benchmark cancellation requested')
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        try:
            if process is not None:
                try:
                    if process.poll() is None:
                        ensure_failed_subprocess_stopped(process, state)
                finally:
                    if process in state.get('processes', []):
                        state['processes'].remove(process)
                    control = getattr(process, '_alexandria_control', None)
                    if control is not None:
                        control.close()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
        finally:
            if input_stream is not None:
                input_stream.close()


class _BenchmarkResource:
    def __init__(self, resource, state):
        self._resource = resource
        self._state = state

    def __getattr__(self, name):
        value = getattr(self._resource, name)
        if name in ('chat', 'completions', 'models'):
            return _BenchmarkResource(value, self._state)
        if name not in ('create', 'list') or not callable(value):
            return value
        def request(*args, **kwargs):
            if self._state.get('cancel'):
                raise BenchmarkCancelled('Benchmark cancellation requested')
            try:
                result = value(*args, **kwargs)
            except Exception:
                if self._state.get('cancel'):
                    raise BenchmarkCancelled('Benchmark cancellation requested') from None
                raise
            if self._state.get('cancel'):
                raise BenchmarkCancelled('Benchmark cancellation requested')
            return result
        return request


def get_cancellable_benchmark_client(client):
    """Guard SDK call boundaries, preserving existing profile pacing/timeouts/retries."""
    state = BENCHMARK_STATE.get()
    return _BenchmarkResource(client, state) if state is not None else client
