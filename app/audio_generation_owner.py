"""Exact-owner admission for Editor synthesis; direct Python callers stay unowned."""
from contextlib import contextmanager
from contextvars import ContextVar

AUDIO_GENERATION_CANCELLED = "Audio generation cancelled"
_owner = ContextVar('audio_generation_owner', default=None)


class AudioGenerationCancelled(RuntimeError):
    def __init__(self):
        super().__init__(AUDIO_GENERATION_CANCELLED)


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
