"""Shared pronunciation editing and disposable auditions; never rewrites a book."""
import asyncio
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

import pronunciation
from audio_validation import validate_generated_audio
from book_state_transaction import ensure_book_state, get_book_operation_token
from core import (project_manager, run_claimed_task_worker, process_state, ensure_book_switch_allowed,
                  is_task_running)
from routers.editor import (check_chunk_identity, SCRIPT_PATH, _ensure_editor_chunks,
                            get_editor_book_identity, get_snapshot_digest)
from project import get_speaker, get_occurrence_speech_projection
from speech_numbers import STATE_KEY as DATE_ORDER_KEY, get_book_date_order_status
from speech_text import get_prelexicon_speech_normalization
from tts import (get_resolved_speaker, is_voice_config_present, resolve_narrator_voice_config,
                 voice_config_for_chunk)
from utils import atomic_json_write, file_lock, safe_load_json

router = APIRouter()
MAX_DOCUMENT_BYTES = 1024 * 1024


class PronunciationEntries(BaseModel):
    names: dict[str, str] = Field(max_length=2000)

    @field_validator('names')
    @classmethod
    def validate_names(cls, names):
        if any(not key.strip() or len(key) > 128 or len(value) > 256
               for key, value in names.items()):
            raise ValueError('Spellings need 1–128 characters; replacements allow up to 256.')
        return names


class PronunciationSave(PronunciationEntries):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')


class PronunciationPreview(PronunciationEntries):
    expected_book_token: str = Field(pattern=r'^[0-9a-f]{64}$')


class PronunciationAudition(BaseModel):
    expected_book_token: str = Field(pattern=r'^[0-9a-f]{64}$')
    index: StrictInt = Field(ge=0)
    expected_uid: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=256)
    replacement: str | None = Field(default=None, min_length=1, max_length=256)


def get_pronunciation_document():
    """Strict editor read; malformed data must not be silently replaced on Save."""
    try:
        with open(pronunciation.DEFAULT_PATH, 'rb') as source:
            raw = source.read(MAX_DOCUMENT_BYTES + 1)
    except FileNotFoundError:
        raw = b''
        document = {'names': {}}
    else:
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise HTTPException(status_code=422, detail='Pronunciation file exceeds the 1 MiB editor limit.')
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise HTTPException(status_code=422, detail='Pronunciation file is invalid; repair it before editing.') from error
    if not isinstance(document, dict):
        raise HTTPException(status_code=422, detail='Pronunciation file must contain an object.')
    names = document['names'] if isinstance(document.get('names'), dict) else document
    if not isinstance(names, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in names.items()):
        raise HTTPException(status_code=422, detail='Pronunciation entries must map spellings to strings.')
    try:
        PronunciationEntries(names=names)
    except ValueError as error:
        raise HTTPException(status_code=422, detail='Pronunciation file exceeds editor entry limits.') from error
    return document, {'names': dict(names), 'revision': hashlib.sha256(raw).hexdigest(), 'scope': 'shared'}


@router.get('/api/pronunciation')
async def get_pronunciation_endpoint():
    _document, result = await asyncio.to_thread(get_pronunciation_document)
    return result


@router.post('/api/pronunciation/save')
async def save_pronunciation_endpoint(request: PronunciationSave):
    return await asyncio.to_thread(save_pronunciation_document, request)


