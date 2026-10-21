"""Per-parent recovery metadata; live Project admission remains authoritative."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import re

from audio_generation_owner import get_audio_generation_claim_id

_parent = ContextVar('synthesis_recovery_parent', default=None)
_batch_parents = ContextVar('synthesis_recovery_batch_parents', default=None)


class RecoveryAdmissionError(RuntimeError):
    """Book authority could not be read; this is never an optional cache miss."""


def get_recovery_claim_digest():
    claim = get_audio_generation_claim_id()
    return hashlib.sha256(claim.encode('ascii')).hexdigest() if claim is not None else None


@dataclass(frozen=True)
class RecoveryParent:
    project_root: str
    uid_sha256: str
    book_sha256: str
    inputs_sha256: str
    claim_sha256: str
    admit: object

    def __post_init__(self):
        if (not isinstance(self.project_root, str) or not self.project_root or not callable(self.admit)
                or any(not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None
                       for value in (self.uid_sha256, self.book_sha256,
                                     self.inputs_sha256, self.claim_sha256))):
            raise ValueError('Recovery parent requires captured digests and live admission')

    def get_semantic_identity(self):
        # Claim epochs authorize IO, but a restarted claim must match the same
        # semantic request. Absolute project paths are not persisted identity.
        return dict(uid_sha256=self.uid_sha256, book_sha256=self.book_sha256,
                    inputs_sha256=self.inputs_sha256)


def get_synthesis_recovery_parent():
    parent = _parent.get()
    if type(parent) is RecoveryParent and get_recovery_claim_digest() == parent.claim_sha256:
        return parent
    return None


@contextmanager
def ensure_synthesis_recovery_parent(parent):
    if parent is not None and type(parent) is not RecoveryParent:
        raise ValueError('Invalid recovery parent')
    token = _parent.set(parent)
    try:
        yield
    finally:
        _parent.reset(token)


@contextmanager
def ensure_synthesis_recovery_batch(parents):
    if (not isinstance(parents, dict) or any(not isinstance(uid, str) or type(parent) is not RecoveryParent
                                            for uid, parent in parents.items())):
        raise ValueError('Invalid recovery parent map')
    token = _batch_parents.set(dict(parents))
    try:
        yield
    finally:
        _batch_parents.reset(token)


@contextmanager
def ensure_synthesis_recovery_batch_parent(uid):
    parents = _batch_parents.get()
    parent = parents.get(uid) if parents is not None and isinstance(uid, str) else None
    # Missing UID must clear an outer parent, never accidentally reuse its cache.
    with ensure_synthesis_recovery_parent(parent):
        yield


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
