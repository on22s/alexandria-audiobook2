"""Optional verified window reuse; Project owns final take publication."""
from contextlib import contextmanager
from dataclasses import asdict
import logging
from pathlib import Path

from synthesis_recovery_cache import (RecoveryCacheStorage, RecoveryCacheUnavailable,
                                      is_recovery_storage_platform_supported)
from synthesis_recovery_context import get_synthesis_recovery_parent
from synthesis_recovery_identity import RecoveryIdentityUnavailable, get_recovery_digest
from synthesis_recovery_native import ensure_native_recovery_model_capture
from synthesis_recovery_serving import (capture_native_custom_serving_identity,
    ensure_native_recovery_serving, ensure_native_recovery_serving_current, _capture_engine_methods)
from synthesis_input import (is_speech_input_capture_active, get_captured_speech_input,
                              capture_speech_input)

_CACHE_ERRORS = (RecoveryCacheUnavailable, OSError, TimeoutError)


class RecoverySession:
    def __init__(self, reason, *, engine=None, request=None, parent=None, identity=None, key=None, budget=0,
                 input_plan=None):
        self.reason = reason
        self.engine, self.request, self.parent = engine, request, parent
        self.identity, self.key, self.budget = identity, key, budget
        self.storage = None
        self.input_plan = input_plan
        self.reused, self.saved = [], []

    def close(self):
        storage, self.storage = self.storage, None
        if storage is not None:
            storage.close()

    def disable_cache(self):
        self.reason = 'cache_unavailable'
        self.close()

    def check(self):
        if self.identity is None:
            return
        with self.parent.admit():
            pass
        ensure_native_recovery_serving_current(self.engine, self.request)

    def capture(self, index, path):
        if self.storage is None:
            return None
        try:
            return self.storage.capture_window(self.key, self.parent.uid_sha256, index, path)
        except _CACHE_ERRORS:
            self.disable_cache()
            Path(path).unlink(missing_ok=True)
            return None

    def accept(self, index):
        # The private copy has passed full audio/hash/shape validation. Renew
        # authority and serving dependencies after releasing the cache lock.
        self.check()
        self.reused.append(index)

    def reject(self, path):
        Path(path).unlink(missing_ok=True)
        self.disable_cache()
        self.check()

    def save(self, index, path, info, sha256):
        if self.storage is None:
            return
        if self.input_plan is not None:
            proof = get_captured_speech_input(path)
            if (proof is None or proof['text'] != self.input_plan.windows[index].text or
                    proof['language'] != self.engine._language or proof['audio']['sha256'] != sha256):
                self.reason = 'unproven_window_input'
                self.close()
                return
        try:
            saved = self.storage.save_window(self.key, self.parent.uid_sha256, index,
                self.parent.claim_sha256, path, dict(size=Path(path).stat().st_size,
                sha256=sha256, frames=info.frames, rate=info.samplerate, channels=info.channels),
                budget_bytes=self.budget)
        except _CACHE_ERRORS:
            self.disable_cache()
        else:
            if saved:
                self.saved.append(index)
            else:
                self.reason = 'capacity_reached'

    def restore_input(self, index, path):
        # input_plan exists only for the versioned request-key contract whose
        # saver required observed leaf input bound to the exact cached PCM.
        if self.input_plan is not None:
            self.check()
            return capture_speech_input(path, self.input_plan.windows[index].text,
                self.engine._language, 'recovered_qualified_api_input_v1')
        return None

    def get_receipt(self):
        receipt = dict(version=1, status=self.reason, reused_windows=list(self.reused),
                       saved_windows=list(self.saved))
        if self.identity is not None:
            receipt.update(request_sha256=self.key, serving_sha256=self.identity.sha256,
                           parent=self.parent.get_semantic_identity())
        if self.input_plan is not None:
            receipt['input_proof_version'] = 1
        return receipt


@contextmanager
def ensure_synthesis_recovery(engine, category, plan, settings, request=None):
    """Eligibility failure is optional only before a serving identity is bound."""
    session = None
    # Off means no code/model/cache inspection and no recovery receipt changes.
    if not getattr(engine, '_synthesis_recovery', None):
        yield None
        return
    reason = None
    parent = get_synthesis_recovery_parent()
    if not is_recovery_storage_platform_supported():
        reason = 'unsupported_durability'
    elif parent is None:
        reason = 'unowned_parent'
    elif category != 'custom' or request is None:
        reason = 'unsupported_voice_category'
    if reason is not None:
        yield RecoverySession(reason)
        return
    try:
        # Refuse public/internal replacements before model initialization.
        _capture_engine_methods(engine, {}, [])
        policy = engine.get_synthesis_recovery_policy()
        with ensure_native_recovery_model_capture():
            identity = capture_native_custom_serving_identity(engine, request)
    except RecoveryIdentityUnavailable:
        yield RecoverySession('unproven_dependencies')
        return
    descriptor = dict(version=1, parent=parent.get_semantic_identity(),
        plan=asdict(plan), policy=settings, serving_sha256=identity.sha256)
    input_plan = plan if is_speech_input_capture_active() else None
    if input_plan is not None:
        descriptor['input_proof_version'] = 1
    key = get_recovery_digest(descriptor)
    session = RecoverySession('eligible', engine=engine, request=request, parent=parent,
        identity=identity, key=key, budget=policy['max_audio_mb'] * 1024 * 1024, input_plan=input_plan)
    try:
        with ensure_native_recovery_serving(engine, identity):
            session.check()
            storage = RecoveryCacheStorage(parent.project_root, parent.admit)
            try:
                storage.__enter__()
                session.storage = storage
                storage.prepare_request(key, parent.uid_sha256, parent.claim_sha256,
                                        budget_bytes=session.budget)
            except _CACHE_ERRORS:
                storage.close()
                session.disable_cache()
            yield session
    finally:
        session.close()


def retire_committed_synthesis_recovery(parent, receipt):
    """Best effort after metadata commit; cleanup never reverses publication."""
    recovery = receipt.get('recovery') if isinstance(receipt, dict) else None
    if (parent is None or not isinstance(recovery, dict)
            or recovery.get('parent') != parent.get_semantic_identity()
            or not isinstance(recovery.get('request_sha256'), str)):
        return
    try:
        with RecoveryCacheStorage(parent.project_root, parent.admit) as storage:
            storage.retire_request(recovery['request_sha256'])
    except Exception:
        logging.getLogger(__name__).debug('Committed synthesis cache cleanup deferred', exc_info=True)


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