def save_pronunciation_document(request):
    with file_lock(pronunciation.DEFAULT_PATH):
        document, current = get_pronunciation_document()
        if request.revision != current['revision']:
            raise HTTPException(status_code=409, detail='Pronunciation rules changed elsewhere. Reload before saving.')
        updated = ({**document, 'names': dict(request.names)}
                   if isinstance(document.get('names'), dict) else
                   {'names': dict(request.names)} if 'names' in request.names else dict(request.names))
        if len(json.dumps(updated, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_DOCUMENT_BYTES:
            raise HTTPException(status_code=422, detail='Pronunciation file would exceed the 1 MiB editor limit.')
        atomic_json_write(updated, pronunciation.DEFAULT_PATH, allow_nonatomic_fallback=False)
        _document, result = get_pronunciation_document()
        return result


@router.post('/api/pronunciation/preview')
async def get_pronunciation_preview_endpoint(request: PronunciationPreview):
    return await asyncio.to_thread(_get_pronunciation_preview_sync, request)


def _get_pronunciation_preview_sync(request):
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        check_chunk_identity(0, request.expected_book_token)
        chunks = _ensure_editor_chunks()
        counts = Counter()
        pattern = pronunciation._compile({key: value for key, value in request.names.items() if value.strip()})
        for row in chunks:
            _text, substitutions = pronunciation.get_pronunciation_result(row.get('text', ''), request.names, pattern)
            counts.update(item['name'] for item in substitutions)
        return {'book_token': request.expected_book_token, 'counts': dict(counts),
                'lines': [{'id': row['id'], 'uid': row['uid'], 'speaker': get_speaker(row)}
                          for row in chunks if row.get('text', '').strip()]}


@router.post('/api/pronunciation/audition')
async def generate_pronunciation_audition_endpoint(request: PronunciationAudition):
    audio = await run_claimed_task_worker('audio', generate_pronunciation_audition, request)
    return Response(audio, media_type='audio/wav', headers={'Cache-Control': 'no-store'})


def generate_pronunciation_audition(request):
    """The owned audio worker holds admission through generation and private cleanup."""
    state = process_state['audio']
    state['status'] = 'running'
    state['error'] = None
    state['start_time'] = time.time()
    state['logs'] = ['Generating a short pronunciation audition (no book audio is replaced).']
    try:
        with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
            check_chunk_identity(request.index, request.expected_book_token, request.expected_uid)
            chunks = project_manager.load_chunks()
            row = chunks[request.index]
            try:
                voices = json.loads(Path(project_manager.voice_config_path).read_text(encoding='utf-8'))
            except FileNotFoundError as error:
                raise HTTPException(status_code=400, detail='Assign a voice to this line before auditioning.') from error
            except (ValueError, UnicodeError) as error:
                raise HTTPException(status_code=422, detail='Repair invalid voice configuration before auditioning.') from error
            if not isinstance(voices, dict):
                raise HTTPException(status_code=422, detail='Voice configuration must contain an object.')
            speaker = get_resolved_speaker(get_speaker(row), voices)
            voices = copy.deepcopy(voice_config_for_chunk(
                resolve_narrator_voice_config(speaker, voices, row), speaker, request.index))
            if not is_voice_config_present(speaker, voices):
                raise HTTPException(status_code=400, detail='Assign a voice to this line before auditioning.')
        if not request.text.strip() or (request.replacement is not None and not request.replacement.strip()):
            raise HTTPException(status_code=422, detail='Enter a spelling to audition.')
        if state.get('cancel'):
            raise HTTPException(status_code=409, detail='Pronunciation audition cancelled.')
        engine = project_manager.get_engine()
        if engine is None:
            raise HTTPException(status_code=503, detail='TTS engine is unavailable.')
        with tempfile.TemporaryDirectory(prefix='aa2-pronunciation-') as directory:
            output = os.path.join(directory, 'audition.wav')
            with pronunciation.ensure_pronunciation_audition(
                    {} if request.replacement is None else {request.text: request.replacement}):
                success = engine.generate_voice(request.text, row.get('instruct', ''), speaker, voices, output)
            if state.get('cancel'):
                raise HTTPException(status_code=409, detail='Pronunciation audition cancelled; its temporary audio was discarded.')
            if not success:
                raise HTTPException(status_code=422, detail='Audition generation failed; check the audio task logs and voice configuration.')
            # Small response materialized before leaving the worker, so a client
            # disconnect cannot strand a FileResponse temporary directory.
            if os.path.getsize(output) > 16 * 1024 * 1024:
                raise HTTPException(status_code=422, detail='Audition exceeded the 16 MiB preview limit.')
            validate_generated_audio(output, 'pronunciation audition')
            audio = Path(output).read_bytes()
        state['status'] = 'done'
        state['logs'].append('Pronunciation audition complete. Existing book audio is unchanged.')
        return audio
    except HTTPException as error:
        state['status'] = 'cancelled' if state.get('cancel') else 'failed'
        state['error'] = error.detail
        state['logs'].append('Pronunciation audition refused or failed; existing book audio is unchanged.')
        raise
    except Exception as error:
        state['status'] = 'failed'
        state['error'] = f'Audition failed: {type(error).__name__}.'
        state['logs'].append(f'Pronunciation audition failed: {type(error).__name__}.')
        raise HTTPException(status_code=500, detail='Audition failed; check the audio task logs and selected voice.') from error


class PronunciationOccurrenceTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_book_token: str = Field(pattern=r'^[0-9a-f]{64}$')
    index: StrictInt = Field(ge=0)
    expected_uid: str = Field(min_length=1, max_length=128)


class PronunciationOccurrenceQuery(PronunciationOccurrenceTarget):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    query: str = Field(min_length=1, max_length=128)

    @field_validator('query')
    @classmethod
    def validate_query(cls, value):
        if not value.strip():
            raise ValueError('Choose a nonempty exact spelling.')
        return value


class PronunciationOccurrenceRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64)
    state: Literal['draft', 'approved', 'rejected']
    coordinate_space: Literal['pre_lexicon_v1']
    offset_unit: Literal['unicode_codepoint']
    uid: str = Field(min_length=1, max_length=128)
    text_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prepared_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=1)
    original: str = Field(min_length=1, max_length=512)
    replacement: str | None = Field(default=None, min_length=1, max_length=512)
    phonetic_hint: str | None = Field(default=None, min_length=1, max_length=512)
    fallback: str | None = Field(default=None, min_length=1, max_length=512)
    restrictions: dict[str, str] = Field(default_factory=dict, max_length=4)
    note: str = Field(default='', max_length=512)

    @model_validator(mode='after')
    def validate_rule(self):
        error = pronunciation.get_occurrence_rule_error(self.model_dump(exclude_none=True))
        if error:
            raise ValueError('Occurrence rule needs review: ' + error)
        return self


