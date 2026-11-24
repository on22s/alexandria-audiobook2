"""Bind an owned, proven native render to its selected encoded artifact.

This records evidence for future explicit restoration; it never selects audio.
Unsupported/older renders remain unproven. Browsing does not inspect a model.

The proof binds the active native serving fingerprint, which includes the loaded
AA2 module code. A release that changes that code yields a different fingerprint,
so older receipts become audition-only instead of restorable; this is not a
promise of restoration across releases. The supported schemas apply only while
every proven dependency still matches.
"""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re

from audio_take_history import (MAX_AUDIO_BYTES, TakeAudioOutsideSnapshotBounds, _decode, _encode,
    _ensure_metadata_depth, _ensure_plain_path, _hash, _open_regular, _version)

KEY = 'take_publication'
NATIVE_TAKE_KINDS = {'native_custom_windows_v1': 'custom',
                     'native_clone_windows_v1': 'clone',
                     'native_lora_windows_v1': 'lora'}
MAX_RECEIPT_BYTES = 4096
_DIGEST = re.compile(r'[a-f0-9]{64}')
_REVISION = re.compile(r'[a-f0-9]{32}')
_DEPENDENCIES = frozenset(('inputs_sha256', 'voice_sha256', 'model_sha256',
    'pronunciation_sha256', 'recipe_sha256', 'serving_sha256'))
_INVALID_EVIDENCE = 'Selected take publication evidence needs inspection; existing audio was preserved.'
_UNSUPPORTED_VERSION = 'This saved take uses an unsupported receipt version; it remains available for audition.'
_V1_KEYS = frozenset(('version', 'kind', 'book_token', 'uid', 'audio_revision', 'index',
    'dependencies', 'speech_sha256', 'pcm', 'audio', 'synthesis_sha256'))
_RECEIPT_SCHEMAS = {1: _V1_KEYS, 2: _V1_KEYS | frozenset(('seed',)),
                    3: _V1_KEYS | frozenset(('seed',))}
_MAX_RECEIPT_VERSION = max(_RECEIPT_SCHEMAS)


def get_take_voice_identity(selected, seed=None, *, canonical=True):
    """Render-affecting voice fields bound by a version-3 take receipt."""
    from tts import get_voice_render_identity
    identity = get_voice_render_identity(selected, canonical=canonical)
    if seed is not None:
        identity['seed'] = seed
    return identity


def get_legacy_v3_voice_digest(selected, seed=None):
    """The pre-#2170 v3 digest, which included an inert description.

    Restoration accepts it only when it is exactly this entry's raw projection,
    so a main-era take still authorizes while its description is unchanged; a
    later edit makes it differ and the take stays audition-only (fail closed).
    """
    from synthesis_recovery_identity import get_recovery_digest
    return get_recovery_digest(get_take_voice_identity(selected, seed, canonical=False))


def get_receipt_voice_digest(version, selected, seed=None):
    """Version 1/2 keep the full-config digest; version 3 uses the projection."""
    from synthesis_recovery_identity import get_recovery_digest
    if version >= 3:
        return get_recovery_digest(get_take_voice_identity(selected, seed))
    if seed is None:
        return get_recovery_digest(selected)
    return get_recovery_digest({**selected, 'seed': seed})


def _is_digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _is_seed(value):
    return type(value) is int and 0 <= value < 2**64


def _is_audio(value):
    return (type(value) is dict and set(value) == {'sha256','size','frames','sample_rate','channels'}
        and _is_digest(value['sha256']) and type(value['size']) is int
        and 0 < value['size'] <= MAX_AUDIO_BYTES and type(value['sample_rate']) is int
        and 0 < value['sample_rate'] <= 192000 and type(value['frames']) is int
        and 0 < value['frames'] <= value['sample_rate'] * 600
        and type(value['channels']) is int and value['channels'] in (1, 2))


def validate_take_publication(row):
    """Admit finite bounded saved evidence before any row encoding/copying.

    Each supported version has an exact schema; older receipts keep their
    original meaning and a newer declaration is refused without interpretation.
    """
    receipt = row.get(KEY)
    if receipt is None:
        return None
    _ensure_metadata_depth(receipt)
    if type(receipt) is not dict or type(receipt.get('version')) is not int:
        raise ValueError(_INVALID_EVIDENCE)
    version = receipt['version']
    if version > _MAX_RECEIPT_VERSION:
        raise ValueError(_UNSUPPORTED_VERSION)
    if (version not in _RECEIPT_SCHEMAS or set(receipt) != _RECEIPT_SCHEMAS[version]
            or type(receipt['kind']) is not str or receipt['kind'] not in NATIVE_TAKE_KINDS
            or not _is_digest(receipt['book_token']) or type(receipt['uid']) is not str
            or not 0 < len(receipt['uid']) <= 128 or receipt['uid'] != row.get('uid')
            or type(receipt['audio_revision']) is not str
            or not _REVISION.fullmatch(receipt['audio_revision'])
            or receipt['audio_revision'] != row.get('audio_revision')
            or type(receipt['index']) is not int or not 0 <= receipt['index'] < 2**31
            or type(receipt['dependencies']) is not dict or set(receipt['dependencies']) != _DEPENDENCIES
            or not all(_is_digest(value) for value in receipt['dependencies'].values())
            or not _is_digest(receipt['speech_sha256']) or not _is_digest(receipt['synthesis_sha256'])
            or not _is_audio(receipt['pcm']) or not _is_audio(receipt['audio'])
            or (version >= 2 and not _is_seed(receipt['seed']))
            or len(_encode(receipt)) > MAX_RECEIPT_BYTES):
        raise ValueError(_INVALID_EVIDENCE)
    return receipt


