"""Snapshot-bound character review; bounded bodies and filesystem work on workers."""
import asyncio
import json
from pathlib import Path
import re

from fastapi import APIRouter, HTTPException, Query, Request, Response
import core
from book_state_transaction import get_snapshot_digest
from character_roster import validate_character_roster_proposal, validate_character_roster
from character_roster_import import get_roster_import_reconciliation
from character_roster_apply import (ensure_character_roster_admission, apply_character_roster,
    apply_character_roster_undo, get_character_roster_undo_status, discard_character_roster_undo,
    get_character_roster_error_detail)
from character_roster_snapshot import (MAX_ROSTER_BYTES, DOMAIN, get_character_roster_snapshot,
    get_character_roster_preview, get_roster_json_bytes, is_character_roster_approval_current)
from project_scope_admission import ensure_scope_admission
from source_evidence import MAX_SOURCE_CHARS, MAX_QUOTE_CHARS, _require_keys, _require_text
from json_metadata import get_unique_json_object as _get_unique_object

router = APIRouter(prefix='/api/character_roster')


def _require_revision(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('A current character review revision is required')


def _require_context(snapshot, expected):
    _require_revision(expected)
    if get_snapshot_digest(snapshot) != expected:
        raise ValueError('Book, source or cast changed; load the character review again')


def _get_metadata(root):
    snapshot, source, data, saved = get_character_roster_snapshot(root)
    cast = []
    if data['cast'] is not None:
        from three_pass_generate import get_cast_from_data
        try:
            current = get_cast_from_data(json.loads(data['cast']))
        except (ValueError, TypeError):
            raise ValueError('Current cast needs inspection in the cast editor') from None
        cast = [{'name': name, 'aliases': group[1:]}
                for name, group in zip(current['names'], current['alias_groups'])]
    return {'context_revision': get_snapshot_digest(snapshot), 'book_token': snapshot['book_token'],
            'undo_revision': snapshot['files']['undo']['sha256'],
            'source_name': Path(snapshot['source']['absolute_path']).name,
            'source_sha256': snapshot['source_binding']['prepared_text_sha256'],
            'source_characters': len(source), 'offset_domain': DOMAIN, 'cast': cast,
            'roster': saved['roster'] if saved is not None else None,
            'last_review': saved.get('approval') if saved is not None else None,
            'approval_current': is_character_roster_approval_current(snapshot, data, saved)}


def _get_public_preview(plan):
    return {'context_revision': get_snapshot_digest(plan['snapshot']), 'revision': plan['revision'],
            'book_token': plan['snapshot']['book_token'], 'source_name': plan['source_name'],
            'source_sha256': plan['snapshot']['source_binding']['prepared_text_sha256'],
            'offset_domain': DOMAIN, 'cast_changed': plan['cast_changed'],
            'undo_available': plan['snapshot']['files']['undo']['exists'],
            'approval_current': plan['approval_current'], 'review': plan['review']}


def _get_source_matches(root, context, quote, offset, length):
    snapshot, source, _data, _saved = get_character_roster_snapshot(root, load_saved=False)
    _require_context(snapshot, context)
    if offset > len(source):
        raise ValueError('Source offset lies outside the current source')
    result = {'context_revision': context, 'book_token': snapshot['book_token'], 'offset_domain': DOMAIN}
    if not quote:
        end = min(offset + length, len(source))
        return {**result, 'start_char': offset, 'end_char': end, 'text': source[offset:end],
                'next_offset': end if end < len(source) else None, 'matches': []}
    _require_text(quote, MAX_QUOTE_CHARS, 'Quote')
    matches = []; position = source.find(quote, offset)
    while position >= 0 and len(matches) < 20:
        end = position + len(quote)
        matches.append({'start_char': position, 'end_char': end, 'source_quote': quote,
                        'before': source[max(0, position - 80):position], 'after': source[end:end + 80]})
        position = source.find(quote, position + 1)
    return {**result, 'matches': matches, 'next_offset': position if position >= 0 else None}


def _run_operation(operation, raw=None, selection=None):
    try:
        payload = None
        if raw is not None:
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_get_unique_object)
            keys = ('proposal', 'imported_roster', 'import_id_mapping', 'context_revision') if operation in ('preview', 'apply') else ('expected_revision',)
            if operation == 'source':
                keys = ('context_revision', 'quote', 'offset')
            if operation == 'apply':
                keys += ('expected_revision',)
            _require_keys(payload, keys, 'Character request')
            # Joint byte/field/count limits apply; reject NaN/deep invalid JSON
            # before any source scan, task admission or application mutation.
            get_roster_json_bytes(payload)
            if operation in ('undo', 'discard'):
                _require_revision(payload['expected_revision'])
            if operation == 'source':
                _require_revision(payload['context_revision'])
                _require_text(payload['quote'], MAX_QUOTE_CHARS, 'Quote')
                if type(payload['offset']) is not int or not 0 <= payload['offset'] <= MAX_SOURCE_CHARS:
                    raise ValueError('Source offset is out of bounds')
                selection = (payload['context_revision'], payload['quote'], payload['offset'], 2000)
            if operation in ('preview', 'apply'):
                validate_character_roster_proposal(payload['proposal'])
                if payload['imported_roster'] is not None:
                    validate_character_roster(payload['imported_roster'])
                if payload['import_id_mapping'] is not None:
                    if payload['imported_roster'] is None:
                        raise ValueError('An ID mapping requires an imported roster')
                    get_roster_import_reconciliation(payload['imported_roster'], payload['import_id_mapping'], payload['proposal']['entries'])
        admission = ensure_character_roster_admission if operation in ('apply', 'undo') else ensure_scope_admission
        with admission(core.DATA_DIR):
            if operation == 'metadata':
                result = _get_metadata(core.DATA_DIR)
            elif operation == 'source':
                result = _get_source_matches(core.DATA_DIR, *selection)
            elif operation == 'undo_status':
                result = get_character_roster_undo_status(core.DATA_DIR)
            elif operation == 'preview':
                plan = get_character_roster_preview(core.DATA_DIR, payload['proposal'], payload['imported_roster'], payload['import_id_mapping'])
                _require_context(plan['snapshot'], payload['context_revision'])
                result = _get_public_preview(plan)
            elif operation == 'apply':
                snapshot = get_character_roster_snapshot(core.DATA_DIR, load_saved=False)[0]
                _require_context(snapshot, payload['context_revision'])
                result = apply_character_roster(core.DATA_DIR, payload['proposal'], payload['expected_revision'], payload['imported_roster'], payload['import_id_mapping'])
                result = {**result, 'book_token': snapshot['book_token'],
                          'source_sha256': snapshot['source_binding']['prepared_text_sha256']}
            elif operation == 'undo':
                result = apply_character_roster_undo(core.DATA_DIR, payload['expected_revision'])
            elif operation == 'discard':
                result = discard_character_roster_undo(core.DATA_DIR, payload['expected_revision'])
            else:
                raise ValueError('Unknown character review operation')
            encoded = get_roster_json_bytes(result, 16 * 1024**2)
            return Response(content=encoded, media_type='application/json')
    except TimeoutError as error:
        raise HTTPException(status_code=409, detail='Book or cast is busy; try again after the current operation') from error
    except (ValueError, OSError, RecursionError) as error:
        detail = get_character_roster_error_detail(error)
        raise HTTPException(status_code=409, detail=detail) from error


