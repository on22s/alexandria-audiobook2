"""Bounded, content-free review lifecycle trace shared by API and CLI workers."""
from contextvars import ContextVar
from pathlib import Path
import json
import logging
import os
import re
import time
import uuid
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
        for key in ('entries', 'batch', 'context_window', 'dedupe', 'force', 'exit_code', 'manual', 'child_pid'):
            if key in options and type(options[key]) in (int, bool):
                record[key] = options[key]
        line = json.dumps(record, ensure_ascii=True) + '\n'
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(str(self.path), timeout=0.25):
                if self.path.exists() and self.path.stat().st_size + len(line) > MAX_BYTES:
                    os.replace(self.path, str(self.path) + '.previous')
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(line)
        except (OSError, TimeoutError):
            logging.getLogger(__name__).warning('Review startup diagnostic could not be persisted (%s)', stage)

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
