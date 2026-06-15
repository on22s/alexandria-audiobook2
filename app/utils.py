import os
import json
import time
import tempfile
import contextlib
import re
import subprocess


# --- GPU stats (rocm-smi) ---

def run_rocm_smi_json(args, rocm_smi_path="rocm-smi", timeout=5):
    """Run `<rocm_smi_path> <args> --json` and return the parsed per-card dict, or None.

    Filters stdout down to JSON-looking lines first, since rocm-smi sometimes
    prints warnings to stdout ahead of the JSON payload. Returns None if the
    binary is missing, times out, or produces no JSON.
    """
    try:
        result = subprocess.run(
            [rocm_smi_path] + list(args) + ["--json"],
            capture_output=True, text=True, timeout=timeout
        )
        json_lines = [line for line in result.stdout.split('\n') if line.strip().startswith('{')]
        if json_lines:
            return json.loads(json_lines[0])
    except Exception:
        pass
    return None


# --- Filename Sanitization ---

def secure_filename(filename: str) -> str:
    """Sanitize a filename to prevent path-traversal attacks.

    Removes path separators and null bytes, keeps only safe characters.
    Returns empty string if the result would be unsafe.
    """
    if not filename:
        return ""
    for sep in ("/", "\\", "\0"):
        filename = filename.replace(sep, "_")
    filename = filename.lstrip(". ")
    filename = re.sub(r"[^\w\-. ]", "_", filename)
    if not filename:
        return ""
    return filename


# --- Atomic JSON write (write-to-temp + rename) ---

def safe_load_json(path, default=None):
    """Load JSON from `path`, returning `default` if missing, empty, or corrupted."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return default


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
                    e.errno in (5, 32)  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
                    or "Access is denied" in str(e)
                    or "being used by another process" in str(e)
                    or "The process cannot access the file" in str(e)
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
    seconds, this raises `TimeoutError` so the caller can decide how to
    handle contention (e.g. skip the operation, return a "busy" error, or
    retry) rather than silently proceeding without the lock. A lock file
    older than `stale_after` seconds is treated as abandoned (e.g. left
    behind by a crashed process) and removed.
    
    Args:
        target_path: Path to the file being protected
        timeout: Maximum seconds to wait for lock acquisition (default: 10)
        stale_after: Seconds after which a lock file is considered abandoned (default: 30)
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
                raise TimeoutError(f"Could not acquire file lock on {lock_path} within {timeout} seconds.")
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
