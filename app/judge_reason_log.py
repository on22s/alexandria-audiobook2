"""Run-scoped, cooperating-writer JSONL storage for accepted judge evidence."""

import contextlib
import contextvars
import json
import os
import threading
import uuid

from utils import file_lock


_current_log = contextvars.ContextVar("judge_reason_log", default=None)


class JudgeReasonLog:
    def __init__(self, path, run_id=None):
        self.path = os.path.abspath(path) if path else None
        self.run_id = run_id or uuid.uuid4().hex
        self._handle = None
        self._lock = threading.Lock()
        self._closed = False

    def record_rows(self, rows):
        if not self.path or not rows:
            return
        batch = "".join(json.dumps({**row, "run_id": self.run_id},
                                   ensure_ascii=False) + "\n" for row in rows)
        with self._lock:
            if self._closed:
                raise RuntimeError("Judge reason log is closed")
            with file_lock(self.path):
                if self._handle is not None:
                    opened = os.fstat(self._handle.fileno())
                    try:
                        current = os.stat(self.path)
                    except FileNotFoundError:
                        current = None
                    if current is None or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                        self._handle.close()
                        self._handle = None
                if self._handle is None:
                    self._handle = open(self.path, "a", encoding="utf-8")
                self._handle.write(batch)
                # Preserve visibility after every accepted batch, including failures later in the run.
                self._handle.flush()

    def close(self):
        with self._lock:
            self._closed = True
            if self._handle is not None:
                self._handle.close()
                self._handle = None


@contextlib.contextmanager
def record_judge_run():
    log = JudgeReasonLog(os.environ.get("JUDGE_WHY_PATH"))
    token = _current_log.set(log)
    try:
        yield log
    finally:
        _current_log.reset(token)
        log.close()


def record_judge_rows(rows, run_id=None):
    log = _current_log.get()
    if log is not None:
        log.record_rows(rows)
    else:
        log = JudgeReasonLog(os.environ.get("JUDGE_WHY_PATH"), run_id)
        try:
            log.record_rows(rows)
        finally:
            log.close()