@dataclass(frozen=True)
class TakePublicationAdmission:
    # Runtime-only references. Never deserialize these from chunks/provider data.
    engine: object
    parent: object
    book_token: str
    uid: str
    index: int
    inputs_sha256: str
    voice_identity_sha256: str
    model_sha256: str
    seed: int
    category: str = 'custom'


def get_take_publication_book_token(admission):
    from synthesis_recovery_context import RecoveryParent, get_recovery_claim_digest
    if (type(admission) is not TakePublicationAdmission or type(admission.parent) is not RecoveryParent
            or get_recovery_claim_digest() != admission.parent.claim_sha256
            or not _is_digest(admission.book_token)
            or admission.category not in ('custom', 'clone', 'lora')):
        return None
    return admission.book_token


def _is_current_engine(engine):
    from synthesis_recovery_identity import RecoveryIdentityUnavailable
    from synthesis_recovery_serving import _capture_engine_methods
    try:
        _capture_engine_methods(engine, {}, [])
    except RecoveryIdentityUnavailable:
        return False
    return True


def capture_take_publication_admission(engine, parent, book_token, uid, index, inputs,
                                       voice_config, speaker):
    """Capture actual built-in inputs under caller's existing book admission.

    Eligibility inspection does not load a model. Only the established native
    window/recovery contract can subsequently supply a serving proof.
    """
    from synthesis_recovery_context import RecoveryParent, get_recovery_claim_digest
    from synthesis_recovery_identity import get_recovery_digest
    from tts import get_resolved_speaker, voice_category
    if (type(parent) is not RecoveryParent or get_recovery_claim_digest() != parent.claim_sha256
            or not _is_digest(book_token) or type(uid) is not str or not 0 < len(uid) <= 128
            or type(index) is not int or not 0 <= index < 2**31 or not _is_current_engine(engine)):
        return None
    windows = getattr(engine, '_synthesis_windows', None)
    recovery = engine.get_synthesis_recovery_policy()
    if type(voice_config) is not dict:
        return None
    try:
        resolved = get_resolved_speaker(speaker, voice_config)
    except (AttributeError, TypeError, ValueError):
        # Optional proof cannot admit malformed voice or alias entries.
        return None
    selected = voice_config.get(resolved)
    if type(selected) is not dict:
        return None
    category = voice_category(selected)
    if category not in ('custom', 'clone', 'lora'):
        return None
    if (engine.get_voice_backend(category) != 'local' or engine._compile_codec_enabled
            or type(windows) is not dict or windows.get('enabled') is not True
            or type(recovery) is not dict or recovery.get('enabled') is not True):
        return None
    # Seed admission is cheap; actual category/reference/adapter execution is
    # certified by the native recovery guard during rendering, before proof.
    try:
        seed = int(selected.get('seed', -1))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= seed < 2**64:
        return None
    with parent.admit():
        model = engine.get_model_settings()
        try:
            _ensure_metadata_depth([inputs, selected, model])
            if len(_encode([inputs, selected, model])) > 128 * 1024:
                return None
        except (ValueError, TypeError):
            return None
        if (parent.uid_sha256 != get_recovery_digest(uid)
                or parent.inputs_sha256 != get_recovery_digest(dict(index=index, inputs=inputs))):
            return None
        # v3 binds only render-affecting voice fields plus the admitted integer
        # seed; Editor may save the seed as a string at the generation boundary.
        # Legacy v1/v2 readers reconstruct their full-config digest separately.
        identity_sha256 = get_recovery_digest(get_take_voice_identity(selected, seed))
        return TakePublicationAdmission(engine, parent, book_token, uid, index,
            get_recovery_digest(inputs), identity_sha256,
            get_recovery_digest(model), seed, category)


def _read_audio(root, path, cancel_check, *, decode=True):
    stream, before = _open_regular(Path(path), root, MAX_AUDIO_BYTES)
    with stream:
        digest, size = _hash(stream, MAX_AUDIO_BYTES, cancel_check=cancel_check)
        shape = _decode(stream, cancel_check=cancel_check, source_path=path) if decode else {}
        if _version(os.fstat(stream.fileno())) != _version(before):
            raise ValueError('Take audio changed while binding publication evidence.')
    if _version(_ensure_plain_path(path, root).stat()) != _version(before):
        raise ValueError('Take audio changed while binding publication evidence.')
    return dict(sha256=digest, size=size, **shape), _version(before)


