"""Queue descendant ownership survives the initiating Bash process."""
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading

from subprocess_ownership import ensure_pidfd_support, open_owned_process, run_owned_command


def open_parent_handle(parent_pid):
    """Verify the launching parent before monitoring its stable kernel handle."""
    if os.getppid() != parent_pid:
        raise OSError("GPU wrapper exited before owner admission")
    libc = ensure_pidfd_support()
    descriptor = open_owned_process(libc, parent_pid)
    if os.getppid() != parent_pid or select.select([descriptor], [], [], 0)[0]:
        os.close(descriptor)
        raise OSError("GPU wrapper exited before owner admission")
    return descriptor


def run_queue_command(parent_pid, wrapper, command, job=None):
    """Hold inherited FD9 until reaping, including after wrapper death."""
    parent_handle = open_parent_handle(parent_pid)
    control = peer = None
    monitor = None
    stopped = threading.Event()
    owner_lost = threading.Event()
    try:
        # Reuse the queue's authoritative ancestry/inode/flock proof.
        subprocess.run(["bash", wrapper, "--check-lock-owner", str(os.getpid())],
                       check=True, pass_fds=(9,))
        os.environ["ALEXANDRIA_GPU_LOCK_HELD"] = "1"
        os.environ["ALEXANDRIA_GPU_LOCK_PID"] = str(os.getpid())
        os.environ["ALEXANDRIA_GPU_LOCK_FD"] = "9"
        if job is not None:
            from gpu_progress import ensure_queue_progress
            os.environ.update(ensure_queue_progress(parent_pid, wrapper, job))
        control, peer = socket.socketpair()
        def monitor_parent():
            while not stopped.is_set():
                if select.select([parent_handle], [], [], .05)[0]:
                    owner_lost.set()
                    peer.shutdown(socket.SHUT_WR)
                    return
        monitor = threading.Thread(target=monitor_parent, daemon=True)
        monitor.start()
        if select.select([parent_handle], [], [], 0)[0]:
            raise OSError("GPU wrapper exited before worker admission")
        # The actual worker closes FD9 via Popen(close_fds=True). This owner
        # alone inherits it and resumes/TERMs before the full checkpoint grace.
        result = run_owned_command(control, command, disconnect_signal=signal.SIGTERM,
                                   stop_timeout=20)
        if select.select([parent_handle], [], [], 0)[0]:
            owner_lost.set()
        return result, owner_lost.is_set()
    finally:
        stopped.set()
        if monitor is not None:
            monitor.join()
        if control is not None:
            control.close()
        if peer is not None:
            peer.close()
        os.close(parent_handle)


if __name__ == "__main__":
    parent_pid = int(sys.argv[1])
    wrapper = str(Path(sys.argv[2]).resolve())
    name = sys.argv[3]
    try:
        result, owner_lost = run_queue_command(parent_pid, wrapper, sys.argv[4:], job=name)
        if owner_lost:
            print(f"gpu_job: launching wrapper exited; owned worker tree reaped for {name}",
                  file=sys.stderr, flush=True)
            subprocess.run(["bash", wrapper, "--write-owner-result", name,
                            str(128 - result if result < 0 else result), str(parent_pid)],
                           check=True, pass_fds=(9,))
        sys.exit(128 - result if result < 0 else result)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"gpu_job: GPU command owner failed: {error}", file=sys.stderr, flush=True)
        sys.exit(4)
