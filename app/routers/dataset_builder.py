import json
import hashlib
import asyncio
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Annotated, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import (
    DATASET_BUILDER_DIR,
    LORA_DATASETS_DIR,
    _require_safe_filename,
    _safe_subpath,
    check_global_gpu_lock,
    claim_gpu_task, reserve_background_task, start_claimed_task_thread,
    release_gpu_task_claim,
    process_state, is_task_running,
    project_manager,
)
from dataset_publication import apply_dataset_publication
from utils import atomic_json_write


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()
_builder_state_locks = {}
_builder_state_locks_guard = threading.Lock()
_builder_task_lock = threading.RLock()


def publish_sample_audio(source, destination):
    """Readers keep the previous complete sample until the new copy is ready."""
    fd, staging = tempfile.mkstemp(
        prefix="." + os.path.basename(destination) + ".",
        dir=os.path.dirname(destination),
    )
    os.close(fd)
    try:
        shutil.copy2(source, staging)
        os.replace(staging, destination)
    finally:
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass


def _get_builder_project_key(name):
    """One canonical path identity for project locks and task ownership."""
    return os.path.realpath(os.path.join(DATASET_BUILDER_DIR, name))


def get_builder_activity(name):
    """Return a project-specific view of the globally admitted worker."""
    with _builder_task_lock:
        task = process_state["dataset_builder"]
        busy = is_task_running("dataset_builder", states=process_state)
        owner = task.get("dataset_name")
        matches = owner is not None and _get_builder_project_key(owner) == _get_builder_project_key(name)
        return {"running": bool(busy and matches),
                "logs": list(task.get("logs", [])) if matches else [],
                "global_running": busy, "active_dataset_name": owner if busy else None}


def _require_builder_idle(name, detail):
    activity = get_builder_activity(name)
    if activity["global_running"] and (activity["running"] or activity["active_dataset_name"] is None):
        raise HTTPException(status_code=409, detail=detail)


def apply_builder_task_owner(name):
    """Caller holds task admission across claiming and assigning the project."""
    process_state["dataset_builder"]["dataset_name"] = name
    process_state["dataset_builder"]["logs"] = []


def _get_builder_state_lock(name):
    """One in-process lock per builder state file for read-modify-write updates."""
    with _builder_state_locks_guard:
        return _builder_state_locks.setdefault(_get_builder_project_key(name), threading.Lock())


class LoraDatasetSample(BaseModel):
    emotion: str = ""
    text: str

class DatasetSampleGenRequest(BaseModel):
    description: str      # full voice description (root + emotion already combined by frontend)
    text: str
    dataset_name: str     # working directory name
    sample_index: int = Field(ge=0, le=4999)  # row number
    seed: int = Field(default=-1, ge=-1)  # -1 = random, >= 0 = manual seed

class DatasetBatchGenRequest(BaseModel):
    name: str
    description: str      # root voice description
    samples: List[LoraDatasetSample]
    indices: Optional[List[int]] = None  # which rows to generate (None = all)
    global_seed: int = Field(default=-1, ge=-1)  # -1 = random, >= 0 = same seed for all lines
    seeds: Optional[List[Annotated[int, Field(ge=-1)]]] = None  # per-line seeds (overrides global_seed)

class DatasetSaveRequest(BaseModel):
    name: str
    # None = "caller expressed no preference, pick the representative clip".
    # This was `int = 0`, which cannot tell a deliberate choice of sample 0
    # from an absent field, so the medoid override silently second-guessed
    # the one caller it promises not to.
    ref_index: Optional[int] = Field(default=None, ge=0)    # which sample to use as ref.wav

class DatasetBuilderCreateRequest(BaseModel):
    name: str

class DatasetBuilderUpdateMetaRequest(BaseModel):
    name: str
    description: str = ""
    global_seed: str = ""

class DatasetBuilderUpdateRowsRequest(BaseModel):
    name: str
    rows: List[dict]  # [{emotion, text, seed}]


