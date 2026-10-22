"""Explicit selection of a saved performance with matching execution evidence.

The original artifact revision and generation receipts stay intact. Restoration
copies encoded bytes; it neither synthesizes speech nor edits authored words.
"""
from contextlib import closing
import copy
from dataclasses import asdict
import os
from pathlib import Path

from audio_generation_owner import (ensure_audio_generation_active,
    ensure_audio_generation_book_work, get_audio_generation_claim_id,
    reuse_audio_generation_book)
from audio_take_history import AudioTakeHistory, _version
from book_state_transaction import ensure_book_state, get_book_operation_token
from take_publication import KEY, _is_current_engine, _read_audio, validate_take_publication
from utils import file_lock


def _get_target(manager, index, request, expected=None):
    from project import get_chunk_generation_inputs
    if (get_book_operation_token(manager.root_dir) != request.expected_book_token
            or request.source_book_token != request.expected_book_token):
        raise ValueError('The saved take belongs to another book generation. Refresh Editor.')
    with manager._chunks_lock, file_lock(manager.chunks_path):
        rows = manager._get_saved_chunks_locked()
        if not 0 <= index < len(rows):
            raise ValueError('The selected Editor line no longer exists.')
        row = rows[index]
        if (row['uid'] != request.expected_uid
                or row.get('audio_revision') != request.expected_audio_revision
                or row.get('audio_path') != request.expected_audio_path
                or type(row.get('take')) is not type(request.expected_take)
                or row.get('take') != request.expected_take
                or row.get('status') == 'generating'
                or expected is not None and (
                    get_chunk_generation_inputs(row) != get_chunk_generation_inputs(expected)
                    or any(row.get(key) != expected.get(key)
                           for key in ('audio_path','audio_revision','status')))):
            raise ValueError('The selected line or take changed. Refresh Editor.')
        manager.ensure_literal_render_inputs(rows, [index])
        return copy.deepcopy(row)


def _get_record(history, request):
    with history._locked() as document:
        if document['version'] != 2:
            raise ValueError('This archive has no supported restoration evidence.')
        row = history._find(document, request.version, request.sha256,
                            request.source_book_token, request.expected_uid)
        if row['state'] != 'ready':
            raise ValueError('Only a complete, undeleted snapshot can be restored.')
        return copy.deepcopy(row)


def _get_candidate(current, record):
    from project import get_chunk_generation_inputs
    from synthesis_recovery_identity import get_recovery_digest
    captured = record['captured']
    proof = validate_take_publication({'uid':record['uid'],
        'audio_revision':record['audio_revision'], KEY:captured.get(KEY)})
    if proof is None:
        raise ValueError('This saved take has no proven generation dependencies; it remains available for audition.')
    take = captured.get('take')
    if type(take) is not int or take < 0:
        raise ValueError('The saved take counter is unknown; this snapshot remains available for audition.')
    candidate = {**current, 'take':take}
    inputs = get_chunk_generation_inputs(candidate)
    synthesis = captured.get('synthesis_receipt')
    if (proof['book_token'] != record['book_token'] or proof['audio']['sha256'] != record['sha256']
            or proof['audio']['size'] != record['bytes']
            or {key:proof['audio'][key] for key in ('frames','sample_rate','channels')} != record['shape']
            or captured.get('audio_revision') != proof['audio_revision']
            or inputs != captured.get('generation_inputs')
            or get_recovery_digest(inputs) != proof['dependencies']['inputs_sha256']
            or type(synthesis) is not dict or synthesis.get('audio_revision') != proof['audio_revision']
            or get_recovery_digest(synthesis) != proof['synthesis_sha256']):
        raise ValueError('Saved inputs or audio evidence differ from this line. The selected take was preserved.')
    return candidate, proof


def capture_restore_target(manager, index, request):
    """Cheap request-time admission, after reserving the existing audio task."""
    from synthesis_workspace import is_synthesis_workspace_supported
    if not is_synthesis_workspace_supported():
        raise ValueError('Verified restoration staging is not qualified on this host; audition remains available.')
    with ensure_book_state(manager.root_dir):
        current = _get_target(manager, index, request)
        _get_candidate(current, _get_record(AudioTakeHistory(manager.root_dir), request))
        return current


