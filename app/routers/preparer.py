import json
import os
import signal
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core import (
    BASE_DIR,
    PREPARER_OUTPUT_DIR,
    PREPARER_SCRIPT_PATH,
    ROOT_DIR,
    UPLOADS_DIR,
    _load_voicelab_config,
    _revalidate_voicelab_paths,
    _run_claimed_background_task,
    _save_upload_limited,
    _send_signal_tree,
    _stream_subprocess_to_logs,
    _validate_voicelab_path,
    check_disk_space,
    check_global_gpu_lock,
    claim_gpu_task,
    process_state,
)
from utils import secure_filename


router = APIRouter()


class PreparerConfig(BaseModel):
    audio_filename: str
    source_filename: Optional[str] = None
    output_filename: str = "alexandria_dataset.zip"
    model: Optional[str] = None
    fallback_model: Optional[str] = None
    source_threshold: float = 0.65
    keep_unaligned: bool = False
    chunk_size: float = 10.0
    lang: str = "en"
    resume: bool = False
    skip_annotation: bool = False
    source_start: Optional[int] = None
    source_start_text: Optional[str] = None
    no_auto_anchor: bool = False
    # Optimization: LLM annotation batch size (3 = ~25% faster)
    batch_size: int = 1
    # LLM enrichment
    enrich_with_llm: bool = False
    llm_model_path: Optional[str] = None
    enrich_speaker_attribution: bool = False
    enrich_narration_style: bool = False
    enrich_emotional_tone: bool = False
    # Quality filtering
    min_chunk_duration: float = 2.0
    min_confidence: float = 0.85
    min_snr: int = 25

class BatchPreparerTask(BaseModel):
    audio_filename: str
    output_filename: str

class BatchPreparerRequest(BaseModel):
    tasks: List[BatchPreparerTask]
    lang: str = "en"
    min_confidence: float = 0.85
    min_snr: int = 25


# ── Preparer ─────────────────────────────────────────────────────────────────

def _resolve_preparer_interpreter() -> str:
    """Return the interpreter to run the preparer with, or raise 503.

    alexandria_preparer_rocm_compatible.py imports torch/llama-cpp/whisper,
    which the web app's own env lacks, so it must run under the configurable
    rocm_python interpreter (shared with Voice Lab) rather than sys.executable.
    """
    interpreter = _load_voicelab_config()["rocm_python"]
    if not os.path.exists(PREPARER_SCRIPT_PATH):
        raise HTTPException(
            status_code=503,
            detail=f"Preparer script not found at {PREPARER_SCRIPT_PATH}.",
        )
    if not os.path.isfile(interpreter):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Preparer needs the ROCm interpreter (torch/llama-cpp); not "
                f"found: {interpreter}. Set 'rocm_python' in Voice Lab settings."
            ),
        )
    # Same denylist voicelab_save_config/voicelab_start enforce on this exact
    # config value - the preparer endpoints execute it too and must not skip
    # the check just because they read it through a different function.
    _validate_voicelab_path(interpreter, "rocm_python")
    return interpreter


