import asyncio
import gc
import json
import logging
import os
import shutil
import sys
import time
import zipfile

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from archive_utils import validate_zip_members

from core import (
    BUILTIN_LORA_DIR,
    LORA_DATASETS_DIR,
    LORA_MODELS_DIR,
    LORA_MODELS_MANIFEST,
    _cancel_task,
    _load_builtin_lora_manifest,
    _load_manifest,
    _require_safe_filename,
    _safe_subpath,
    _save_manifest,
    _save_upload_limited,
    check_global_gpu_lock,
    claim_gpu_task,
    process_state,
    project_manager,
    run_process,
)
from hf_utils import (
    builtin_hf_name,
    download_builtin_adapter,
    fetch_builtin_manifest,
    is_adapter_downloaded,
)
from utils import get_unique_id, is_path_inside, secure_filename


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


def _safe_extractall(zf: "zipfile.ZipFile", dest_dir: str) -> None:
    """zipfile.extractall, but reject members that would escape dest_dir
    (Zip-Slip path traversal via '../' entries or absolute paths)."""
    try:
        validate_zip_members(zf, dest_dir)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    zf.extractall(dest_dir)


class LoraTrainingRequest(BaseModel):
    name: str
    dataset_id: str
    epochs: int = Field(default=5, ge=1, le=1000)
    lr: float = Field(default=5e-6, gt=0, le=1)
    batch_size: int = Field(default=1, ge=1, le=64)
    lora_r: int = Field(default=32, ge=1, le=1024)
    lora_alpha: int = Field(default=128, ge=1, le=4096)
    gradient_accumulation_steps: int = Field(default=8, ge=1, le=1024)
    language: str = "english"

class LoraTestRequest(BaseModel):
    adapter_id: str
    text: str
    instruct: str = ""


## ── LoRA Training ──────────────────────────────────────────────


