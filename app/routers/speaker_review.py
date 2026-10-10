"""Native, explicitly started speaker-label reviews and individually approved aliases."""
import asyncio
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt

import core
from book_state_transaction import ensure_book_state
from config_settings import load_app_config
from generation_checkpoint_deltas import load_generation_delta_checkpoint
from lmstudio_settings import get_active_llm_config
from routers.scripts_library import _build_saved_scripts_list, _require_saved_book_name
from speaker_identity import get_validated_alias_graph
from speaker_review import (SCHEMA, SOURCE_REVISION, PROMPT, MODES, MAX_PAIRS, MAX_ATTEMPTS,
    WARNING, digest, validate_entries, get_checkpoint_prefix, get_candidates,
    get_profile_settings, get_native_entries, require_prompt_capacity, review_key, require_alias_pair,
    validate_verdict, make_reviewer)
from utils import atomic_json_write, file_lock
from task_ownership import (acquire_task_slot, ensure_task_ownership_directory, TaskOwnershipBusy)

router = APIRouter(prefix='/api/speaker_review', tags=['speaker_review'])
TASK = 'speaker_review'
LIMITS = {'max_pairs': MAX_PAIRS, 'max_attempts': MAX_ATTEMPTS, 'modes': list(MODES)}
MAX_INPUT_BYTES = 128 * 1024 * 1024
TOKEN_PATTERN = r'^[0-9a-f]{64}$'


class Selection(BaseModel):
    model_config = {'extra': 'forbid'}
    source: Literal['current', 'checkpoint'] = 'current'
    reference_name: str = Field(min_length=1, max_length=255)
    max_pairs: StrictInt = Field(default=20, ge=1, le=MAX_PAIRS)
    allow_partial: StrictBool = False


class StartRequest(Selection):
    snapshot: str = Field(pattern=TOKEN_PATTERN)
    allow_network: StrictBool = False


class ApplyRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    snapshot: str = Field(pattern=TOKEN_PATTERN)
    candidate_id: str = Field(pattern=TOKEN_PATTERN)
    alias: str = Field(min_length=1, max_length=1024)
    canonical: str = Field(min_length=1, max_length=1024)


def _checkpoint_path():
    return core.SCRIPT_PATH + '.threepass_checkpoint.json'


def _runs_dir():
    root = Path(core.DATA_DIR) / '.speaker_review'
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise ValueError('Review storage must not be a symlink')
    return root


def _run_path(run_id):
    if not isinstance(run_id, str) or re.fullmatch(TOKEN_PATTERN, run_id) is None:
        raise HTTPException(status_code=400, detail='Invalid speaker-review run')
    path = _runs_dir() / (run_id + '.json')
    if path.is_symlink():
        raise ValueError('Review artifacts must not be symlinks')
    return path


def _read_bytes(path, missing=False):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Review inputs must not be symlinks')
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except FileNotFoundError:
        if missing:
            return None
        raise ValueError('A selected review input is no longer available') from None
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('Review input exceeds the bounded 128 MiB size')
    return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


@contextlib.contextmanager
def _inputs_locked(reference_name=None):
    """Match native book/saved-book order; hold compare-and-swap through alias write."""
    with ensure_book_state(core.DATA_DIR), ensure_book_state(core.SCRIPTS_DIR), contextlib.ExitStack() as locks:
        # Match voice edits: chunks before voices. A checkpoint reader is told it is locked.
        paths = [core.SCRIPT_PATH, core.CHUNKS_PATH, core.VOICE_CONFIG_PATH,
                 core.CHARACTER_ALIASES_PATH, core.CONFIG_PATH, _checkpoint_path()]
        if reference_name is not None:
            safe = _require_saved_book_name(reference_name)
            paths.append(os.path.join(core.SCRIPTS_DIR, safe + '.json'))
        state_raw = _read_bytes(Path(core.DATA_DIR) / 'state.json', missing=True)
        state = json.loads(state_raw) if state_raw else {}
        input_path = state.get('input_file_path') if isinstance(state, dict) else None
        if isinstance(input_path, str) and input_path and Path(input_path).is_file():
            paths.append(input_path)
        for path in dict.fromkeys(paths):
            locks.enter_context(file_lock(path))
        yield


