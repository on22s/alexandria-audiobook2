import logging
import os
import time
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from config_settings import load_app_config

from core import (
    AUDIOBOOK_PATH,
    CONFIG_PATH,
    DATA_DIR,
    M4B_PATH,
    REPORTS_DIR,
    SCRIPTS_DIR,
    SCRIPT_PATH,
    _load_voicelab_config,
    _save_upload_limited,
    _warn_corrupted_json,
    check_global_gpu_lock,
    claim_gpu_task,
    process_state,
    project_manager,
)
from utils import safe_load_json
from project import CHAPTER_EXPORT_DIR, CHAPTER_TEMPLATE_FIELDS, DEFAULT_CHAPTER_TEMPLATE
import voice_drift


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


class ChunkUpdate(BaseModel):
    text: Optional[str] = None
    instruct: Optional[str] = None
    speaker: Optional[str] = None
    pause_after: Optional[int] = None

class BatchGenerateRequest(BaseModel):
    indices: List[int]


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
    chunks = project_manager.load_chunks()
    return chunks

class ChunkRestoreRequest(BaseModel):
    chunk: dict
    at_index: int

@router.post("/api/chunks/restore")
async def restore_chunk(request: ChunkRestoreRequest):
    """Re-insert a previously deleted chunk at a specific index."""
    chunks = project_manager.restore_chunk(request.at_index, request.chunk)
    if chunks is None:
        raise HTTPException(status_code=400, detail="Failed to restore chunk")
    return {"status": "ok", "total": len(chunks)}

@router.post("/api/chunks/{index}")
async def update_chunk(index: int, update: ChunkUpdate):
    updates = update.model_dump(exclude_unset=True)
    logger.info(f"Updating chunk {index} with data: {updates}")
    chunk = project_manager.update_chunk(index, updates)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    logger.info(f"Chunk {index} updated, instruct is now: '{chunk.get('instruct', '')}'")
    return chunk

@router.post("/api/chunks/{index}/insert")
async def insert_chunk(index: int):
    """Insert an empty chunk after the given index."""
    chunks = project_manager.insert_chunk(index)
    if chunks is None:
        raise HTTPException(status_code=404, detail="Invalid chunk index")
    return {"status": "ok", "total": len(chunks)}

@router.delete("/api/chunks/{index}")
async def delete_chunk(index: int):
    """Delete a chunk at the given index."""
    result = project_manager.delete_chunk(index)
    if result is None:
        raise HTTPException(status_code=400, detail="Cannot delete chunk (invalid index or last remaining chunk)")
    deleted, chunks = result
    return {"status": "ok", "deleted": deleted, "total": len(chunks)}

@router.post("/api/chunks/{index}/generate")
async def generate_chunk_endpoint(index: int, background_tasks: BackgroundTasks):
    chunks = project_manager.load_chunks()
    if not (0 <= index < len(chunks)):
        raise HTTPException(status_code=404, detail="Invalid chunk index")
    if not chunks[index].get("text", "").strip():
        raise HTTPException(status_code=400, detail="Cannot generate audio for an empty line")

    def task():
        try:
            project_manager.generate_chunk_audio(index)
        finally:
            process_state["audio"]["running"] = False

    # Same GPU resource as /api/generate_batch - must not race it. See F-032.
    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started"}

@router.post("/api/merge")
async def merge_audio_endpoint(background_tasks: BackgroundTasks):
    # Reuse audio process state for merge if possible, or just background it
    # For simplicity, we just background it and frontend will assume it works
    # Or we can link it to process_state["audio"]

    def task():
        process_state["audio"]["start_time"] = time.time()
        process_state["audio"]["cancel"] = False
        process_state["audio"]["logs"] = ["Starting merge..."]
        try:
            success, msg = project_manager.merge_audio(
                cancel_check=lambda: process_state["audio"]["cancel"],
                progress_callback=lambda m: process_state["audio"]["logs"].append(m))
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

    # Claim the GPU/TTS slot atomically on the request thread: a merge shares
    # process_state["audio"] with generation, so without this two rapid POSTs (or
    # a merge started during generation) both pass and clobber each other, and a
    # merge's early finally would free the lock while TTS is still in flight.
    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started"}

