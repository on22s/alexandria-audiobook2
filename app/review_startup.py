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
from utils import file_lock

CURRENT = ContextVar('review_startup_trace', default=None)
MAX_BYTES = 256 * 1024
ENV_ID = 'ALEXANDRIA_REVIEW_STARTUP_ID'
ENV_PATH = 'ALEXANDRIA_REVIEW_STARTUP_PATH'


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
                record.update(get_exception_evidence(error))
                cause = error
                seen = {id(error)}
                causes = []
                for _index in range(4):
                    cause = cause.__cause__ or (cause.__context__ if not cause.__suppress_context__ else None)
                    if cause is None or id(cause) in seen:
                        break
                    seen.add(id(cause))
                    causes.append(get_exception_evidence(cause))
                if causes:
                    record['causes'] = causes
        for key in ('entries', 'batch', 'context_window', 'dedupe', 'force', 'exit_code', 'manual', 'child_pid', 'status_code'):
            if key in options and type(options[key]) in (int, bool):
                record[key] = options[key]
        line = json.dumps(record, ensure_ascii=True) + '\n'
        if options.get('failure'):
            logging.getLogger('AlexandriaDiagnostics').log(options.get('level', logging.ERROR), '%s', line.rstrip())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(str(self.path), timeout=0.25):
                if self.path.exists() and self.path.stat().st_size + len(line) > MAX_BYTES:
                    os.replace(self.path, str(self.path) + '.previous')
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(line)
        except (OSError, TimeoutError) as persistence_error:
            logging.getLogger(__name__).warning('Diagnostic trace could not be persisted (%s; %s)',
                                                stage, type(persistence_error).__name__)

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
    trace = CURRENT.get()
    result = dict(os.environ if env is None else env)
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


def get_exception_evidence(error):
    from collections import deque
    frames = deque(({'file': Path(frame.f_code.co_filename).name[:128],
                     'function': frame.f_code.co_name[:64], 'line': line_number}
                    for frame, line_number in traceback.walk_tb(error.__traceback__)), maxlen=12)
    evidence = {'error_type': type(error).__name__[:64], 'traceback': list(frames)}
    for key in ('errno', 'winerror', 'status_code'):
        value = getattr(error, key, None)
        if type(value) is int:
            evidence[key] = value
    return evidence


def record_failure(stage, error=None, *, task, resource=None, level=logging.ERROR, **options):
    """Persist content-free evidence using the existing bounded trace writer."""
    current = CURRENT.get()
    trace = ReviewStartupTrace(get_failure_log_path(), current.correlation if current else None)
    trace.event(stage, error=error, failure=True, task=task, resource=resource,
                level=level, **options)
