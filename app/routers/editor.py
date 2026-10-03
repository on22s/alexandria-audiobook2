from merge_integrity import get_source_integrity
from pathlib import Path
from review_report import get_review_report_info, apply_review_report_explanation
from book_state_transaction import ensure_book_state
import asyncio
import logging
import json
import os
import tempfile
import time
import uuid
import warnings
import zipfile
from typing import Annotated, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, StrictInt, field_validator
from config_settings import load_app_config

from core import (
    AUDIOBOOK_PATH,
    CONFIG_PATH,
    DATA_DIR,
    M4B_PATH,
    REPORTS_DIR,
    SCRIPTS_DIR,
    SCRIPT_PATH,
    _load_voicelab_config, _llm_summarize_report, _is_evidence_bound_summary,
    _save_upload_limited,
    _warn_corrupted_json,
    _gpu_lock,
    check_global_gpu_lock,
    claim_gpu_task, schedule_claimed_background_task,
    process_state,
    project_manager,
)
from generation_checkpoint_deltas import load_generation_checkpoint_document
from generation_checkpoint_shards import get_generation_checkpoint_artifacts
from editor_poll_snapshot import EditorPollSnapshots
from utils import safe_load_json, is_path_inside
from project import (CHAPTER_EXPORT_DIR, CHAPTER_TEMPLATE_FIELDS,
                     DEFAULT_CHAPTER_TEMPLATE, get_chapter_export_rows, get_index_selection)
from routers.voices import get_script_speaker
import voice_drift


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


def build_zip_download(members, filename):
    """Build a ZIP on disk and remove it after FileResponse finishes."""
    fd, path = tempfile.mkstemp(prefix=".alexandria-export-", suffix=".zip", dir=DATA_DIR)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for source, name in members:
                archive.write(source, name)
    except BaseException:
        os.unlink(path)
        raise
    return FileResponse(path, filename=filename, media_type="application/zip",
                        background=BackgroundTask(os.unlink, path))


class ChunkUpdate(BaseModel):
    text: Optional[str] = None
    instruct: Optional[str] = None
    speaker: Optional[str] = None
    pause_after: Optional[int] = None

    @field_validator("text", "instruct", "speaker", mode="before")
    @classmethod
    def validate_render_field(cls, value):
        if value is None:
            raise ValueError("Render fields cannot be null; omit unchanged fields")
        return value

class BatchGenerateRequest(BaseModel):
    indices: List[StrictInt] = Field(min_length=1)


class DriftCheckRequest(BaseModel):
    indices: Optional[List[int]] = None       # default: every done chunk


@router.get("/api/audiobook")
async def get_audiobook():
    if not os.path.exists(AUDIOBOOK_PATH):
        raise HTTPException(status_code=404, detail="Audiobook not found")
    return FileResponse(AUDIOBOOK_PATH, filename="audiobook.mp3", media_type="audio/mpeg")

# --- Chunk Management Endpoints ---

@router.get("/api/chunks")
async def get_chunks():
    return await asyncio.to_thread(_ensure_chunk_listing)


def _ensure_chunk_listing():
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        chunks = project_manager.load_chunks()
    return chunks

_editor_poll_snapshots = EditorPollSnapshots()


@router.get("/api/chunks/status")
async def get_chunk_status_snapshot(revision: Annotated[Optional[str], Query(max_length=64)] = None):
    return await asyncio.to_thread(_ensure_chunk_status_snapshot, revision)


def _ensure_chunk_status_snapshot(revision):
    root = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        chunks = project_manager.load_chunks()
        try:
            with open(os.path.join(root, 'state.json'), encoding='utf-8') as stream:
                state = json.load(stream)
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError('Invalid book identity for editor polling')
        identity = [os.path.abspath(root), state.get('active_book_id'), state.get('book_generation')]
        return _editor_poll_snapshots.ensure_snapshot(chunks, identity, revision)


class ChunkRestoreRequest(BaseModel):
    chunk: dict
    at_index: int

def _apply_chunk_edit(operation, *args):
    """Keep the conflict check and chunk mutation atomic with task claims."""
    with _gpu_lock:
        check_global_gpu_lock("audio")
        return operation(*args)

@router.post("/api/chunks/restore")
async def restore_chunk(request: ChunkRestoreRequest):
    """Re-insert a previously deleted chunk at a specific index."""
    chunks = await asyncio.to_thread(_apply_chunk_edit, project_manager.restore_chunk, request.at_index, request.chunk)
    if chunks is None:
        raise HTTPException(status_code=400, detail="Failed to restore chunk")
    return {"status": "ok", "total": len(chunks)}