def _source_data(source, state, script_raw, checkpoint):
    if source == 'current':
        if script_raw is None:
            raise ValueError('Generate or load an active script first')
        entries = json.loads(script_raw)
        authoritative = (checkpoint.get('segmented') if isinstance(checkpoint, dict)
                         and checkpoint.get('stage') == 'done' else None)
        entries = get_native_entries(entries, authoritative)
        if any(row.get('attribution_unchecked') for row in entries):
            raise ValueError('The active script contains unchecked attribution; use its completed checkpoint prefix')
        frozen = [{'text': row['text'], 'type': row['type']} for row in entries]
        return frozen, [{**row, 'entry_index': index} for index, row in enumerate(entries)]
    if not isinstance(checkpoint, dict):
        raise ValueError('No valid three-pass attribution checkpoint is available')
    input_path = state.get('input_file_path')
    if not input_path or not Path(input_path).is_file():
        raise ValueError('Checkpoint source is unavailable; regenerate or use the active script')
    from three_pass_generate import get_prepared_source
    expected = (checkpoint.get('fingerprint') or {}).get('source_sha256')
    # Native generation has an explicit strip-front-matter choice; accept either exact
    # native preprocessing identity, never fuzzy text or a different selected book.
    matches = False
    for strip in (True, False):
        text, _report = get_prepared_source(input_path, strip_front_matter=strip)
        if hashlib.sha256(text.encode('utf-8')).hexdigest() == expected:
            matches = True
            break
    if not matches:
        raise ValueError('Attribution checkpoint belongs to a different or changed source book')
    return get_checkpoint_prefix(checkpoint)


def _capture_locked(selection):
    state_raw = _read_bytes(Path(core.DATA_DIR) / 'state.json', missing=True)
    state = json.loads(state_raw) if state_raw is not None else {}
    if not isinstance(state, dict):
        raise ValueError('Book state must be an object')
    script_raw = _read_bytes(core.SCRIPT_PATH, missing=True)
    checkpoint = (load_generation_delta_checkpoint(_checkpoint_path(), locked=True)
                  if Path(_checkpoint_path()).exists() else None)
    source, prediction = _source_data(selection['source'], state, script_raw, checkpoint)
    reference_path = os.path.join(core.SCRIPTS_DIR, _require_saved_book_name(selection['reference_name']) + '.json')
    reference_raw = _read_bytes(reference_path)
    reference = get_native_entries(json.loads(reference_raw))
    candidates, skipped, complete = get_candidates(source, prediction, reference,
        selection['allow_partial'], selection['max_pairs'])
    config = load_app_config(core.CONFIG_PATH)
    profile = copy.deepcopy(get_active_llm_config(config))
    try:
        public_profile, settings = get_profile_settings(config, profile)
        public_profile.update(available=True, reason=None)
    except ValueError as exc:
        public_profile = {'model': '', 'endpoint': '', 'available': False, 'reason': str(exc)}
        settings = {'profile_digest': digest(profile), 'llm_mode': config.get('llm_mode'),
                    'llm_failover': config.get('llm_failover'), 'unavailable': str(exc),
                    'context_length': 4096}
    admitted = []
    for candidate in candidates:
        try:
            if public_profile['available']:
                require_prompt_capacity(candidate, settings)
        except ValueError:
            skipped.append({'entry_index': candidate['entry_index'], 'reason': 'context_limit'})
        else:
            admitted.append(candidate)
    candidates = admitted
    aliases_raw = _read_bytes(core.CHARACTER_ALIASES_PATH, missing=True)
    aliases = get_validated_alias_graph(json.loads(aliases_raw) if aliases_raw is not None else {})
    input_raw = (_read_bytes(state['input_file_path'], missing=True)
                 if isinstance(state.get('input_file_path'), str) and state['input_file_path'] else None)
    provenance = {'schema': SCHEMA, 'source_revision': SOURCE_REVISION,
        'book': {key: state.get(key) for key in ('active_book_id', 'input_file_path', 'book_generation')},
        'script_sha256': _sha(script_raw), 'source_sha256': _sha(input_raw),
        'checkpoint_sha256': digest(checkpoint), 'reference_sha256': _sha(reference_raw),
        'aliases_sha256': _sha(aliases_raw),
        'chunks_sha256': _sha(_read_bytes(core.CHUNKS_PATH, missing=True)),
        'voices_sha256': _sha(_read_bytes(core.VOICE_CONFIG_PATH, missing=True)),
        'settings': settings, 'prompt_sha256': digest(PROMPT),
        'evidence_sha256': digest(candidates), 'selection': selection}
    snapshot = digest(provenance)
    book_id = state.get('active_book_id') or Path(state.get('input_file_path') or '').stem or 'active_book'
    public = {'snapshot': snapshot, 'book_id': book_id, **selection,
        'phase': 'reconciled' if complete else 'provisional', 'warning': WARNING + ' This is a stopped-run snapshot review; generation must be idle, and any source or checkpoint changes require a new preview.',
        'candidates': [{**candidate, 'can_apply': False,
                        'apply_refusal': 'Both review modes must first agree'} for candidate in candidates],
        'skipped': skipped[:500], 'skipped_count': len(skipped),
        'call_count': len(candidates) * len(MODES), 'profile': public_profile, 'limits': LIMITS,
        'entries': len(prediction), 'total': len(source),
        'source_alignment': ('frozen generation text/type' if selection['source'] == 'checkpoint' or
                             isinstance(checkpoint, dict) and checkpoint.get('stage') == 'done'
                             else 'active script text; missing types inferred from native narrator labels'),
        'reference_alignment': 'saved script text; missing types inferred from native narrator labels'}
    return {'public': public, 'provenance': provenance, 'profile': profile,
            'aliases': aliases, 'selection': selection}


