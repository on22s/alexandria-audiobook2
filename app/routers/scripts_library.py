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
from book_state_transaction import ensure_book_state, apply_book_state_locked

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
    _gpu_lock,
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
from speaker_repair import apply_speaker_selections, build_speaker_review
from content_repair import apply_content_selections, build_content_review
from utils import (atomic_json_write, backup_file_with_timestamp, file_lock,
                   get_timestamped_backup_path, is_generic_speaker, safe_load_json)


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


def _get_saved_book_companions(path):
    """Every known saved-book companion, including older checkpoint names."""
    stem = os.path.splitext(path)[0]
    return list(dict.fromkeys([
        stem + ".voice_config.json", stem + ".meta.json",
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
                               for path in _get_saved_book_companions("book.json"))
    return (filename.endswith(".json") and not filename.startswith(".")
            and not filename.endswith(companion_suffixes))


def _require_saved_book_name(name):
    safe = _require_safe_filename(name, "Invalid script name.")
    if safe != name or not _is_saved_book_filename(safe + ".json"):
        raise HTTPException(status_code=400, detail="Invalid or reserved script name.")
    return safe


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
    with ensure_book_state(SCRIPTS_DIR):
        return _build_saved_scripts_list()


@router.get("/api/scripts", operation_id="list_saved_scripts")
async def list_saved_scripts():
    """List saved scripts without blocking the FastAPI event loop on disk I/O."""
    return await asyncio.to_thread(_list_saved_scripts_sync)

class ScriptSaveRequest(BaseModel):
    name: str

def _save_script_sync(request):
    safe_name = _require_saved_book_name(request.name)
    dest = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    with ensure_book_state(DATA_DIR), ensure_book_state(SCRIPTS_DIR), contextlib.ExitStack() as locks:
        for path in (dest, SCRIPT_PATH, VOICE_CONFIG_PATH, *_get_saved_book_companions(dest)):
            locks.enter_context(file_lock(path))
        if not os.path.isfile(SCRIPT_PATH):
            raise HTTPException(status_code=404, detail="No annotated script to save. Generate a script first.")
        script_bytes = Path(SCRIPT_PATH).read_bytes()
        if not isinstance(json.loads(script_bytes), list):
            raise HTTPException(status_code=422, detail="Active script must contain an entry list.")
        replacements = {os.path.basename(dest): script_bytes,
            f"{safe_name}.meta.json": json.dumps({"book_id": get_active_book_id() or safe_name}).encode("utf-8")}
        voice_name = f"{safe_name}.voice_config.json"
        if os.path.isfile(VOICE_CONFIG_PATH):
            voice_bytes = Path(VOICE_CONFIG_PATH).read_bytes()
            if not isinstance(json.loads(voice_bytes), dict):
                raise HTTPException(status_code=422, detail="Active voices must contain an object.")
            replacements[voice_name] = voice_bytes
        removals = [os.path.basename(path) for path in _get_saved_book_companions(dest)
                    if os.path.basename(path) not in replacements]
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
    with _gpu_lock, ensure_book_state(DATA_DIR), ensure_book_state(SCRIPTS_DIR), file_lock(src), file_lock(SCRIPT_PATH), file_lock(VOICE_CONFIG_PATH), file_lock(CHUNKS_PATH):
        if not os.path.isfile(src):
            raise HTTPException(status_code=404, detail=f"Saved script '{request.name}' not found.")
        _require_saved_book_idle()
        busy = [k for k in ("audio", "script", "review", "persona", "nicknames",
                            "audacity_export", "m4b_export", "chapter_export")
                if is_task_running(k, process_state)]
        if busy:
            raise HTTPException(status_code=409,
                detail=f"Cannot load a script while these tasks are running: {', '.join(busy)}.")
        script_bytes = Path(src).read_bytes()
        if not isinstance(json.loads(script_bytes), list):
            raise HTTPException(status_code=400, detail="Saved script must contain an entry list.")
        state_path = os.path.join(DATA_DIR, "state.json")
        state = safe_load_json(state_path, default={})
        state.update(active_book_id=_get_saved_book_id(safe_name), input_file_path=src,
                     book_generation=uuid.uuid4().hex)
        replacements = {os.path.relpath(SCRIPT_PATH, DATA_DIR): script_bytes,
                        "state.json": json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")}
        removals = [AUDIOBOOK_PATH, M4B_PATH, os.path.join(DATA_DIR, "audacity_export.zip"),
                    os.path.join(DATA_DIR, CHAPTER_EXPORT_DIR), CHUNKS_PATH,
                    get_chunk_status_journal_path(CHUNKS_PATH),
                    *get_generation_checkpoint_artifacts(_checkpoint_path(SCRIPT_PATH)),
                    *get_generation_checkpoint_artifacts(three_pass_checkpoint_path(SCRIPT_PATH)),
                    three_pass_manifest_path(SCRIPT_PATH),
                    *get_generation_checkpoint_artifacts(get_generation_checkpoint_path(SCRIPT_PATH)),
                    get_generation_quality_path(SCRIPT_PATH)]
        companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
        if os.path.exists(companion):
            voice_bytes = Path(companion).read_bytes()
            if not isinstance(json.loads(voice_bytes), dict):
                raise HTTPException(status_code=400, detail="Saved voices must contain an object.")
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
    with ensure_book_state(SCRIPTS_DIR):
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
    with open(path, "rb") as script_file:
        script_bytes = script_file.read()
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
        with open(source_path, "rb") as source_file:
            source_bytes = source_file.read()
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Source file is not valid UTF-8.") from exc
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
    with ensure_book_state(SCRIPTS_DIR):
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
    with _gpu_lock, ensure_book_state(SCRIPTS_DIR):
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
                backup = backup_file_with_timestamp(path)
                atomic_json_write(repair["entries"], path)
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="Script is busy; retry the preview.") from exc
        return {"status": "repaired", "backup": os.path.basename(backup),
                "changes": repair["changes"], "result_entry_count": len(repair["entries"])}


@router.get("/api/scripts/{name}/repair/speakers/preview")
async def preview_speaker_repair(name: str):
    return await asyncio.to_thread(_preview_speaker_repair_sync, name)


def _preview_speaker_repair_sync(name: str):
    with ensure_book_state(SCRIPTS_DIR):
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
    with _gpu_lock, ensure_book_state(SCRIPTS_DIR):
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
                if voice_backup:
                    if not os.path.isfile(voice_path) or os.path.islink(voice_path):
                        raise HTTPException(status_code=409, detail="Saved voices must be a regular file.")
                    replacements[os.path.basename(voice_backup)] = Path(voice_path).read_bytes()
                # Entry-level reassignment cannot prove a voice identity rename.
                # Preserve the exact assignments for recovery, then require a
                # fresh cast instead of carrying obsolete approvals forward.
                removals = [os.path.basename(companion) for companion in _get_saved_book_companions(path)
                            if companion != os.path.splitext(path)[0] + ".meta.json"]
                apply_book_state_locked(SCRIPTS_DIR, replacements, removals)
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="Script is busy; preview it again.") from exc
        return {"status": "repaired", "backup": os.path.basename(backup),
                "changes": repair["changes"], "voice_config_invalidated": bool(voice_backup),
                "voice_config_backup": os.path.basename(voice_backup) if voice_backup else None,
                "message": "Speaker assignments changed; assign and approve voices again before rendering."}


@router.get("/api/scripts/{name}/repair/content/preview")
async def preview_content_repair(name: str):
    return await asyncio.to_thread(_preview_content_repair_sync, name)


def _preview_content_repair_sync(name: str):
    with ensure_book_state(SCRIPTS_DIR):
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
    with _gpu_lock, ensure_book_state(SCRIPTS_DIR):
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
                backup = backup_file_with_timestamp(path)
                atomic_json_write(repair["entries"], path)
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="Script is busy; preview it again.") from exc
        return {"status": "repaired", "backup": os.path.basename(backup),
                "changes": repair["changes"], "result_entry_count": len(repair["entries"])}

def _delete_script_sync(name):
    safe_name = _require_saved_book_name(name)
    filepath = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    artifacts = [filepath, *_get_saved_book_companions(filepath)]
    with _gpu_lock, ensure_book_state(SCRIPTS_DIR), contextlib.ExitStack() as locks:
        _require_saved_book_idle()
        for path in artifacts:
            locks.enter_context(file_lock(path))
        if not os.path.isfile(filepath):
            raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
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