# Registered before /api/chunks/{index}: FastAPI matches routes in order, and
# the integer wildcard swallowed this literal path (422 since #525).
@router.post("/api/chunks/drift_check")
async def drift_check_endpoint(request: DriftCheckRequest, background_tasks: BackgroundTasks):
    """Score done chunks against their speaker's reference voice (ECAPA cosine,
    CPU, sibling interpreter) and flag the ones that drifted. Never
    regenerates anything; the per-chunk Gen button is the fix."""
    state = process_state["drift_check"]
    if state["running"]:
        raise HTTPException(status_code=400, detail="A voice-drift check is already running.")
    threshold = voice_drift.get_drift_threshold(await asyncio.to_thread(load_app_config, CONFIG_PATH))
    python_bin = voice_drift.get_speaker_model_python(await asyncio.to_thread(_load_voicelab_config))
    indices = request.indices

    def task():
        state["running"] = True
        state["logs"] = ["Checking generated chunks against their reference voices..."]
        try:
            from tts import _resolve_asset_path
            chunks = project_manager.load_chunks()
            voice_config = safe_load_json(project_manager.voice_config_path, default={}) or {}
            report = voice_drift.check_voice_drift(
                chunks, voice_config, project_manager.root_dir, python_bin, threshold,
                indices=indices, resolve_alias=project_manager._resolve_alias,
                resolve_asset_path=_resolve_asset_path)
            if report["error"]:
                state["logs"].append(f"NOT MEASURED: {report['error']}")
                return
            flagged = voice_drift.apply_drift_results(project_manager, report["results"], threshold)
            scored = sum(1 for r in report["results"] if r["score"] is not None)
            state["logs"].append(
                f"Checked {scored} chunk(s) at threshold {threshold}: {flagged} flagged.")
            for r in report["results"]:
                if r["flagged"]:
                    state["logs"].append(f"  Chunk {r['index']} drifted: {r['score']} vs {r['reference']}")
        except Exception as e:
            logger.exception("Voice-drift check error")
            state["logs"].append(f"Voice-drift check error: {e}")
        finally:
            state["running"] = False

    # Reserve the task before handing it to FastAPI's background runner.  The
    # request-time running check above is only an early rejection; this atomic
    # claim closes the gap between two rapid requests both seeing it as idle.
    schedule_claimed_background_task(background_tasks, "drift_check", task)
    return {"status": "started", "threshold": threshold, "measured": python_bin is not None}

@router.post("/api/chunks/{index}")
async def update_chunk(index: int, update: ChunkUpdate):
    updates = update.model_dump(exclude_unset=True)
    logger.info(f"Updating chunk {index} with data: {updates}")
    chunk = await asyncio.to_thread(_apply_chunk_edit, project_manager.update_chunk, index, updates)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    logger.info(f"Chunk {index} updated, instruct is now: '{chunk.get('instruct', '')}'")
    return chunk

@router.post("/api/chunks/{index}/insert")
async def insert_chunk(index: int):
    """Insert an empty chunk after the given index."""
    chunks = await asyncio.to_thread(_apply_chunk_edit, project_manager.insert_chunk, index)
    if chunks is None:
        raise HTTPException(status_code=404, detail="Invalid chunk index")
    return {"status": "ok", "total": len(chunks)}

@router.delete("/api/chunks/{index}")
async def delete_chunk(index: int):
    """Delete a chunk at the given index."""
    result = await asyncio.to_thread(_apply_chunk_edit, project_manager.delete_chunk, index)
    if result is None:
        raise HTTPException(status_code=400, detail="Cannot delete chunk (invalid index or last remaining chunk)")
    deleted, chunks = result
    return {"status": "ok", "deleted": deleted, "total": len(chunks)}

@router.post("/api/chunks/{index}/generate")
async def generate_chunk_endpoint(index: int, background_tasks: BackgroundTasks):
    chunks = await asyncio.to_thread(project_manager.load_chunks)
    if not (0 <= index < len(chunks)):
        raise HTTPException(status_code=404, detail="Invalid chunk index")
    if not chunks[index].get("text", "").strip():
        raise HTTPException(status_code=400, detail="Cannot generate audio for an empty line")

    def task():
        state = process_state["audio"]
        state["start_time"] = time.time()
        state["logs"] = [f"Generating chunk {index}..."]
        state["status"] = "running"
        state["error"] = None
        try:
            success, message = project_manager.generate_chunk_audio(index)
            state["status"] = "done" if success else "failed"
            state["error"] = None if success else message
            state["logs"].append(
                f"Generation {'complete' if success else 'failed'}: {message}")
        except Exception as exc:
            logger.exception("Single-chunk generation error")
            state["status"] = "failed"
            state["error"] = str(exc)
            state["logs"].append(f"Generation failed: {exc}")
        finally:
            state["running"] = False

    # Same GPU resource as /api/generate_batch - must not race it. See F-032.
    schedule_claimed_background_task(background_tasks, "audio", task)
    return {"status": "started"}

class MergeRequest(BaseModel):
    integrity_confirmation: Optional[str] = Field(None, min_length=64, max_length=64,
                                                  pattern="^[0-9a-f]{64}$")


