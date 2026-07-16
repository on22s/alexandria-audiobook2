import hashlib
import logging
import os
import shutil

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import (
    CHUNKS_PATH,
    SCRIPT_PATH,
    SCRIPTS_DIR,
    UPLOADS_DIR,
    VOICE_CONFIG_PATH,
    _get_saved_book_id,
    _require_safe_filename,
    _save_active_book_id,
    _saved_book_meta_path,
    get_active_book_id,
    process_state,
)
from review_script import _checkpoint_path, clear_checkpoint
from script_preflight import audit_script
from script_repair import build_deterministic_repair
from speaker_repair import apply_speaker_selections, build_speaker_review
from content_repair import apply_content_selections, build_content_review
from utils import (atomic_json_write, backup_file_with_timestamp, file_lock,
                   is_generic_speaker, safe_load_json)


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


@router.get("/api/scripts")
async def list_saved_scripts():
    """List all saved scripts in the scripts/ directory.

    Uses a whitelist approach: only includes .json files that do NOT end with
    any known companion/internal suffix (voice_config, metadata, checkpoint, etc.).
    """
    scripts = []
    companion_suffixes = (".voice_config.json", ".meta.json", ".review_checkpoint.json",
                          ".generation_checkpoint.json", ".checkpoint.jsonl")
    for f in os.listdir(SCRIPTS_DIR):
        if not f.endswith(".json"):
            continue
        if f.startswith(".") or f.endswith(companion_suffixes):
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

class ScriptSaveRequest(BaseModel):
    name: str

@router.post("/api/scripts/save")
async def save_script(request: ScriptSaveRequest):
    """Save the current annotated_script.json (and voice_config.json) under a name."""
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=404, detail="No annotated script to save. Generate a script first.")

    safe_name = _require_safe_filename(request.name, "Invalid script name.")

    dest = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    shutil.copy2(SCRIPT_PATH, dest)

    if os.path.exists(VOICE_CONFIG_PATH):
        shutil.copy2(VOICE_CONFIG_PATH, os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json"))
    atomic_json_write({"book_id": get_active_book_id() or safe_name},
                      _saved_book_meta_path(safe_name))

    logger.info(f"Script saved as '{safe_name}'")
    return {"status": "saved", "name": safe_name}

class ScriptLoadRequest(BaseModel):
    name: str

@router.post("/api/scripts/load")
async def load_script(request: ScriptLoadRequest):
    """Load a saved script, replacing the current annotated_script.json and chunks."""
    # Block while ANY task that writes annotated_script.json / voice_config.json
    # is running — not just audio. A script/review/persona/nicknames run finishes
    # by writing those files and would silently overwrite the book we load here.
    busy = [k for k in ("audio", "script", "review", "persona", "nicknames")
            if process_state.get(k, {}).get("running")]
    if busy:
        raise HTTPException(status_code=409,
            detail=f"Cannot load a script while these tasks are running: {', '.join(busy)}.")

    safe_name = _require_safe_filename(request.name, "Invalid script name.")

    src = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail=f"Saved script '{request.name}' not found.")

    shutil.copy2(src, SCRIPT_PATH)
    _save_active_book_id(_get_saved_book_id(safe_name), src)

    companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
    if os.path.exists(companion):
        shutil.copy2(companion, VOICE_CONFIG_PATH)
    elif os.path.exists(VOICE_CONFIG_PATH):
        os.remove(VOICE_CONFIG_PATH)

    # Delete chunks so they regenerate from the loaded script
    if os.path.exists(CHUNKS_PATH):
        os.remove(CHUNKS_PATH)

    # Clear any review checkpoint left over from the PREVIOUS active book. The
    # checkpoint is keyed to SCRIPT_PATH, not to a book identity (load_checkpoint
    # validates only batch_size/context_window), so a resume after this load would
    # otherwise splice the old book's corrected entries into the one just loaded.
    clear_checkpoint(SCRIPT_PATH)

    logger.info(f"Script '{request.name}' loaded")
    return {"status": "loaded", "name": request.name}


class ScriptPreflightRequest(BaseModel):
    source_filename: str | None = None


@router.post("/api/scripts/{name}/preflight")
async def preflight_saved_script(name: str, request: ScriptPreflightRequest):
    """Audit a saved script and optional uploaded source without changing either."""
    safe_name = _require_safe_filename(name, "Invalid script name.")
    script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(script_path):
        raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")

    entries = safe_load_json(script_path, None)
    if entries is None:
        raise HTTPException(status_code=422, detail=f"Saved script '{name}' is not valid JSON.")

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
    entry_number: int = Field(ge=1)
    expected_text: str


class DirectionSelection(BaseModel):
    entry_number: int = Field(ge=1)
    expected_instruct: str
    new_instruct: str


class ContentRepairRequest(BaseModel):
    expected_sha256: str
    front_matter_removals: list[FrontMatterSelection] = Field(default_factory=list)
    direction_changes: list[DirectionSelection] = Field(default_factory=list)


