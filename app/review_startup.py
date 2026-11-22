"""Bounded, content-free lifecycle evidence shared by API and CLI workers."""
from contextvars import ContextVar
from pathlib import Path
import json
import logging
import os
import re
import time
import uuid
import hashlib
import traceback
import threading
import sys
from utils import file_lock

CURRENT = ContextVar('review_startup_trace', default=None)
MAX_BYTES = 256 * 1024
ENV_ID = 'ALEXANDRIA_REVIEW_STARTUP_ID'
ENV_PATH = 'ALEXANDRIA_REVIEW_STARTUP_PATH'
# The codec a child script's stdout pipe speaks. core's launcher passes the
# same constant to Popen, so the writer (worker_environment, below) and the
# reader cannot disagree: a Windows child defaults to the ANSI code page for a
# pipe, and one print() of any other character — U+2770 in a review's diff
# preview, or any CJK text — raised UnicodeEncodeError after the work was
# already done (#2062).
WORKER_STDIO_ENCODING = 'utf-8'
_failure_limits = {}
_failure_limit_lock = threading.Lock()


def report_diagnostic_sink_failure(error):
    try:
        sys.__stderr__.write('Diagnostic sink failed (' + type(error).__name__[:64] + ')\n')
    except Exception:
        pass


def emit_diagnostic_log(logger, level, message, *args):
    try:
        logging.getLogger(logger).log(level, message, *args)
    except Exception as error:
        report_diagnostic_sink_failure(error)


class ReviewStartupTrace:
    def __init__(self, path, correlation=None):
        self.path = Path(path)
        self.correlation = correlation if re.fullmatch(r'[a-f0-9]{32}', correlation or '') else uuid.uuid4().hex
        self.started = time.monotonic()

    def event(self, stage, error=None, **options):
        # Call sites supply static stage labels. Never serialize error messages,
        # paths, commands, configuration, prompt text, names or endpoint URLs.
        if not re.fullmatch(r'[a-z_]{1,48}', stage):
            raise ValueError('Invalid review diagnostic stage')
        record = {'id': self.correlation, 'stage': stage, 'time': round(time.time(), 3),
                  'elapsed_ms': round((time.monotonic() - self.started) * 1000), 'pid': os.getpid(), 'parent_pid': os.getppid()}
        if error is not None:
            record['error_type'] = type(error).__name__[:64]
        if options.get('failure'):
            for key in ('task', 'operation', 'expected_type', 'actual_type'):
                value = options.get(key)
                if isinstance(value, str) and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}', value):
                    record[key] = value
            resource = options.get('resource')
            if resource is not None:
                record['resource_id'] = hashlib.sha256(os.fsencode(resource)).hexdigest()[:16]
            if error is not None:
                include_frames = stage != 'llm_test_failed'
                record.update(get_exception_evidence(error, include_frames=include_frames))
                cause = error if isinstance(error, BaseException) else None
                seen = {id(error)}
                causes = []
                for _index in range(4):
                    if cause is None:
                        break
                    cause = cause.__cause__ or (cause.__context__ if not cause.__suppress_context__ else None)
                    if cause is None or id(cause) in seen:
                        break
                    seen.add(id(cause))
                    causes.append(get_exception_evidence(cause, include_frames=include_frames))
                if causes:
                    record['causes'] = causes
        for key in ('entries', 'batch', 'context_window', 'dedupe', 'force', 'exit_code', 'manual', 'child_pid', 'status_code'):
            if key in options and type(options[key]) in (int, bool):
                record[key] = options[key]
        line = json.dumps(record, ensure_ascii=True) + '\n'
        if options.get('failure') and not options.get('mirror'):
            emit_diagnostic_log('AlexandriaDiagnostics', options.get('level', logging.ERROR), '%s', line.rstrip())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(str(self.path), timeout=0.25):
                if self.path.exists() and self.path.stat().st_size + len(line) > MAX_BYTES:
                    os.replace(self.path, str(self.path) + '.previous')
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(line)
        except (OSError, TimeoutError) as persistence_error:
            emit_diagnostic_log(__name__, logging.WARNING, 'Diagnostic trace could not be persisted (%s; %s)',
                                stage, type(persistence_error).__name__)
            if options.get('raise_persistence_error'):
                raise
            return False
        return True

    def call(self, stage, callback, *args, **kwargs):
        token = CURRENT.set(self)
        self.event(stage + '_received')
        try:
            result = callback(*args, **kwargs)
        except BaseException as error:
            self.event(stage + '_failed', error=error)
            raise
        else:
            self.event(stage + '_returned')
            return result
        finally:
            CURRENT.reset(token)


