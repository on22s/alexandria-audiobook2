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


class ExportCancelled(Exception):
    """Raised when the caller cancels audio assembly or export."""


@dataclass(frozen=True)
class AudioGenerationOwner:
    """Captured live task admission plus stable, non-authorizing claim metadata.

    Thread workers share this immutable wrapper. Only the live callback grants
    authority; a persisted claim ID never substitutes for calling it.

    ``validate`` is an optional plain callback for commit-time claim/cancel
    checks. It must never reenter ``admit``: the owner mutex is non-reentrant
    by contract and a publication lease may already be held. Owners without it
    (direct callers and fixtures) simply skip the commit re-check.
    """
    claim_id: str
    admit: object
    validate: object = None
    validate_claim: object = None

    def __post_init__(self):
        if (not isinstance(self.claim_id, str)
                or re.fullmatch(r'[0-9a-f]{32}', self.claim_id) is None
                or not callable(self.admit)
                or (self.validate is not None and not callable(self.validate))
                or (self.validate_claim is not None and not callable(self.validate_claim))):
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


def ensure_audio_generation_commit_active():
    """Re-check the live claim/cancel state at the final publication commit.

    Uses the production owner's optional plain ``validate`` callback rather
    than reentering ``admit``: the owner mutex is non-reentrant and a
    publication lease may already be held, including with the book or Editor
    lock held. Owners without ``validate`` (direct callers and fixtures) are
    unchanged and skip this check.

    Production callers place this after output admission and fsync, directly
    before the rename, so cancellation arriving during blocking precommit I/O
    is observed. Residual race: the check and the rename are not one atomic
    operation, so a cancel landing in that final gap can still publish; no
    broader guarantee is claimed.
    """
    owner = _owner.get()
    if owner is None:
        return
    validate = getattr(owner, 'validate', None)
    if validate is not None:
        validate()


def ensure_audio_generation_claim_current():
    """Revalidate claim identity at a cleanup settlement boundary.

    Cancellation is expected during cleanup, so this path checks only the
    exact claim id and phase. It uses the same coherent snapshot as the
    commit validator and never reenters owner admission.
    """
    owner = _owner.get()
    if owner is None:
        return
    validate = getattr(owner, 'validate_claim', None)
    if validate is not None:
        validate()


# Backstop only: a user cancel, claim replacement or book switch ends an
# owned wait first. Observed publication holds were 56-65 s for 100 files
# under load (review of #2163), so this leaves an order of magnitude.
_OWNED_BOOK_WAIT_SECONDS = 600.0
_BOOK_WAIT_RETRY_SECONDS = .01
_BOOK_CHANGED_DETAIL = 'Book or Editor selection changed while waiting to publish audio.'


def _get_book_identity(root, attempts=1):
    """The selected book generation, or None when it cannot be read right now.

    A waiter reads without the book lock, so an ordinary atomic save can replace
    state.json mid-read. That is not evidence of a book switch: the caller keeps
    waiting and compares only identities it really read.
    """
    from book_state_transaction import get_book_operation_identity
    for attempt in range(attempts):
        try:
            return get_book_operation_identity(root)
        except (OSError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(_BOOK_WAIT_RETRY_SECONDS)
    return None


@contextmanager
def ensure_audio_book_admission(root, *, timeout=10, recover=True, cancel_check=None,
                                hold_publication=False, reuse=False, timeout_message=None):
    """Admit the book under one audio-owner wait policy.

    Owned publishers used to queue on the task mutex, unbounded, before trying
    the book. With that mutex released, the queueing moved to the book try-lock,
    so a chapter publication longer than the old bound would discard an already
    finished export. An owned waiter therefore retries until admitted, until its
    exact owner stops or is replaced (re-checked on every retry), until the
    selected book generation changes, or until the owned backstop expires.
    Unowned callers keep the historical single bounded admission.

    No lock is held while waiting. ``cancel_check`` is an independent caller
    predicate (for example an export's request event) and is polled without a
    lock. ``hold_publication`` keeps the exact-owner lease for the body
    (publishers); otherwise the owner is only re-checked before each attempt.
    ``reuse`` accepts this thread's live book scope instead of taking it again.
    ``timeout_message`` replaces the raw lock timeout only for the admission
    attempt, never for a TimeoutError the caller's body raises.
    """
    from book_state_transaction import ensure_book_state, reuse_book_state

    def admit_book(wait):
        if reuse:
            return reuse_book_state(root, timeout=wait)
        return ensure_book_state(root, timeout=wait, recover=recover)

    if _owner.get() is None:
        with ExitStack() as stack:
            try:
                admitted = stack.enter_context(admit_book(timeout))
            except TimeoutError as error:
                if timeout_message is None:
                    raise
                raise TimeoutError(timeout_message) from error
            yield admitted
        return
    deadline = time.monotonic() + max(timeout, _OWNED_BOOK_WAIT_SECONDS)
    book = _get_book_identity(root, attempts=10)

    def ensure_same_book(attempts=1):
        current = _get_book_identity(root, attempts)
        if book is not None and current is not None and current != book:
            raise ValueError(_BOOK_CHANGED_DETAIL)

    while True:
        if cancel_check is not None and cancel_check():
            raise AudioGenerationCancelled()
        with ExitStack() as stack:
            if hold_publication:
                stack.enter_context(ensure_audio_generation_publication())
            else:
                ensure_audio_generation_active()
            try:
                stack.enter_context(admit_book(0))
            except TimeoutError:
                pass
            else:
                ensure_same_book(attempts=10)
                yield
                return
        ensure_same_book()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Could not admit audio publication to the active book.')
        time.sleep(min(_BOOK_WAIT_RETRY_SECONDS, remaining))


@contextmanager
def ensure_audio_generation_book(root, *, timeout=10, cancel_check=None):
    """Admit owner and book without waiting for a book while holding the owner.

    Script admission takes the book before its task mutex. A failed try-lock
    must release our owner mutex before retrying so that admission can finish.
    Owned publishers may outwait a long book publication; see
    ``ensure_audio_book_admission``.
    """
    with ensure_audio_book_admission(
            root, timeout=timeout, cancel_check=cancel_check, hold_publication=True,
            timeout_message='Could not admit audio publication to the active book.'):
        yield


@contextmanager
def reuse_audio_generation_book(root, *, timeout=10, cancel_check=None):
    """Explicit parent publication inside a retention-owned book scope."""
    from book_state_transaction import is_book_state_admitted, reuse_book_state
    if is_book_state_admitted(root):
        with ensure_audio_generation_publication(), reuse_book_state(root):
            yield
    else:
        with ensure_audio_generation_book(root, timeout=timeout, cancel_check=cancel_check):
            yield


@contextmanager
def ensure_audio_generation_book_work(root, *, timeout=10, cancel_check=None):
    """Keep book identity stable for cancellable work, without an owner mutex.

    Renew exact owner admission before parent publication. Contending publishers
    use a book try-lock so a book holder can poll cancellation without deadlock.
    """
    with ensure_audio_book_admission(root, timeout=timeout, reuse=True,
                                     cancel_check=cancel_check):
        ensure_audio_generation_active()
        yield


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