@router.post("/api/preparer/start")
async def preparer_start(
    background_tasks: BackgroundTasks,
    config_json: str = Form(...),
    audio_file: UploadFile = File(...),
    source_file: Optional[UploadFile] = File(None),
):
    """Upload audio (and optionally a source EPUB/TXT) and run the preparer
    to generate a voice training dataset."""
    try:
        config = PreparerConfig(**json.loads(config_json))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid config: {e}")
    if config.skip_annotation:
        raise HTTPException(status_code=400, detail="Skip annotation is not implemented.")
    if config.enrich_with_llm:
        if not config.llm_model_path:
            raise HTTPException(status_code=400, detail="LLM model path is required for enrichment.")
        if not any((config.enrich_speaker_attribution,
                    config.enrich_narration_style,
                    config.enrich_emotional_tone)):
            raise HTTPException(status_code=400, detail="Select at least one enrichment category.")

    interpreter = _resolve_preparer_interpreter()
    check_global_gpu_lock("preparer")

    has_space, free_gb = check_disk_space(ROOT_DIR, 2.0)
    if not has_space:
        raise HTTPException(status_code=400, detail=f"Insufficient disk space ({free_gb} GB free, 2 GB required).")

    audio_filename = secure_filename(config.audio_filename)
    if not audio_filename:
        raise HTTPException(status_code=400, detail="Invalid audio filename")
    output_filename = secure_filename(config.output_filename)
    if not output_filename:
        raise HTTPException(status_code=400, detail="Invalid output filename")
    audio_path = os.path.join(UPLOADS_DIR, audio_filename)
    source_path = None
    try:
        await _save_upload_limited(audio_file, audio_path, 20 * 1024**3)
        if source_file is not None:
            source_filename = secure_filename(config.source_filename or source_file.filename)
            if not source_filename:
                raise HTTPException(status_code=400, detail="Invalid source filename")
            source_path = os.path.join(UPLOADS_DIR, source_filename)
            await _save_upload_limited(source_file, source_path, 512 * 1024**2)
    except Exception:
        for upload_path in (audio_path, source_path):
            if upload_path and os.path.exists(upload_path):
                os.remove(upload_path)
        raise

    def _run():
        state = process_state["preparer"]
        state["running"] = True
        state["logs"] = []
        state["status"] = "running"
        state["output_file"] = None
        state["process"] = None

        # Re-validate immediately before exec, not just synchronously above -
        # background_tasks.add_task defers this whole closure until after the
        # HTTP response is sent, leaving a window where rocm_python (or a
        # model/fallback_model/llm_model_path pointed inside an
        # upload/generated-content directory) could be repointed before the
        # subprocess below actually starts.
        e = _revalidate_voicelab_paths(
            (interpreter, "rocm_python"),
            (config.model, "model"),
            (config.fallback_model, "fallback_model"),
            (config.llm_model_path, "llm_model_path"),
        )
        if e:
            state["status"] = "failed"
            state["running"] = False
            state["logs"].append(f"Aborted: {e.detail}")
            return

        cmd = [interpreter, "-u", PREPARER_SCRIPT_PATH,
               "--audio", audio_path,
               "--output", os.path.join(PREPARER_OUTPUT_DIR, output_filename),
               "--lang", config.lang,
               "--min-confidence", str(config.min_confidence),
               "--min-snr", str(config.min_snr),
               "--chunk-size", str(config.chunk_size),
               "--min-chunk-duration", str(config.min_chunk_duration),
               "--batch-size", str(config.batch_size)]
        if config.resume:
            cmd.append("--resume")
        if config.model:
            cmd.extend(["--model", config.model])
        if config.fallback_model:
            cmd.extend(["--fallback-model", config.fallback_model])
        # Source-alignment options only make sense with a source file.
        if source_path:
            cmd.extend(["--source", source_path,
                        "--source-threshold", str(config.source_threshold)])
            if config.keep_unaligned:
                cmd.append("--keep-unaligned")
            if config.source_start is not None:
                cmd.extend(["--source-start", str(config.source_start)])
            if config.source_start_text:
                cmd.extend(["--source-start-text", config.source_start_text])
            if config.no_auto_anchor:
                cmd.append("--no-auto-anchor")
        if config.enrich_with_llm:
            cmd.append("--enrich-with-llm")
            if config.llm_model_path:
                cmd.extend(["--llm-model-path", config.llm_model_path])
            if config.enrich_speaker_attribution:
                cmd.append("--enrich-speaker-attribution")
            if config.enrich_narration_style:
                cmd.append("--enrich-narration-style")
            if config.enrich_emotional_tone:
                cmd.append("--enrich-emotional-tone")

        rc, _ = _stream_subprocess_to_logs(cmd, BASE_DIR, state)

        if state.get("cancel"):
            state["status"] = "cancelled"
            state["logs"].append("Preparer cancelled.")
        elif rc == 0:
            state["status"] = "done"
            state["output_file"] = output_filename
            state["logs"].append("Preparer completed successfully.")
        else:
            state["status"] = "failed"
            state["logs"].append(f"Preparer failed (exit code {rc}).")

        state["running"] = False
        state["process"] = None

    claim_gpu_task("preparer")
    background_tasks.add_task(_run_claimed_background_task, "preparer", _run)
    return {"status": "started"}


@router.post("/api/preparer/cancel")
async def preparer_cancel():
    state = process_state["preparer"]
    if not state["running"]:
        raise HTTPException(status_code=400, detail="No preparer is currently running.")
    state["cancel"] = True
    proc = state.get("process")
    if proc and proc.poll() is None:
        try:
            _send_signal_tree(proc, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    return {"status": "cancel_requested"}


@router.get("/api/preparer/list")
async def preparer_list_outputs():
    """List completed dataset ZIP files available for download."""
    files = []
    if not os.path.exists(PREPARER_OUTPUT_DIR):
        return {"files": files}
    for fname in sorted(os.listdir(PREPARER_OUTPUT_DIR)):
        if not fname.endswith(".zip"):
            continue
        fpath = os.path.join(PREPARER_OUTPUT_DIR, fname)
        try:
            entry = {
                "filename": fname,
                "size_mb": round(os.path.getsize(fpath) / (1024 * 1024), 1),
                "modified": os.path.getmtime(fpath),
            }
        except OSError:
            # File vanished between listdir and stat (concurrent delete) - skip it.
            continue
        files.append(entry)
    return {"files": files}


@router.get("/api/preparer/download/{filename:path}")
async def preparer_download(filename: str):
    """Download a generated dataset ZIP."""
    root = os.path.realpath(PREPARER_OUTPUT_DIR)
    file_path = os.path.realpath(os.path.join(PREPARER_OUTPUT_DIR, filename))
    if not file_path.startswith(root + os.sep) and file_path != root:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, media_type="application/zip", filename=os.path.basename(file_path))