def _extract_lora_dataset_archive(archive_path, dataset_dir):
    """Perform potentially multi-gigabyte extraction outside the event loop."""
    os.makedirs(dataset_dir, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        _safe_extractall(archive, dataset_dir)

@router.post("/api/lora/upload_dataset")
async def lora_upload_dataset(file: UploadFile = File(...)):
    """Upload a ZIP containing WAV files and metadata.jsonl."""
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a .zip archive")

    # Derive dataset name from ZIP filename
    base_name = os.path.splitext(file.filename)[0]
    dataset_name = secure_filename(base_name)
    if not dataset_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name from filename")

    dataset_dir = os.path.join(LORA_DATASETS_DIR, dataset_name)
    if os.path.exists(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{dataset_name}' already exists")

    # Save ZIP temporarily, then extract
    tmp_path = os.path.join(LORA_DATASETS_DIR, f"_tmp_{dataset_name}.zip")
    try:
        await _save_upload_limited(file, tmp_path, 4 * 1024**3)

        await asyncio.to_thread(_extract_lora_dataset_archive, tmp_path, dataset_dir)

        # Check for metadata.jsonl (may be inside a subdirectory)
        metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
        if not os.path.exists(metadata_path):
            # Check one level deep
            for entry in os.listdir(dataset_dir):
                candidate = os.path.join(dataset_dir, entry, "metadata.jsonl")
                if os.path.isdir(os.path.join(dataset_dir, entry)) and os.path.exists(candidate):
                    # Move contents up
                    nested = os.path.join(dataset_dir, entry)
                    for item in os.listdir(nested):
                        shutil.move(os.path.join(nested, item), os.path.join(dataset_dir, item))
                    os.rmdir(nested)
                    metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
                    break

        if not os.path.exists(metadata_path):
            shutil.rmtree(dataset_dir)
            raise HTTPException(status_code=400, detail="ZIP must contain metadata.jsonl")

        # Count samples and validate audio file presence
        sample_count = 0
        valid_sample_count = 0
        missing_audio = []
        malformed_lines = []
        with open(metadata_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError(f"line is valid JSON but not an object (got {type(entry).__name__})")
                    audio_rel = entry.get("audio_filepath") or entry.get("audio", "")
                    audio_path = os.path.realpath(os.path.join(dataset_dir, audio_rel)) if audio_rel else ""
                    if (not audio_rel or not is_path_inside(audio_path, dataset_dir)
                            or not os.path.isfile(audio_path)):
                        missing_audio.append(audio_rel)
                    else:
                        valid_sample_count += 1
                    sample_count += 1
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    malformed_lines.append((line_num, str(e)))

        wav_count = sum(1 for f in os.listdir(dataset_dir) if f.lower().endswith(".wav"))
        ref_wav = os.path.exists(os.path.join(dataset_dir, "ref.wav"))
        ref_text = os.path.exists(os.path.join(dataset_dir, "ref_text.txt"))

        logger.info(
            f"LoRA dataset '{dataset_name}': {sample_count} metadata entries, "
            f"{wav_count} WAV files, ref.wav={'yes' if ref_wav else 'MISSING'}, "
            f"ref_text.txt={'yes' if ref_text else 'missing'}"
        )
        if missing_audio:
            logger.warning(
                f"LoRA dataset '{dataset_name}': {len(missing_audio)} audio file(s) in "
                f"metadata.jsonl not found in ZIP: {missing_audio[:5]}"
                f"{'  (+more)' if len(missing_audio) > 5 else ''}"
            )
        else:
            logger.info(f"LoRA dataset '{dataset_name}': all {sample_count} audio files present in ZIP")
        if malformed_lines:
            logger.warning(
                f"LoRA dataset '{dataset_name}': {len(malformed_lines)} malformed "
                f"metadata.jsonl line(s) skipped: {malformed_lines[:5]}"
                f"{'  (+more)' if len(malformed_lines) > 5 else ''}"
            )

        if valid_sample_count == 0:
            raise HTTPException(status_code=400, detail="Dataset contains no usable training audio.")

        return {"status": "uploaded", "dataset_id": dataset_name,
                "sample_count": valid_sample_count, "metadata_count": sample_count}
    except Exception:
        if os.path.isdir(dataset_dir):
            shutil.rmtree(dataset_dir)
        raise
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@router.get("/api/lora/datasets")
async def lora_list_datasets():
    """List uploaded LoRA training datasets."""
    datasets = []
    if not os.path.exists(LORA_DATASETS_DIR):
        return datasets

    for name in sorted(os.listdir(LORA_DATASETS_DIR)):
        dataset_dir = os.path.join(LORA_DATASETS_DIR, name)
        if not os.path.isdir(dataset_dir):
            continue
        metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
        sample_count = 0
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        sample_count += 1
        except FileNotFoundError:
            # No metadata yet, or it vanished mid-listing (concurrent delete).
            pass
        datasets.append({"dataset_id": name, "sample_count": sample_count})
    return datasets

@router.delete("/api/lora/datasets/{dataset_id}")
async def lora_delete_dataset(dataset_id: str):
    """Delete an uploaded dataset."""
    dataset_dir = _safe_subpath(LORA_DATASETS_DIR, dataset_id)
    if not os.path.isdir(dataset_dir):
        raise HTTPException(status_code=404, detail="Dataset not found")

    shutil.rmtree(dataset_dir)
    logger.info(f"LoRA dataset deleted: {dataset_id}")
    return {"status": "deleted", "dataset_id": dataset_id}

@router.post("/api/lora/train/cancel")
async def lora_cancel_training():
    """Cancel a running LoRA training subprocess (it holds the global GPU lock for
    hours, so without this the only way to stop it was killing the whole server)."""
    return _cancel_task("lora_training",
                        "No LoRA training is currently running.",
                        "LoRA training already exited.")


@router.post("/api/lora/train")
async def lora_start_training(request: LoraTrainingRequest, background_tasks: BackgroundTasks):
    """Start LoRA training as a subprocess."""
    check_global_gpu_lock("lora_training")

    # Validate dataset exists
    dataset_dir = _safe_subpath(LORA_DATASETS_DIR, request.dataset_id)
    if not os.path.isdir(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{request.dataset_id}' not found")

    # Build output directory
    safe_name = _require_safe_filename(request.name, "Invalid adapter name")

    adapter_id = get_unique_id(safe_name)
    output_dir = os.path.join(LORA_MODELS_DIR, adapter_id)

    # Log dataset details and effective settings before training
    try:
        meta_path = os.path.join(dataset_dir, "metadata.jsonl")
        with open(meta_path, encoding="utf-8") as metadata_file:
            dataset_sample_count = sum(1 for line in metadata_file if line.strip())
        total_passes = dataset_sample_count * request.epochs
        alpha_r = request.lora_alpha / request.lora_r
        logger.info(
            f"LoRA training '{request.name}': dataset='{request.dataset_id}' "
            f"samples={dataset_sample_count}, epochs={request.epochs}, "
            f"total_passes={total_passes}, lr={request.lr:.2e}, "
            f"r={request.lora_r}, alpha={request.lora_alpha} (scale={alpha_r:.1f}x), "
            f"grad_accum={request.gradient_accumulation_steps}, language={request.language}"
        )
    except (OSError, ValueError, ZeroDivisionError):
        pass
    if project_manager.engine is not None:
        logger.info("Unloading TTS engine for LoRA training...")
        project_manager.engine = None
        gc.collect()

    # Build subprocess command
    command = [
        sys.executable, "-u", "train_lora.py",
        "--data_dir", dataset_dir,
        "--output_dir", output_dir,
        "--epochs", str(request.epochs),
        "--lr", str(request.lr),
        "--batch_size", str(request.batch_size),
        "--lora_r", str(request.lora_r),
        "--lora_alpha", str(request.lora_alpha),
        "--gradient_accumulation_steps", str(request.gradient_accumulation_steps),
        "--language", request.language,
    ]

    def on_training_complete():
        """After training subprocess finishes, update manifest if adapter was saved."""
        run_process(command, "lora_training")

        # Check if training produced an adapter
        if os.path.isdir(output_dir) and os.path.exists(os.path.join(output_dir, "training_meta.json")):
            try:
                with open(os.path.join(output_dir, "training_meta.json"), "r") as f:
                    meta = json.load(f)

                manifest = _load_manifest(LORA_MODELS_MANIFEST)
                manifest.append({
                    "id": adapter_id,
                    "name": request.name,
                    "dataset_id": request.dataset_id,
                    "epochs": meta.get("epochs", request.epochs),
                    "final_loss": meta.get("final_loss"),
                    "sample_count": meta.get("num_samples"),
                    "lora_r": meta.get("lora_r"),
                    "lr": meta.get("lr"),
                    "created": time.time(),
                })
                _save_manifest(LORA_MODELS_MANIFEST, manifest)
                logger.info(f"LoRA adapter registered: {adapter_id}")
            except Exception as e:
                logger.error(f"Failed to update LoRA manifest: {e}")

    claim_gpu_task("lora_training")
    background_tasks.add_task(on_training_complete)
    return {"status": "started", "adapter_id": adapter_id}

@router.get("/api/lora/models")
async def lora_list_models():
    """List all LoRA adapters (built-in + user-trained)."""
    models = _load_builtin_lora_manifest() + _load_manifest(LORA_MODELS_MANIFEST)
    for m in models:
        is_builtin = m.get("builtin", False)
        is_downloaded = m.get("downloaded", True)  # user-trained are always downloaded

        if not is_downloaded:
            m["preview_audio_url"] = None
            continue

        if is_builtin:
            adapter_dir = os.path.join(BUILTIN_LORA_DIR, m["id"])
            url_prefix = f"/builtin_lora/{m['id']}"
        else:
            adapter_dir = os.path.join(LORA_MODELS_DIR, m["id"])
            url_prefix = f"/lora_models/{m['id']}"
        preview_path = os.path.join(adapter_dir, "preview_sample.wav")
        m["preview_audio_url"] = f"{url_prefix}/preview_sample.wav" if os.path.exists(preview_path) else None
    return models

@router.delete("/api/lora/models/{adapter_id}")
async def lora_delete_model(adapter_id: str):
    """Delete a trained LoRA adapter. Built-in adapters cannot be deleted."""
    builtin = _load_builtin_lora_manifest()
    if any(m["id"] == adapter_id for m in builtin):
        raise HTTPException(status_code=403, detail="Built-in adapters cannot be deleted")
    manifest = _load_manifest(LORA_MODELS_MANIFEST)
    entry = next((m for m in manifest if m["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Adapter not found")

    # Delete adapter directory
    adapter_dir = os.path.join(LORA_MODELS_DIR, adapter_id)
    if os.path.isdir(adapter_dir):
        shutil.rmtree(adapter_dir)

    # Remove from manifest
    manifest = [m for m in manifest if m["id"] != adapter_id]
    _save_manifest(LORA_MODELS_MANIFEST, manifest)

    logger.info(f"LoRA adapter deleted: {adapter_id}")
    return {"status": "deleted", "adapter_id": adapter_id}

@router.post("/api/lora/download/{adapter_id}")
async def lora_download_builtin(adapter_id: str):
    """Download a built-in LoRA adapter from HuggingFace."""
    manifest = await asyncio.to_thread(fetch_builtin_manifest, BUILTIN_LORA_DIR)
    hf_name = builtin_hf_name(adapter_id)
    entry = next((e for e in manifest if e["id"] == hf_name or e["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown built-in adapter: {adapter_id}")

    if is_adapter_downloaded(adapter_id, BUILTIN_LORA_DIR):
        return {"status": "already_downloaded", "adapter_id": adapter_id}

    try:
        await asyncio.to_thread(download_builtin_adapter, adapter_id, BUILTIN_LORA_DIR)
        logger.info(f"Built-in adapter downloaded: {adapter_id}")
        return {"status": "downloaded", "adapter_id": adapter_id}
    except Exception as e:
        logger.exception("Download failed for %s", adapter_id)
        raise HTTPException(status_code=500, detail="Built-in adapter download failed — see server logs for details.") from e

@router.post("/api/lora/test")
async def lora_test_model(request: LoraTestRequest):
    """Generate test audio using a LoRA adapter (built-in or user-trained)."""
    # Fail fast before the manifest lookup / possible adapter auto-download
    # below. See F-039.
    check_global_gpu_lock("lora_test")
    # Check both manifests
    builtin = _load_builtin_lora_manifest()
    user_trained = _load_manifest(LORA_MODELS_MANIFEST)
    all_adapters = builtin + user_trained
    entry = next((m for m in all_adapters if m["id"] == request.adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Adapter not found")

    is_builtin = entry.get("builtin", False)
    if is_builtin:
        adapter_dir = os.path.join(BUILTIN_LORA_DIR, request.adapter_id)
        audio_url_prefix = f"/builtin_lora/{request.adapter_id}"
    else:
        adapter_dir = os.path.join(LORA_MODELS_DIR, request.adapter_id)
        audio_url_prefix = f"/lora_models/{request.adapter_id}"

    if not os.path.isdir(adapter_dir) and not is_builtin:
        raise HTTPException(status_code=404, detail="Adapter files not found")

    # Claim the GPU slot now, before the possible adapter download and the
    # engine load below - both can take real time and the engine load
    # allocates VRAM. Claiming only after them (the old order) left a window
    # where two concurrent /api/lora/test (or .../preview, which shares this
    # slot) requests could both pass check_global_gpu_lock above and both
    # start that slow/VRAM work before either's claim landed.
    claim_gpu_task("lora_test")
    try:
        if not os.path.isdir(adapter_dir) and is_builtin:
            try:
                download_builtin_adapter(request.adapter_id, BUILTIN_LORA_DIR)
                adapter_dir = os.path.join(BUILTIN_LORA_DIR, request.adapter_id)
            except Exception as e:
                logger.exception("Auto-download failed for %s", request.adapter_id)
                raise HTTPException(status_code=500, detail="Adapter auto-download failed — see server logs for details.") from e

        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

        output_filename = f"test_{request.adapter_id}_{int(time.time())}.wav"
        output_path = os.path.join(adapter_dir, output_filename)

        voice_data = {
            "type": "lora",
            "adapter_id": request.adapter_id,
            "adapter_path": adapter_dir,
        }
        voice_config = {"_lora_test_": voice_data}
        engine.generate_voice(
            text=request.text,
            instruct_text=request.instruct or "",
            speaker="_lora_test_",
            voice_config=voice_config,
            output_path=output_path,
        )

        return {
            "status": "ok",
            "audio_url": f"{audio_url_prefix}/{output_filename}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("LoRA test generation failed")
        raise HTTPException(status_code=500, detail="LoRA test generation failed — see server logs for details.") from e
    finally:
        process_state["lora_test"]["running"] = False

LORA_PREVIEW_TEXT = "The ancient library stood at the crossroads of two forgotten paths, its weathered stone walls covered in ivy that had been growing for centuries."

@router.post("/api/lora/preview/{adapter_id}")
async def lora_preview(adapter_id: str):
    """Generate or return cached preview audio for a LoRA adapter."""
    builtin = _load_builtin_lora_manifest()
    user_trained = _load_manifest(LORA_MODELS_MANIFEST)
    all_adapters = builtin + user_trained
    entry = next((m for m in all_adapters if m["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Adapter not found")

    is_builtin = entry.get("builtin", False)
    if is_builtin:
        adapter_dir = os.path.join(BUILTIN_LORA_DIR, adapter_id)
        url_prefix = f"/builtin_lora/{adapter_id}"
    else:
        adapter_dir = os.path.join(LORA_MODELS_DIR, adapter_id)
        url_prefix = f"/lora_models/{adapter_id}"

    if not os.path.isdir(adapter_dir) and not is_builtin:
        raise HTTPException(status_code=404, detail="Adapter files not found")

    preview_path = os.path.join(adapter_dir, "preview_sample.wav")

    # Return cached if exists. This check intentionally runs BEFORE the lock -
    # no GPU/download work happens on a cache hit, regardless of whether the
    # adapter directory exists yet (a cached preview implies it does).
    if os.path.exists(preview_path):
        return {"status": "cached", "audio_url": f"{url_prefix}/preview_sample.wav"}

    # Cache miss past this point. Shares the "lora_test" slot with
    # /api/lora/test since both are "try out this adapter" operations that
    # shouldn't run concurrently with each other either. See F-040.
    check_global_gpu_lock("lora_test")
    # Claim immediately after the check, before the possible adapter download
    # AND the engine load below - both can take real time and the engine
    # load allocates VRAM, so the claim has to land before either starts, not
    # after, or two concurrent preview/test requests can both pass the check
    # above and both begin downloading/loading the model.
    claim_gpu_task("lora_test")
    try:
        if not os.path.isdir(adapter_dir) and is_builtin:
            try:
                download_builtin_adapter(adapter_id, BUILTIN_LORA_DIR)
                adapter_dir = os.path.join(BUILTIN_LORA_DIR, adapter_id)
            except Exception as e:
                logger.exception("Auto-download failed for %s", adapter_id)
                raise HTTPException(status_code=500, detail="Adapter auto-download failed — see server logs for details.") from e

        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

        voice_data = {
            "type": "lora",
            "adapter_id": adapter_id,
            "adapter_path": adapter_dir,
        }
        voice_config = {"_lora_preview_": voice_data}
        engine.generate_voice(
            text=LORA_PREVIEW_TEXT,
            instruct_text="",
            speaker="_lora_preview_",
            voice_config=voice_config,
            output_path=preview_path,
        )
        return {"status": "generated", "audio_url": f"{url_prefix}/preview_sample.wav"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("LoRA preview generation failed")
        raise HTTPException(status_code=500, detail="LoRA preview generation failed — see server logs for details.") from e
    finally:
        process_state["lora_test"]["running"] = False
