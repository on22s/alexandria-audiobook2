"""Exact-owner admission for Editor synthesis; direct Python callers stay unowned."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import re
import time
import threading

AUDIO_GENERATION_CANCELLED = "Audio generation cancelled"
_owner = ContextVar('audio_generation_owner', default=None)
_held_publication = ContextVar('audio_generation_held_publication', default=None)


class AudioGenerationCancelled(RuntimeError):
    def __init__(self,message=AUDIO_GENERATION_CANCELLED):
        super().__init__(message)


@dataclass(frozen=True)
class AudioGenerationOwner:
    """Captured live task admission plus stable, non-authorizing claim metadata.

    Thread workers share this immutable wrapper. Only the live callback grants
    authority; a persisted claim ID never substitutes for calling it.
    """
    claim_id: str
    admit: object

    def __post_init__(self):
        if (not isinstance(self.claim_id, str)
                or re.fullmatch(r'[0-9a-f]{32}', self.claim_id) is None
                or not callable(self.admit)):
            raise ValueError('Audio owner needs a live admission and task claim')

    def __call__(self, *, allow_cancelled=False):
        return self.admit(allow_cancelled=allow_cancelled)


def get_audio_generation_claim_id():
    """Metadata only; unknown callable owners remain valid but unqualified."""
    owner = _owner.get()
    return owner.claim_id if type(owner) is AudioGenerationOwner else None


def get_audio_generation_owner():
    return _owner.get()


@contextmanager
def ensure_audio_generation_owner(owner):
    token = _owner.set(owner)
    try:
        yield
    finally:
        _owner.reset(token)


@dataclass
class _HeldPublication:
    owner: object
    thread: int
    allow_cancelled: bool
    active: bool = True


@contextmanager
def ensure_audio_generation_publication(*, allow_cancelled=False):
    """Acquire exact-owner admission; nested queries reuse only a live local lease.

    A short publication transaction may check a generic cancellation predicate
    which queries this same owner. Never reenter its nonreentrant task mutex.
    Copied contexts cannot reuse an expired lease or a different thread's lease.
    """
    owner = _owner.get()
    if owner is None:
        yield
        return
    held = _held_publication.get()
    if (held is not None and held.active and held.owner is owner
            and held.thread == threading.get_ident()):
        if held.allow_cancelled and not allow_cancelled:
            raise AudioGenerationCancelled()
        yield
        return
    with owner(allow_cancelled=allow_cancelled):
        held = _HeldPublication(owner, threading.get_ident(), allow_cancelled)
        token = _held_publication.set(held)
        try:
            yield
        finally:
            held.active = False
            _held_publication.reset(token)


def ensure_audio_generation_active():
    with ensure_audio_generation_publication():
        pass


@contextmanager
def ensure_audio_generation_book(root, *, timeout=10):
    """Admit owner and book without waiting for a book while holding the owner.

    Script admission takes the book before its task mutex. A failed try-lock
    must release our owner mutex before retrying so that admission can finish.
    """
    from book_state_transaction import ensure_book_state
    deadline = time.monotonic() + timeout
    while True:
        with ExitStack() as stack:
            stack.enter_context(ensure_audio_generation_publication())
            try:
                stack.enter_context(ensure_book_state(root, timeout=0))
            except TimeoutError:
                pass
            else:
                yield
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Could not admit audio publication to the active book.')
        time.sleep(min(.01, remaining))


def is_audio_generation_cancelled():
    try:
        ensure_audio_generation_active()
        return False
    except AudioGenerationCancelled:
        return True


def run_with_audio_generation_owner(owner, callback, *args):
    """Thread pools inherit only this admission, not unrelated speech contexts."""
    with ensure_audio_generation_owner(owner):
        return callback(*args)


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
