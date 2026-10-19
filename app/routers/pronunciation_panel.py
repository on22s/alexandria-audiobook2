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

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictInt, field_validator

import pronunciation
from audio_validation import validate_generated_audio
from book_state_transaction import ensure_book_state
from core import project_manager, run_claimed_task_worker, process_state
from routers.editor import check_chunk_identity, SCRIPT_PATH
from project import get_speaker
from tts import (get_resolved_speaker, is_voice_config_present, resolve_narrator_voice_config,
                 voice_config_for_chunk)
from utils import atomic_json_write, file_lock

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
        chunks = project_manager.load_chunks()
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
