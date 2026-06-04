import errno
import os
import json
import shutil
import time
import tempfile


def atomic_json_write(data, target_path, max_retries=5):
    """Atomically write JSON data using a temp file and os.replace.

    Includes retry logic with exponential backoff for Windows file locking
    (Access is denied / file in use errors).  On cross-device paths (e.g.
    Linux bind mounts or NAS shares) os.replace raises EXDEV; in that case
    the function falls back to shutil.move (copy + delete) so the write
    succeeds rather than raising an unhandled OSError.
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
                if e.errno == errno.EXDEV:
                    # Cross-device rename is not supported by the kernel.
                    # shutil.move falls back to copy+delete, which is not
                    # atomic but avoids a hard failure.
                    shutil.move(tmp_path, target_path)
                    return
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