class PronunciationOccurrenceEdit(PronunciationOccurrenceTarget):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    rules: list[PronunciationOccurrenceRule] = Field(max_length=pronunciation.MAX_OCCURRENCE_RULES)


def get_occurrence_rule_document(row):
    """Strict bounded editor read, retaining stale anchors for explicit removal."""
    rules = row.get('pronunciation_rules', [])
    if (not isinstance(rules, list) or len(rules) > pronunciation.MAX_OCCURRENCE_RULES
            or any(pronunciation.get_occurrence_rule_error(rule) for rule in rules)):
        raise HTTPException(status_code=409, detail='Saved occurrence rules need repair; existing data was retained.')
    try:
        encoded = json.dumps(rules, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, UnicodeError) as error:
        raise HTTPException(status_code=409, detail='Saved occurrence rules need repair; existing data was retained.') from error
    if len(encoded) > pronunciation.MAX_OCCURRENCE_RULE_BYTES:
        raise HTTPException(status_code=409, detail='Saved occurrence rules exceed the line editor limit.')
    return rules, hashlib.sha256(encoded).hexdigest()


def get_occurrence_target(request, chunks):
    """The caller holds the book lock; no identity repair or UID backfill."""
    root = os.path.dirname(SCRIPT_PATH)
    if get_snapshot_digest(get_editor_book_identity(root)) != request.expected_book_token:
        raise HTTPException(status_code=409, detail='Active book changed. Refresh Editor before editing pronunciation.')
    if not 0 <= request.index < len(chunks) or chunks[request.index]['uid'] != request.expected_uid:
        raise HTTPException(status_code=409, detail='The selected line changed. Reload its occurrence rules.')
    row = chunks[request.index]
    if len(row.get('text', '')) > 131072:
        raise HTTPException(status_code=422, detail='This line exceeds the occurrence editor text limit.')
    return row


def require_occurrence_revision(request, revision):
    if hasattr(request, 'revision') and request.revision != revision:
        raise HTTPException(status_code=409, detail='Occurrence rules changed elsewhere. Reload before continuing.')