def _ensure_editor_integrity_locked():
    """Caller holds book state; native chunk loading may initialize missing rows."""
    chunks = project_manager.load_chunks()
    state = safe_load_json(os.path.join(DATA_DIR, "state.json"), {})
    try:
        script_bytes = Path(SCRIPT_PATH).read_bytes()
    except FileNotFoundError:
        script_bytes = b""
    return get_source_integrity(state, script_bytes, chunks), chunks


def _ensure_editor_integrity():
    with ensure_book_state(DATA_DIR):
        report, _chunks = _ensure_editor_integrity_locked()
        return report


@router.get("/api/editor/integrity")
async def get_editor_integrity():
    return await asyncio.to_thread(_ensure_editor_integrity)


@router.post("/api/merge")
async def merge_audio_endpoint(background_tasks: BackgroundTasks, request: Optional[MergeRequest] = None):
    return await asyncio.to_thread(_start_merge_audio, background_tasks, request)


def _start_merge_audio(background_tasks, request):
    with ensure_book_state(DATA_DIR):
        report, _chunks = _ensure_editor_integrity_locked()
        confirmation = request.integrity_confirmation if request is not None else None
        if ((confirmation is not None and confirmation != report["snapshot"])
                or (report["status"] != "verified" and confirmation is None)):
            raise HTTPException(status_code=409, detail=report)
        admitted_snapshot = report["snapshot"]

        def task():
            process_state["audio"]["start_time"] = time.time()
            process_state["audio"]["logs"] = ["Starting merge..."]
            try:
                with ensure_book_state(DATA_DIR):
                    current, chunks = _ensure_editor_integrity_locked()
                    if current["snapshot"] != admitted_snapshot:
                        raise ValueError("Source or editor chunks changed after merge admission; review again.")
                success, msg = project_manager.merge_audio(
                    cancel_check=lambda: process_state["audio"]["cancel"],
                    progress_callback=lambda m: process_state["audio"]["logs"].append(m),
                    chunks=chunks)
                if success:
                    process_state["audio"]["logs"].append(f"Merge complete: {msg}")
                elif msg == "Merge cancelled":
                    process_state["audio"]["logs"].append("Merge cancelled")
                else:
                    process_state["audio"]["logs"].append(f"Merge failed: {msg}")
            except Exception as e:
                process_state["audio"]["logs"].append(f"Merge error: {e}")
            finally:
                process_state["audio"]["running"] = False
                process_state["audio"]["cancel"] = False

        # Keep the existing atomic owned GPU/TTS admission after integrity approval.
    schedule_claimed_background_task(background_tasks, "audio", task)
    return {"status": "started"}

def get_export_task_result(success, message):
    """Adapt the project's success flag and exact cancellation sentinel, not logs."""
    return {"status": "done" if success else ("cancelled" if message == "Export cancelled" else "failed"),
            "message": str(message)}


@router.post("/api/export_audacity")
async def export_audacity_endpoint(background_tasks: BackgroundTasks):
    # Atomic check-and-set on the request thread (closes the double-start TOCTOU
    # where two rapid POSTs both pass a plain running check before either sets it).
    # audacity_export is a NON_GPU_TASK, so this only guards against self-double-start.

    def task():
        process_state["audacity_export"]["logs"] = ["Starting Audacity export..."]
        try:
            success, msg = project_manager.export_audacity(
                progress_callback=lambda m: process_state["audacity_export"]["logs"].append(m))
            process_state["audacity_export"]["result"] = get_export_task_result(success, msg)
            if success:
                process_state["audacity_export"]["logs"].append(f"Export complete: {msg}")
            else:
                process_state["audacity_export"]["logs"].append(f"Export failed: {msg}")
        except Exception as e:
            process_state["audacity_export"]["result"] = {"status": "failed", "message": str(e)}
            process_state["audacity_export"]["logs"].append(f"Export error: {e}")
        finally:
            process_state["audacity_export"]["running"] = False

    schedule_claimed_background_task(background_tasks, "audacity_export", task)
    process_state["audacity_export"]["result"] = None
    return {"status": "started"}

@router.get("/api/export_zip")
async def export_zip():
    return await asyncio.to_thread(_build_export_zip_response)


def _build_export_zip_response():
    """One zip of whatever exported audio exists (MP3, M4B); 404 when nothing does."""
    members = [(path, name) for path, name in ((AUDIOBOOK_PATH, "audiobook.mp3"), (M4B_PATH, "audiobook.m4b"))
               if os.path.exists(path)]
    if not members:
        raise HTTPException(status_code=404, detail="No exported audio found. Merge or export first.")
    return build_zip_download(members, "alexandria_export.zip")

@router.get("/api/export_audacity")
async def get_audacity_export():
    zip_path = os.path.join(DATA_DIR, "audacity_export.zip")
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Audacity export not found. Generate it first.")
    return FileResponse(zip_path, filename="audacity_export.zip", media_type="application/zip")

