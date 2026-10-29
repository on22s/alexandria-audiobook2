from book_state_transaction import ensure_saved_book_state
from chunk_status_journal import get_chunk_status_journal_path
from generation_checkpoint_shards import get_generation_checkpoint_artifacts
import hashlib
import json
import asyncio
import logging
import os
import shutil
import uuid
import contextlib
from pathlib import Path
from book_state_transaction import (ensure_book_state, apply_book_state_locked,
                                    get_book_operation_token)
from publication_source import (get_publication_source_path, get_publication_source_capture,
                                get_saved_publication_source_bytes)

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import (
    AUDIOBOOK_PATH,
    CHUNKS_PATH,
    DATA_DIR,
    M4B_PATH,
    SCRIPT_PATH,
    SCRIPTS_DIR,
    UPLOADS_DIR,
    VOICE_CONFIG_PATH,
    _get_saved_book_id,
    _gpu_lock, ensure_book_switch_allowed, ensure_saved_book_destination_available,
    _require_safe_filename,
    _save_active_book_id,
    _saved_book_meta_path,
    get_active_book_id,
    process_state, is_task_running,
)
from project import CHAPTER_EXPORT_DIR
from review_script import _checkpoint_path, clear_checkpoint
from completed_review_receipt import get_review_receipt_path
from generate_script import get_generation_checkpoint_path, get_generation_quality_path
from three_pass_generate import three_pass_checkpoint_path, three_pass_manifest_path
from script_preflight import audit_script
from script_repair import build_deterministic_repair
from script_selection_binding import (BINDING_NAME, MAX_BINDING_BYTES,
    capture_generation_selection, check_binding_source, parse_script_binding,
    make_script_selection_binding, updated_saved_binding, _read_selection_state,
    saved_script_is_active_source)
from editor_poll_snapshot import get_snapshot_digest
from completed_review_receipt import get_review_entries_fingerprint
from speaker_repair import apply_speaker_selections, build_speaker_review
from content_repair import apply_content_selections, build_content_review
from utils import (atomic_json_write, backup_file_with_timestamp, file_lock,
                   get_timestamped_backup_path, is_generic_speaker, safe_load_json)
from sound_design_registry import (ACTIVE_CUES, get_saved_sound_design_path,
                                   MAX_REGISTRY_BYTES)
from project_scope_files import capture_scope_file
from speech_numbers import STATE_KEY as DATE_ORDER_KEY, DATE_ORDER_CHOICES
from sound_design_renditions import (ACTIVE_RENDITIONS, get_saved_sound_renditions_path,
                                     capture_sound_design_companions, MAX_RENDITION_BYTES)


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()
MAX_SCRIPT_BYTES = 512 * 1024**2


def _get_saved_book_companions(path):
    """Every known saved-book companion, including older checkpoint names."""
    stem = os.path.splitext(path)[0]
    return list(dict.fromkeys([
        stem + ".voice_config.json", stem + ".meta.json",
        get_saved_sound_design_path(path), get_saved_sound_renditions_path(path),
        get_publication_source_path(path),
        stem + '.selection_binding.json',
        get_review_receipt_path(path),
        *get_generation_checkpoint_artifacts(_checkpoint_path(path)),
        *get_generation_checkpoint_artifacts(get_generation_checkpoint_path(path)),
        get_generation_quality_path(path),
        *get_generation_checkpoint_artifacts(three_pass_checkpoint_path(path)),
        three_pass_manifest_path(path), path + ".checkpoint.jsonl",
        *get_generation_checkpoint_artifacts(stem + ".generation_checkpoint.json"),
        *get_generation_checkpoint_artifacts(stem + ".threepass_checkpoint.json"),
        *get_generation_checkpoint_artifacts(stem + ".review_checkpoint.json"),
        *[stem + suffix for suffix in (
            ".generation_checkpoint.json", ".generation_quality.json",
            ".threepass_checkpoint.json", ".threepass_manifest.json", ".checkpoint.jsonl")]]))


def _is_saved_book_filename(filename):
    companion_suffixes = tuple(os.path.basename(path)[len("book"):]
                               for path in _get_saved_book_companions("book.json")
                               if not os.path.basename(path).startswith('.'))
    return (filename.endswith(".json") and not filename.startswith(".")
            and not filename.endswith(companion_suffixes))


def _require_saved_book_name(name):
    safe = _require_safe_filename(name, "Invalid script name.")
    if safe != name or not _is_saved_book_filename(safe + ".json"):
        raise HTTPException(status_code=400, detail="Invalid or reserved script name.")
    return safe


def _capture_sound_companions(root, cue_relative, rendition_relative, book_id):
    try:
        return capture_sound_design_companions(root, cue_relative, rendition_relative,
                                               expected_book_id=book_id)
    except (OSError, ValueError):
        raise HTTPException(status_code=409, detail=(
            'Sound-design metadata requires inspection; existing book files were preserved.')) from None


