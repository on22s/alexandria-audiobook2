"""Refuse to talk to the GPU behind the queue's back.

WHAT HAPPENED. An experiment's result looked wrong, so I ran a handful of LLM
calls by hand to see what the model was replying. A book generation was holding
the GPU at the time; my calls queued against the same server, took the card's
attention away from the job that had properly acquired it, and timed out after
two minutes having diagnosed nothing.

The queue exists so work does not collide. Nothing stopped me stepping around
it, and "remember not to do that" is not a mechanism.

HOW IT KNOWS. gpu_job.sh exports ALEXANDRIA_GPU_LOCK_HELD=1 into every job it
starts, so anything running UNDER the queue is allowed through untouched. A
process that finds the lock held without that sentinel is by definition working
beside a job rather than as one, and is refused.

It refuses rather than waits. A wait would hide the mistake and quietly serve
the same collision later; an error names the queue and tells the caller how to
join it. ALEXANDRIA_ALLOW_CONTENTION=1 overrides it for a call that genuinely
must run beside a job.
"""
import fcntl
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LOCK = os.path.join(REPO, "ab_test_runtime", "logs", "alexandria_gpu.lock")


def gpu_is_busy(lock_path=None):
    """-> True when another process holds the GPU lock.

    Tested by trying to take it, not by reading a file: a pid file can be
    stale, a held flock cannot. The probe releases immediately, so it never
    delays the job that owns it.
    """
    path = lock_path or os.environ.get("GPU_LOCK") or DEFAULT_LOCK
    try:
        handle = open(path, "a")
    except OSError:
        return False                      # no lock file, no queue to respect
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def require_free_gpu(what="this call", lock_path=None):
    """Raise unless we hold the queue, or nothing else does."""
    if os.environ.get("ALEXANDRIA_GPU_LOCK_HELD") == "1":
        return                            # we ARE the queued job
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