async def _dispatch(operation, request=None, selection=None):
    raw = None
    if request is not None:
        parts = []; size = 0
        async for part in request.stream():
            size += len(part)
            if size > MAX_ROSTER_BYTES:
                raise HTTPException(status_code=413, detail='Character review request exceeds its 2 MiB limit')
            parts.append(part)
        raw = b''.join(parts)
    return await asyncio.to_thread(_run_operation, operation, raw, selection)


@router.get('')
async def get_character_roster():
    return await _dispatch('metadata')


@router.get('/source')
async def get_character_source(context_revision: str = Query(pattern=r'^[0-9a-f]{64}$'),
                               quote: str = Query(default='', max_length=MAX_QUOTE_CHARS),
                               offset: int = Query(default=0, ge=0, le=MAX_SOURCE_CHARS),
                               length: int = Query(default=2000, ge=1, le=8000)):
    return await _dispatch('source', selection=(context_revision, quote, offset, length))


@router.post('/preview')
async def preview_character_roster(request: Request):
    return await _dispatch('preview', request)


@router.post('/source')
async def find_character_source(request: Request):
    # Long Unicode quotes exceed ordinary HTTP request-line limits as query
    # strings. Keep the same quote limit in a bounded, worker-validated body.
    return await _dispatch('source', request)


@router.post('/apply')
async def approve_character_roster(request: Request):
    return await _dispatch('apply', request)


@router.get('/undo')
async def get_character_undo():
    return await _dispatch('undo_status')


@router.post('/undo')
async def undo_character_roster(request: Request):
    return await _dispatch('undo', request)


@router.post('/discard_undo')
async def discard_character_undo(request: Request):
    return await _dispatch('discard', request)