def validate_occurrence_edit(rules, row):
    prepared = get_prelexicon_speech_normalization(row.get('text', ''))['text']
    try:
        pronunciation.validate_occurrence_pronunciation_rules(rules, row['uid'], row.get('text', ''), prepared)
    except pronunciation.OccurrenceAnchorConflict as error:
        raise HTTPException(status_code=409, detail=(
            'The line text or selected occurrence changed. Remove stale rules and choose fresh matches.')) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def get_occurrence_response(request, row, rules, revision, projection, settings_source):
    return {'book_token': request.expected_book_token, 'uid': row['uid'], 'index': request.index,
            'revision': revision, 'rules': rules, 'coordinate_space': pronunciation.OCCURRENCE_COORDINATE_SPACE,
            'offset_unit': 'unicode_codepoint', 'context': projection['context'],
            'context_source': settings_source,
            'preview': {'text': projection['text'], 'decisions': projection.get('pronunciation_decisions', [])}}


def get_occurrence_editor(request):
    try:
        with ensure_book_state(os.path.dirname(SCRIPT_PATH), recover=False):
            chunks = project_manager.get_saved_chunks()
            row = get_occurrence_target(request, chunks)
            rules, revision = get_occurrence_rule_document(row)
            require_occurrence_revision(request, revision)
            voices = project_manager.get_voice_config_for_update()
            settings = project_manager.get_pronunciation_preview_settings()
            effective = rules
            if isinstance(request, PronunciationOccurrenceEdit):
                effective = [rule.model_dump(exclude_none=True) for rule in request.rules]
                validate_occurrence_edit(effective, row)
            with pronunciation.ensure_pronunciation_snapshot():
                projection = get_occurrence_speech_projection(
                    {**row, 'pronunciation_rules': effective}, request.index, voices, settings['settings'])
                current = (get_occurrence_speech_projection(row, request.index, voices, settings['settings'])
                           if isinstance(request, PronunciationOccurrenceEdit) else projection)
            result = get_occurrence_response(request, row, effective, revision, projection, settings['source'])
            if isinstance(request, PronunciationOccurrenceEdit):
                result['audio_will_be_detached'] = current['text'] != projection['text']
            if isinstance(request, PronunciationOccurrenceQuery):
                prepared = get_prelexicon_speech_normalization(row.get('text', ''))['text']
                candidates = pronunciation.get_occurrence_pronunciation_candidates(
                    prepared, row['uid'], row.get('text', ''), request.query)
                result.update(truncated=candidates['truncated'], candidates=[
                    {'anchor': {key: value for key, value in candidate.items() if key not in ('before', 'after')},
                     'before': candidate['before'], 'after': candidate['after']}
                    for candidate in candidates['candidates']])
            return result
    except HTTPException:
        raise
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=409, detail=(
            'Saved book state needs preparation or repair in Editor before preview. Existing files were retained.')) from error


@router.post('/api/pronunciation/occurrences')
async def get_occurrence_editor_endpoint(request: PronunciationOccurrenceTarget):
    return await asyncio.to_thread(get_occurrence_editor, request)


@router.post('/api/pronunciation/occurrences/candidates')
async def get_occurrence_candidates_endpoint(request: PronunciationOccurrenceQuery):
    return await asyncio.to_thread(get_occurrence_editor, request)


@router.post('/api/pronunciation/occurrences/preview')
async def get_occurrence_preview_endpoint(request: PronunciationOccurrenceEdit):
    return await asyncio.to_thread(get_occurrence_editor, request)