def get_unready_export_speakers():
    """Read readiness without treating unusable state as an empty roster."""
    script = safe_load_json(SCRIPT_PATH)
    voices = safe_load_json(os.path.join(DATA_DIR, "voice_config.json"))
    if not isinstance(script, list):
        raise HTTPException(status_code=503, detail="Export readiness unavailable: active script must be a JSON array")
    if not isinstance(voices, dict):
        raise HTTPException(status_code=503, detail="Export readiness unavailable: voice config must be a JSON object")
    speakers = {get_script_speaker(entry) for entry in script}
    missing = []
    for speaker in sorted(speakers - {""}):
        config = voices.get(speaker)
        if config is not None and not isinstance(config, dict):
            raise HTTPException(status_code=503, detail=f"Export readiness unavailable: voice config for {speaker} must be an object")
        if not config or not config.get("ready"):
            missing.append(speaker)
    return missing


async def validate_export_readiness(require_ready):
    if require_ready:
        missing = await asyncio.to_thread(get_unready_export_speakers)
        if missing:
            raise HTTPException(status_code=409, detail={
                "message": "Mark every speaker ready before exporting.", "speakers": missing})


class M4bExportRequest(BaseModel):
    per_chunk_chapters: bool = False
    title: str = ""
    author: str = ""
    narrator: str = ""
    year: str = ""
    description: str = ""
    require_ready: bool = False

@router.post("/api/merge_m4b")
async def merge_m4b_endpoint(request: M4bExportRequest, background_tasks: BackgroundTasks):
    await validate_export_readiness(request.require_ready)
    # Atomic check-and-set on the request thread (closes the double-start TOCTOU
    # where two rapid POSTs both pass a plain running check before either sets it).

    def task():
        process_state["m4b_export"]["logs"] = ["Starting M4B export..."]
        try:
            meta = {
                "title": request.title,
                "author": request.author,
                "narrator": request.narrator,
                "year": request.year,
                "description": request.description,
                "cover_path": os.path.join(DATA_DIR, "m4b_cover.jpg") if os.path.exists(os.path.join(DATA_DIR, "m4b_cover.jpg")) else "",
            }
            success, msg = project_manager.merge_m4b(
                per_chunk_chapters=request.per_chunk_chapters, metadata=meta,
                cancel_check=lambda: process_state["m4b_export"].get("cancel", False),
                progress_callback=lambda message: process_state["m4b_export"]["logs"].append(message))
            process_state["m4b_export"]["result"] = get_export_task_result(success, msg)
            if success:
                process_state["m4b_export"]["logs"].append(f"Export complete: {msg}")
            else:
                process_state["m4b_export"]["logs"].append(f"Export failed: {msg}")
        except Exception as e:
            process_state["m4b_export"]["result"] = {"status": "failed", "message": str(e)}
            process_state["m4b_export"]["logs"].append(f"Export error: {e}")
        finally:
            process_state["m4b_export"]["running"] = False

    schedule_claimed_background_task(background_tasks, "m4b_export", task)
    process_state["m4b_export"]["result"] = None
    return {"status": "started"}

@router.post("/api/merge_m4b/cancel")
async def cancel_m4b_export():
    state = process_state["m4b_export"]
    if not state["running"]:
        raise HTTPException(status_code=400, detail="No M4B export is running.")
    state["cancel"] = True
    return {"status": "cancelling"}


class ChapterExportRequest(BaseModel):
    format: str = "mp3"
    per_chunk_chapters: bool = False
    template: str = DEFAULT_CHAPTER_TEMPLATE
    padding: int = Field(2, ge=0, le=6)
    book_name: str = ""
    series_name: str = ""
    volume_number: str = ""
    chapters: Optional[List[StrictInt]] = None     # indices to export; None = all
    changed_only: bool = False     
    require_ready: bool = False


def _chapter_export_dir():
    return os.path.join(DATA_DIR, CHAPTER_EXPORT_DIR)


@router.post("/api/export_chapters")
async def export_chapters(request: ChapterExportRequest, background_tasks: BackgroundTasks):
    """Write chapters as separate MP3/WAV files (CPU only, no GPU lock)."""
    if request.format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp3 or wav")
    await validate_export_readiness(request.require_ready)
    chapters = request.chapters
    if chapters is not None:
        try:
            preview = await asyncio.to_thread(
                project_manager.preview_chapter_filenames,
                fmt=request.format, per_chunk_chapters=request.per_chunk_chapters,
                template=request.template, padding=request.padding,
                book_name=request.book_name, series_name=request.series_name,
                volume_number=request.volume_number)
            chapters = get_index_selection(chapters, len(preview))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    state = process_state["chapter_export"]

    def task():
        state["logs"] = ["Starting chapter export..."]
        try:
            success, msg = project_manager.export_chapters(
                fmt=request.format, per_chunk_chapters=request.per_chunk_chapters,
                template=request.template, padding=request.padding,
                book_name=request.book_name, series_name=request.series_name,
                volume_number=request.volume_number, chapters=chapters,
                changed_only=request.changed_only,
                progress_callback=lambda m: state["logs"].append(m),
                cancel_check=lambda: state["cancel"])
            state["result"] = get_export_task_result(success, msg)
            state["logs"].append(f"Export complete: {msg}" if success else f"Export failed: {msg}")
        except Exception as e:
            state["result"] = {"status": "failed", "message": str(e)}
            state["logs"].append(f"Export error: {e}")
        finally:
            state["running"] = False
            state["cancel"] = False

    schedule_claimed_background_task(background_tasks, "chapter_export", task)
    state["result"] = None
    return {"status": "started"}