def _validate_sound_destination(root, relative, *, limit):
    # Save may intentionally overwrite stale/corrupt content, but its existing
    # destination must still be a bounded, owned regular metadata file.
    try:
        capture_scope_file(root, relative, limit=limit)
    except (OSError, ValueError):
        raise HTTPException(status_code=409, detail=(
            'Sound-design destination requires inspection; existing book files were preserved.')) from None


@contextlib.contextmanager
def _saved_mutation_admission():
    """Do not wait for a book lock while holding the task mutex."""
    with contextlib.ExitStack() as admission:
        try:
            admission.enter_context(_gpu_lock)
            admission.enter_context(ensure_book_state(DATA_DIR, timeout=0))
            admission.enter_context(ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR, timeout=0))
        except TimeoutError as error:
            raise HTTPException(status_code=409, detail='Book is busy; try again.') from error
        yield


def _require_saved_book_idle():
    busy = [name for name in ("script", "review", "persona", "nicknames",
                             "batch_script", "batch_review")
            if is_task_running(name, process_state)]
    if busy:
        raise HTTPException(status_code=409, detail=(
            "Cannot change saved books while these tasks are running: " + ", ".join(busy)))


def _build_saved_scripts_list():
    """List all saved scripts in the scripts/ directory.

    Uses a whitelist approach: only includes .json files that do NOT end with
    any known companion/internal suffix (voice_config, metadata, checkpoint, etc.).
    """
    scripts = []
    for f in os.listdir(SCRIPTS_DIR):
        if not _is_saved_book_filename(f):
            continue
        name = f[:-5]  # strip .json
        filepath = os.path.join(SCRIPTS_DIR, f)
        companion = os.path.join(SCRIPTS_DIR, f"{name}.voice_config.json")
        try:
            created = os.path.getmtime(filepath)
        except OSError:
            # File vanished between listdir and stat (concurrent delete) - skip it.
            continue
        scripts.append({
            "name": name,
            "created": created,
            "has_voice_config": os.path.exists(companion)
        })
    scripts.sort(key=lambda x: x["created"], reverse=True)
    return scripts


def _list_saved_scripts_sync():
    try:
        with ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR, recover=False):
            return _build_saved_scripts_list()
    except (OSError, ValueError, TypeError) as error:
        raise HTTPException(status_code=409, detail=(
            "Saved books could not be read. Complete any pending library recovery "
            "before refreshing the list. No recovery was attempted.")) from error


@router.get("/api/scripts", operation_id="list_saved_scripts")
async def list_saved_scripts():
    """List saved scripts without blocking the FastAPI event loop on disk I/O."""
    return await asyncio.to_thread(_list_saved_scripts_sync)

def _get_book_workspace_sync():
    from book_workspace import get_book_workspace
    from routers.voices import get_voice_rows
    from core import project_manager

    try:
        with ensure_book_state(DATA_DIR, recover=False):
            return get_book_workspace(DATA_DIR, {"MP3": AUDIOBOOK_PATH, "M4B": M4B_PATH},
                                      get_voice_rows, project_manager.get_chunk_audio_path, export_states=process_state)
    except (OSError, ValueError, TypeError) as error:
        logger.warning("Book workspace inputs could not be read: %s", type(error).__name__)
        raise HTTPException(status_code=409, detail=(
            "Book workspace could not read the saved book. Complete any pending book recovery "
            "or inspect the script and voice settings. No recovery was attempted. "
            "No new chunks or audio were generated.")) from error


@router.get("/api/workspace")
async def get_book_workspace_status():
    """Summarize selected-book progress without initializing chunks or generating audio."""
    return await asyncio.to_thread(_get_book_workspace_sync)


class ScriptSaveRequest(BaseModel):
    name: str
    expected_book_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