def _capture(selection):
    with _inputs_locked(selection['reference_name']):
        return _capture_locked(selection)


def _read_run(run_id):
    path = _run_path(run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail='Speaker-review run not found')
    record = json.loads(_read_bytes(path))
    if (not isinstance(record, dict) or record.get('schema') != SCHEMA
            or record.get('run_id') != run_id or record.get('snapshot') != run_id
            or type(record.get('attempts')) is not int or not 0 <= record['attempts'] <= MAX_ATTEMPTS
            or not isinstance(record.get('cache'), dict)
            or not isinstance(record.get('reviews'), list)):
        raise ValueError('Invalid speaker-review cache; it cannot be resumed or applied')
    for value in record['cache'].values():
        validate_verdict(value)
    return record


def _write_run(record):
    record['updated_at'] = time.time()
    atomic_json_write(record, str(_run_path(record['run_id'])), allow_nonatomic_fallback=False)


def _cancel_path(run_id):
    return _run_path(run_id).with_suffix('.cancel')


def _is_owned(run_id):
    """Tie the durable latest-run identity to a live kernel task lease, across workers."""
    if core.is_task_running(TASK) and core.process_state[TASK].get('review_run_id') == run_id:
        return True
    index = _runs_dir() / 'latest.json'
    if not index.exists():
        return False
    latest = json.loads(_read_bytes(index))
    if not isinstance(latest, dict) or latest.get('run_id') != run_id:
        return False
    try:
        probe = acquire_task_slot(ensure_task_ownership_directory(core.DATA_DIR), TASK)
    except TaskOwnershipBusy:
        return True
    else:
        probe.close()
        return False