class DatasetBuilderRowEdit(BaseModel):
    index: int = Field(ge=0)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    row: dict


class DatasetBuilderEditRowsRequest(BaseModel):
    name: str
    expected_count: int = Field(ge=0)
    edits: List[DatasetBuilderRowEdit] = Field(min_length=1)


def get_dataset_row_revision(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_dataset_row_definitions(rows):
    if any(not isinstance(row, dict)
           or not isinstance(row.get("text", ""), str)
           or not isinstance(row.get("emotion", ""), str)
           or type(row.get("seed", "")) not in (str, int)
           for row in rows):
        raise HTTPException(status_code=400, detail="Each row needs string text and emotion, and a string or integer seed")


def get_effective_dataset_seed(row_seed, global_seed):
    """Per-row seed takes precedence over the global seed; blank means random."""
    for value in (row_seed, global_seed):
        try:
            seed = int(value)
        except (TypeError, ValueError):
            continue
        if seed >= 0:
            return seed
    return -1


def get_merged_dataset_sample(row, existing=None):
    sample = {"emotion": row.get("emotion", ""), "text": row.get("text", "").strip(),
              "seed": row.get("seed", ""), "status": "pending", "audio_url": None}
    if (existing is not None
            and existing.get("text", "").strip() == sample["text"]
            and existing.get("emotion", "") == sample["emotion"]
            and existing.get("seed", "") == sample["seed"]):
        sample["status"] = existing.get("status", "pending")
        sample["audio_url"] = existing.get("audio_url")
    return sample


## ── Dataset Builder ──────────────────────────────────────────

def _load_builder_state(name):
    """Load project state from dataset builder working directory."""
    state_path = os.path.join(DATASET_BUILDER_DIR, name, "state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError(f"Expected a JSON object, got {type(state).__name__}")
            # Ensure new fields exist for backward compat
            state.setdefault("description", "")
            state.setdefault("global_seed", "")
            state.setdefault("samples", [])
            return state
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to load builder state '{name}': {e}")
    return {"description": "", "global_seed": "", "samples": []}

def _save_builder_state(name, state):
    """Save per-sample state to dataset builder working directory atomically."""
    work_dir = os.path.join(DATASET_BUILDER_DIR, name)
    os.makedirs(work_dir, exist_ok=True)
    atomic_json_write(state, os.path.join(work_dir, "state.json"))

@router.get("/api/dataset_builder/list")
async def dataset_builder_list():
    """List existing dataset builder projects."""
    projects = []
    if os.path.isdir(DATASET_BUILDER_DIR):
        for name in sorted(os.listdir(DATASET_BUILDER_DIR)):
            state_path = os.path.join(DATASET_BUILDER_DIR, name, "state.json")
            if os.path.isfile(state_path):
                state = _load_builder_state(name)
                samples = state.get("samples", [])
                projects.append({
                    "name": name,
                    "description": state.get("description", ""),
                    "sample_count": len(samples),
                    "done_count": sum(1 for s in samples if s.get("status") == "done"),
                })
    return projects

@router.post("/api/dataset_builder/create")
async def dataset_builder_create(request: DatasetBuilderCreateRequest):
    """Create a new dataset builder project."""
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if os.path.exists(work_dir):
        raise HTTPException(status_code=400, detail=f"Project '{safe_name}' already exists")
    _save_builder_state(safe_name, {"description": "", "global_seed": "", "samples": []})
    return {"name": safe_name}

def _dataset_builder_update_meta_sync(request: DatasetBuilderUpdateMetaRequest):
    """Invalidate cached samples whose inherited generation inputs changed."""
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    with _get_builder_state_lock(safe_name):
        _require_builder_idle(safe_name, "Stop dataset generation before editing its metadata")
        if not os.path.exists(work_dir):
            raise HTTPException(status_code=404, detail="Project not found")
        state = _load_builder_state(safe_name)
        description_changed = state.get("description", "") != request.description
        seed_changed = get_effective_dataset_seed("", state.get("global_seed", "")) != get_effective_dataset_seed("", request.global_seed)
        state["samples"] = [
            get_merged_dataset_sample(row) if (description_changed or
                (seed_changed and get_effective_dataset_seed(row.get("seed", ""), "") < 0))
            else row for row in state.get("samples", [])]
        state["description"] = request.description
        state["global_seed"] = request.global_seed
        _save_builder_state(safe_name, state)
    return {"status": "ok"}


@router.post("/api/dataset_builder/update_meta")
async def dataset_builder_update_meta(request: DatasetBuilderUpdateMetaRequest):
    return await asyncio.to_thread(_dataset_builder_update_meta_sync, request)

def _dataset_builder_update_rows_sync(request: DatasetBuilderUpdateRowsRequest):
    """Update row definitions, preserving existing generation status/audio."""
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    validate_dataset_row_definitions(request.rows)
    with _get_builder_state_lock(safe_name):
        _require_builder_idle(safe_name, "Stop dataset generation before editing its rows")
        if not os.path.exists(work_dir):
            raise HTTPException(status_code=404, detail="Project not found")
        state = _load_builder_state(safe_name)
        existing = state.get("samples", [])
        # Merge: keep status/audio_url only where all generation inputs are unchanged
        new_samples = []
        for i, row in enumerate(request.rows):
            new_samples.append(get_merged_dataset_sample(row, existing[i] if i < len(existing) else None))
        state["samples"] = new_samples
        _save_builder_state(safe_name, state)
    return {"status": "ok", "sample_count": len(new_samples),
            "row_revisions": [get_dataset_row_revision(row) for row in new_samples]}


@router.post("/api/dataset_builder/update_rows")
async def dataset_builder_update_rows(request: DatasetBuilderUpdateRowsRequest):
    return await asyncio.to_thread(_dataset_builder_update_rows_sync, request)

def _dataset_builder_edit_rows_sync(request: DatasetBuilderEditRowsRequest):
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
    validate_dataset_row_definitions([edit.row for edit in request.edits])
    indices = [edit.index for edit in request.edits]
    if len(set(indices)) != len(indices):
        raise HTTPException(status_code=400, detail="Each row may appear only once in an edit")
    with _get_builder_state_lock(safe_name):
        _require_builder_idle(safe_name, "Stop dataset generation before editing its rows")
        if not os.path.isfile(os.path.join(DATASET_BUILDER_DIR, safe_name, "state.json")):
            raise HTTPException(status_code=404, detail="Project not found")
        state = _load_builder_state(safe_name)
        samples = state.get("samples", [])
        if len(samples) != request.expected_count:
            raise HTTPException(status_code=409, detail="Dataset rows changed; reload before editing")
        for edit in request.edits:
            if (edit.index >= len(samples) or not isinstance(samples[edit.index], dict)
                    or get_dataset_row_revision(samples[edit.index]) != edit.expected_revision):
                raise HTTPException(status_code=409, detail="Dataset row changed; reload before editing")
        updated = list(samples)
        revisions = {}
        for edit in request.edits:
            updated[edit.index] = get_merged_dataset_sample(edit.row, samples[edit.index])
            revisions[str(edit.index)] = get_dataset_row_revision(updated[edit.index])
        if request.edits:
            state["samples"] = updated
            _save_builder_state(safe_name, state)
    return {"status": "ok", "row_revisions": revisions}


@router.post("/api/dataset_builder/edit_rows")
async def dataset_builder_edit_rows(request: DatasetBuilderEditRowsRequest):
    return await asyncio.to_thread(_dataset_builder_edit_rows_sync, request)


def _generate_dataset_sample_sync(request: DatasetSampleGenRequest):
    """Generate a single dataset sample using VoiceDesign."""
    safe_name = _require_safe_filename(request.dataset_name, "Invalid dataset name")

    # Same "dataset_builder" slot as the sibling /generate_batch route -
    # fail fast before any setup work below. See F-043.
    check_global_gpu_lock("dataset_builder")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    with _get_builder_state_lock(safe_name):
        if not os.path.isfile(os.path.join(work_dir, "state.json")):
            raise HTTPException(status_code=404, detail="Project not found")
        samples = _load_builder_state(safe_name).get("samples", [])
        if request.sample_index >= len(samples):
            raise HTTPException(status_code=400, detail="Sample index is outside the project's rows")
        with _builder_task_lock:
            claim_id = claim_gpu_task("dataset_builder")
            apply_builder_task_owner(safe_name)
    try:
        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

        wav_path, _ = engine.generate_voice_design(
            description=request.description,
            sample_text=request.text,
            seed=request.seed,
        )

        dest_filename = f"sample_{request.sample_index:03d}.wav"
        dest_path = os.path.join(work_dir, dest_filename)
        publish_sample_audio(wav_path, dest_path)

        # Update state (cache-bust URL so browser loads fresh audio on regen)
        cache_bust = time.time_ns()
        audio_url = f"/dataset_builder/{safe_name}/{dest_filename}?t={cache_bust}"
        with _get_builder_state_lock(safe_name):
            state = _load_builder_state(safe_name)
            samples = state.get("samples", [])
            while len(samples) <= request.sample_index:
                samples.append({"status": "pending"})
            existing_sample = samples[request.sample_index]
            samples[request.sample_index] = {
                **existing_sample, "status": "done", "audio_url": audio_url,
                "text": request.text.strip(), "description": request.description,
                "seed": request.seed,
            }
            state["samples"] = samples
            _save_builder_state(safe_name, state)

        return {
            "status": "done",
            "sample_index": request.sample_index,
            "audio_url": audio_url,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Dataset builder sample generation failed")
        # Mark as error in state
        with _get_builder_state_lock(safe_name):
            state = _load_builder_state(safe_name)
            samples = state.get("samples", [])
            while len(samples) <= request.sample_index:
                samples.append({"status": "pending"})
            samples[request.sample_index] = {
                **samples[request.sample_index], "status": "error", "error": str(e),
            }
            state["samples"] = samples
            _save_builder_state(safe_name, state)
        raise HTTPException(status_code=500, detail="Sample generation failed — see server logs for details.") from e
    finally:
        release_gpu_task_claim("dataset_builder", claim_id)


@router.post("/api/dataset_builder/generate_sample")
async def dataset_builder_generate_sample(request: DatasetSampleGenRequest):
    """Generate a sample without blocking the API loop; worker owns its claim."""
    return await asyncio.to_thread(_generate_dataset_sample_sync, request)

@router.post("/api/dataset_builder/generate_batch")
async def dataset_builder_generate_batch(request: DatasetBatchGenRequest):
    """Batch generate dataset samples as a background task."""
    check_global_gpu_lock("dataset_builder")

    if not request.samples or len(request.samples) == 0:
        raise HTTPException(status_code=400, detail="No samples provided")

    safe_name = _require_safe_filename(request.name, "Invalid dataset name")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    root_desc = request.description.strip()

    # Determine which indices to generate
    if request.indices is not None:
        to_generate = request.indices
    else:
        to_generate = list(range(len(request.samples)))

    if not to_generate:
        raise HTTPException(status_code=400, detail="Select at least one sample index")
    if len(set(to_generate)) != len(to_generate):
        raise HTTPException(status_code=400, detail="Sample indices must be unique")

    # Reject out-of-range indices up front (e.g. a stale frontend selection
    # referencing a row that was since removed) - letting one through used to
    # crash the background thread with an uncaught IndexError before it ever
    # reached the per-sample try/except, leaving claim_gpu_task's "running"
    # flag stuck True forever and permanently deadlocking every other GPU
    # task behind check_global_gpu_lock until the server was restarted.
    bad_indices = [idx for idx in to_generate if not (0 <= idx < len(request.samples))]
    if bad_indices:
        raise HTTPException(status_code=400,
                            detail=f"indices out of range for {len(request.samples)} sample(s): {bad_indices}")

    total = len(to_generate)

    # Snapshot request data for the thread (request object may not survive)
    samples_snapshot = [(s.emotion.strip(), s.text.strip()) for s in request.samples]
    global_seed = request.global_seed
    per_seeds = request.seeds

    def task():
        process_state["dataset_builder"]["running"] = True
        process_state["dataset_builder"]["logs"] = []
        # Wrapped in try/finally so ANY unexpected exception in this thread -
        # not just the ones already anticipated by the per-sample try/except
        # below - still releases the GPU lock. An uncaught exception in a
        # background thread doesn't propagate or crash the process; it just
        # kills the thread silently, which previously left "running" stuck
        # True forever and permanently deadlocked every other GPU task behind
        # check_global_gpu_lock until the server was restarted.
        try:
            engine = project_manager.get_engine()
            if not engine:
                process_state["dataset_builder"]["logs"].append("[ERROR] Failed to initialize TTS engine")
                return

            state = _load_builder_state(safe_name)
            samples_state = state.get("samples", [])
            # Ensure list is large enough for all samples
            while len(samples_state) < len(samples_snapshot):
                samples_state.append({"status": "pending"})

            completed = 0
            for i, idx in enumerate(to_generate):
                if process_state["dataset_builder"]["cancel"]:
                    process_state["dataset_builder"]["logs"].append(f"[CANCEL] Stopped at {completed}/{total}")
                    break

                emotion, text = samples_snapshot[idx]
                description = f"{root_desc}, {emotion}" if emotion else root_desc

                # Mark as generating (preserve existing fields like emotion, seed)
                existing_s = samples_state[idx] if idx < len(samples_state) else {}
                samples_state[idx] = {**existing_s, "status": "generating", "text": text, "emotion": emotion, "description": description}
                state["samples"] = samples_state
                _save_builder_state(safe_name, state)

                process_state["dataset_builder"]["logs"].append(
                    f"[{i+1}/{total}] {('[' + emotion + '] ' if emotion else '')}\"{text[:60]}{'...' if len(text) > 60 else ''}\""
                )

                try:
                    # Resolve seed: per-line > global > random
                    seed = get_effective_dataset_seed(
                        per_seeds[idx] if per_seeds and idx < len(per_seeds) else -1,
                        global_seed)

                    wav_path, _ = engine.generate_voice_design(
                        description=description,
                        sample_text=text,
                        seed=seed,
                    )
                    dest_filename = f"sample_{idx:03d}.wav"
                    dest_path = os.path.join(work_dir, dest_filename)
                    publish_sample_audio(wav_path, dest_path)

                    samples_state[idx] = {
                        **samples_state[idx],
                        "status": "done",
                        "audio_url": f"/dataset_builder/{safe_name}/{dest_filename}?t={time.time_ns()}",
                        "text": text,
                        "emotion": emotion,
                        "description": description,
                    }
                    completed += 1
                except Exception as e:
                    logger.error(f"Dataset builder sample {idx} failed: {e}")
                    process_state["dataset_builder"]["logs"].append(f"  Error: {e}")
                    samples_state[idx] = {**samples_state[idx], "status": "error", "error": str(e), "text": text, "emotion": emotion}

                state["samples"] = samples_state
                _save_builder_state(safe_name, state)

            process_state["dataset_builder"]["logs"].append(
                f"[DONE] Generated {completed}/{total} samples"
            )
        except Exception as e:
            logger.exception("Dataset builder batch generation crashed")
            process_state["dataset_builder"]["logs"].append(f"[ERROR] Batch generation crashed: {e}")
        finally:
            process_state["dataset_builder"]["running"] = False

    with _get_builder_state_lock(safe_name):
        with _builder_task_lock:
            claim_id = reserve_background_task("dataset_builder")
            apply_builder_task_owner(safe_name)
            try:
                os.makedirs(work_dir, exist_ok=True)
                start_claimed_task_thread("dataset_builder", claim_id, task)
            except BaseException:
                release_gpu_task_claim("dataset_builder", claim_id, pending_only=True)
                raise
    return {"status": "started", "dataset_name": safe_name, "total": total}

@router.post("/api/dataset_builder/cancel")
async def dataset_builder_cancel(name: Optional[str] = None):
    """Cancel the requested project's worker; unnamed callers target the active job."""
    safe_name = _require_safe_filename(name, "Invalid dataset name") if name is not None else None
    with _builder_task_lock:
        activity = get_builder_activity(safe_name) if safe_name is not None else None
        busy = activity["running"] if activity is not None else is_task_running("dataset_builder", states=process_state)
        if busy:
            process_state["dataset_builder"]["cancel"] = True
            return {"status": "cancelling"}
        return {"status": "not_running"}

@router.get("/api/dataset_builder/status/{name}")
async def dataset_builder_status(name: str):
    """Get per-sample generation status for a dataset builder project."""
    safe_name = _require_safe_filename(name, "Invalid dataset name")
    if not os.path.isfile(os.path.join(DATASET_BUILDER_DIR, safe_name, "state.json")):
        raise HTTPException(status_code=404, detail="Dataset builder project not found")
    state = _load_builder_state(safe_name)
    return {
        "description": state.get("description", ""),
        "global_seed": state.get("global_seed", ""),
        "samples": state.get("samples", []),
        "row_revisions": [get_dataset_row_revision(row) for row in state.get("samples", [])],
        "dataset_name": safe_name,
        **get_builder_activity(safe_name),
    }

def _dataset_builder_save_sync(request: DatasetSaveRequest):
    """Finalize dataset builder project as a training dataset."""
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")

    with _get_builder_state_lock(safe_name):
        _require_builder_idle(safe_name, "Wait for dataset generation before saving")
        work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
        if not os.path.exists(work_dir):
            raise HTTPException(status_code=404, detail="Dataset builder project not found")

        state = _load_builder_state(safe_name)
        samples = state.get("samples", [])

        # Collect completed samples
        done_samples = [(i, s) for i, s in enumerate(samples) if s.get("status") == "done"]
        if not done_samples:
            raise HTTPException(status_code=400, detail="No completed samples to save")
        missing_samples = [
            f"sample_{i:03d}.wav" for i, _sample in done_samples
            if not os.path.isfile(os.path.join(work_dir, f"sample_{i:03d}.wav"))
        ]
        if missing_samples:
            preview = ", ".join(missing_samples[:5])
            suffix = " and more" if len(missing_samples) > 5 else ""
            raise HTTPException(
                status_code=400,
                detail=f"Completed sample audio is missing: {preview}{suffix}. Regenerate it before saving.",
            )

        # Check ref_index is valid
        ref_idx = request.ref_index
        ref_sample = next((s for i, s in done_samples if i == ref_idx), None)
        if ref_sample is None:
            if request.ref_index is not None:
                raise HTTPException(status_code=400, detail="Selected reference must be a completed, available sample")
            # Fall back to first completed sample
            ref_idx = done_samples[0][0]
            ref_sample = done_samples[0][1]

        # PICK A REPRESENTATIVE REFERENCE, NOT JUST THE FIRST CLIP.
        #
        # train_lora.py anchors the speaker embedding for ALL samples to this one
        # file, so an unrepresentative clip here poisons the whole adapter. Across
        # the 75 shipped adapters, a mismatched reference made an adapter 6.4x more
        # likely to fail (86% vs 13%), and correlated +0.76 with adapter quality.
        # The worst case was anchored to a clip scoring -0.026 against its own
        # dataset while a 0.882 clip sat unused in the same data.
        #
        # The medoid - the clip most similar to all the others - is representative
        # by construction and robust to a minority of misdiarized clips.
        #
        # Only when the caller did not choose one. An explicit ref_index is a
        # deliberate human decision and is not second-guessed.
        if request.ref_index is None and len(done_samples) >= 3:
            from voice_reference import select_reference_sample
            candidates = [os.path.join(work_dir, f"sample_{i:03d}.wav")
                          for i, _ in done_samples]
            pick, score = select_reference_sample(candidates, dataset_root=work_dir)
            if pick is not None:
                chosen_i, chosen_sample = done_samples[pick]
                logger.info(
                    f"Reference: chose sample_{chosen_i:03d} as the medoid "
                    f"(similarity {score} to the rest of the dataset) instead of "
                    f"the first clip")
                ref_idx, ref_sample = chosen_i, chosen_sample
            else:
                # Not measurable here - the speaker model lives in the sibling
                # interpreter. Say so, rather than leaving the reference silently
                # unverified, which is how this defect survived.
                logger.warning(
                    "Reference: could not verify sample_%03d is representative "
                    "(speaker model unavailable); using it unchecked", ref_idx)

        try:
            with apply_dataset_publication(LORA_DATASETS_DIR, safe_name) as dataset_dir:
                metadata_lines = []
                for i, sample in done_samples:
                    src_filename = f"sample_{i:03d}.wav"
                    src_path = os.path.join(work_dir, src_filename)

                    dest_filename = f"sample_{i:03d}.wav"
                    shutil.copy2(src_path, os.path.join(dataset_dir, dest_filename))

                    metadata_lines.append(json.dumps({
                        "audio_filepath": dest_filename,
                        "text": sample.get("text", ""),
                        "ref_audio": "ref.wav",
                    }, ensure_ascii=False))

                # Copy ref sample and save its text for correct clone prompt alignment
                ref_src = os.path.join(dataset_dir, f"sample_{ref_idx:03d}.wav")
                shutil.copy2(ref_src, os.path.join(dataset_dir, "ref.wav"))
                ref_text = ref_sample.get("text", "")
                with open(os.path.join(dataset_dir, "ref_text.txt"), "w", encoding="utf-8") as f:
                    f.write(ref_text)

                # Write metadata
                with open(os.path.join(dataset_dir, "metadata.jsonl"), "w", encoding="utf-8") as f:
                    f.write("\n".join(metadata_lines) + "\n")

            sample_count = len(metadata_lines)
            logger.info(f"Dataset saved: '{safe_name}' ({sample_count} samples, ref=sample_{ref_idx:03d})")

            return {
                "status": "saved",
                "dataset_id": safe_name,
                "sample_count": sample_count,
            }
        except FileExistsError as e:
            raise HTTPException(status_code=400, detail=f"Dataset '{safe_name}' already exists in training datasets") from e
        except Exception as e:
            logger.exception("Dataset save failed")
            raise HTTPException(status_code=500, detail="Dataset save failed — see server logs for details.") from e


@router.post("/api/dataset_builder/save")
async def dataset_builder_save(request: DatasetSaveRequest):
    return await asyncio.to_thread(_dataset_builder_save_sync, request)

def _dataset_builder_delete_sync(name: str):
    """Discard a dataset builder working project."""
    work_dir = _safe_subpath(DATASET_BUILDER_DIR, name)
    project_name = os.path.relpath(work_dir, os.path.realpath(DATASET_BUILDER_DIR)).split(os.sep)[0]
    with _get_builder_state_lock(project_name):
        _require_builder_idle(project_name, "Stop dataset generation before deleting its project")
        if not os.path.exists(work_dir):
            raise HTTPException(status_code=404, detail="Dataset builder project not found")
        shutil.rmtree(work_dir)
        logger.info(f"Dataset builder project discarded: {name}")
        return {"status": "deleted", "name": name}


@router.delete("/api/dataset_builder/{name}")
async def dataset_builder_delete(name: str):
    return await asyncio.to_thread(_dataset_builder_delete_sync, name)
