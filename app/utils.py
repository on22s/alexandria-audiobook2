import os
import json
import time
import tempfile
import contextlib
import re
import subprocess
import uuid


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
        # rocm-smi sometimes prints warnings to stdout ahead of the JSON, and
        # the JSON payload itself may be pretty-printed across several lines.
        # Parse everything from the first line that opens the JSON object so a
        # multi-line payload isn't truncated to just "{".
        lines = result.stdout.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith('{'):
                return json.loads('\n'.join(lines[i:]))
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
    # Windows: trailing dots/spaces are illegal and silently stripped by the OS.
    filename = filename.rstrip(". ")
    if not filename:
        return ""
    # Windows reserved device names are unusable even with an extension
    # (CON.txt still maps to the console device). Prefix them so the sanitized
    # name is a normal file on every platform.
    _WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                     *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}
    if filename.split(".")[0].upper() in _WIN_RESERVED:
        filename = "_" + filename
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
        # mkstemp creates the temp file 0600; relax to 0644 so the replaced file
        # isn't owner-only (other local tools/users may read it). A fixed chmod
        # avoids mutating the process-global umask, which would race other
        # file-creating threads.
        try:
            os.chmod(tmp_path, 0o644)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            # Flush to disk before the rename. os.replace is atomic against a
            # process crash, but NOT against power loss with unflushed data —
            # which is exactly the case the checkpoint-resume feature must survive
            # (a truncated/empty target would otherwise read back as "no state").
            f.flush()
            os.fsync(f.fileno())

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
    # Unique owner token written into the lock file, so reaping and release can
    # tell OUR lock from one another process reaped-and-recreated.
    my_token = f"{os.getpid()}-{uuid.uuid4().hex}".encode()
    deadline = time.time() + timeout
    acquired = False
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, my_token)
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > stale_after:
                    # Claim the stale lock ATOMICALLY via rename before deleting.
                    # If two waiters both judge it stale, only one rename succeeds;
                    # the loser gets OSError and retries — so they can't both
                    # delete it and each then acquire a fresh lock (double-hold).
                    reaped = f"{lock_path}.reap.{os.getpid()}-{uuid.uuid4().hex}"
                    os.rename(lock_path, reaped)
                    os.remove(reaped)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise TimeoutError(f"Could not acquire file lock on {lock_path} within {timeout} seconds.")
            time.sleep(0.05)
    try:
        yield
    finally:
        # Only remove the lock if it STILL holds our token. If we held it past
        # stale_after and a waiter reaped it (renaming it away and possibly
        # recreating its own), the file is now either gone or someone else's —
        # deleting it by path would break their mutual exclusion.
        if acquired:
            try:
                with open(lock_path, "rb") as f:
                    still_ours = f.read() == my_token
                if still_ours:
                    os.remove(lock_path)
            except OSError:
                pass