def _public_run(record, stale=False, aliases=None):
    result = {key: copy.deepcopy(value) for key, value in record.items()
              if key not in ('cache', 'provenance', 'selection')}
    result['stale'] = stale or bool(record.get('applied'))
    if result['status'] == 'running':
        if not _is_owned(record['run_id']):
            result['status'] = 'interrupted'
        elif _cancel_path(record['run_id']).exists():
            # Keep polling the live owner until it confirms cancellation.
            result['cancel_requested'] = True
    if result['stale']:
        result['status'] = 'stale'
    for candidate in result['candidates']:
        directions = []
        for direction, alias, canonical in (
                ('forward', candidate['prediction_label'], candidate['reference_label']),
                ('reverse', candidate['reference_label'], candidate['prediction_label'])):
            refusal = None
            try:
                if result['stale'] or result['status'] != 'completed':
                    raise ValueError('This review is stale or unfinished; preview and review again')
                require_alias_pair(candidate, record['reviews'], alias, canonical, aliases or {})
            except ValueError as exc:
                refusal = str(exc)
            directions.append({'direction': direction, 'alias': alias, 'canonical': canonical,
                               'can_apply': refusal is None, 'apply_refusal': refusal})
        allowed = any(item['can_apply'] for item in directions)
        candidate.update(apply_directions=directions, can_apply=allowed,
                         apply_refusal=None if allowed else directions[0]['apply_refusal'])
    return result


def _options():
    sources = []
    with _inputs_locked():
        state_raw = _read_bytes(Path(core.DATA_DIR) / 'state.json', missing=True)
        state = json.loads(state_raw) if state_raw else {}
        if not isinstance(state, dict):
            raise ValueError('Invalid book state')
        script_raw = _read_bytes(core.SCRIPT_PATH, missing=True)
        checkpoint = (load_generation_delta_checkpoint(_checkpoint_path(), locked=True)
                      if Path(_checkpoint_path()).exists() else None)
        for source, label in (('current', 'Current script'), ('checkpoint', 'Completed attribution prefix')):
            item = {'id': source, 'label': label, 'available': False, 'reason': None,
                    'entries': 0, 'total': 0, 'complete': False}
            try:
                frozen, prediction = _source_data(source, state, script_raw, checkpoint)
            except (ValueError, OSError) as exc:
                item['reason'] = str(exc)
            else:
                item.update(available=True, entries=len(prediction), total=len(frozen),
                            complete=len(prediction) == len(frozen))
            sources.append(item)
        config = load_app_config(core.CONFIG_PATH)
        try:
            profile, _settings = get_profile_settings(config, get_active_llm_config(config))
            profile.update(available=True, reason=None)
        except ValueError as exc:
            profile = {'model': '', 'endpoint': '', 'available': False, 'reason': str(exc)}
        result = {'book_id': state.get('active_book_id') or Path(state.get('input_file_path') or '').stem or 'active_book',
                  'sources': sources, 'references': _build_saved_scripts_list(),
                  'profile': profile, 'limits': LIMITS, 'active_run': None, 'recent_run': None}
    live = core.process_state[TASK]
    if core.is_task_running(TASK) and live.get('review_run_id'):
        result['active_run'] = {'run_id': live['review_run_id'], 'book_id': live.get('book_id'), 'status': 'running'}
    # A single compact index gives reload/restart recovery without scanning private reports.
    index = Path(core.DATA_DIR) / '.speaker_review' / 'latest.json'
    if index.exists():
        latest = json.loads(_read_bytes(index))
        if isinstance(latest, dict):
            if latest.get('book_id') == result['book_id']:
                result['recent_run'] = latest
            if isinstance(latest.get('run_id'), str) and _is_owned(latest['run_id']):
                result['active_run'] = {**latest, 'status': 'running'}
    return result


def _selection(request):
    return {key: getattr(request, key) for key in Selection.model_fields}


