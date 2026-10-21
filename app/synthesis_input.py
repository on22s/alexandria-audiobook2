"""Call-local exact submitted speech, attached only to matching completed artifacts."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import os

MAX_ARTIFACTS = 2048
MAX_TEXT = 20000
_inputs = ContextVar('completed_synthesis_api_inputs', default=None)


class _SpeechInputs:
    def __init__(self):
        self.records = {}
        self.overflow = False

    def save(self, path, record):
        key = os.path.abspath(path)
        if self.overflow:
            return
        if key not in self.records and len(self.records) == MAX_ARTIFACTS:
            self.records.clear()
            self.overflow = True
            return
        self.records[key] = copy.deepcopy(record)


@contextmanager
def ensure_speech_input_capture(enabled=True):
    """Each scope is isolated, including workers receiving copy_context().

    Returning an immutable copy from the worker then promoting the accepted
    artifact prevents late futures from mutating the caller's captured inputs.
    Disabled scopes never inspect a provider, model or output artifact.
    """
    collector = _SpeechInputs() if enabled else None
    token = _inputs.set(collector)
    try:
        yield collector
    finally:
        _inputs.reset(token)


def is_speech_input_capture_active():
    return _inputs.get() is not None


def invalidate_speech_input(path):
    collector = _inputs.get()
    if collector is not None:
        collector.save(path, None)


def _get_artifact(path):
    from speech_verification_runtime import get_verifier_file_identity
    from word_timing import MAX_AUDIO_BYTES
    row = get_verifier_file_identity(path, MAX_AUDIO_BYTES, lambda: False)
    return {'sha256': row['sha256'], 'size': row['size']}


def capture_speech_input(path, text, language, provenance):
    """Known leaf calls this after it saves audio for the exact API text argument.

    Optional metadata failure never turns a successful legacy render into an
    audio failure; absence means unproven input, not reconstructed expectation.
    """
    collector = _inputs.get()
    if collector is None or collector.overflow:
        return None
    record = None
    if (isinstance(text, str) and 0 < len(text) <= MAX_TEXT and
            not any(0xD800 <= ord(char) <= 0xDFFF for char in text) and
            isinstance(language, str) and 0 < len(language) <= 64 and
            isinstance(provenance, str) and 0 < len(provenance) <= 128):
        try:
            record = dict(version=1, text=text, language=language, provenance=provenance,
                          audio=_get_artifact(path))
        except (OSError, ValueError):
            pass
    collector.save(path, record)
    return copy.deepcopy(record)


def get_captured_speech_input(path):
    collector = _inputs.get()
    if collector is None or collector.overflow:
        return None
    record = collector.records.get(os.path.abspath(path))
    if record is None:
        return None
    try:
        return copy.deepcopy(record) if _get_artifact(path) == record['audio'] else None
    except (OSError, ValueError):
        return None


def promote_speech_input(record, output_path):
    """Accept a completed byte-preserving move/copy from an isolated child scope."""
    collector = _inputs.get()
    invalidate_speech_input(output_path)
    if collector is None or not isinstance(record, dict) or record.get('version') != 1:
        return None
    try:
        if _get_artifact(output_path) != record['audio']:
            return None
        collector.save(output_path, record)
        return copy.deepcopy(record)
    except (KeyError, OSError, ValueError):
        return None


def capture_composite_speech_input(children, output_path, text, kind):
    """Known composition promotes complete exact child requests, never guesses.

    Windows concatenate exact submitted spans. Ensemble members share one
    script simultaneously and therefore must never multiply expected speech.
    The caller supplies existing child artifact paths before their cleanup.
    """
    invalidate_speech_input(output_path)
    if not is_speech_input_capture_active() or kind not in ('windows', 'ensemble'):
        return None
    if not isinstance(children, (list, tuple)) or not 1 <= len(children) <= MAX_ARTIFACTS:
        return None
    records = [get_captured_speech_input(path) for path in children]
    if any(row is None for row in records):
        return None
    language = records[0]['language']
    if any(row['language'] != language for row in records):
        return None
    expected = ''.join(row['text'] for row in records) if kind == 'windows' else records[0]['text']
    if expected != text or kind == 'ensemble' and any(row['text'] != text for row in records):
        return None
    return capture_speech_input(output_path, text, language, 'composed_' + kind + '_api_inputs')


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