@router.post("/api/export_audacity")
async def export_audacity_endpoint(background_tasks: BackgroundTasks):
    # Atomic check-and-set on the request thread (closes the double-start TOCTOU
    # where two rapid POSTs both pass a plain running check before either sets it).
    # audacity_export is a NON_GPU_TASK, so this only guards against self-double-start.
    claim_gpu_task("audacity_export")

    def task():
        process_state["audacity_export"]["logs"] = ["Starting Audacity export..."]
        try:
            success, msg = project_manager.export_audacity(
                progress_callback=lambda m: process_state["audacity_export"]["logs"].append(m))
            if success:
                process_state["audacity_export"]["logs"].append(f"Export complete: {msg}")
            else:
                process_state["audacity_export"]["logs"].append(f"Export failed: {msg}")
        except Exception as e:
            process_state["audacity_export"]["logs"].append(f"Export error: {e}")
        finally:
            process_state["audacity_export"]["running"] = False

    background_tasks.add_task(task)
    return {"status": "started"}

@router.get("/api/export_zip")
async def export_zip():
    """One zip of whatever exported audio exists (MP3, M4B); 404 when nothing does."""
    import io
    import zipfile
    members = [(path, name) for path, name in ((AUDIOBOOK_PATH, "audiobook.mp3"), (M4B_PATH, "audiobook.m4b"))
               if os.path.exists(path)]
    if not members:
        raise HTTPException(status_code=404, detail="No exported audio found. Merge or export first.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # already-compressed audio
        for path, name in members:
            zf.write(path, name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": "attachment; filename=alexandria_export.zip"})

@router.get("/api/export_audacity")
async def get_audacity_export():
    zip_path = os.path.join(DATA_DIR, "audacity_export.zip")
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Audacity export not found. Generate it first.")
    return FileResponse(zip_path, filename="audacity_export.zip", media_type="application/zip")

class M4bExportRequest(BaseModel):
    per_chunk_chapters: bool = False
    title: str = ""
    author: str = ""
    narrator: str = ""
    year: str = ""
    description: str = ""

@router.post("/api/merge_m4b")
async def merge_m4b_endpoint(request: M4bExportRequest, background_tasks: BackgroundTasks):
    # Atomic check-and-set on the request thread (closes the double-start TOCTOU
    # where two rapid POSTs both pass a plain running check before either sets it).
    claim_gpu_task("m4b_export")

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
            success, msg = project_manager.merge_m4b(per_chunk_chapters=request.per_chunk_chapters, metadata=meta)
            if success:
                process_state["m4b_export"]["logs"].append(f"Export complete: {msg}")
            else:
                process_state["m4b_export"]["logs"].append(f"Export failed: {msg}")
        except Exception as e:
            process_state["m4b_export"]["logs"].append(f"Export error: {e}")
        finally:
            process_state["m4b_export"]["running"] = False

    background_tasks.add_task(task)
    return {"status": "started"}

class ChapterExportRequest(BaseModel):
    format: str = "mp3"
    per_chunk_chapters: bool = False
    template: str = DEFAULT_CHAPTER_TEMPLATE
    padding: int = 2
    book_name: str = ""
    series_name: str = ""
    volume_number: str = ""
    chapters: Optional[List[int]] = None     # indices to export; None = all
    changed_only: bool = False


def _chapter_export_dir():
    return os.path.join(DATA_DIR, CHAPTER_EXPORT_DIR)


@router.post("/api/export_chapters")
async def export_chapters(request: ChapterExportRequest, background_tasks: BackgroundTasks):
    """Write chapters as separate MP3/WAV files (CPU only, no GPU lock)."""
    if request.format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp3 or wav")
    if not 0 <= request.padding <= 6:
        raise HTTPException(status_code=400, detail="padding must be 0-6 digits")
    claim_gpu_task("chapter_export")
    state = process_state["chapter_export"]

    def task():
        state["cancel"] = False
        state["logs"] = ["Starting chapter export..."]
        try:
            success, msg = project_manager.export_chapters(
                fmt=request.format, per_chunk_chapters=request.per_chunk_chapters,
                template=request.template, padding=request.padding,
                book_name=request.book_name, series_name=request.series_name,
                volume_number=request.volume_number, chapters=request.chapters,
                changed_only=request.changed_only,
                progress_callback=lambda m: state["logs"].append(m),
                cancel_check=lambda: state["cancel"])
            state["logs"].append(f"Export complete: {msg}" if success else f"Export failed: {msg}")
        except Exception as e:
            state["logs"].append(f"Export error: {e}")
        finally:
            state["running"] = False
            state["cancel"] = False

    background_tasks.add_task(task)
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
                                    volume_number: str = ""):
    """The filenames an export would produce, without decoding any audio."""
    if format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp3 or wav")
    return {"fields": list(CHAPTER_TEMPLATE_FIELDS),
            "chapters": project_manager.preview_chapter_filenames(
                fmt=format, per_chunk_chapters=per_chunk_chapters, template=template,
                padding=max(0, min(padding, 6)), book_name=book_name,
                series_name=series_name, volume_number=volume_number)}


@router.get("/api/chapter_exports")
async def list_chapter_exports():
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    rows = []
    for row in manifest.get("chapters", []):
        path = os.path.join(_chapter_export_dir(), row.get("file", ""))
        rows.append({**row, "exists": os.path.isfile(path),
                     "bytes": os.path.getsize(path) if os.path.isfile(path) else 0})
    return {**manifest, "chapters": rows}


def _exported_chapter_path(name):
    safe = os.path.basename(name)
    path = os.path.join(_chapter_export_dir(), safe)
    if safe != name or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No such exported chapter")
    return path, safe


@router.get("/api/chapter_exports/file/{name}")
async def download_chapter(name: str):
    path, safe = _exported_chapter_path(name)
    media = "audio/mpeg" if safe.lower().endswith(".mp3") else "audio/wav"
    return FileResponse(path, filename=safe, media_type=media)


@router.get("/api/chapter_exports/zip")
async def download_chapters_zip(names: Optional[str] = None):
    """All exported chapters, or the comma-separated `names`, in one zip."""
    import io
    import zipfile
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    wanted = set(n for n in (names or "").split(",") if n) or None
    members = [(row["file"]) for row in manifest.get("chapters", [])
               if (wanted is None or row["file"] in wanted)
               and os.path.isfile(os.path.join(_chapter_export_dir(), row["file"]))]
    if not members:
        raise HTTPException(status_code=404, detail="No exported chapters found. Export first.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # already-compressed audio
        for name in members:
            zf.write(os.path.join(_chapter_export_dir(), name), name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": "attachment; filename=chapters.zip"})


@router.get("/api/audiobook_m4b")
async def get_audiobook_m4b():
    if not os.path.exists(M4B_PATH):
        raise HTTPException(status_code=404, detail="M4B audiobook not found. Export it first.")
    return FileResponse(M4B_PATH, filename="audiobook.m4b", media_type="audio/mp4")

@router.post("/api/m4b_cover")
async def upload_m4b_cover(file: UploadFile = File(...)):
    """Upload a cover image for M4B export."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    cover_path = os.path.join(DATA_DIR, "m4b_cover.jpg")
    cover_tmp = cover_path + ".upload"
    await _save_upload_limited(file, cover_tmp, 25 * 1024**2)
    os.replace(cover_tmp, cover_path)
    return {"status": "uploaded", "path": cover_path}

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
    cfg = load_app_config(CONFIG_PATH)
    workers = max(1, cfg.get("tts", {}).get("parallel_workers", 2))

    indices = request.indices
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

    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started", "workers": workers, "total_chunks": total}

@router.post("/api/chunks/drift_check")
async def drift_check_endpoint(request: DriftCheckRequest, background_tasks: BackgroundTasks):
    """Score done chunks against their speaker's reference voice (ECAPA cosine,
    CPU, sibling interpreter) and flag the ones that drifted. Never
    regenerates anything; the per-chunk Gen button is the fix."""
    state = process_state["drift_check"]
    if state["running"]:
        raise HTTPException(status_code=400, detail="A voice-drift check is already running.")
    threshold = voice_drift.get_drift_threshold(load_app_config(CONFIG_PATH))
    python_bin = voice_drift.get_speaker_model_python(_load_voicelab_config())
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

    background_tasks.add_task(task)
    return {"status": "started", "threshold": threshold, "measured": python_bin is not None}

@router.post("/api/generate_batch_fast")
async def generate_batch_fast_endpoint(request: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """Generate multiple chunks using batch TTS API with single seed. Faster but less flexible.
    Requires custom Qwen3-TTS with /generate_batch endpoint."""
    check_global_gpu_lock("audio")

    # Load batch_seed and batch_size from config
    batch_seed = -1
    batch_size = 4
    batch_group_by_type = False
    cfg = load_app_config(CONFIG_PATH)
    try:
        tts_cfg = cfg.get("tts", {})
        seed_val = tts_cfg.get("batch_seed")
        if seed_val is not None and seed_val != "":
            batch_seed = int(seed_val)
        batch_size = max(1, tts_cfg.get("parallel_workers", 4))
        batch_group_by_type = tts_cfg.get("batch_group_by_type", False)
    except (TypeError, ValueError) as e:
        _warn_corrupted_json("config", CONFIG_PATH, "using default batch settings", e)

    indices = request.indices
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

    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started", "batch_seed": batch_seed, "batch_size": batch_size, "total_chunks": total}

@router.post("/api/cancel_audio")
async def cancel_audio():
    """Cancel ongoing audio generation and reset in-progress chunks."""
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
    """List all generated review reports in the reports/ directory, newest first."""
    if not os.path.isdir(REPORTS_DIR):
        return []
    reports = []
    for f in os.listdir(REPORTS_DIR):
        if not f.endswith(".md"):
            continue
        filepath = os.path.join(REPORTS_DIR, f)
        try:
            entry = {
                "filename": f,
                "type": "batch" if f.startswith("batch_review_") else "review",
                "mtime": os.path.getmtime(filepath),
                "size": os.path.getsize(filepath),
            }
        except OSError:
            # File vanished between listdir and stat (concurrent delete) - skip it.
            continue
        reports.append(entry)
    reports.sort(key=lambda r: r["mtime"], reverse=True)
    return reports


@router.get("/api/reports/{filename}")
async def get_report(filename: str):
    """Return the raw Markdown contents of a generated report."""
    # Prevent directory traversal via URL encoding or other tricks
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid report filename.")

    filepath = os.path.join(REPORTS_DIR, safe_name)
    # Resolve to absolute path and verify it's within REPORTS_DIR
    abs_filepath = os.path.abspath(filepath)
    abs_reports_dir = os.path.abspath(REPORTS_DIR)
    if not abs_filepath.startswith(abs_reports_dir):
        raise HTTPException(status_code=400, detail="Invalid report path.")

    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Report not found.")
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/markdown")


def _summarize_review_checkpoint(path: str) -> Optional[dict]:
    """Summarize a *.review_checkpoint.json for the UI: how far it got and where
    a resumed review would pick up. Returns None if the file isn't a usable
    checkpoint."""
    data = safe_load_json(path)
    if not isinstance(data, dict) or "completed_batches" not in data:
        return None
    completed = data.get("completed_batches", 0) or 0
    total = data.get("total_batches", 0) or 0
    failed = sorted(data.get("failed_batches", []) or [])
    batch_lengths = data.get("batch_lengths", []) or []
    stats = data.get("total_stats", {}) or {}
    # Mirror load_checkpoint's rewind: a failed batch (with full batch_lengths
    # coverage) rewinds the resume point back to the first failed batch.
    resume_from_batch = completed + 1
    if failed and len(batch_lengths) == completed:
        resume_from_batch = failed[0]
    return {
        "completed_batches": completed,
        "total_batches": total,
        "resume_from_batch": resume_from_batch,
        "entries_done": len(data.get("all_corrected", []) or []),
        "batch_size": data.get("batch_size"),
        "context_window": data.get("context_window"),
        "failed_batches": failed,
        "batches_skipped_vram": stats.get("batches_skipped_vram", 0),
        "text_changed": stats.get("text_changed", 0),
        "speaker_changed": stats.get("speaker_changed", 0),
        "mtime": os.path.getmtime(path) if os.path.exists(path) else None,
    }


@router.get("/api/review/checkpoints")
async def list_review_checkpoints():
    """List saved review checkpoints (what's done + where a re-run resumes), plus
    the live pass/order if a batch review is currently running."""
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
