"""Exact-owner admission for Editor synthesis; direct Python callers stay unowned."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re

AUDIO_GENERATION_CANCELLED = "Audio generation cancelled"
_owner = ContextVar('audio_generation_owner', default=None)


class AudioGenerationCancelled(RuntimeError):
    def __init__(self):
        super().__init__(AUDIO_GENERATION_CANCELLED)


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


@contextmanager
def ensure_audio_generation_publication(*, allow_cancelled=False):
    """Acquire owner admission before chunk locks; never wrap inference/conversion."""
    owner = _owner.get()
    if owner is None:
        yield
    else:
        with owner(allow_cancelled=allow_cancelled):
            yield


def ensure_audio_generation_active():
    with ensure_audio_generation_publication():
        pass


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
