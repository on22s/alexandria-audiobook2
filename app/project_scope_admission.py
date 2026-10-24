"""Portable task/book/file admission for explicit project-scope operations."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import os
from pathlib import Path
import threading
from book_state_transaction import ensure_book_state, is_book_state_admitted
from utils import file_lock

_admitted = ContextVar('project_scope_admission', default=())


def _get_owner(root):
    return os.path.normcase(os.path.realpath(root)), os.getpid(), threading.get_ident()


def is_scope_admitted(root):
    return is_book_state_admitted(root) and _get_owner(root) in _admitted.get()


def _require_configured_root(root):
    import core
    if _get_owner(root)[0] != _get_owner(core.DATA_DIR)[0]:
        raise ValueError('Source scope must use the configured project directory.')


@contextmanager
def _ensure_scope_files(root):
    with ExitStack() as locks:
        for name in ('annotated_script.json', 'voice_config.json', 'chunks.json'):
            locks.enter_context(file_lock(str(Path(root)/name), timeout=0))
        token = _admitted.set((*_admitted.get(), _get_owner(root)))
        try:
            yield
        finally:
            _admitted.reset(token)


@contextmanager
def ensure_scope_admission(root):
    """Exclude native/foreign tasks before taking the established book/file locks."""
    import core
    _require_configured_root(root)
    with core.ensure_book_switch_allowed(operation='change source scope', hold_task_mutex=False):
        with ensure_book_state(root, timeout=0, recover=False), _ensure_scope_files(root):
            yield


@contextmanager
def ensure_audio_scope_admission(root):
    """Capture inside an existing live audio worker, without admitting new work.

    Keep the book/file guards through capture, but never hold the task mutex
    across I/O. The worker still owns its original task lease and cancellation.
    Pending book transactions are refused rather than recovered by a read.
    """
    from audio_generation_owner import (AudioGenerationOwner,
        get_audio_generation_owner, ensure_audio_generation_active)
    _require_configured_root(root)
    if type(get_audio_generation_owner()) is not AudioGenerationOwner:
        raise ValueError('Sound capture requires an existing audio task owner')
    ensure_audio_generation_active()
    with ensure_book_state(root, timeout=0, recover=False), _ensure_scope_files(root):
        ensure_audio_generation_active()
        yield
        ensure_audio_generation_active()