def event(stage, **options):
    trace = CURRENT.get()
    if trace is not None:
        trace.event(stage, **options)


def worker_environment(env):
    """Copy the environment a child script runs with.

    The parent reads the child's stdout pipe as WORKER_STDIO_ENCODING, so the
    child is told to write it in that encoding rather than inheriting the
    console code page (cp1252 on native Windows; #2062). An explicit value in
    `env` is overridden on purpose — agreeing with the reader is the point.
    """
    trace = CURRENT.get()
    result = dict(os.environ if env is None else env)
    result['PYTHONIOENCODING'] = WORKER_STDIO_ENCODING
    if trace is not None:
        result.update({ENV_ID: trace.correlation, ENV_PATH: str(trace.path)})
    return result


def read_trace(path):
    path = Path(path)
    try:
        with file_lock(str(path), timeout=0.25):
            retained = []
            for candidate in (Path(str(path) + '.previous'), path):
                try:
                    with candidate.open('rb') as stream:
                        stream.seek(0, os.SEEK_END)
                        stream.seek(max(0, stream.tell() - MAX_BYTES))
                        retained.append(stream.read(MAX_BYTES))
                except FileNotFoundError:
                    continue
            return b''.join(retained)
    except FileNotFoundError:
        return b''
    except (OSError, TimeoutError) as error:
        return ('# Review startup trace could not be read (' + type(error).__name__
                + '). Retry the log download.\n').encode('utf-8')


def bind_current(callback):
    """Carry one trace explicitly into executor threads (ContextVars do not)."""
    from functools import partial
    trace = CURRENT.get()
    return partial(trace.call, 'batch', callback) if trace else callback


def get_failure_log_path():
    from utils import get_runtime_data_dir
    return Path(get_runtime_data_dir(str(Path(__file__).resolve().parent.parent)),
                'logs', 'api', 'diagnostics-latest.log')


def get_exception_evidence(error, *, include_frames=True):
    if error is None:
        return {}
    if not isinstance(error, BaseException):
        return {'error_type': type(error).__name__[:64]}
    from collections import deque
    evidence = {'error_type': type(error).__name__[:64]}
    if include_frames:
        frames = deque(({'file': Path(frame.f_code.co_filename).name[:128],
                         'function': frame.f_code.co_name[:64], 'line': line_number}
                        for frame, line_number in traceback.walk_tb(error.__traceback__)), maxlen=12)
        evidence['traceback'] = list(frames)
    for key in ('errno', 'winerror', 'status_code'):
        value = getattr(error, key, None)
        if type(value) is int:
            evidence[key] = value
    return evidence


def record_failure(stage, error=None, *, task, resource=None, level=logging.ERROR,
                   rate_limit_seconds=0, mirror_path=None, **options):
    """Best-effort diagnostics must never replace a caller's failure or result."""
    try:
        return _persist_failure(stage, error, task=task, resource=resource, level=level,
                               rate_limit_seconds=rate_limit_seconds, mirror_path=mirror_path, **options)
    except Exception as sink_error:
        report_diagnostic_sink_failure(sink_error)
        return False


def _persist_failure(stage, error=None, *, task, resource=None, level=logging.ERROR,
                     rate_limit_seconds=0, mirror_path=None, **options):
    if rate_limit_seconds:
        now = time.monotonic()
        resource_id = hashlib.sha256(os.fsencode(resource)).hexdigest() if resource is not None else None
        key = (stage, task, type(error).__name__ if error is not None else None, resource_id)
        with _failure_limit_lock:
            expired = [entry for entry, deadline in _failure_limits.items() if deadline <= now]
            for entry in expired:
                del _failure_limits[entry]
            if key in _failure_limits or len(_failure_limits) >= 512:
                return False
            _failure_limits[key] = now + rate_limit_seconds
    current = CURRENT.get()
    trace = ReviewStartupTrace(get_failure_log_path(), current.correlation if current else None)
    persisted = trace.event(stage, error=error, failure=True, task=task, resource=resource,
                            level=level, **options)
    if mirror_path is not None:
        persisted = ReviewStartupTrace(mirror_path, trace.correlation).event(
            stage, error=error, failure=True, task=task, resource=resource, level=level,
            raise_persistence_error=True, mirror=True, **options)
    return persisted
