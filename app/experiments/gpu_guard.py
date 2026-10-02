"""Refuse to talk to the GPU behind the queue's back.

WHAT HAPPENED. An experiment's result looked wrong, so I ran a handful of LLM
calls by hand to see what the model was replying. A book generation was holding
the GPU at the time; my calls queued against the same server, took the card's
attention away from the job that had properly acquired it, and timed out after
two minutes having diagnosed nothing.

The queue exists so work does not collide. Nothing stopped me stepping around
it, and "remember not to do that" is not a mechanism.

HOW IT KNOWS. gpu_job.sh exports an owner PID with its inherited marker.
The queue ownership CLI verifies kernel ancestry, fd9 identity and an acquired
exclusive flock. An unverifiable inherited claim is refused; an unmarked call
probes the lock normally.

It refuses rather than waits. A wait would hide the mistake and quietly serve
the same collision later; an error names the queue and tells the caller how to
join it. ALEXANDRIA_ALLOW_CONTENTION=1 overrides it for a call that genuinely
must run beside a job.
"""
import fcntl
import functools
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# THE SAME DEFAULT gpu_job.sh USES, read from gpu_job.sh itself rather than
# repeated here. This file used to name a repo-local path while gpu_job.sh
# locked ${GPU_LOCK:-$HOME/.gpu.lock} - so with GPU_LOCK unset, which is
# exactly the hand-run case this guard exists for, it probed a file nobody
# holds, CREATED it by opening in append mode, took the flock cleanly and
# reported the GPU free while a job was running. Three files answered "where
# is the lock" and the guard picked the wrong one (Rule 15).
GPU_JOB = os.path.join(REPO, "gpu_job.sh")


@functools.lru_cache(maxsize=1)
def default_lock():
    """-> the lock gpu_job.sh uses, asked of gpu_job.sh itself.

    This used to regex the assignment out of the shell source, with a
    hard-coded fallback if no line matched - so reformatting that one line
    (which happened the same day, splitting it across an if/else) silently
    moved Python's idea of the lock, and the fallback made the break look like
    a normal answer instead of an error. `--print-lock` makes the shell the
    single source of the answer (Rule 15), and a failure to get one raises.
    """
    result = subprocess.run(["bash", GPU_JOB, "--print-lock"],
                            capture_output=True, text=True, timeout=30)
    path = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if result.returncode != 0 or not path.startswith("/"):
        raise RuntimeError(
            "cannot determine the GPU lock: `bash %s --print-lock` exited %d "
            "and said %r. Refusing to guess a path - guessing is what let a "
            "hand-run job take a free lock while the queue held a different "
            "one." % (GPU_JOB, result.returncode, result.stdout.strip()))
    return path


def gpu_is_busy(lock_path=None):
    """-> True when another process holds the GPU lock.

    Tested by trying to take it, not by reading a file: a pid file can be
    stale, a held flock cannot. The probe releases immediately, so it never
    delays the job that owns it.
    """
    path = lock_path or os.environ.get("GPU_LOCK") or default_lock()
    if not os.path.exists(path):
        return False                      # no queue on this machine to respect
    try:
        handle = open(path, "a")
    except OSError:
        # FAIL CLOSED. An unopenable lock is a configuration error, and
        # answering "not busy" to it is the plausible-value fallback that
        # turns a broken check into a silent one (Rule 21). The flock branch
        # below already fails closed; this one used to fail open.
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def is_queue_lock_held_by_us(lock_path=None):
    """Verify the inherited lease using the queue's kernel ownership CLI."""
    if os.environ.get("ALEXANDRIA_GPU_LOCK_HELD") != "1":
        return False
    environment = dict(os.environ)
    if lock_path is not None:
        environment["GPU_LOCK"] = os.fspath(lock_path)
    owner = environment.get("ALEXANDRIA_GPU_LOCK_PID", "")
    try:
        result = subprocess.run(["bash", GPU_JOB, "--check-lock-owner", owner,
                                 environment.get("ALEXANDRIA_GPU_LOCK_FD", "9")],
                                env=environment, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Cannot verify inherited GPU lock ownership: {error}") from error
    if result.returncode:
        raise RuntimeError("Cannot verify inherited GPU lock ownership: " +
                           (result.stderr.strip() or "queue ownership probe failed"))
    return True


def acquire_gpu_lock(lock_path=None):
    """Hold the queue's flock until release_gpu_lock; refuse contention."""
    if is_queue_lock_held_by_us(lock_path):
        return None
    path = lock_path or os.environ.get("GPU_LOCK") or default_lock()
    handle = open(path, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise RuntimeError(f"GPU lock is held by another job: {path}") from error
    return handle


def release_gpu_lock(handle):
    if handle is not None:
        handle.close()


def require_free_gpu(what="this call", lock_path=None):
    """Raise unless we hold the queue, or nothing else does."""
    # Every inherited lease uses the same kernel ownership proof as shell chains.
    if is_queue_lock_held_by_us(lock_path):
        return
    if os.environ.get("ALEXANDRIA_ALLOW_CONTENTION") == "1":
        return
    if not gpu_is_busy(lock_path):
        return
    raise SystemExit(
        f"refusing to run {what}: a GPU job is running and this would compete "
        f"with it.\n"
        f"  queue it:   ./gpu_job.sh <name> <command...>\n"
        f"  or wait:    ./gpu_pause.sh status\n"
        f"  or override: ALEXANDRIA_ALLOW_CONTENTION=1 (say why in the log)")
