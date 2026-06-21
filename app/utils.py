import os
import json
import time
import tempfile
import contextlib
import re
import subprocess
import sys

# --- GPU stats (rocm-smi) ---
# Canonical implementation lives in gpu_stats.py at the repo root, shared
# with the standalone alexandria_*.py scripts which can't import from
# inside this package. Re-exported here so existing `from utils import
# run_rocm_smi_json` call sites in app/ keep working unchanged.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gpu_stats import run_rocm_smi_json  # noqa: F401


# --- Balanced-bracket text extraction ---

def extract_balanced(text, open_char, close_char):
    """Find the first `open_char ... close_char`-balanced span in `text`,
    tracking string-escaping so a quoted brace/bracket doesn't desync the
    depth count. Returns the matched substring, or None if `open_char`
    never appears or never balances back to depth 0.

    Shared by clean_json_string ([...]) and extract_json_object ({...}) -
    both need the same escape-aware bracket-matching, just for a different
    delimiter pair."""
    start = text.find(open_char)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue

        if ch == '\\':
            if in_string:
                escape_next = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json_object(text):
    """Extract the first JSON object from text using robust parsing.

    Tries standard json.loads first, then falls back to escape-aware
    brace-matching (extract_balanced) for free-form LLM output that wraps
    the object in other text.
    """
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    span = extract_balanced(text, '{', '}')
    if span is None:
        return None
    try:
        return json.loads(span)
    except json.JSONDecodeError:
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
