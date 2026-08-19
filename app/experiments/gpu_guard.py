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


def require_free_gpu(what="this call", lock_path=None):
    """Raise unless we hold the queue, or nothing else does."""
    # THE SENTINEL MUST BELONG TO A LIVE JOB. gpu_job.sh exports it into the
    # job's environment, and a shell or editor opened from inside a chain
    # inherits it for the rest of its life - including after that job ends and
    # a different one takes the card. Pairing it with the exporting pid, and
    # checking that pid is still alive, keeps the exemption tied to the job it
    # was granted for.
    if os.environ.get("ALEXANDRIA_GPU_LOCK_HELD") == "1":
        owner = os.environ.get("ALEXANDRIA_GPU_LOCK_PID")
        if not owner:
            return                        # older jobs: no pid to check
        try:
            os.kill(int(owner), 0)
            return                        # the job that was granted it is alive
        except (ValueError, ProcessLookupError):
            pass                          # stale sentinel; fall through
        except PermissionError:
            return                        # exists, not ours to signal
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
