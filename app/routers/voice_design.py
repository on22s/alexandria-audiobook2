import asyncio
import logging
import os
import shutil
import time
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from core import (
    CLONE_VOICES_DIR,
    DESIGNED_VOICES_DIR,
    _load_manifest,
    _require_safe_filename,
    _safe_subpath,
    _save_manifest,
    _save_upload_limited,
    run_claimed_task_worker,
    process_state,
    project_manager,
)
from utils import file_lock, get_unique_id
from voice_reference_import import import_reference_audio
from tts import UnsupportedVoiceBackendError


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


class VoiceDesignPreviewRequest(BaseModel):
    description: str
    sample_text: str
    language: Optional[str] = None

class VoiceDesignSaveRequest(BaseModel):
    name: str
    description: str
    sample_text: str
    preview_file: str
    # Set by the Designer's Edit flow: replace this manifest entry (and its
    # audio) instead of appending a duplicate.
    voice_id: Optional[str] = None


## ── Voice Designer ──────────────────────────────────────────────

DESIGNED_VOICES_MANIFEST = os.path.join(DESIGNED_VOICES_DIR, "manifest.json")


def _append_manifest_entry(path, entry):
    """Append one immutable voice entry without losing a concurrent upload."""
    with file_lock(path):
        manifest = _load_manifest(path)
        manifest.append(entry)
        _save_manifest(path, manifest)


@router.post("/api/voice_design/preview")
async def voice_design_preview(request: VoiceDesignPreviewRequest):
    """Generate a preview voice from a text description."""
    return await run_claimed_task_worker("voice_design", _generate_voice_design_preview, request)


def _generate_voice_design_preview(request):
    try:
        # Model initialization allocates VRAM too, so it belongs inside the same
        # reservation as inference rather than happening before the lock check.
        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")
        wav_path, _ = engine.generate_voice_design(
            description=request.description,
            sample_text=request.sample_text,
            language=request.language,
        )
        # Return relative URL for the static mount
        filename = os.path.basename(wav_path)
        return {"status": "ok", "audio_url": f"/designed_voices/previews/{filename}"}
    except UnsupportedVoiceBackendError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as e:
        logger.exception("Voice design preview failed")
        raise HTTPException(status_code=500, detail="Voice design preview failed — see server logs for details.") from e

@router.post("/api/voice_design/save")
async def voice_design_save(request: VoiceDesignSaveRequest):
    """Save a preview voice as a permanent designed voice."""
    previews_dir = os.path.join(DESIGNED_VOICES_DIR, "previews")
    # Constrain to the previews dir so preview_file can't traverse out and copy
    # an arbitrary host file (e.g. ../../etc/passwd) into the web-served dir.
    preview_path = _safe_subpath(previews_dir, request.preview_file)

    if not os.path.exists(preview_path):
        raise HTTPException(status_code=404, detail="Preview file not found")

    safe_name = _require_safe_filename(request.name, "Invalid voice name")

    with file_lock(DESIGNED_VOICES_MANIFEST):
        manifest = _load_manifest(DESIGNED_VOICES_MANIFEST)
        existing = None
        if request.voice_id:
            existing = next((m for m in manifest if m.get("id") == request.voice_id), None)
            if existing is None:
                raise HTTPException(status_code=404, detail="Designed voice to update not found")
            voice_id = existing["id"]
            dest_filename = existing.get("filename") or f"{voice_id}.wav"
        else:
            voice_id = get_unique_id(safe_name)
            dest_filename = f"{voice_id}.wav"
        dest_path = os.path.join(DESIGNED_VOICES_DIR, dest_filename)
        staging_path = os.path.join(DESIGNED_VOICES_DIR,
                                    f".{dest_filename}.{get_unique_id('stage')}.tmp")
        entry = {"id": voice_id, "name": request.name,
                 "description": request.description, "sample_text": request.sample_text,
                 "filename": dest_filename}
        try:
            shutil.copy2(preview_path, staging_path)
            if existing is not None:
                existing.update(entry)
                _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)
                os.replace(staging_path, dest_path)
            else:
                os.replace(staging_path, dest_path)
                manifest.append(entry)
                try:
                    _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)
                except Exception:
                    os.remove(dest_path)
                    raise
        finally:
            if os.path.exists(staging_path):
                os.remove(staging_path)

    logger.info(f"Designed voice {'updated' if existing else 'saved'}: '{request.name}' as {dest_filename}")
    return {"status": "updated" if existing else "saved", "voice_id": voice_id}

@router.get("/api/voice_design/list")
async def voice_design_list():
    """List all saved designed voices."""
    return _load_manifest(DESIGNED_VOICES_MANIFEST)