def _get_matching_request(manager, engine, index, candidate, proof):
    from lora_evidence import get_tts_model_settings
    from project import get_occurrence_speech_projection
    from synthesis_recovery_identity import get_recovery_digest
    from tts import get_custom_voice_request, get_resolved_speaker, voice_category
    if not _is_current_engine(engine):
        raise ValueError('The active renderer has no supported native restoration identity.')
    settings = engine.get_model_settings()
    if (settings != get_tts_model_settings(manager._read_config())
            or get_recovery_digest(settings) != proof['dependencies']['model_sha256']):
        raise ValueError('Current model settings differ from this saved performance.')
    voices = manager.get_voice_config_for_update()
    speaker = manager._resolve_alias(candidate['speaker'], voices)
    choices = [manager.get_chunk_render_voice_config(voices, speaker, candidate, index)]
    batch_seed = manager._load_tts_config().get('batch_seed', -1)
    if type(batch_seed) is int:
        choices.append(manager.get_chunk_render_voice_config(
            voices, speaker, candidate, index, batch_seed=batch_seed))
    request = None
    for choice in choices:
        selected = choice.get(get_resolved_speaker(speaker, choice))
        if (voice_category(selected) == 'custom'
                and get_recovery_digest(selected) == proof['dependencies']['voice_sha256']):
            request = get_custom_voice_request(choice, get_resolved_speaker(speaker, choice),
                                               candidate.get('instruct', ''))
            break
    if request is None:
        raise ValueError('Current voice or seed selection differs from this saved performance.')
    projection = get_occurrence_speech_projection(candidate, index, voices, settings)
    preparation = {key:value for key,value in projection.items() if key != 'context'}
    plan = engine._get_synthesis_window_plan(preparation['text'], 'custom')
    if (plan is None or get_recovery_digest(preparation) != proof['dependencies']['pronunciation_sha256']
            or plan.text_sha256 != proof['speech_sha256']
            or get_recovery_digest(dict(plan=asdict(plan),policy=settings.get('synthesis_windows')))
                != proof['dependencies']['recipe_sha256']):
        raise ValueError('Current pronunciation or synthesis windows differ from this saved performance.')
    return request, plan


def _get_speech_receipt(candidate, record, proof):
    from speech_verification_receipt import get_current_speech_verification_receipt
    original = record['captured'].get('speech_verification')
    if original is None:
        return None
    receipt = get_current_speech_verification_receipt({**candidate,
        'audio_revision':proof['audio_revision'], 'speech_verification':original}, proof['book_token'])
    if (receipt is None or receipt['audio'] != {key:proof['audio'][key] for key in ('sha256','size')}
            or receipt['expected_sha256'] != proof['speech_sha256']
            or receipt['status'] == 'corroborated_content_difference'):
        raise ValueError('Saved speech evidence needs inspection; the selected take was preserved.')
    return receipt


def _ensure_claim(claim):
    ensure_audio_generation_active()
    if claim is None or get_audio_generation_claim_id() != claim:
        raise ValueError('Restore requires its original owned Editor audio operation.')