@router.post("/api/export_chapters/cancel")
async def cancel_chapter_export():
    state = process_state["chapter_export"]
    if not state["running"]:
        raise HTTPException(status_code=400, detail="No chapter export is running.")
    state["cancel"] = True
    return {"status": "cancelling"}


@router.get("/api/export_chapters/preview")
async def preview_chapter_filenames(format: str = "mp3", per_chunk_chapters: bool = False,
                                    template: str = DEFAULT_CHAPTER_TEMPLATE, padding: int = 2,
                                    book_name: str = "", series_name: str = "",
                                    volume_number: str = "", chapters: Annotated[Optional[List[int]], Query()] = None,
                                    changed_only: bool = False, require_ready: bool = False):
    """The selected filenames an export would write after existing-file reuse."""
    if format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp3 or wav")
    if not 0 <= padding <= 6:
        raise HTTPException(status_code=400, detail="padding must be 0-6 digits")
    await validate_export_readiness(require_ready)
    try:
        rows = await asyncio.to_thread(project_manager.preview_chapter_filenames,
            fmt=format, per_chunk_chapters=per_chunk_chapters, template=template,
            padding=padding, book_name=book_name,
            series_name=series_name, volume_number=volume_number,
            chapters=chapters, changed_only=changed_only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"fields": list(CHAPTER_TEMPLATE_FIELDS), "chapters": rows}


@router.get("/api/chapter_exports")
async def list_chapter_exports():
    return await asyncio.to_thread(_get_chapter_export_listing)


def _get_chapter_export_listing():
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    rows = []
    for row in get_chapter_export_rows(manifest):
        try:
            path, _ = _exported_chapter_path(row["file"])
        except HTTPException:
            rows.append({**row, "exists": False, "bytes": 0})
        else:
            rows.append({**row, "exists": True, "bytes": os.path.getsize(path)})
    return {**manifest, "chapters": rows}


def _exported_chapter_path(name):
    safe = os.path.basename(name)
    path = os.path.join(_chapter_export_dir(), safe)
    if (safe != name or "\\" in name
            or os.path.splitext(safe)[1].lower() not in {".mp3", ".wav"}
            or not is_path_inside(path, _chapter_export_dir())
            or not os.path.isfile(path)):
        raise HTTPException(status_code=404, detail="No such exported chapter")
    return path, safe


@router.get("/api/chapter_exports/file/{name}")
async def download_chapter(name: str):
    path, safe = await asyncio.to_thread(_get_listed_chapter_path, name)
    media = "audio/mpeg" if safe.lower().endswith(".mp3") else "audio/wav"
    return FileResponse(path, filename=safe, media_type=media)


def _get_listed_chapter_path(name):
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    if not any(row["file"] == name for row in get_chapter_export_rows(manifest)):
        raise HTTPException(status_code=404, detail="No such exported chapter")
    return _exported_chapter_path(name)


@router.get("/api/chapter_exports/zip")
async def download_chapters_zip(names: Optional[str] = None):
    return await asyncio.to_thread(_build_chapter_zip_response, names)


def _build_chapter_zip_response(names: Optional[str] = None):
    """All exported chapters, or the comma-separated `names`, in one zip."""
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    wanted = set(n for n in (names or "").split(",") if n) or None
    members = []
    for row in get_chapter_export_rows(manifest):
        if wanted is not None and row["file"] not in wanted:
            continue
        try:
            members.append(_exported_chapter_path(row["file"]))
        except HTTPException:
            continue
    if not members:
        raise HTTPException(status_code=404, detail="No exported chapters found. Export first.")
    return build_zip_download(
        members,
        "chapters.zip")


@router.get("/api/audiobook_m4b")
async def get_audiobook_m4b():
    if not os.path.exists(M4B_PATH):
        raise HTTPException(status_code=404, detail="M4B audiobook not found. Export it first.")
    return FileResponse(M4B_PATH, filename="audiobook.m4b", media_type="audio/mp4")