def _save_occurrence_rules_admitted(request):
    with ensure_book_switch_allowed(operation='save occurrence rules'):
        with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
            settings = project_manager.get_pronunciation_preview_settings()
            voices = project_manager.get_voice_config_for_update()
            with project_manager._chunks_lock, file_lock(project_manager.chunks_path):
                try:
                    chunks = project_manager._get_saved_chunks_locked()
                    row = get_occurrence_target(request, chunks)
                    _current_rules, revision = get_occurrence_rule_document(row)
                    require_occurrence_revision(request, revision)
                    rules = [rule.model_dump(exclude_none=True) for rule in request.rules]
                    validate_occurrence_edit(rules, row)
                    updated = {**row, 'pronunciation_rules': rules}
                    with pronunciation.ensure_pronunciation_snapshot():
                        before = get_occurrence_speech_projection(row, request.index, voices, settings['settings'])
                        after = get_occurrence_speech_projection(updated, request.index, voices, settings['settings'])
                    changed = before['text'] != after['text']
                    if changed:
                        updated.update(status='pending', audio_path=None, error=None, drift=None)
                    if updated != row:
                        chunks[request.index] = updated
                        project_manager._save_chunks_locked(chunks, uids_backfilled=True)
                    _saved, revision = get_occurrence_rule_document(updated)
                    result = get_occurrence_response(request, updated, rules, revision, after, settings['source'])
                    result.update(audio_detached=changed, status=updated.get('status'))
                    return result
                except HTTPException:
                    raise
                except (OSError, ValueError) as error:
                    raise HTTPException(status_code=409, detail=(
                        'Could not save occurrence rules. Refresh or repair saved state before retrying.')) from error


def save_occurrence_rules(request):
    try:
        return _save_occurrence_rules_admitted(request)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=409, detail=(
            'Saved book state needs preparation or repair before saving occurrence rules.')) from error


@router.post('/api/pronunciation/occurrences/save')
async def save_occurrence_rules_endpoint(request: PronunciationOccurrenceEdit):
    return await asyncio.to_thread(save_occurrence_rules, request)


class DateOrderChoice(BaseModel):
    choice: Literal['auto', 'mdy', 'dmy']
    expected_book_token: str = Field(pattern=r'^[0-9a-f]{64}$')


INVALID_STATE_DETAIL = ('Book state (state.json) is unreadable or not an object; restore it from a saved book '
                        'or fix it by hand before choosing a date order.')


def _get_date_order_view_locked(data_dir):
    """The status and the token from one book selection; caller holds ensure_book_state."""
    try:
        token = get_book_operation_token(data_dir)
    except ValueError:
        raise HTTPException(status_code=422, detail=INVALID_STATE_DETAIL)
    return {**get_book_date_order_status(data_dir), 'book_token': token}


def get_date_order_view():
    """The active book's slash-date order: the choice, what auto found, and why (#1546)."""
    data_dir = os.path.dirname(SCRIPT_PATH)
    # A book switch between reading the status and the token would pair one book's
    # order with another's token, and a save against that token would then succeed.
    with ensure_book_state(data_dir):
        return _get_date_order_view_locked(data_dir)


@router.get('/api/pronunciation/date_order')
async def get_date_order_endpoint():
    return await asyncio.to_thread(get_date_order_view)


@router.post('/api/pronunciation/date_order')
async def save_date_order_endpoint(request: DateOrderChoice):
    return await asyncio.to_thread(save_date_order, request)


def save_date_order(request):
    """Record the reader's choice in the active book state; ambiguous dates follow it.

    The choice applies to renders from now on; audio already rendered and its
    history are left as they are.
    """
    data_dir = os.path.dirname(SCRIPT_PATH)
    state_path = os.path.join(data_dir, 'state.json')
    with ensure_book_state(data_dir), file_lock(state_path):
        if _get_date_order_view_locked(data_dir)['book_token'] != request.expected_book_token:
            raise HTTPException(status_code=409, detail='Active book changed; reload before choosing a date order.')
        # Preparation must not change under a render that already prepared its text.
        if is_task_running('audio', process_state):
            raise HTTPException(status_code=409, detail='Audio is generating; change the date order after it finishes.')
        state = safe_load_json(state_path, default={})
        if not isinstance(state, dict):
            raise HTTPException(status_code=422, detail=INVALID_STATE_DETAIL)
        updated = {key: value for key, value in state.items() if key != DATE_ORDER_KEY}
        if request.choice != 'auto':
            updated[DATE_ORDER_KEY] = request.choice
        atomic_json_write(updated, state_path, allow_nonatomic_fallback=False)
        return _get_date_order_view_locked(data_dir)