def _start(request, background_tasks):
    if request.allow_network is not True:
        raise HTTPException(status_code=400, detail='Explicit model/network review consent is required')
    selection = _selection(request)
    captured = _capture(selection)
    if captured['public']['snapshot'] != request.snapshot:
        raise HTTPException(status_code=409, detail='Book, evidence, model profile, or aliases changed; preview again')
    if not captured['public']['profile']['available']:
        raise HTTPException(status_code=409, detail=captured['public']['profile']['reason'])
    if not captured['public']['candidates']:
        raise HTTPException(status_code=409, detail='No bounded, uniquely matched label pairs are available')
    run_id = request.snapshot
    path = _run_path(run_id)
    claim = core.reserve_background_task(TASK)
    try:
        with file_lock(path, timeout=0):
            if path.exists():
                record = _read_run(run_id)
                if record.get('applied'):
                    raise HTTPException(status_code=409, detail='An alias was already applied; preview again')
            else:
                record = {'schema': SCHEMA, **captured['public'], 'run_id': run_id,
                    'status': 'interrupted', 'attempts': 0, 'reviews': [], 'cache': {}, 'applied': [],
                    'provenance': captured['provenance'], 'selection': selection, 'created_at': time.time()}
            # Only a new explicit start under the acquired lease may clear cancellation.
            _cancel_path(run_id).unlink(missing_ok=True)
            record.update(status='running', error=None)
            _write_run(record)
            core.process_state[TASK].update(review_run_id=run_id, book_id=record['book_id'],
                status='running', logs=['Reviewing explicitly selected speaker evidence.'])
            atomic_json_write({'run_id': run_id, 'book_id': record['book_id'], 'status': 'running'},
                              str(_runs_dir() / 'latest.json'), allow_nonatomic_fallback=False)
            core.register_claimed_background_task(background_tasks, TASK, claim, _run_review,
                                                 run_id, captured['profile'])
    except BaseException:
        core.release_gpu_task_claim(TASK, claim, pending_only=True)
        raise
    return {'run_id': run_id, 'status': 'running'}


class ReviewStopped(Exception):
    pass


class ReviewStale(Exception):
    pass


def _require_current(record):
    if core.process_state[TASK].get('cancel') or _cancel_path(record['run_id']).exists():
        raise ReviewStopped()
    try:
        captured = _capture(record['selection'])
    except (ValueError, OSError, HTTPException):
        raise ReviewStale() from None
    if captured['public']['snapshot'] != record['snapshot']:
        raise ReviewStale()
    return captured


def _run_review(run_id, profile):
    client = None
    record = None
    try:
        with file_lock(_run_path(run_id), timeout=0):
            record = _read_run(run_id)
            _require_current(record)
            record['reviews'] = []
            settings = record['provenance']['settings']
            for candidate in record['candidates']:
                for mode in MODES:
                    _require_current(record)
                    key = review_key(candidate, mode, settings)
                    result = {'key': key, 'candidate_id': candidate['id'],
                              'entry_index': candidate['entry_index'], 'mode': mode}
                    if key in record['cache']:
                        result.update(verdict=record['cache'][key], cached=True)
                    elif record['attempts'] >= MAX_ATTEMPTS:
                        result['error'] = 'The 40-request lifetime limit has been reached'
                    else:
                        if client is None:
                            client, reviewer = make_reviewer(profile)
                        # Durable pre-dispatch accounting survives interruption without overspend.
                        record['attempts'] += 1
                        _write_run(record)
                        _require_current(record)
                        try:
                            verdict = reviewer(candidate, mode)
                            validate_verdict(verdict)
                        except Exception as exc:
                            # Provider errors can contain credentials and full private payloads.
                            result['error'] = type(exc).__name__
                        else:
                            record['cache'][key] = verdict
                            result.update(verdict=verdict, cached=False)
                    record['reviews'].append(result)
                    _write_run(record)
            _require_current(record)
            record['status'] = 'completed'
    except ReviewStopped:
        if record is not None:
            record.update(status='cancelled', error='Review cancelled; no aliases were applied')
    except ReviewStale:
        if record is not None:
            record.update(status='stale', error='Book, evidence, model profile, or aliases changed; preview again')
    except Exception as exc:
        if record is not None:
            record.update(status='failed', error=type(exc).__name__)
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            if record is not None:
                _write_run(record)
                core.process_state[TASK]['status'] = record['status']
                core.process_state[TASK]['logs'] = [record.get('error') or 'Speaker-label review finished.']
                atomic_json_write({'run_id': run_id, 'book_id': record['book_id'], 'status': record['status']},
                                  str(_runs_dir() / 'latest.json'), allow_nonatomic_fallback=False)


