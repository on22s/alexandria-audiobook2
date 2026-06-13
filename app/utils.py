import os
import json
import time
import tempfile
import contextlib


def atomic_json_write(data, target_path, max_retries=5):
    """Atomically write JSON data using a temp file and os.replace.

    Includes retry logic with exponential backoff for Windows file locking
    (Access is denied / file in use errors).
    """
    directory = os.path.dirname(target_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        for attempt in range(max_retries):
            try:
                os.replace(tmp_path, target_path)
                return
            except OSError as e:
                if attempt < max_retries - 1 and (
                    e.errno == 5
                    or "Access is denied" in str(e)
                    or "being used by another process" in str(e)
                ):
                    delay = 0.05 * (2 ** attempt)
                    time.sleep(delay)
                    continue
                raise
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@contextlib.contextmanager
def file_lock(target_path, timeout=10, stale_after=30):
    """Advisory cross-process lock for read-modify-write access to target_path.

    Coordinates against a sibling `<target_path>.lock` marker file, created via
    an atomic exclusive open (works on POSIX and Windows). Without this, two
    processes that each read-modify-write the same JSON file (e.g. a batch
    review remapping speaker names in a voice_config.json while the UI applies
    a saved cast to it) can silently lose one side's update.

    This is advisory only: if the lock can't be acquired within `timeout`
    seconds, the caller proceeds anyway rather than failing the whole
    operation. A lock file older than `stale_after` seconds is treated as
    abandoned (e.g. left behind by a crashed process) and removed.
    """
    lock_path = target_path + ".lock"
    deadline = time.time() + timeout
    acquired = False
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > stale_after:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        # Only remove the lock file if we created it - a timed-out waiter that
        # never acquired the lock must not delete another process's active lock.
        if acquired:
            try:
                os.remove(lock_path)
            except OSError:
                pass