@router.post("/api/preparer/batch/start")
async def preparer_batch_start(request: BatchPreparerRequest, background_tasks: BackgroundTasks):
    """Process multiple audio files sequentially through the preparer script."""
    interpreter = _resolve_preparer_interpreter()
    check_global_gpu_lock("batch_preparer")

    has_space, free_gb = check_disk_space(ROOT_DIR, 5.0)
    if not has_space:
        raise HTTPException(status_code=400, detail=f"Insufficient disk space ({free_gb} GB free, 5 GB recommended).")

    def _run():
        state = process_state["batch_preparer"]
        state["running"] = True
        state["logs"] = [f"Starting batch of {len(request.tasks)} tasks..."]
        state["tasks"] = [{"audio": t.audio_filename, "status": "pending"} for t in request.tasks]
        state["current_task_idx"] = -1

        existing_outputs = set()
        if os.path.exists(PREPARER_OUTPUT_DIR):
            existing_outputs = {e.name for e in os.scandir(PREPARER_OUTPUT_DIR) if e.is_file()}

        for i, task in enumerate(request.tasks):
            if state["cancel"]:
                state["logs"].append("Batch cancelled.")
                break

            # Re-validate before EVERY subprocess launch, not just once before
            # the loop - this batch makes many sequential launches all reusing
            # the same captured `interpreter`, and background_tasks.add_task's
            # deferral means even an up-front check only proves the path was
            # valid when the request was *received*, not at each later launch.
            # A single pre-loop check (the original version of this fix) left
            # tasks 2..N unprotected against a config change made after task 1
            # had already started - exactly the race this exists to close.
            e = _revalidate_voicelab_paths((interpreter, "rocm_python"))
            if e:
                state["logs"].append(f"Aborted: {e.detail}")
                state["tasks"][i]["status"] = "failed"
                break

            state["current_task_idx"] = i
            state["tasks"][i]["status"] = "running"

            audio_filename = secure_filename(task.audio_filename)
            audio_path = os.path.join(UPLOADS_DIR, audio_filename) if audio_filename else None
            if not audio_path or not os.path.exists(audio_path):
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — audio not found: {task.audio_filename}")
                state["tasks"][i]["status"] = "failed"
                continue

            state["logs"].append(f"--- [{i+1}/{len(request.tasks)}] {task.audio_filename} ---")

            # Sanitize output filename to prevent path traversal
            safe_output = secure_filename(task.output_filename)
            if not safe_output:
                state["logs"].append(f"[{i+1}] Skipping — invalid output filename: {task.output_filename}")
                state["tasks"][i]["status"] = "failed"
                continue

            # Ensure unique filename across directory and current batch
            base, ext = os.path.splitext(safe_output)
            candidate = safe_output
            counter = 1
            while candidate in existing_outputs:
                if counter > 1000:
                    state["logs"].append(f"[{i+1}] Skipping — too many filename collisions for: {safe_output}")
                    state["tasks"][i]["status"] = "failed"
                    candidate = None
                    break
                counter += 1
                candidate = f"{base}_{counter}{ext}"
            if candidate is None:
                continue
            existing_outputs.add(candidate)
            safe_output = candidate

            cmd = [interpreter, "-u", PREPARER_SCRIPT_PATH,
                   "--audio", audio_path,
                   "--output", os.path.join(PREPARER_OUTPUT_DIR, safe_output),
                   "--lang", request.lang,
                   "--min-confidence", str(request.min_confidence),
                   "--min-snr", str(request.min_snr)]

            rc, _ = _stream_subprocess_to_logs(cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ")

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                break
            elif rc == 0:
                state["tasks"][i]["status"] = "done"
                state["logs"].append(f"[{i+1}] Done: {task.audio_filename}")
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{i+1}] Failed (exit {rc}): {task.audio_filename}")

        state["running"] = False
        state["logs"].append("Batch processing finished.")

    claim_gpu_task("batch_preparer")
    background_tasks.add_task(_run_claimed_background_task, "batch_preparer", _run)
    return {"status": "started", "task_count": len(request.tasks)}


@router.post("/api/preparer/batch/cancel")
async def preparer_batch_cancel():
    process_state["batch_preparer"]["cancel"] = True
    return {"status": "cancel_requested"}