def _get_run(run_id):
    current = None
    record = _read_run(run_id)
    try:
        current = _capture(record['selection'])
        stale = current['public']['snapshot'] != record['snapshot']
    except (ValueError, OSError, HTTPException):
        stale = True
    return _public_run(record, stale, current['aliases'] if current is not None else {})


def _cancel(run_id):
    record = _read_run(run_id)
    # This marker does not wait for the report lock held by an in-flight request.
    # Every app worker observes it before dispatching the next request.
    if _is_owned(run_id):
        atomic_json_write({'cancel_requested': True}, str(_cancel_path(run_id)),
                          allow_nonatomic_fallback=False)
        with core._gpu_lock:
            if core.process_state[TASK].get('review_run_id') == run_id:
                core.process_state[TASK]['cancel'] = True
        return {'status': 'cancelling'}
    return {'status': 'interrupted' if record['status'] == 'running' else record['status']}


def _apply(run_id, request):
    with core._gpu_lock:
        if core.is_task_running(TASK):
            raise HTTPException(status_code=409, detail='Wait for the speaker review to finish before applying')
        # Registry changes must not race generation/review or nickname discovery.
        busy = [name for name in core.LLM_TASKS if core.is_task_running(name)]
        if busy:
            raise HTTPException(status_code=409, detail='Wait for model tasks to finish before saving an alias')
        try:
            lease = core.acquire_task_lease(core.DATA_DIR, TASK, core.LLM_TASKS)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail='Another app worker owns a model task; wait before saving an alias') from exc
        with contextlib.closing(lease):
            with file_lock(_run_path(run_id), timeout=0):
                record = _read_run(run_id)
                with _inputs_locked(record['selection']['reference_name']):
                    captured = _capture_locked(record['selection'])
                    if (request.snapshot != record['snapshot'] or captured['public']['snapshot'] != record['snapshot']
                            or record['status'] != 'completed' or record.get('applied')):
                        raise HTTPException(status_code=409, detail='This review changed or is stale; preview and review again')
                    candidate = next((item for item in captured['public']['candidates']
                                      if item['id'] == request.candidate_id), None)
                    if candidate is None:
                        raise HTTPException(status_code=409, detail='The selected candidate is no longer relevant')
                    updated = require_alias_pair(candidate, record['reviews'], request.alias,
                                                 request.canonical, captured['aliases'])
                    # Same graph validator, registry lock and atomic write as the native alias
                    # editor, with all book/evidence checks still inside their shared locks.
                    atomic_json_write(updated, core.CHARACTER_ALIASES_PATH, allow_nonatomic_fallback=False)
                    record['applied'] = [candidate['id']]
                    record['status'] = 'stale'
                    _write_run(record)
                    return {'status': 'saved', 'alias': request.alias, 'canonical': request.canonical,
                            'run_id': run_id, 'applied': record['applied']}


async def _dispatch(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail='Book or review is busy; retry after the current operation') from exc
    except (ValueError, OSError) as exc:
        # Input validation errors are our own bounded messages; I/O paths stay private.
        detail = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Review input is unavailable or invalid'
        raise HTTPException(status_code=409, detail=detail) from exc


@router.get('/options')
async def options():
    return await _dispatch(_options)


@router.post('/preview')
async def preview(request: Selection):
    captured = await _dispatch(_capture, _selection(request))
    return captured['public']


@router.post('/start')
async def start(request: StartRequest, background_tasks: BackgroundTasks):
    return await _dispatch(_start, request, background_tasks)


@router.get('/{run_id}')
async def get_run(run_id: str):
    return await _dispatch(_get_run, run_id)


@router.post('/{run_id}/cancel')
async def cancel(run_id: str):
    return await _dispatch(_cancel, run_id)


@router.post('/{run_id}/apply')
async def apply(run_id: str, request: ApplyRequest):
    return await _dispatch(_apply, run_id, request)