def get_voice_asset_path(base_dir, entry):
    """Resolve a stored voice filename within its asset directory."""
    name = entry.get("filename")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="Invalid voice asset filename")
    return _safe_subpath(base_dir, name)


@router.delete("/api/voice_design/{voice_id}")
async def voice_design_delete(voice_id: str):
    """Delete a saved designed voice."""
    with file_lock(DESIGNED_VOICES_MANIFEST):
        manifest = _load_manifest(DESIGNED_VOICES_MANIFEST)
        entry = next((v for v in manifest if v["id"] == voice_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Voice not found")
        wav_path = get_voice_asset_path(DESIGNED_VOICES_DIR, entry)
        _save_manifest(DESIGNED_VOICES_MANIFEST,
                       [v for v in manifest if v["id"] != voice_id])
        if os.path.exists(wav_path):
            os.remove(wav_path)

    logger.info(f"Designed voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}

## ── Clone Voice Uploads ───────────────────────────────────────

CLONE_VOICES_MANIFEST = os.path.join(CLONE_VOICES_DIR, "manifest.json")
ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg"}

@router.get("/api/clone_voices/list")
async def clone_voices_list():
    """List all uploaded clone voices."""
    return _load_manifest(CLONE_VOICES_MANIFEST)

@router.post("/api/clone_voices/upload")
async def clone_voices_upload(file: UploadFile = File(...),
                              ref_text: str = Form(""),
                              source_title: str = Form(""),
                              source_url: str = Form(""),
                              rights_basis: str = Form(""),
                              rights_confirmed: bool = Form(False)):
    """Import a reference clip for voice cloning.

    The clip is decoded, normalised to what the engine reads (24 kHz mono
    PCM16 WAV), measured, and refused if it is not one clean 3-30 s sentence
    (see voice_reference_import). The exact transcript and a rights
    confirmation are required; source title/URL/basis are recorded when given.
    Every import is a new immutable id - a changed reference is a new voice."""
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported format. Use: {', '.join(ALLOWED_AUDIO_EXTS)}")
    # Called directly (tests) the Form defaults arrive as Form objects, not values.
    def form_str(value):
        return value.strip() if isinstance(value, str) else ""
    ref_text, source_title, source_url, rights_basis = (
        form_str(ref_text), form_str(source_title), form_str(source_url), form_str(rights_basis))
    if not ref_text:
        raise HTTPException(status_code=400, detail="The exact transcript of the clip is required (ref_text).")
    if rights_confirmed is not True:
        raise HTTPException(status_code=400, detail="Confirm you have the right to use this voice (rights_confirmed).")

    base_name = os.path.splitext(file.filename)[0]
    safe_name = _require_safe_filename(base_name, "Invalid filename")

    voice_id = get_unique_id(safe_name)
    dest_filename = f"{voice_id}.wav"
    dest_path = os.path.join(CLONE_VOICES_DIR, dest_filename)
    upload_path = os.path.join(CLONE_VOICES_DIR, f".upload_{voice_id}{ext}")

    await _save_upload_limited(file, upload_path, 512 * 1024**2)
    def _import():
        try:
            measures, problems = import_reference_audio(upload_path, dest_path)
        finally:
            if os.path.exists(upload_path):
                os.remove(upload_path)
        if problems:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise HTTPException(status_code=400, detail="Reference clip refused: " + "; ".join(problems))

        try:
            _append_manifest_entry(CLONE_VOICES_MANIFEST, {
                "id": voice_id,
                "name": base_name,
                "filename": dest_filename,
                "ref_text": ref_text,
                "source_title": source_title,
                "source_url": source_url,
                "rights_basis": rights_basis,
                "rights_confirmed": True,
                "imported_at": time.time(),
                **measures,
            })
        except BaseException:
            # This immutable import owns its new file until the manifest accepts it.
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise


        logger.info(f"Clone voice imported: '{base_name}' as {dest_filename} "
                    f"({measures['duration_s']}s, sha256 {measures['sha256'][:12]})")
        return {"status": "uploaded", "voice_id": voice_id, "filename": dest_filename,
                "measures": measures}

    return await asyncio.to_thread(_import)

@router.delete("/api/clone_voices/{voice_id}")
async def clone_voices_delete(voice_id: str):
    """Delete an uploaded clone voice."""
    with file_lock(CLONE_VOICES_MANIFEST):
        manifest = _load_manifest(CLONE_VOICES_MANIFEST)
        entry = next((v for v in manifest if v["id"] == voice_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Clone voice not found")
        wav_path = get_voice_asset_path(CLONE_VOICES_DIR, entry)
        if os.path.exists(wav_path):
            os.remove(wav_path)
        _save_manifest(CLONE_VOICES_MANIFEST,
                       [v for v in manifest if v["id"] != voice_id])

    logger.info(f"Clone voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}