def validate_book_json_bytes(raw, expected_type, status_code, label):
    """Refuse corrupt stored JSON before a book transaction publishes anything."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=status_code,
                            detail=f"{label} contains malformed JSON; repair the file before continuing.") from error
    if not isinstance(value, expected_type):
        shape = "an entry list" if expected_type is list else "an object"
        raise HTTPException(status_code=status_code, detail=f"{label} must contain {shape}.")


def _saved_repair_binding(path, old_bytes, new_bytes):
    """Carry an existing, verified saved binding across an admitted repair."""
    companion = os.path.splitext(path)[0] + '.selection_binding.json'
    try:
        _, raw = capture_scope_file(SCRIPTS_DIR, os.path.basename(companion),
                                    limit=MAX_BINDING_BYTES, include_bytes=True)
        if raw is None and saved_script_is_active_source(DATA_DIR, path):
            _, active_binding = capture_scope_file(DATA_DIR, BINDING_NAME,
                                                    limit=MAX_BINDING_BYTES, include_bytes=True)
            state = _read_selection_state(DATA_DIR, required=False)
            if active_binding is not None or state.get('script_binding_required') is True:
                raise ValueError('Active saved-script binding is missing')
        return updated_saved_binding(raw, old_bytes, new_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise HTTPException(status_code=409, detail='Saved script identity requires inspection; repair was not applied.') from error


def _publish_saved_repair(path, entries):
    if saved_script_is_active_source(DATA_DIR, path):
        raise HTTPException(status_code=409, detail=(
            'This saved book is currently loaded; switch books before repairing it.'))
    try:
        _, old = capture_scope_file(SCRIPTS_DIR, os.path.basename(path),
                                    limit=MAX_SCRIPT_BYTES, include_bytes=True)
        if old is None:
            raise ValueError('Saved script disappeared')
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=409, detail=(
            'Saved script identity requires inspection; repair was not applied.')) from error
    new = json.dumps(entries, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    binding = _saved_repair_binding(path, old, new)
    backup = get_timestamped_backup_path(path)
    if os.path.lexists(backup):
        raise HTTPException(status_code=409, detail='Repair backup already exists; retry the repair.')
    replacements = {os.path.basename(path):new, os.path.basename(backup):old}
    if binding is not None:
        replacements[os.path.basename(os.path.splitext(path)[0] + '.selection_binding.json')] = binding
    apply_book_state_locked(SCRIPTS_DIR, replacements, [])
    return backup


def _save_script_sync(request):
    safe_name = _require_saved_book_name(request.name)
    dest = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    with ensure_saved_book_destination_available(dest), ensure_book_state(DATA_DIR), ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR), contextlib.ExitStack() as locks:
        for path in (dest, SCRIPT_PATH, VOICE_CONFIG_PATH, os.path.join(DATA_DIR, BINDING_NAME),
                     get_review_receipt_path(SCRIPT_PATH),
                     *_get_saved_book_companions(dest)):
            locks.enter_context(file_lock(path))
        if not os.path.isfile(SCRIPT_PATH):
            raise HTTPException(status_code=404, detail="No annotated script to save. Generate a script first.")
        try:
            _, script_bytes = capture_scope_file(DATA_DIR, os.path.relpath(SCRIPT_PATH, DATA_DIR),
                                                 limit=MAX_SCRIPT_BYTES, include_bytes=True)
            if script_bytes is None:
                raise ValueError('Active script disappeared during save')
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=409, detail=(
                'Active script requires inspection; existing files were preserved.')) from error
        validate_book_json_bytes(script_bytes, list, 422, "Active script")
        try:
            active_state = _read_selection_state(DATA_DIR, required=False)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            raise HTTPException(status_code=409, detail=(
                'Book state requires inspection; existing files were preserved.')) from error
        if request.expected_book_token is not None:
            if get_book_operation_token(DATA_DIR, state=active_state) != request.expected_book_token:
                raise HTTPException(status_code=409, detail="Active book changed; reopen Save Current before saving")
        if (not isinstance(active_state, dict)
                or ('script_selection_pending' in active_state
                    and active_state['script_selection_pending'] is not False)):
            raise HTTPException(status_code=409, detail=(
                'The selected source does not yet have a published script. '
                'Finish generation or load a saved script before saving.'))
        try:
            _, binding_raw = capture_scope_file(DATA_DIR, BINDING_NAME,
                                               limit=MAX_BINDING_BYTES, include_bytes=True)
            if ('script_binding_required' in active_state
                    and type(active_state['script_binding_required']) is not bool):
                raise ValueError('Invalid script binding requirement')
            if binding_raw is None and active_state.get('script_binding_required') is True:
                raise ValueError('Required script binding is missing')
            if binding_raw is not None:
                selected = capture_generation_selection(DATA_DIR, active_state.get('input_file_path'))
                published_script_hash = check_binding_source(binding_raw, selected['book_token'],
                    selected['source_sha256'], source_path=selected['source_path'])
                if hashlib.sha256(script_bytes).hexdigest() != published_script_hash:
                    raise ValueError('Changed script has no verified edit provenance')
                if parse_script_binding(binding_raw)['published_entries_sha256'] != get_review_entries_fingerprint(json.loads(script_bytes)):
                    raise ValueError('Script binding entry fingerprint is inconsistent')
        except (OSError, ValueError, TypeError, RecursionError) as error:
            raise HTTPException(status_code=409, detail=(
                'The selected source or script identity changed; existing files were preserved.')) from error
        book_id = get_active_book_id(state=active_state) or safe_name
        cue_bytes, rendition_bytes = _capture_sound_companions(DATA_DIR, ACTIVE_CUES, ACTIVE_RENDITIONS, book_id)
        _validate_sound_destination(SCRIPTS_DIR, os.path.basename(get_saved_sound_design_path(dest)), limit=MAX_REGISTRY_BYTES)
        _validate_sound_destination(SCRIPTS_DIR, os.path.basename(get_saved_sound_renditions_path(dest)), limit=MAX_RENDITION_BYTES)
        meta = {"book_id": book_id}
        if isinstance(active_state, dict) and active_state.get(DATE_ORDER_KEY) in DATE_ORDER_CHOICES:
            meta[DATE_ORDER_KEY] = active_state[DATE_ORDER_KEY]
        replacements = {os.path.basename(dest): script_bytes,
            f"{safe_name}.meta.json": json.dumps(meta).encode("utf-8")}
        if binding_raw is not None:
            replacements[f'{safe_name}.selection_binding.json'] = binding_raw
        if cue_bytes is not None:
            replacements[os.path.basename(get_saved_sound_design_path(dest))] = cue_bytes
        if rendition_bytes is not None:
            replacements[os.path.basename(get_saved_sound_renditions_path(dest))] = rendition_bytes
        try:
            publication = get_saved_publication_source_bytes(active_state.get('input_file_path'), script_bytes)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=409, detail=f'Publication provenance cannot be saved: {error}') from error
        if publication is not None:
            replacements[os.path.basename(get_publication_source_path(dest))] = publication
        voice_name = f"{safe_name}.voice_config.json"
        if os.path.lexists(VOICE_CONFIG_PATH):
            try:
                _, voice_bytes = capture_scope_file(
                    DATA_DIR, os.path.relpath(VOICE_CONFIG_PATH, DATA_DIR),
                    limit=MAX_SCRIPT_BYTES, include_bytes=True)
                if voice_bytes is None:
                    raise ValueError('Active voices disappeared')
            except (OSError, ValueError) as error:
                raise HTTPException(status_code=409, detail=(
                    'Active voices require inspection; existing files were preserved.')) from error
            validate_book_json_bytes(voice_bytes, dict, 422, "Active voices")
            replacements[voice_name] = voice_bytes
        removals = [os.path.basename(path) for path in _get_saved_book_companions(dest)
                    if os.path.basename(path) not in replacements]
        source_path = active_state.get('input_file_path')
        saving_active_source = (binding_raw is not None and isinstance(source_path, str)
                                and os.path.abspath(source_path) == os.path.abspath(dest))
        if saving_active_source:
            # The selected source is the saved script itself. Update the active
            # binding and saved family in one recoverable transaction, otherwise
            # replacing the source leaves the active identity stale on next save.
            # The saved binding describes the original source, not the loaded
            # copy's self-reference. Advance it only from its verified old bytes.
            try:
                _, old_saved = capture_scope_file(SCRIPTS_DIR, os.path.basename(dest),
                                                   limit=MAX_SCRIPT_BYTES, include_bytes=True)
                _, old_saved_binding = capture_scope_file(
                    SCRIPTS_DIR, f'{safe_name}.selection_binding.json',
                    limit=MAX_BINDING_BYTES, include_bytes=True)
                if old_saved is None or old_saved_binding is None:
                    raise ValueError('Saved source or binding is missing')
                saved_binding = updated_saved_binding(old_saved_binding, old_saved, script_bytes)
            except (OSError, ValueError, TypeError, RecursionError) as error:
                raise HTTPException(status_code=409, detail=(
                    'Saved script identity requires inspection; existing files were preserved.')) from error
            updated = make_script_selection_binding(get_book_operation_token(DATA_DIR, state=active_state),
                hashlib.sha256(script_bytes).hexdigest(), script_bytes, source_path=source_path)
            updated_raw = json.dumps(updated, sort_keys=True, separators=(',', ':')).encode()
            replacements[f'{safe_name}.selection_binding.json'] = saved_binding
            relative = lambda path: os.path.relpath(path, DATA_DIR).replace(os.sep, '/')
            root_replacements = {relative(os.path.join(SCRIPTS_DIR, name)): value
                                 for name, value in replacements.items()}
            root_replacements[BINDING_NAME] = updated_raw
            root_removals = [relative(os.path.join(SCRIPTS_DIR, name)) for name in removals]
            apply_book_state_locked(DATA_DIR, root_replacements, root_removals)
        else:
            apply_book_state_locked(SCRIPTS_DIR, replacements, removals)
    logger.info(f"Script saved as '{safe_name}'")
    return {"status": "saved", "name": safe_name}


@router.post("/api/scripts/save")
async def save_script(request: ScriptSaveRequest):
    """Save the current annotated_script.json (and voice_config.json) under a name."""
    try:
        return await asyncio.to_thread(_save_script_sync, request)
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail="Book is busy; try saving again.") from exc


class ScriptLoadRequest(BaseModel):
    name: str

@router.post("/api/scripts/load")
async def load_script(request: ScriptLoadRequest):
    'Load a saved script, replacing the current annotated_script.json and chunks.'
    return await asyncio.to_thread(_load_script_sync, request)


def _load_script_sync(request: ScriptLoadRequest):
    """Load a saved script, replacing the current annotated_script.json and chunks."""
    safe_name = _require_saved_book_name(request.name)

    src = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")

    # The durable book transaction protects the entire switch, including old
    # exports and recovery sidecars, rather than publishing unrelated copies.
    with (ensure_book_switch_allowed(), ensure_book_state(DATA_DIR, timeout=0),
          ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR), file_lock(src), file_lock(SCRIPT_PATH),
          file_lock(VOICE_CONFIG_PATH), file_lock(CHUNKS_PATH),
          file_lock(os.path.join(DATA_DIR, BINDING_NAME))):
        if not os.path.isfile(src):
            raise HTTPException(status_code=404, detail=f"Saved script '{request.name}' not found.")
        _require_saved_book_idle()
        busy = [k for k in ("audio", "script", "review", "persona", "nicknames",
                            "audacity_export", "m4b_export", "chapter_export")
                if is_task_running(k, process_state)]
        if busy:
            raise HTTPException(status_code=409,
                detail=f"Cannot load a script while these tasks are running: {', '.join(busy)}.")
        try:
            _, script_bytes = capture_scope_file(SCRIPTS_DIR, os.path.basename(src),
                                                 limit=MAX_SCRIPT_BYTES, include_bytes=True)
            if script_bytes is None:
                raise ValueError('Saved script disappeared')
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=409, detail=(
                'Saved script identity requires inspection; existing files were preserved.')) from error
        validate_book_json_bytes(script_bytes, list, 400, "Saved script")
        saved_binding_path = os.path.splitext(src)[0] + '.selection_binding.json'
        try:
            _, saved_binding_raw = capture_scope_file(SCRIPTS_DIR, os.path.basename(saved_binding_path),
                                                       limit=MAX_BINDING_BYTES, include_bytes=True)
            if saved_binding_raw is not None:
                saved_binding = parse_script_binding(saved_binding_raw)
                if (saved_binding['published_script_sha256'] != hashlib.sha256(script_bytes).hexdigest()
                        or saved_binding['published_entries_sha256'] != get_review_entries_fingerprint(json.loads(script_bytes))):
                    raise ValueError('Saved script differs from its binding')
        except (OSError, ValueError, TypeError, RecursionError) as error:
            raise HTTPException(status_code=409, detail='Saved script identity requires inspection; existing files were preserved.') from error
        try:
            capture_scope_file(DATA_DIR, BINDING_NAME, limit=MAX_BINDING_BYTES)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=409, detail='Active script identity requires inspection; existing files were preserved.') from error
        try:
            get_publication_source_capture(src)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=409, detail=f'Saved publication provenance requires review: {error}') from error
        state_path = os.path.join(DATA_DIR, "state.json")
        state = safe_load_json(state_path, default={})
        book_id = _get_saved_book_id(safe_name)
        cue_bytes, rendition_bytes = _capture_sound_companions(SCRIPTS_DIR,
            os.path.basename(get_saved_sound_design_path(src)),
            os.path.basename(get_saved_sound_renditions_path(src)), book_id)
        # Refuse corrupt active metadata before replacing it, retaining recovery
        # files. Valid old-book metadata may be replaced by the selected snapshot.
        _capture_sound_companions(DATA_DIR, ACTIVE_CUES, ACTIVE_RENDITIONS, state.get('active_book_id'))
        state.update(active_book_id=book_id, input_file_path=src,
                     book_generation=uuid.uuid4().hex)
        state.pop('script_selection_pending', None)
        state.pop('script_binding_required', None)
        state.pop('_source_scope', None)
        state.pop('_source_scope_exports', None)
        # The book's date order travels with it (#1546); absent means auto.
        saved_order = safe_load_json(_saved_book_meta_path(safe_name), default={})
        saved_order = saved_order.get(DATE_ORDER_KEY) if isinstance(saved_order, dict) else None
        if saved_order in DATE_ORDER_CHOICES:
            state[DATE_ORDER_KEY] = saved_order
        else:
            state.pop(DATE_ORDER_KEY, None)
        replacements = {os.path.relpath(SCRIPT_PATH, DATA_DIR): script_bytes,
                        "state.json": json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")}
        if saved_binding_raw is not None:
            token = get_book_operation_token(DATA_DIR, state=state)
            binding = make_script_selection_binding(token, hashlib.sha256(script_bytes).hexdigest(),
                                                    script_bytes, source_path=src)
            replacements[BINDING_NAME] = json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()
            state['script_binding_required'] = True
            replacements['state.json'] = json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8')
        if cue_bytes is not None:
            replacements[ACTIVE_CUES] = cue_bytes
        if rendition_bytes is not None:
            replacements[ACTIVE_RENDITIONS] = rendition_bytes
        removals = [AUDIOBOOK_PATH, M4B_PATH, os.path.join(DATA_DIR, "audacity_export.zip"),
                    os.path.join(DATA_DIR, CHAPTER_EXPORT_DIR), CHUNKS_PATH,
                    get_chunk_status_journal_path(CHUNKS_PATH),
                    *get_generation_checkpoint_artifacts(_checkpoint_path(SCRIPT_PATH)),
                    *get_generation_checkpoint_artifacts(three_pass_checkpoint_path(SCRIPT_PATH)),
                    three_pass_manifest_path(SCRIPT_PATH),
                    *get_generation_checkpoint_artifacts(get_generation_checkpoint_path(SCRIPT_PATH)),
                    get_generation_quality_path(SCRIPT_PATH)]
        if saved_binding_raw is None:
            removals.append(os.path.join(DATA_DIR, BINDING_NAME))
        if cue_bytes is None:
            removals.append(os.path.join(DATA_DIR, ACTIVE_CUES))
        if rendition_bytes is None:
            removals.append(os.path.join(DATA_DIR, ACTIVE_RENDITIONS))
        companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
        if os.path.lexists(companion):
            try:
                _, voice_bytes = capture_scope_file(SCRIPTS_DIR, os.path.basename(companion),
                                                     limit=MAX_SCRIPT_BYTES, include_bytes=True)
                if voice_bytes is None:
                    raise ValueError('Saved voices disappeared')
            except (OSError, ValueError) as error:
                raise HTTPException(status_code=409, detail=(
                    'Saved voices require inspection; existing files were preserved.')) from error
            validate_book_json_bytes(voice_bytes, dict, 400, "Saved voices")
            replacements[os.path.relpath(VOICE_CONFIG_PATH, DATA_DIR)] = voice_bytes
        else:
            removals.append(VOICE_CONFIG_PATH)
        apply_book_state_locked(DATA_DIR, replacements,
                                [os.path.relpath(path, DATA_DIR) for path in removals])

    logger.info(f"Script '{request.name}' loaded")
    return {"status": "loaded", "name": request.name}


class ScriptPreflightRequest(BaseModel):
    source_filename: str | None = None


@router.post("/api/scripts/{name}/preflight")
async def preflight_saved_script(name: str, request: ScriptPreflightRequest):
    'Audit a saved script and optional uploaded source without changing either.'
    return await asyncio.to_thread(_preflight_saved_script_sync, name, request)


def _preflight_saved_script_sync(name: str, request: ScriptPreflightRequest):
    with ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR):
        """Audit a saved script and optional uploaded source without changing either."""
        safe_name = _require_saved_book_name(name)
        script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
        if not os.path.exists(script_path):
            raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")

        entries = safe_load_json(script_path, None)
        if entries is None:
            raise HTTPException(status_code=422, detail=f"Saved script '{name}' is not valid JSON.")
        if not isinstance(entries, list):
            raise HTTPException(status_code=422, detail=f"Saved script '{name}' must contain a list of entries.")

        source_text = None
        if request.source_filename:
            safe_source = _require_safe_filename(request.source_filename, "Invalid source filename.")
            if not safe_source.lower().endswith((".txt", ".md")):
                raise HTTPException(status_code=400, detail="Source must be a TXT or Markdown file.")
            source_path = os.path.join(UPLOADS_DIR, safe_source)
            if not os.path.exists(source_path):
                raise HTTPException(status_code=404, detail=f"Uploaded source '{safe_source}' not found.")
            try:
                with open(source_path, "r", encoding="utf-8") as source_file:
                    source_text = source_file.read()
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=422, detail="Source file is not valid UTF-8.") from exc

        return audit_script(entries, source_text, is_generic_speaker)


class ScriptRepairRequest(BaseModel):
    source_filename: str
    expected_sha256: str | None = None


class SpeakerSelection(BaseModel):
    entry_number: int = Field(ge=1)
    expected_speaker: str
    new_speaker: str


class SpeakerRepairRequest(BaseModel):
    expected_sha256: str | None = None
    selections: list[SpeakerSelection] = Field(default_factory=list)


class FrontMatterSelection(BaseModel):
    entry_number: int = Field(ge=1, strict=True)
    expected_text: str


class DirectionSelection(BaseModel):
    entry_number: int = Field(ge=1, strict=True)
    expected_instruct: str
    new_instruct: str


class ContentRepairRequest(BaseModel):
    expected_sha256: str
    front_matter_removals: list[FrontMatterSelection] = Field(default_factory=list)
    direction_changes: list[DirectionSelection] = Field(default_factory=list)


def get_repair_script_snapshot(path):
    """Parse and hash the same captured script bytes for preview and apply."""
    try:
        _, script_bytes = capture_scope_file(SCRIPTS_DIR, os.path.basename(path),
                                             limit=MAX_SCRIPT_BYTES, include_bytes=True)
        if script_bytes is None:
            raise ValueError('Saved script disappeared')
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409,
                            detail='Saved script identity requires inspection.') from exc
    try:
        entries = json.loads(script_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Saved script must contain a JSON array.") from exc
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail="Saved script must contain a JSON array.")
    return script_bytes, hashlib.sha256(script_bytes).hexdigest(), entries


def _load_repair_inputs(name, source_filename):
    safe_name = _require_saved_book_name(name)
    safe_source = _require_safe_filename(source_filename, "Invalid source filename.")
    if not safe_source.lower().endswith((".txt", ".md")):
        raise HTTPException(status_code=400, detail="Source must be a TXT or Markdown file.")
    script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    source_path = os.path.join(UPLOADS_DIR, safe_source)
    if not os.path.exists(script_path):
        raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail=f"Uploaded source '{safe_source}' not found.")
    try:
        script_bytes, _script_sha256, entries = get_repair_script_snapshot(script_path)
        _, source_bytes = capture_scope_file(UPLOADS_DIR, os.path.basename(source_path),
                                             limit=MAX_SCRIPT_BYTES, include_bytes=True)
        if source_bytes is None:
            raise ValueError('Uploaded source disappeared')
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Source file is not valid UTF-8.") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail='Source file identity requires inspection.') from exc
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail="Saved script must contain a JSON array.")
    combined_sha256 = hashlib.sha256(
        hashlib.sha256(script_bytes).digest() + hashlib.sha256(source_bytes).digest()
    ).hexdigest()
    return script_path, combined_sha256, build_deterministic_repair(entries, source_text)


@router.post("/api/scripts/{name}/repair/deterministic/preview")
async def preview_deterministic_repair(name: str, request: ScriptRepairRequest):
    'Preview only source-proven Unicode and adjacent-duplicate repairs.'
    return await asyncio.to_thread(_preview_deterministic_repair_sync, name, request)


def _preview_deterministic_repair_sync(name: str, request: ScriptRepairRequest):
    with ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR):
        """Preview only source-proven Unicode and adjacent-duplicate repairs."""
        _path, sha256, repair = _load_repair_inputs(name, request.source_filename)
        return {"sha256": sha256, "changes": repair["changes"], "unresolved": repair["unresolved"],
                "notes": repair.get("notes", []),
                "result_entry_count": len(repair["entries"])}


@router.post("/api/scripts/{name}/repair/deterministic/apply")
async def apply_deterministic_repair(name: str, request: ScriptRepairRequest):
    'Apply an unchanged preview, preserving the original in a timestamped backup.'
    return await asyncio.to_thread(_apply_deterministic_repair_sync, name, request)


def _apply_deterministic_repair_sync(name: str, request: ScriptRepairRequest):
    with _saved_mutation_admission():
        _require_saved_book_idle()
        """Apply an unchanged preview, preserving the original in a timestamped backup."""
        if not request.expected_sha256:
            raise HTTPException(status_code=400, detail="expected_sha256 from preview is required.")
        safe_name = _require_saved_book_name(name)
        script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
        try:
            with file_lock(script_path):
                path, sha256, repair = _load_repair_inputs(name, request.source_filename)
                if sha256 != request.expected_sha256:
                    raise HTTPException(status_code=409, detail="Script changed after preview; preview it again.")
                if repair["unresolved"]:
                    raise HTTPException(status_code=409, detail="Repair has unresolved findings and was not applied.")
                if not repair["changes"]:
                    return {"status": "unchanged", "sha256": sha256, "changes": [],
                            "notes": repair.get("notes", [])}
                backup = _publish_saved_repair(path, repair["entries"])
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="Script is busy; retry the preview.") from exc
        return {"status": "repaired", "backup": os.path.basename(backup),
                "changes": repair["changes"], "result_entry_count": len(repair["entries"])}


@router.get("/api/scripts/{name}/repair/speakers/preview")
async def preview_speaker_repair(name: str):
    return await asyncio.to_thread(_preview_speaker_repair_sync, name)


def _preview_speaker_repair_sync(name: str):
    with ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR):
        safe_name = _require_saved_book_name(name)
        path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
        _script_bytes, sha256, entries = get_repair_script_snapshot(path)
        return {"sha256": sha256, "candidates": build_speaker_review(entries)}


@router.post("/api/scripts/{name}/repair/speakers/apply")
async def apply_speaker_repair(name: str, request: SpeakerRepairRequest):
    return await asyncio.to_thread(_apply_speaker_repair_sync, name, request)


def _apply_speaker_repair_sync(name: str, request: SpeakerRepairRequest):
    with _saved_mutation_admission():
        _require_saved_book_idle()
        if not request.expected_sha256:
            raise HTTPException(status_code=400, detail="expected_sha256 from preview is required.")
        safe_name = _require_saved_book_name(name)
        path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
        try:
            with file_lock(path), contextlib.ExitStack() as locks:
                for companion in _get_saved_book_companions(path):
                    locks.enter_context(file_lock(companion))
                if not os.path.exists(path):
                    raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
                active_source = saved_script_is_active_source(DATA_DIR, path)
                if active_source:
                    raise HTTPException(status_code=409, detail=(
                        'This saved book is currently loaded; switch books before repairing it.'))
                script_bytes, sha256, entries = get_repair_script_snapshot(path)
                if sha256 != request.expected_sha256:
                    raise HTTPException(status_code=409, detail="Script changed after preview; preview it again.")
                try:
                    repair = apply_speaker_selections(entries, [item.model_dump() for item in request.selections])
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if not repair["changes"]:
                    return {"status": "unchanged", "changes": []}
                backup = get_timestamped_backup_path(path)
                voice_path = os.path.splitext(path)[0] + ".voice_config.json"
                voice_backup = (get_timestamped_backup_path(voice_path)
                                if os.path.lexists(voice_path) else None)
                for candidate in (backup, voice_backup):
                    if candidate and os.path.lexists(candidate):
                        raise HTTPException(status_code=409, detail="Repair backup already exists; retry the repair.")
                replacements = {
                    os.path.basename(path): json.dumps(repair["entries"], ensure_ascii=False, indent=2).encode("utf-8"),
                    os.path.basename(backup): script_bytes,
                }
                binding = _saved_repair_binding(path, script_bytes, replacements[os.path.basename(path)])
                if binding is not None:
                    replacements[os.path.basename(os.path.splitext(path)[0] + '.selection_binding.json')] = binding
                if voice_backup:
                    try:
                        _, voice_bytes = capture_scope_file(
                            SCRIPTS_DIR, os.path.basename(voice_path),
                            limit=MAX_SCRIPT_BYTES, include_bytes=True)
                        if voice_bytes is None:
                            raise ValueError('Saved voices disappeared')
                    except (OSError, ValueError) as error:
                        raise HTTPException(status_code=409,
                                            detail="Saved voices must be a regular file.") from error
                    replacements[os.path.basename(voice_backup)] = voice_bytes
                # Entry-level reassignment cannot prove a voice identity rename.
                # Preserve the exact assignments for recovery, then require a
                # fresh cast instead of carrying obsolete approvals forward.
                removals = [os.path.basename(companion) for companion in _get_saved_book_companions(path)
                            if companion not in (os.path.splitext(path)[0] + ".meta.json",
                                                 os.path.splitext(path)[0] + '.selection_binding.json',
                                                 get_publication_source_path(path),
                                                 get_saved_sound_design_path(path),
                                                 get_saved_sound_renditions_path(path))]
                apply_book_state_locked(SCRIPTS_DIR, replacements, removals)
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="Script is busy; preview it again.") from exc
        return {"status": "repaired", "backup": os.path.basename(backup),
                "changes": repair["changes"],
                "voice_config_invalidated": bool(voice_backup),
                "voice_config_backup": os.path.basename(voice_backup) if voice_backup else None,
                "active_voice_config_backup": None,
                "message": "Speaker assignments changed; assign and approve voices again before rendering."}


@router.get("/api/scripts/{name}/repair/content/preview")
async def preview_content_repair(name: str):
    return await asyncio.to_thread(_preview_content_repair_sync, name)


def _preview_content_repair_sync(name: str):
    with ensure_saved_book_state(DATA_DIR, SCRIPTS_DIR):
        safe_name = _require_saved_book_name(name)
        path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
        _script_bytes, sha256, entries = get_repair_script_snapshot(path)
        return {"sha256": sha256, **build_content_review(entries)}


@router.post("/api/scripts/{name}/repair/content/apply")
async def apply_content_repair(name: str, request: ContentRepairRequest):
    return await asyncio.to_thread(_apply_content_repair_sync, name, request)


def _apply_content_repair_sync(name: str, request: ContentRepairRequest):
    with _saved_mutation_admission():
        _require_saved_book_idle()
        safe_name = _require_saved_book_name(name)
        path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
        try:
            with file_lock(path):
                if not os.path.exists(path):
                    raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
                _script_bytes, sha256, entries = get_repair_script_snapshot(path)
                if sha256 != request.expected_sha256:
                    raise HTTPException(status_code=409, detail="Script changed after preview; preview it again.")
                try:
                    repair = apply_content_selections(
                        entries, [item.model_dump() for item in request.front_matter_removals],
                        [item.model_dump() for item in request.direction_changes])
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if not repair["changes"]:
                    return {"status": "unchanged", "changes": []}
                backup = _publish_saved_repair(path, repair["entries"])
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="Script is busy; preview it again.") from exc
        return {"status": "repaired", "backup": os.path.basename(backup),
                "changes": repair["changes"], "result_entry_count": len(repair["entries"])}

def _delete_script_sync(name):
    safe_name = _require_saved_book_name(name)
    filepath = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    artifacts = [filepath, *_get_saved_book_companions(filepath)]
    with _saved_mutation_admission(), contextlib.ExitStack() as locks:
        _require_saved_book_idle()
        for path in artifacts:
            locks.enter_context(file_lock(path))
        if not os.path.isfile(filepath):
            raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
        if saved_script_is_active_source(DATA_DIR, filepath):
            raise HTTPException(status_code=409,
                                detail='This saved book is currently loaded; switch books before deleting it.')
        apply_book_state_locked(SCRIPTS_DIR, {}, [os.path.basename(path) for path in artifacts])
    logger.info(f"Script '{name}' deleted")
    return {"status": "deleted", "name": name}


@router.delete("/api/scripts/{name}")
async def delete_script(name: str):
    """Delete a saved script."""
    try:
        return await asyncio.to_thread(_delete_script_sync, name)
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail="Book is busy; try deleting again.") from exc