def restore_editor_take(manager, index, request, expected):
    """Retain the displaced take, verify compatibility, then atomically select."""
    from synthesis_recovery_native import ensure_native_recovery_model_capture
    from synthesis_recovery_serving import capture_native_custom_serving_identity
    from synthesis_workspace import (SynthesisWorkspaceRegistry, ensure_synthesis_workspace,
                                     is_synthesis_workspace_supported)
    from tts import _LOCAL_TTS_LOCK
    import pronunciation
    claim = get_audio_generation_claim_id()
    _ensure_claim(claim)
    if not is_synthesis_workspace_supported():
        raise ValueError('Verified restoration staging is not qualified on this host; audition remains available.')
    history = AudioTakeHistory(manager.root_dir)
    with ensure_audio_generation_book_work(manager.root_dir):
        current = _get_target(manager, index, request, expected)
        record = _get_record(history, request)
        candidate, proof = _get_candidate(current, record)
        speech = _get_speech_receipt(candidate, record, proof)
        # Capacity and the durable displaced-performance hold precede model
        # loading. A refused/cancelled attempt keeps this safe retry reference.
        with manager._ensure_automatic_predecessor(current, required=True):
            pass
    engine = manager.get_engine()
    native_request, _ = _get_matching_request(manager, engine, index, candidate, proof)
    _ensure_claim(claim)
    # Initialization/asset inspection happens outside book and owner locks.
    with ensure_native_recovery_model_capture(), _LOCAL_TTS_LOCK:
        identity = capture_native_custom_serving_identity(engine, native_request)
    if identity.sha256 != proof['dependencies']['serving_sha256']:
        raise ValueError('Current native execution dependencies differ from this saved performance.')
    _ensure_claim(claim)
    with ensure_audio_generation_book_work(manager.root_dir):
        _get_target(manager, index, request, expected)
        opened, source = history.open_for_restore(request.version, request.sha256,
            request.source_book_token, request.expected_uid, cancel_check=ensure_audio_generation_active)
        with closing(source):
            if opened != record:
                raise ValueError('The saved snapshot changed during restoration.')
            parent = manager._capture_synthesis_recovery_parent(engine, index, current)
            if parent is None:
                raise ValueError('Exact owned restoration staging admission is unavailable.')
            native_request, plan = _get_matching_request(manager, engine, index, candidate, proof)
            with ensure_synthesis_workspace(parent, len(plan.windows)) as workspace:
                path = workspace / ('assembled-parent' + record['suffix'])
                before = _version(os.fstat(source.fileno()))
                size = 0
                with parent.admit():
                    with SynthesisWorkspaceRegistry(manager.root_dir) as registry:
                        fd = registry.create_encoded_parent(workspace.name, record['suffix'])
                try:
                    target = os.fdopen(fd, 'wb')
                except BaseException:
                    os.close(fd)
                    raise
                with target:
                    while True:
                        _ensure_claim(claim)
                        block = source.read(1024**2)
                        if not block:
                            break
                        size += len(block)
                        if size > record['bytes'] or target.write(block) != len(block):
                            raise ValueError('Saved audio changed while copying the restoration.')
                    target.flush(); os.fsync(target.fileno())
                    copied_version = _version(os.fstat(target.fileno()))
                if size != record['bytes'] or _version(os.fstat(source.fileno())) != before:
                    raise ValueError('Saved audio changed while copying the restoration.')
                actual, staged_version = _read_audio(Path(manager.root_dir).resolve(), path, ensure_audio_generation_active)
                if actual != proof['audio'] or staged_version != copied_version:
                    raise ValueError('Restored bytes differ from the saved publication evidence.')
                history.verify_retained(request.version, request.sha256, request.source_book_token,
                    request.expected_uid, record['captured'], cancel_check=ensure_audio_generation_active)
                # The shared lexicon saver does not take the book lock. Keep its
                # existing save admission through selection, without waiting
                # under the owner mutex. Override inherited worker snapshots so
                # this check compares the current shared document.
                with manager._ensure_automatic_predecessor(current, required=True), _LOCAL_TTS_LOCK, \
                        file_lock(pronunciation.DEFAULT_PATH), \
                        pronunciation.ensure_pronunciation_audition(pronunciation.load_lexicon(force=True)):
                    native_request, _ = _get_matching_request(manager, engine, index, candidate, proof)
                    with ensure_native_recovery_model_capture():
                        final = capture_native_custom_serving_identity(engine, native_request)
                    if final.sha256 != identity.sha256:
                        raise ValueError('Native execution dependencies changed before restoration publication.')
                    _ensure_claim(claim)
                    with reuse_audio_generation_book(manager.root_dir):
                        _get_target(manager, index, request, expected)
                        updated = manager._update_chunk_fields_by_uid(current['uid'], expected_chunk=current,
                            expected_fields={key:current.get(key) for key in ('audio_path','audio_revision','status')},
                            audio_source=str(path), publication_audio=proof['audio'],
                            restore_speech_receipt=speech is not None,
                            take=candidate['take'], status='done', audio_revision=proof['audio_revision'],
                            error=None, drift=None, pronunciation_receipt=record['captured'].get('pronunciation_receipt'),
                            synthesis_receipt=record['captured']['synthesis_receipt'], speech_verification=speech,
                            speech_verification_failure=None, speech_verification_recheck=None, **{KEY:proof})
                    if updated is None:
                        raise ValueError('The selected line changed before restoration publication.')
                    return updated
