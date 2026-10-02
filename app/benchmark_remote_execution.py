"""Benchmark SSH control keeps the task claimed until remote shutdown is proven."""
import json
import os
import shlex
import socket
import subprocess
import tempfile
import time
import uuid

from subprocess_ownership import start_owned_subprocess
from benchmark_remote_command import STOPPED_MARKER


def run_remote_benchmark_subprocess(command, worker, state, *, timeout, check,
                                    input, capture_output, **kwargs):
    from benchmark_execution import BenchmarkCancelled, get_benchmark_process_options
    from core import ensure_failed_subprocess_stopped, CANCEL_TERMINATE_GRACE_SECONDS
    if not capture_output or not kwargs.get('text'):
        raise ValueError('Remote benchmark commands require captured text output')
    token = uuid.uuid4().hex
    app_dir = os.path.dirname(worker[1])
    supervisor = shlex.join([worker[0], os.path.join(app_dir, 'benchmark_remote_command.py')])
    transport = [command[0], command[1], supervisor]
    packet = (json.dumps({'command':worker, 'input':input, 'token':token}) + '\n').encode('utf-8')
    probe_code = (
        'import sys;sys.path.insert(0,sys.argv[1]);'
        'from benchmark_remote_command import get_remote_benchmark_receipt;'
        'p=get_remote_benchmark_receipt(sys.argv[2]);'
        'print("BENCHMARK_RECEIPT="+sys.argv[2] if p.exists() and p.read_text()=="stopped\\n" else "pending")')
    probe = [command[0], command[1], shlex.join([worker[0], '-c', probe_code, app_dir, token])]
    cleanup_code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                    'from benchmark_remote_command import apply_remote_benchmark_receipt_cleanup;'
                    'apply_remote_benchmark_receipt_cleanup(sys.argv[2])')
    cleanup = [command[0], command[1], shlex.join([worker[0], '-c', cleanup_code, app_dir, token])]
    sender, receiver = socket.socketpair()
    sender.setblocking(False)
    process = None
    started = time.monotonic()
    reason = None
    stopping_started = None
    pending = packet
    cancel_sent = False
    confirmed = False
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        options = dict(kwargs, stdin=receiver, stdout=stdout, stderr=stderr)
        try:
            process = start_owned_subprocess(transport, **get_benchmark_process_options(state, options))
            receiver.close()
            state.setdefault('processes', []).append(process)
            while process.poll() is None:
                if reason is None:
                    if state.get('cancel'):
                        reason = BenchmarkCancelled('Benchmark cancellation requested')
                    elif timeout is not None and time.monotonic() - started >= timeout:
                        reason = subprocess.TimeoutExpired(command, timeout)
                    if reason is not None:
                        stopping_started = time.monotonic()
                if not pending and reason is not None and not cancel_sent:
                    pending = b'cancel\n'
                    cancel_sent = True
                if pending:
                    try:
                        pending = pending[sender.send(pending):]
                    except BlockingIOError:
                        pass
                    except (BrokenPipeError, ConnectionResetError):
                        break
                if stopping_started is not None and time.monotonic() - stopping_started >= CANCEL_TERMINATE_GRACE_SECONDS:
                    break
                time.sleep(.05)
            # Finish local ownership first. Disconnect triggers the remote
            # supervisor's EOF policy; its independent owner retains cleanup.
            if process.poll() is None:
                ensure_failed_subprocess_stopped(process, state)
            stdout.seek(0); stderr.seek(0)
            encoding = kwargs.get('encoding') or 'utf-8'
            errors = kwargs.get('errors') or 'strict'
            output = stdout.read().decode(encoding, errors)
            error_output = stderr.read().decode(encoding, errors)
            if reason is None and state.get('cancel'):
                reason = BenchmarkCancelled('Benchmark cancellation requested')
            if reason is not None:
                raise reason
            result = subprocess.CompletedProcess(command, process.returncode, output, error_output)
            if check:
                result.check_returncode()
            return result
        finally:
            sender.close(); receiver.close()
            if process is not None:
                try:
                    if process.poll() is None:
                        ensure_failed_subprocess_stopped(process, state)
                finally:
                    # Even decoding/parsing/transport failures retain the claim
                    # until the remote owner proves its entire tree stopped.
                    stdout.seek(0)
                    confirmed = STOPPED_MARKER + token in stdout.read().decode('utf-8', 'replace').splitlines()
                    if not confirmed:
                        state.setdefault('logs', []).append('Remote benchmark connection ended without shutdown acknowledgment; retaining the task until its owner receipt is verified.')
                    while not confirmed:
                        try:
                            receipt = subprocess.run(probe, capture_output=True, text=True,
                                                     timeout=15, env=kwargs.get('env'))
                            confirmed = receipt.returncode == 0 and 'BENCHMARK_RECEIPT=' + token in receipt.stdout.splitlines()
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                        if not confirmed:
                            time.sleep(1)
                    # A separate post-confirmation command is essential: a
                    # lost receipt-query reply must leave its proof recoverable.
                    try:
                        removed = subprocess.run(cleanup, capture_output=True, text=True,
                                                 timeout=15, env=kwargs.get('env'))
                        if removed.returncode:
                            state.setdefault('logs', []).append('Remote shutdown was verified; receipt cleanup failed. Check the worker host for retained diagnostics.')
                    except (OSError, subprocess.TimeoutExpired):
                        state.setdefault('logs', []).append('Remote shutdown was verified; receipt cleanup connection failed. Check the worker host for retained diagnostics.')
                    control = getattr(process, '_alexandria_control', None)
                    if control is not None:
                        control.close()
                    if process in state.get('processes', []):
                        state['processes'].remove(process)
