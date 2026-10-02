"""Stdlib-only remote worker owner, controlled through the SSH stdin channel."""
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading

from subprocess_ownership import start_owned_subprocess, save_subprocess_stop_receipt

STOPPED_MARKER = 'BENCHMARK_OWNER_STOPPED='


def get_remote_benchmark_receipt(token):
    if not isinstance(token, str) or re.fullmatch('[0-9a-f]{32}', token) is None:
        raise ValueError('Invalid remote benchmark ownership token')
    return Path(tempfile.gettempdir()) / ('alexandria-benchmark-owner-' + token) / 'stopped'


def is_llm_benchmark_command(command):
    return len(command) >= 2 and Path(command[1]).name == 'llm_benchmark_worker.py'


def apply_remote_benchmark_receipt_cleanup(token):
    receipt = get_remote_benchmark_receipt(token)
    if receipt.read_text() != 'stopped\n':
        raise ValueError('Remote benchmark shutdown receipt is incomplete')
    receipt.unlink()
    receipt.parent.rmdir()


def run_remote_benchmark_command(packet, control_input):
    """Acknowledge shutdown only after every owned Linux descendant is reaped."""
    if sys.platform != 'linux':
        raise RuntimeError('Remote benchmark ownership requires Linux')
    command = packet.get('command')
    token = packet.get('token')
    data = packet.get('input')
    if (not isinstance(command, list) or not command
            or any(not isinstance(arg, str) for arg in command)
            or not isinstance(token, str) or re.fullmatch('[0-9a-f]{32}', token) is None
            or (data is not None and not isinstance(data, str))):
        raise ValueError('Invalid remote benchmark command packet')
    cancelled = threading.Event()
    receipt = get_remote_benchmark_receipt(token)
    # Exclusive private admission directory prevents another run from reusing
    # a stale shutdown receipt. Keep it until the orchestrator verifies it.
    receipt.parent.mkdir(mode=0o700)
    def read_control():
        while True:
            line = control_input.readline()
            if not line or line.strip() == 'cancel':
                cancelled.set()
                return
    process = None
    cooperative = is_llm_benchmark_command(command)
    cancellation_sent = False
    with tempfile.TemporaryFile() as worker_input:
        if data is not None:
            worker_input.write(data.encode('utf-8'))
        worker_input.seek(0)
        try:
            try:
                process = start_owned_subprocess(command, stdin=worker_input, start_new_session=True,
                    stop_receipt_path=receipt,
                    disconnect_signal=signal.SIGTERM if cooperative else signal.SIGKILL)
            except OSError:
                # The ownership helper waits for its failed owner before
                # raising: rejected admission cannot leave live descendants.
                if not receipt.exists():
                    save_subprocess_stop_receipt(receipt)
                print('\n' + STOPPED_MARKER + token, flush=True)
                raise
            threading.Thread(target=read_control, daemon=True).start()
            while process.poll() is None:
                if cancelled.is_set() and not cancellation_sent:
                    # Server-backed requests finish under their existing SDK
                    # policy; other workers use the owner's disconnect fail-safe.
                    if cooperative:
                        process._alexandria_control.sendall(b'15\n')
                    else:
                        process._alexandria_control.close()
                    cancellation_sent = True
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    continue
            result = process.returncode
        finally:
            if process is not None:
                control = getattr(process, '_alexandria_control', None)
                if control is not None:
                    control.close()
                process.wait()
    print('\n' + STOPPED_MARKER + token, flush=True)
    return 130 if cancelled.is_set() else result


def main():
    packet = json.loads(sys.stdin.readline())
    return run_remote_benchmark_command(packet, sys.stdin)


if __name__ == '__main__':
    sys.exit(main())