def validate_published_take_audio(root, path, expected, cancel_check=None):
    """Check the installed slot before its proof and selection are committed."""
    actual, version = _read_audio(Path(root).resolve(), path, cancel_check, decode=False)
    if actual != {key:expected[key] for key in ('sha256','size')}:
        raise ValueError('Finished audio changed during take evidence publication')
    return version


def get_take_publication_receipt(admission, inputs, preparation, speech_input, synthesis,
                                 pcm_path, encoded_path, revision, cancel_check=None):
    """Return (bounded evidence, staged version), or (None, None) if unproven.

    Once admitted audio is inspected, integrity/cancellation failures propagate;
    they cannot silently attach a certificate to changed or invalid bytes.
    """
    from synthesis_recovery_identity import get_recovery_digest
    from speech_verification_receipt import get_validated_speech_input
    if get_take_publication_book_token(admission) is None or not _is_current_engine(admission.engine):
        return None, None
    if (get_recovery_digest(inputs) != admission.inputs_sha256
            or get_recovery_digest(admission.engine.get_model_settings()) != admission.model_sha256
            or type(synthesis) is not dict):
        return None, None
    _ensure_metadata_depth(synthesis)
    if len(_encode(synthesis)) > 128 * 1024:
        return None, None
    recovery = synthesis.get('recovery')
    if (type(recovery) is not dict or type(recovery.get('version')) is not int
            or recovery['version'] != 1 or type(recovery.get('input_proof_version')) is not int
            or recovery['input_proof_version'] != 1
            or recovery.get('status') not in ('eligible','cache_unavailable','capacity_reached')
            or not _is_digest(recovery.get('serving_sha256'))
            or not _is_digest(recovery.get('request_sha256'))
            or recovery.get('parent') != admission.parent.get_semantic_identity()
            or synthesis.get('audio_revision') != revision or synthesis.get('scope') != 'pre-encoding PCM'):
        return None, None
    text = preparation['text']
    plan = admission.engine._get_synthesis_window_plan(text, admission.category)
    policy = admission.engine.get_model_settings().get('synthesis_windows')
    if plan is None or policy != synthesis.get('policy'):
        return None, None
    descriptor = dict(version=1, parent=admission.parent.get_semantic_identity(),
        plan=asdict(plan), policy=policy, serving_sha256=recovery['serving_sha256'], input_proof_version=1)
    windows = synthesis.get('windows')
    if (get_recovery_digest(descriptor) != recovery['request_sha256']
            or type(windows) is not list or len(windows) != len(plan.windows)
            or synthesis.get('prepared_text_sha256') != plan.text_sha256
            or synthesis.get('window_count') != len(plan.windows)
            or any(type(row) is not dict or (row.get('index'),row.get('start'),row.get('end'),row.get('boundary'))
                != (i,window.start,window.end,window.boundary)
                for i,(row,window) in enumerate(zip(windows,plan.windows)))):
        return None, None
    observed = get_validated_speech_input(speech_input, pcm_path, cancel_check or (lambda: False))
    if observed is None or observed['text'] != text:
        return None, None
    root = Path(admission.parent.project_root).resolve()
    # Optional evidence must not impose archive limits on first-pass output.
    # Ineligible size/shape stays unproven; integrity and cancellation errors
    # from admitted bounded files still propagate.
    # Admission of the path/type remains strict even for oversized output.
    # This opens metadata only and never hashes an out-of-policy file.
    encoded_size = _ensure_plain_path(encoded_path, root).stat().st_size
    if encoded_size > MAX_AUDIO_BYTES:
        stream, _ = _open_regular(Path(encoded_path), root, encoded_size)
        stream.close()
        return None, None
    try:
        pcm, _ = _read_audio(root, pcm_path, cancel_check)
    except TakeAudioOutsideSnapshotBounds:
        return None, None
    if (observed['audio'] != {key:pcm[key] for key in ('sha256','size')}
            or (pcm['frames'],pcm['sample_rate'],pcm['channels']) !=
                (synthesis.get('output_frames'),synthesis.get('sample_rate'),synthesis.get('channels'))):
        raise ValueError('Observed synthesis evidence differs from the completed parent PCM.')
    try:
        audio, version = _read_audio(root, encoded_path, cancel_check)
    except TakeAudioOutsideSnapshotBounds:
        return None, None
    with admission.parent.admit():
        receipt = dict(version=3, seed=admission.seed, kind=next(kind for kind, category in NATIVE_TAKE_KINDS.items() if category == admission.category), book_token=admission.book_token,
            uid=admission.uid, audio_revision=revision, index=admission.index,
            dependencies=dict(inputs_sha256=admission.inputs_sha256,
                              voice_sha256=admission.voice_identity_sha256,
                model_sha256=admission.model_sha256, pronunciation_sha256=get_recovery_digest(preparation),
                recipe_sha256=get_recovery_digest(dict(plan=asdict(plan),policy=policy)),
                serving_sha256=recovery['serving_sha256']),
            speech_sha256=plan.text_sha256, pcm=pcm, audio=audio,
            synthesis_sha256=get_recovery_digest(synthesis))
        validate_take_publication({'uid':admission.uid,'audio_revision':revision,KEY:receipt})
        return receipt, version


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