def _apply_m4b_cover(cover_tmp, cover_path):
    """Validate uploaded image data before publishing an MP4-compatible cover."""
    from PIL import Image, UnidentifiedImageError
    normalized = cover_tmp + ".jpg"
    try:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(cover_tmp) as image:
                    image.verify()
                with Image.open(cover_tmp) as image:
                    image.load()
                    converted = image.convert("RGB") if image.format not in {"JPEG", "PNG"} else None
        except (UnidentifiedImageError, OSError, ValueError,
                Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise HTTPException(status_code=400, detail="Cover must contain a complete, decodable image") from exc
        source = cover_tmp
        if converted is not None:
            try:
                converted.save(normalized, format="JPEG")
            finally:
                converted.close()
            source = normalized
        os.replace(source, cover_path)
    finally:
        for path in (cover_tmp, normalized):
            if os.path.exists(path):
                os.remove(path)


@router.post("/api/m4b_cover")
async def upload_m4b_cover(file: UploadFile = File(...)):
    """Upload a cover image for M4B export."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    cover_path = os.path.join(DATA_DIR, "m4b_cover.jpg")
    cover_tmp = cover_path + f".upload.{uuid.uuid4().hex}"
    try:
        await _save_upload_limited(file, cover_tmp, 25 * 1024**2)
        await asyncio.to_thread(_apply_m4b_cover, cover_tmp, cover_path)
    finally:
        if os.path.exists(cover_tmp):
            os.remove(cover_tmp)
    return {"status": "uploaded", "path": "m4b_cover.jpg"}

@router.delete("/api/m4b_cover")
async def delete_m4b_cover():
    """Remove the uploaded cover image."""
    cover_path = os.path.join(DATA_DIR, "m4b_cover.jpg")
    if os.path.exists(cover_path):
        os.remove(cover_path)
    return {"status": "removed"}

@router.post("/api/generate_batch")
async def generate_batch_endpoint(request: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """Generate multiple chunks in parallel using configured worker count."""
    check_global_gpu_lock("audio")

    # Load worker count from config
    workers = 2
    cfg = await asyncio.to_thread(load_app_config, CONFIG_PATH)
    workers = max(1, cfg.get("tts", {}).get("parallel_workers", 2))

    try:
        chunks = await asyncio.to_thread(project_manager.load_chunks)
        indices = get_index_selection(request.indices, len(chunks))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    total = len(indices)

    def progress_callback(completed, failed, total):
        """Update logs with progress."""
        process_state["audio"]["logs"].append(
            f"Progress: {completed + failed}/{total} ({completed} done, {failed} failed)"
        )

    def cancel_check():
        return process_state["audio"]["cancel"]

    def task():
        process_state["audio"]["running"] = True
        process_state["audio"]["start_time"] = time.time()
        process_state["audio"]["logs"] = [
            f"Starting parallel generation of {total} chunks with {workers} workers..."
        ]
        try:
            results = project_manager.generate_chunks_parallel(
                indices, workers, progress_callback, cancel_check=cancel_check
            )
            completed = len(results["completed"])
            failed = len(results["failed"])
            cancelled = results.get("cancelled", 0)
            msg = f"Batch generation complete: {completed} succeeded, {failed} failed"
            if cancelled:
                msg += f", {cancelled} cancelled"
            process_state["audio"]["logs"].append(msg)
            if results["failed"]:
                for idx, err in results["failed"]:
                    process_state["audio"]["logs"].append(f"  Chunk {idx} failed: {err}")
        except Exception as e:
            logger.exception("Batch generation error")
            process_state["audio"]["logs"].append(f"Batch generation error: {e}")
        finally:
            process_state["audio"]["running"] = False
            process_state["audio"]["cancel"] = False

    schedule_claimed_background_task(background_tasks, "audio", task)
    return {"status": "started", "workers": workers, "total_chunks": total}


@router.post("/api/generate_batch_fast")
async def generate_batch_fast_endpoint(request: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """Generate multiple chunks using batch TTS API with single seed. Faster but less flexible.
    Requires custom Qwen3-TTS with /generate_batch endpoint."""
    check_global_gpu_lock("audio")

    # Load batch_seed and batch_size from config
    batch_seed = -1
    batch_size = 4
    batch_group_by_type = False
    cfg = await asyncio.to_thread(load_app_config, CONFIG_PATH)
    try:
        tts_cfg = cfg.get("tts", {})
        seed_val = tts_cfg.get("batch_seed")
        if seed_val is not None and seed_val != "":
            batch_seed = int(seed_val)
        batch_size = max(1, tts_cfg.get("parallel_workers", 4))
        batch_group_by_type = tts_cfg.get("batch_group_by_type", False)
    except (TypeError, ValueError) as e:
        _warn_corrupted_json("config", CONFIG_PATH, "using default batch settings", e)

    try:
        chunks = await asyncio.to_thread(project_manager.load_chunks)
        indices = get_index_selection(request.indices, len(chunks))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    total = len(indices)

    def progress_callback(completed, failed, total):
        process_state["audio"]["logs"].append(
            f"Progress: {completed + failed}/{total} ({completed} done, {failed} failed)"
        )

    def cancel_check():
        return process_state["audio"]["cancel"]

    def task():
        process_state["audio"]["running"] = True
        process_state["audio"]["start_time"] = time.time()
        process_state["audio"]["logs"] = [
            f"Starting batch generation of {total} chunks (batch_size={batch_size}, seed={batch_seed})..."
        ]
        try:
            results = project_manager.generate_chunks_batch(
                indices, batch_seed, batch_size, progress_callback,
                batch_group_by_type=batch_group_by_type,
                cancel_check=cancel_check,
            )
            completed = len(results["completed"])
            failed = len(results["failed"])
            cancelled = results.get("cancelled", 0)
            msg = f"Batch generation complete: {completed} succeeded, {failed} failed"
            if cancelled:
                msg += f", {cancelled} cancelled"
            process_state["audio"]["logs"].append(msg)
            if results["failed"]:
                for idx, err in results["failed"]:
                    process_state["audio"]["logs"].append(f"  Chunk {idx} failed: {err}")
        except Exception as e:
            logger.exception("Batch generation error")
            process_state["audio"]["logs"].append(f"Batch generation error: {e}")
        finally:
            process_state["audio"]["running"] = False
            process_state["audio"]["cancel"] = False

    schedule_claimed_background_task(background_tasks, "audio", task)
    return {"status": "started", "batch_seed": batch_seed, "batch_size": batch_size, "total_chunks": total}

@router.post("/api/cancel_audio")
async def cancel_audio():
    return await asyncio.to_thread(_apply_audio_cancel)


def _apply_audio_cancel():
    """Cancel ongoing audio generation and reset in-progress chunks."""
    # Keep the idle decision and disk reset atomic with new audio claims.
    with _gpu_lock:
        if process_state["audio"]["running"]:
            process_state["audio"]["cancel"] = True
            process_state["audio"]["logs"].append("[CANCEL] Cancellation requested")
            return {"status": "cancelling"}

        reset_count = 0
        chunks = project_manager.load_chunks()
        if chunks:
            for chunk in chunks:
                if chunk.get("status") == "generating":
                    chunk["status"] = "pending"
                    reset_count += 1
            if reset_count:
                project_manager.save_chunks(chunks)
        return {"status": "not_running", "reset_chunks": reset_count}

## ── Saved Scripts ──────────────────────────────────────────────

@router.get("/api/reports")
async def list_reports():
    return await asyncio.to_thread(_get_reports)


def _get_report_path(filename: str):
    """Resolve one regular Markdown report within the configured report root."""
    if (os.path.basename(filename) != filename or "\\" in filename
            or not filename.endswith(".md")):
        raise HTTPException(status_code=400, detail="Invalid report filename.")
    filepath = os.path.realpath(os.path.join(REPORTS_DIR, filename))
    if not is_path_inside(filepath, REPORTS_DIR):
        raise HTTPException(status_code=400, detail="Invalid report path.")
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Report not found.")
    return filepath


def _get_reports():
    """List all generated review reports in the reports/ directory, newest first."""
    if not os.path.isdir(REPORTS_DIR):
        return []
    reports = []
    for f in os.listdir(REPORTS_DIR):
        if not f.endswith(".md"):
            continue
        try:
            filepath = _get_report_path(f)
            entry = {
                "filename": f,
                "type": "batch" if f.startswith("batch_review_") else "review",
                "mtime": os.path.getmtime(filepath),
                "size": os.path.getsize(filepath),
            }
        except (OSError, HTTPException):
            # File vanished between listdir and stat (concurrent delete) - skip it.
            continue
        entry["can_explain"] = False
        try:
            entry["can_explain"] = not get_review_report_info(filepath)["incomplete"]
        except (OSError, ValueError):
            # Legacy, edited and incomplete records remain readable in the list.
            pass
        reports.append(entry)
    reports.sort(key=lambda r: r["mtime"], reverse=True)
    return reports


@router.get("/api/reports/{filename}")
async def get_report(filename: str):
    return await asyncio.to_thread(_get_report_response, filename)


@router.post("/api/reports/{filename}/explain")
async def explain_report(filename: str, background_tasks: BackgroundTasks):
    filepath = _get_report_path(filename)
    try:
        info = await asyncio.to_thread(get_review_report_info, filepath)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if info["incomplete"]:
        raise HTTPException(status_code=409, detail="Incomplete reviews retain their deterministic summary")

    def run():
        state = process_state["report_explanation"]
        state.update(status="running", filename=filename, error=None, logs=[])
        try:
            if state.get("cancel"):
                state["status"] = "cancelled"
                return
            candidate = _llm_summarize_report(info["content"])
            if not candidate or not _is_evidence_bound_summary(candidate):
                raise ValueError("Explanation unavailable or unsupported; the original report was retained")
            if state.get("cancel"):
                state["status"] = "cancelled"
                return
            apply_review_report_explanation(filepath, info["sha256"], candidate)
            state["status"] = "done"
            state["logs"].append("Report explanation saved")
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = str(exc)
            state["logs"].append(str(exc))

    run_id = schedule_claimed_background_task(background_tasks, "report_explanation", run)
    process_state["report_explanation"].update(run_id=run_id, filename=filename,
                                              status="pending", error=None, logs=[])
    return {"status":"started", "filename":filename, "run_id":run_id}


class ReportExplanationCancelRequest(BaseModel):
    run_id: str


@router.post("/api/reports/explanation/cancel")
async def cancel_report_explanation(request: ReportExplanationCancelRequest):
    state = process_state["report_explanation"]
    if not state["running"] or state.get("run_id") != request.run_id:
        raise HTTPException(status_code=409, detail="This explanation run is no longer active")
    state["cancel"] = True
    return {"status":"cancel_requested"}


def _get_report_response(filename: str):
    """Return the raw Markdown contents of a generated report."""
    filepath = _get_report_path(filename)
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/markdown")


def _summarize_review_checkpoint(path: str) -> Optional[dict]:
    """Summarize a *.review_checkpoint.json for the UI: how far it got and where
    a resumed review would pick up. Returns None if the file isn't a usable
    checkpoint."""
    try:
        data = load_generation_checkpoint_document(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "completed_batches" not in data:
        return None
    completed = data.get("completed_batches")
    total = data.get("total_batches", 0)
    failed = data.get("failed_batches")
    batch_lengths = data.get("batch_lengths")
    stats = data.get("total_stats")
    corrected = data.get("all_corrected")
    # Legacy optional null fields still mean no progress; other shapes cannot
    # safely support arithmetic, sorting, or the UI's entry counts.
    completed = 0 if completed is None else completed
    total = 0 if total is None else total
    failed = [] if failed is None else failed
    batch_lengths = [] if batch_lengths is None else batch_lengths
    stats = {} if stats is None else stats
    corrected = [] if corrected is None else corrected
    if (not isinstance(completed, int) or not isinstance(total, int)
            or not isinstance(failed, list)
            or any(not isinstance(batch, int) for batch in failed)
            or not isinstance(batch_lengths, list)
            or not isinstance(stats, dict) or not isinstance(corrected, list)):
        return None
    failed = sorted(failed)
    # Mirror load_checkpoint's rewind: a failed batch (with full batch_lengths
    # coverage) rewinds the resume point back to the first failed batch.
    resume_from_batch = completed + 1
    if failed and len(batch_lengths) == completed:
        resume_from_batch = failed[0]
    try:
        mtime = os.path.getmtime(path)
        for directory in get_generation_checkpoint_artifacts(path)[1:]:
            mtime = max(mtime, os.path.getmtime(directory))
    except FileNotFoundError:
        return None
    return {
        "completed_batches": completed,
        "total_batches": total,
        "resume_from_batch": resume_from_batch,
        "entries_done": len(corrected),
        "batch_size": data.get("batch_size"),
        "context_window": data.get("context_window"),
        "failed_batches": failed,
        "batches_skipped_vram": stats.get("batches_skipped_vram", 0),
        "text_changed": stats.get("text_changed", 0),
        "speaker_changed": stats.get("speaker_changed", 0),
        "mtime": mtime,
    }


@router.get("/api/review/checkpoints")
async def list_review_checkpoints():
    """List saved review checkpoints (what's done + where a re-run resumes), plus
    the live pass/order if a batch review is currently running."""
    out = await asyncio.to_thread(_get_review_checkpoint_rows)

    # Live pass/order while a bidirectional batch is mid-flight.
    bstate = process_state.get("batch_review", {})
    live = None
    if bstate.get("running"):
        live = {
            "bidirectional": bstate.get("bidirectional", False),
            "current_pass": bstate.get("current_pass"),  # "fwd" / "bwd" / None
            "current_task_idx": bstate.get("current_task_idx"),
            "tasks": [
                {"name": t.get("name"), "status": t.get("status")}
                for t in bstate.get("tasks", []) if isinstance(t, dict)
            ],
        }
    return {"checkpoints": out, "live": live}


def _get_review_checkpoint_rows():
    out = []
    suffix = ".review_checkpoint.json"

    active_cp = SCRIPT_PATH + suffix
    if os.path.exists(active_cp):
        s = _summarize_review_checkpoint(active_cp)
        if s:
            out.append({"book": "(active script)", **s})

    if os.path.isdir(SCRIPTS_DIR):
        for f in sorted(os.listdir(SCRIPTS_DIR)):
            if not f.endswith(suffix):
                continue
            book = f[:-len(suffix)]
            if book.endswith(".json"):
                book = book[:-5]  # "{name}.json.review_checkpoint.json" -> "{name}"
            s = _summarize_review_checkpoint(os.path.join(SCRIPTS_DIR, f))
            if s:
                out.append({"book": book, **s})

    out.sort(key=lambda c: c.get("mtime") or 0, reverse=True)

    return out