def _load_repair_inputs(name, source_filename):
    safe_name = _require_safe_filename(name, "Invalid script name.")
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
        with open(script_path, "rb") as script_file:
            script_bytes = script_file.read()
        entries = safe_load_json(script_path, None)
        with open(source_path, "r", encoding="utf-8") as source_file:
            source_text = source_file.read()
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Source file is not valid UTF-8.") from exc
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail="Saved script must contain a JSON array.")
    return script_path, hashlib.sha256(script_bytes).hexdigest(), build_deterministic_repair(entries, source_text)


@router.post("/api/scripts/{name}/repair/deterministic/preview")
async def preview_deterministic_repair(name: str, request: ScriptRepairRequest):
    """Preview only source-proven Unicode and adjacent-duplicate repairs."""
    _path, sha256, repair = _load_repair_inputs(name, request.source_filename)
    return {"sha256": sha256, "changes": repair["changes"], "unresolved": repair["unresolved"],
            "result_entry_count": len(repair["entries"])}


@router.post("/api/scripts/{name}/repair/deterministic/apply")
async def apply_deterministic_repair(name: str, request: ScriptRepairRequest):
    """Apply an unchanged preview, preserving the original in a timestamped backup."""
    if not request.expected_sha256:
        raise HTTPException(status_code=400, detail="expected_sha256 from preview is required.")
    safe_name = _require_safe_filename(name, "Invalid script name.")
    script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    try:
        with file_lock(script_path):
            path, sha256, repair = _load_repair_inputs(name, request.source_filename)
            if sha256 != request.expected_sha256:
                raise HTTPException(status_code=409, detail="Script changed after preview; preview it again.")
            if repair["unresolved"]:
                raise HTTPException(status_code=409, detail="Repair has unresolved findings and was not applied.")
            if not repair["changes"]:
                return {"status": "unchanged", "sha256": sha256, "changes": []}
            backup = backup_file_with_timestamp(path)
            atomic_json_write(repair["entries"], path)
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail="Script is busy; retry the preview.") from exc
    return {"status": "repaired", "backup": os.path.basename(backup),
            "changes": repair["changes"], "result_entry_count": len(repair["entries"])}


@router.get("/api/scripts/{name}/repair/speakers/preview")
async def preview_speaker_repair(name: str):
    safe_name = _require_safe_filename(name, "Invalid script name.")
    path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
    with open(path, "rb") as script_file:
        sha256 = hashlib.sha256(script_file.read()).hexdigest()
    entries = safe_load_json(path, None)
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail="Saved script must contain a JSON array.")
    return {"sha256": sha256, "candidates": build_speaker_review(entries)}


@router.post("/api/scripts/{name}/repair/speakers/apply")
async def apply_speaker_repair(name: str, request: SpeakerRepairRequest):
    if not request.expected_sha256:
        raise HTTPException(status_code=400, detail="expected_sha256 from preview is required.")
    safe_name = _require_safe_filename(name, "Invalid script name.")
    path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    try:
        with file_lock(path):
            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
            with open(path, "rb") as script_file:
                sha256 = hashlib.sha256(script_file.read()).hexdigest()
            if sha256 != request.expected_sha256:
                raise HTTPException(status_code=409, detail="Script changed after preview; preview it again.")
            entries = safe_load_json(path, None)
            try:
                repair = apply_speaker_selections(entries, [item.model_dump() for item in request.selections])
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not repair["changes"]:
                return {"status": "unchanged", "changes": []}
            backup = backup_file_with_timestamp(path)
            atomic_json_write(repair["entries"], path)
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail="Script is busy; preview it again.") from exc
    return {"status": "repaired", "backup": os.path.basename(backup),
            "changes": repair["changes"]}


@router.get("/api/scripts/{name}/repair/content/preview")
async def preview_content_repair(name: str):
    safe_name = _require_safe_filename(name, "Invalid script name.")
    path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
    with open(path, "rb") as script_file:
        sha256 = hashlib.sha256(script_file.read()).hexdigest()
    entries = safe_load_json(path, None)
    if not isinstance(entries, list):
        raise HTTPException(status_code=422, detail="Saved script must contain a JSON array.")
    return {"sha256": sha256, **build_content_review(entries)}


@router.post("/api/scripts/{name}/repair/content/apply")
async def apply_content_repair(name: str, request: ContentRepairRequest):
    safe_name = _require_safe_filename(name, "Invalid script name.")
    path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    try:
        with file_lock(path):
            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")
            with open(path, "rb") as script_file:
                sha256 = hashlib.sha256(script_file.read()).hexdigest()
            if sha256 != request.expected_sha256:
                raise HTTPException(status_code=409, detail="Script changed after preview; preview it again.")
            entries = safe_load_json(path, None)
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

@router.delete("/api/scripts/{name}")
async def delete_script(name: str):
    """Delete a saved script."""
    safe_name = _require_safe_filename(name, "Invalid script name.")

    filepath = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")

    os.remove(filepath)
    companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
    if os.path.exists(companion):
        os.remove(companion)
    meta_path = _saved_book_meta_path(safe_name)
    if os.path.exists(meta_path):
        os.remove(meta_path)
    checkpoint = _checkpoint_path(filepath)
    if os.path.exists(checkpoint):
        os.remove(checkpoint)

    logger.info(f"Script '{name}' deleted")
    return {"status": "deleted", "name": name}
