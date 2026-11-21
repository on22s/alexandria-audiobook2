"""One admitted, book-fenced path to the shared character alias registry.

The native alias editor and an applied speaker-label review both write the same
registry. They share task admission and the protected-merge write policy so a
snapshot captured under one alias map cannot be published under another, and
UNKNOWN can never be folded into a named speaker by a registry write.
"""
import contextlib
from pathlib import Path

from fastapi import HTTPException

import core
from book_state_transaction import ensure_book_state
from utils import file_lock

ALIAS_TASK = 'speaker_review'


@contextlib.contextmanager
def alias_task_lease(root):
    """Reserve the applied-alias slot, excluding running model tasks.

    Task/GPU admission comes before book and file locks, matching generation
    and applied-review order: a lease holder never waits on locks that a task
    claim takes in the opposite order. Fail closed with 409 while tasks run.
    """
    with core._gpu_lock:
        busy = [name for name in core.LLM_TASKS if core.is_task_running(name)]
        if busy:
            raise HTTPException(status_code=409, detail='Wait for model tasks to finish before changing aliases')
        try:
            lease = core.acquire_task_lease(root, ALIAS_TASK, core.LLM_TASKS)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail='Another app worker owns a model task; wait before changing aliases') from exc
    with contextlib.closing(lease):
        yield


@contextlib.contextmanager
def alias_registry_admission(root):
    """The full alias-editor admission: task lease plus book/file fences.

    Book state and the script/voice/chunk file locks match the applied-review
    path. Startup-recovery's idle scan is deliberately not repeated here: this
    path already holds its own lease, so that check would refuse its own claim.
    """
    with alias_task_lease(root):
        try:
            with ensure_book_state(root, timeout=0, recover=False), contextlib.ExitStack() as locks:
                for name in ('annotated_script.json', 'voice_config.json', 'chunks.json'):
                    locks.enter_context(file_lock(str(Path(root) / name), timeout=0))
                yield
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail='The book or alias registry is busy; retry shortly.') from exc
