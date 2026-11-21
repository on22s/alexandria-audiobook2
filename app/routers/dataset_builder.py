import json
from contextlib import contextmanager
import hashlib
import asyncio
import math
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from typing import Annotated, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import core

from core import (
    AppError,
    DATASET_BUILDER_DIR,
    LORA_DATASETS_DIR,
    _require_safe_filename,
    _safe_subpath,
    check_global_gpu_lock,
    claim_gpu_task, reserve_background_task, start_claimed_task_thread,
    release_gpu_task_claim,
    process_state, is_task_running, get_task_lifecycle,
    project_manager,
)
from dataset_publication import apply_dataset_publication
from utils import atomic_json_write
from task_ownership import ensure_task_resources_available, TaskOwnershipBusy


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


def generate_sample_audio(engine, destination, *, description, sample_text, seed):
    """Publish a generated sample, then retire only this call's preview."""
    preview, _ = engine.generate_voice_design(
        description=description, sample_text=sample_text, seed=seed)
    try:
        publish_sample_audio(preview, destination)
    finally:
        if os.path.abspath(preview) != os.path.abspath(destination):
            try:
                os.unlink(preview)
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning("Dataset sample preview cleanup failed at %s: %s", preview, error)


def _get_builder_project_key(name):
    """One canonical path identity for project locks and task ownership."""
    return os.path.realpath(os.path.join(DATASET_BUILDER_DIR, name))


def get_builder_activity(name):
    """Return a project-specific view of the globally admitted worker."""
    with _builder_task_lock:
        task = process_state["dataset_builder"]
        busy = is_task_running("dataset_builder", states=process_state)
        lifecycle = get_task_lifecycle('dataset_builder', states=process_state)
        owner = task.get("dataset_name")
        matches = owner is not None and _get_builder_project_key(owner) == _get_builder_project_key(name)
        return {**(lifecycle if matches else get_task_lifecycle('', states={})),
                "running": bool(lifecycle['running'] and matches),
                "busy": bool(busy and matches), "global_busy": busy,
                "logs": list(task.get("logs", [])) if matches else [],
                "global_running": lifecycle['running'],
                "global_ownership_unconfirmed": lifecycle['ownership_unconfirmed'],
                "active_dataset_name": owner if busy else None}


def _require_builder_idle(name, detail):
    activity = get_builder_activity(name)
    if activity["global_busy"] and (activity["busy"] or activity["active_dataset_name"] is None):
        raise HTTPException(status_code=409, detail=detail)


def apply_builder_task_owner(name):
    """Caller holds task admission across claiming and assigning the project."""
    process_state["dataset_builder"]["dataset_name"] = name
    process_state["dataset_builder"]["logs"] = []


def _get_builder_state_lock(name):
    """One in-process lock per builder state file for read-modify-write updates."""
    with _builder_state_locks_guard:
        return _builder_state_locks.setdefault(_get_builder_project_key(name), threading.Lock())


@contextmanager
def ensure_builder_project_mutation(name, detail):
    with _get_builder_state_lock(name):
        _require_builder_idle(name, detail)
        try:
            with ensure_task_resources_available(core.DATA_DIR, ('dataset_builder',),
                                                  (_get_builder_project_key(name),), mutation_timeout=10):
                yield
        except TaskOwnershipBusy as error:
            raise HTTPException(status_code=409, detail=detail) from error


class LoraDatasetSample(BaseModel):
    emotion: str = ""
    text: str

class DatasetSampleGenRequest(BaseModel):
    description: str      # full voice description (root + emotion already combined by frontend)
    text: str
    dataset_name: str     # working directory name
    sample_index: int = Field(ge=0, le=4999)  # row number
    seed: int = Field(default=-1, ge=-1)  # effective generation seed
    inherit_seed: bool = False  # retain the row's global-seed inheritance
    expected_count: Optional[int] = Field(default=None, ge=0)
    expected_revision: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")

class DatasetBatchGenRequest(BaseModel):
    name: str
    description: str      # root voice description
    samples: List[LoraDatasetSample]
    indices: Optional[List[int]] = None  # which rows to generate (None = all)
    global_seed: int = Field(default=-1, ge=-1)  # -1 = random, >= 0 = same seed for all lines
    seeds: Optional[List[Annotated[int, Field(ge=-1)]]] = None  # per-line seeds (overrides global_seed)
    expected_count: Optional[int] = Field(default=None, ge=0)
    expected_revisions: Optional[Dict[int, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]] = None

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


class _MissingBuilderState:
    pass


_MISSING_BUILDER_STATE = _MissingBuilderState()


def _load_builder_state_for_generation(name):
    """Verified absence is first-run; nonregular or unreadable state is a failure."""
    state_path = os.path.join(DATASET_BUILDER_DIR, name, "state.json")
    if not os.path.lexists(state_path):
        return _MISSING_BUILDER_STATE
    try:
        if not stat.S_ISREG(os.stat(state_path).st_mode):
            raise OSError("State path is not a regular file")
        with open(state_path, "r", encoding="utf-8") as source:
            state = json.load(source)
        if not isinstance(state, dict) or not isinstance(state.get("samples", []), list):
            raise ValueError("Expected a JSON object with a samples list")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        logger.warning("Failed to read builder state '%s' for generation: %s", name, error)
        raise AppError(500, "Dataset project state could not be read",
                       key='error.dataset.state_unreadable') from error
    state.setdefault("description", "")
    state.setdefault("global_seed", "")
    state.setdefault("samples", [])
    return state


def _require_existing_builder_state(name):
    state = _load_builder_state_for_generation(name)
    if state is _MISSING_BUILDER_STATE:
        raise HTTPException(status_code=404, detail="Project not found")
    return state


def _is_uninitialized_dataset_row(row):
    return isinstance(row, dict) and not any(
        key in row for key in ("text", "emotion", "seed", "description"))


def _canonical_dataset_generation_row(row, state):
    text = row.get("text", "")
    emotion = row.get("emotion", "")
    if not isinstance(text, str):
        text = ""
    if not isinstance(emotion, str):
        emotion = ""
    raw_seed = row.get("seed", "")
    seed_field = raw_seed if isinstance(raw_seed, (str, int)) and not isinstance(raw_seed, bool) else ""
    root_description = state.get("description", "")
    saved_description = row.get("description")
    if (not isinstance(root_description, str) or not root_description.strip()) \
            and isinstance(saved_description, str) and saved_description.strip():
        description = saved_description
    else:
        description = f"{root_description}, {emotion}" if emotion else root_description
    return {
        "text": text.strip(), "emotion": emotion, "description": description,
        "seed_field": seed_field,
        "generated_seed": get_effective_dataset_seed(seed_field, state.get("global_seed", "")),
    }


def _new_dataset_row(values, state):
    resolved = _creation_dataset_generation_row(values, state)
    return {"emotion": resolved["emotion"], "text": resolved["text"],
            "seed": resolved["seed_field"], "description": resolved["description"],
            "status": "pending", "audio_url": None}


def _effective_project_metadata(state, creation_metadata):
    metadata = creation_metadata or {}
    description = state.get("description", "")
    if not isinstance(description, str) or not description.strip():
        description = metadata.get("description", "")
    global_seed = state.get("global_seed", "")
    if _coerce_integral_seed(global_seed) is None:
        global_seed = metadata.get("global_seed", "")
    return {"description": description, "global_seed": global_seed}


def _creation_dataset_generation_row(values, state):
    text, emotion = values.get("text", ""), values.get("emotion", "")
    seed_field = values.get("seed_field", "")
    return {
        "text": text.strip() if isinstance(text, str) else "",
        "emotion": emotion if isinstance(emotion, str) else "",
        "description": values.get("description", ""),
        "seed_field": seed_field,
        "generated_seed": get_effective_dataset_seed(seed_field, state.get("global_seed", "")),
    }


def resolve_dataset_generation_inputs(name, *, requested, expected_count, expected_revisions,
                                      require_existing=False, creation_definitions=None,
                                      creation_metadata=None, bootstrap_allowed=None):
    """Validate the binding and resolve canonical inputs for each generated row.

    Caller holds the per-project state lock both before the kernel lease is
    taken and again after it has been acquired. Existing rows are always driven
    by the persisted definition and the project's current root description and
    global seed; caller-supplied content only creates rows that do not exist.
    """
    bound = expected_revisions is not None
    if bound and expected_count is None:
        raise AppError(400, "Dataset generation needs an expected row count with revisions",
                       key='error.dataset.revision_count_required')
    payload_count = len(creation_definitions) if creation_definitions is not None else len(requested)
    state = _load_builder_state_for_generation(name)
    if state is _MISSING_BUILDER_STATE:
        if require_existing:
            raise AppError(404, "Project not found", key='error.dataset.project_not_found')
        if bound or expected_count not in (None, 0):
            raise AppError(409, "Dataset rows changed; regenerate", key='error.dataset.rows_changed')
        metadata = creation_metadata or {}
        state = {"description": metadata.get("description", ""),
                 "global_seed": metadata.get("global_seed", ""), "samples": []}
    samples = state.get("samples", [])
    count = len(samples)
    if expected_count is not None and expected_count != count:
        raise AppError(409, "Dataset rows changed; regenerate", key='error.dataset.rows_changed')
    if bound and set(expected_revisions) != set(requested):
        raise AppError(400, "Dataset generation needs a revision for every requested row",
                       key='error.dataset.revision_map_incomplete')
    if not require_existing and count and payload_count != count:
        raise AppError(409, "Dataset rows changed; regenerate", key='error.dataset.rows_changed')
    if count == 0 and (bound or bootstrap_allowed is False):
        raise AppError(409, "Dataset rows changed; regenerate", key='error.dataset.rows_changed')
    if count == 0:
        state = {**state, **_effective_project_metadata(state, creation_metadata)}
    resolved = {}
    for index, values in requested.items():
        row = samples[index] if index < count and isinstance(samples[index], dict) else None
        if row is None:
            if bound or require_existing or count:
                raise AppError(409, "Dataset row changed; regenerate", key='error.dataset.row_changed')
            definition = (creation_definitions or {}).get(index)
            if definition is None:
                raise AppError(409, "Dataset row changed; regenerate", key='error.dataset.row_changed')
            resolved[index] = _creation_dataset_generation_row(definition, state)
        else:
            if bound and get_dataset_row_revision(row) != expected_revisions[index]:
                raise AppError(409, "Dataset row changed; regenerate", key='error.dataset.row_changed')
            if _is_uninitialized_dataset_row(row) and creation_definitions and index in creation_definitions:
                resolved[index] = _creation_dataset_generation_row(creation_definitions[index], state)
            else:
                resolved[index] = _canonical_dataset_generation_row(row, state)
    return resolved


def validate_dataset_row_definitions(rows):
    if any(not isinstance(row, dict)
           or not isinstance(row.get("text", ""), str)
           or not isinstance(row.get("emotion", ""), str)
           or type(row.get("seed", "")) not in (str, int)
           for row in rows):
        raise HTTPException(status_code=400, detail="Each row needs string text and emotion, and a string or integer seed")


def _coerce_integral_seed(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if math.isfinite(number) and number.is_integer() else None


def get_effective_dataset_seed(row_seed, global_seed):
    """Per-row seed takes precedence; whole-number notation, blank means random."""
    for value in (row_seed, global_seed):
        seed = _coerce_integral_seed(value)
        if seed is not None and seed >= 0:
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
        if "generated_seed" in existing:
            sample["generated_seed"] = existing["generated_seed"]
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
    atomic_json_write(state, os.path.join(work_dir, "state.json"), allow_nonatomic_fallback=False)

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
    def create():
        with ensure_builder_project_mutation(safe_name, "Wait for dataset generation before creating its project"):
            if os.path.exists(work_dir):
                raise HTTPException(status_code=400, detail=f"Project '{safe_name}' already exists")
            _save_builder_state(safe_name, {"description": "", "global_seed": "", "samples": []})
    await asyncio.to_thread(create)
    return {"name": safe_name}

def _dataset_builder_update_meta_sync(request: DatasetBuilderUpdateMetaRequest):
    """Invalidate cached samples whose inherited generation inputs changed."""
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
    with ensure_builder_project_mutation(safe_name, "Stop dataset generation before editing its metadata"):
        state = _require_existing_builder_state(safe_name)
        description_changed = state.get("description", "") != request.description
        seed_changed = get_effective_dataset_seed("", state.get("global_seed", "")) != get_effective_dataset_seed("", request.global_seed)
        state["samples"] = [
            get_merged_dataset_sample(row) if (description_changed or
                (seed_changed and get_effective_dataset_seed(row.get("seed", ""), "") < 0))
            else row for row in state.get("samples", [])]
        state["description"] = request.description
        state["global_seed"] = request.global_seed
        _save_builder_state(safe_name, state)
    return {"status": "ok", "description": state["description"],
            "global_seed": state["global_seed"], "samples": state["samples"],
            "row_revisions": [get_dataset_row_revision(row) for row in state["samples"]]}


@router.post("/api/dataset_builder/update_meta")
async def dataset_builder_update_meta(request: DatasetBuilderUpdateMetaRequest):
    return await asyncio.to_thread(_dataset_builder_update_meta_sync, request)

def _dataset_builder_update_rows_sync(request: DatasetBuilderUpdateRowsRequest):
    """Update row definitions, preserving existing generation status/audio."""
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
    validate_dataset_row_definitions(request.rows)
    with ensure_builder_project_mutation(safe_name, "Stop dataset generation before editing its rows"):
        state = _require_existing_builder_state(safe_name)
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
    with ensure_builder_project_mutation(safe_name, "Stop dataset generation before editing its rows"):
        state = _require_existing_builder_state(safe_name)
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
    index = request.sample_index
    if not request.text.strip():
        raise AppError(400, "This row has no text to generate", key='error.dataset.row_has_no_text')
    requested = {index: {"text": request.text.strip(), "emotion": "", "description": request.description}}
    recovery = {"text": request.text.strip(), "emotion": "",
                "description": request.description,
                "seed_field": "" if request.inherit_seed else request.seed}
    creation_definitions = {index: recovery}
    expected_revisions = (None if request.expected_revision is None
                          else {index: request.expected_revision})
    binding = {"expected_count": request.expected_count, "expected_revisions": expected_revisions,
               "bootstrap_allowed": False}

    with _get_builder_state_lock(safe_name):
        initial_state = _load_builder_state_for_generation(safe_name)
        if initial_state is not _MISSING_BUILDER_STATE and index >= len(initial_state.get("samples", [])):
            raise HTTPException(status_code=400, detail="Sample index is outside the project's rows")
        resolve_dataset_generation_inputs(safe_name, requested=requested, require_existing=True,
                                          creation_definitions=creation_definitions, **binding)
        with _builder_task_lock:
            claim_id = claim_gpu_task("dataset_builder", resources=(_get_builder_project_key(safe_name),))
            try:
                resolved = resolve_dataset_generation_inputs(
                    safe_name, requested=requested, require_existing=True,
                    creation_definitions=creation_definitions, **binding)
                apply_builder_task_owner(safe_name)
            except BaseException:
                release_gpu_task_claim("dataset_builder", claim_id)
                raise
    inputs = resolved[index]
    try:
        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

        dest_filename = f"sample_{index:03d}.wav"
        dest_path = os.path.join(work_dir, dest_filename)
        generate_sample_audio(engine, dest_path, description=inputs["description"],
                              sample_text=inputs["text"], seed=inputs["generated_seed"])

        # Update state (cache-bust URL so browser loads fresh audio on regen)
        cache_bust = time.time_ns()
        audio_url = f"/dataset_builder/{safe_name}/{dest_filename}?t={cache_bust}"
        with _get_builder_state_lock(safe_name):
            state = _load_builder_state_for_generation(safe_name)
            samples = state.get("samples", [])
            if index >= len(samples) or not isinstance(samples[index], dict):
                raise AppError(409, "Dataset row changed; regenerate", key='error.dataset.row_changed')
            samples[index] = {
                **{key: value for key, value in samples[index].items() if key != "error"},
                "status": "done", "audio_url": audio_url,
                "text": inputs["text"], "emotion": inputs["emotion"],
                "description": inputs["description"], "seed": inputs["seed_field"],
                "generated_seed": inputs["generated_seed"],
            }
            state["samples"] = samples
            _save_builder_state(safe_name, state)
            new_revision = get_dataset_row_revision(samples[index])

        return {
            "status": "done",
            "sample_index": index,
            "audio_url": audio_url,
            "row_revision": new_revision,
            "row": samples[index],
        }
    except HTTPException:
        raise
    except core.WindowsOwnerCleanupError as error:
        core._retain_windows_owner_failure(process_state['dataset_builder'], error)
        raise
    except Exception as e:
        logger.exception("Dataset builder sample generation failed")
        committed, revision = None, None
        with _get_builder_state_lock(safe_name):
            state = _load_builder_state_for_generation(safe_name)
            samples = state.get("samples", [])
            if index < len(samples) and isinstance(samples[index], dict):
                samples[index] = {**samples[index], "status": "error", "error": str(e)}
                state["samples"] = samples
                _save_builder_state(safe_name, state)
                committed, revision = samples[index], get_dataset_row_revision(samples[index])
        detail = {"message": "Sample generation failed — see server logs for details."}
        if revision is not None:
            detail["row_revision"] = revision
            detail["row"] = {key: value for key, value in committed.items() if key != "error"}
        raise HTTPException(status_code=500, detail=detail) from e
    finally:
        release_gpu_task_claim("dataset_builder", claim_id)


@router.post("/api/dataset_builder/generate_sample")
async def dataset_builder_generate_sample(request: DatasetSampleGenRequest):
    """Generate a sample without blocking the API loop; worker owns its claim."""
    return await asyncio.to_thread(_generate_dataset_sample_sync, request)

@router.post("/api/dataset_builder/generate_batch")
async def dataset_builder_generate_batch(request: DatasetBatchGenRequest):
    """Batch generate dataset samples as a background task."""
    return await asyncio.to_thread(_dataset_builder_generate_batch, request)


def _dataset_builder_generate_batch(request: DatasetBatchGenRequest):
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
    sample_count = len(request.samples)
    global_seed = request.global_seed
    per_seeds = request.seeds

    # Full definitions for every supplied row: first-run creation and recovery
    # of legacy status-only placeholders use these, never an existing row edit.
    creation_definitions = {}
    for idx, sample in enumerate(request.samples):
        emotion, text = sample.emotion.strip(), sample.text.strip()
        seed_field = (per_seeds[idx] if per_seeds and idx < len(per_seeds) and per_seeds[idx] >= 0
                      else "")
        creation_definitions[idx] = {
            "text": text, "emotion": emotion,
            "description": f"{root_desc}, {emotion}" if emotion else root_desc,
            "seed_field": seed_field,
        }
    requested = {idx: creation_definitions[idx] for idx in to_generate}
    creation_metadata = {"description": root_desc, "global_seed": global_seed}
    binding = {"expected_count": request.expected_count,
               "expected_revisions": request.expected_revisions}

    def task(resolved, definitions):
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

            state = _load_builder_state_for_generation(safe_name)
            if state is _MISSING_BUILDER_STATE:
                state = {"description": "", "global_seed": "", "samples": []}
            if not state.get("samples"):
                state.update(_effective_project_metadata(state, creation_metadata))
            samples_state = state.get("samples", [])
            if len(samples_state) < sample_count:
                if len(samples_state) > 0:
                    raise ValueError("Dataset rows changed during generation")
                samples_state = [_new_dataset_row(definitions[i], state) for i in range(sample_count)]
                state["samples"] = samples_state
                _save_builder_state(safe_name, state)
            else:
                repaired = False
                for i, values in definitions.items():
                    if i < len(samples_state) and _is_uninitialized_dataset_row(samples_state[i]):
                        samples_state[i] = _new_dataset_row(values, state)
                        repaired = True
                if repaired:
                    state["samples"] = samples_state
                    _save_builder_state(safe_name, state)

            completed = 0
            for i, idx in enumerate(to_generate):
                if process_state["dataset_builder"]["cancel"]:
                    process_state["dataset_builder"]["logs"].append(f"[CANCEL] Stopped at {completed}/{total}")
                    break

                text = resolved[idx]["text"]
                emotion = resolved[idx]["emotion"]
                description = resolved[idx]["description"]

                # Mark as generating (preserve existing fields like emotion, seed)
                existing_s = samples_state[idx] if idx < len(samples_state) else {}
                samples_state[idx] = {
                    **{key: value for key, value in existing_s.items() if key != "error"},
                    "status": "generating", "text": text, "emotion": emotion, "description": description}
                state["samples"] = samples_state
                _save_builder_state(safe_name, state)

                process_state["dataset_builder"]["logs"].append(
                    f"[{i+1}/{total}] {('[' + emotion + '] ' if emotion else '')}\"{text[:60]}{'...' if len(text) > 60 else ''}\""
                )

                try:
                    seed = resolved[idx]["generated_seed"]

                    dest_filename = f"sample_{idx:03d}.wav"
                    dest_path = os.path.join(work_dir, dest_filename)
                    generate_sample_audio(engine, dest_path, description=description,
                                          sample_text=text, seed=seed)

                    samples_state[idx] = {
                        **{key: value for key, value in samples_state[idx].items() if key != "error"},
                        "status": "done",
                        "audio_url": f"/dataset_builder/{safe_name}/{dest_filename}?t={time.time_ns()}",
                        "text": text,
                        "emotion": emotion,
                        "description": description,
                    }
                    completed += 1
                except core.WindowsOwnerCleanupError:
                    raise
                except Exception as e:
                    logger.error(f"Dataset builder sample {idx} failed: {e}")
                    process_state["dataset_builder"]["logs"].append(f"  Error: {e}")
                    samples_state[idx] = {**samples_state[idx], "status": "error", "error": str(e), "text": text, "emotion": emotion}

                state["samples"] = samples_state
                _save_builder_state(safe_name, state)

            process_state["dataset_builder"]["logs"].append(
                f"[DONE] Generated {completed}/{total} samples"
            )
        except core.WindowsOwnerCleanupError:
            raise
        except Exception as e:
            logger.exception("Dataset builder batch generation crashed")
            process_state["dataset_builder"]["logs"].append(f"[ERROR] Batch generation crashed: {e}")
        finally:
            process_state["dataset_builder"]["running"] = False

    with _get_builder_state_lock(safe_name):
        initial_state = _load_builder_state_for_generation(safe_name)
        bootstrap_allowed = initial_state is _MISSING_BUILDER_STATE or not initial_state.get("samples", [])
        resolve_dataset_generation_inputs(safe_name, requested=requested,
                                          creation_definitions=creation_definitions,
                                          creation_metadata=creation_metadata,
                                          bootstrap_allowed=bootstrap_allowed, **binding)
        with _builder_task_lock:
            claim_id = reserve_background_task("dataset_builder", resources=(_get_builder_project_key(safe_name),))
            try:
                resolved = resolve_dataset_generation_inputs(
                    safe_name, requested=requested, creation_definitions=creation_definitions,
                    creation_metadata=creation_metadata, bootstrap_allowed=bootstrap_allowed, **binding)
                apply_builder_task_owner(safe_name)
                os.makedirs(work_dir, exist_ok=True)
                start_claimed_task_thread("dataset_builder", claim_id, task, resolved, creation_definitions)
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
        busy = activity["busy"] if activity is not None else is_task_running("dataset_builder", states=process_state)
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

    with ensure_builder_project_mutation(safe_name, "Wait for dataset generation before saving"):
        work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
        state = _require_existing_builder_state(safe_name)
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
    with ensure_builder_project_mutation(project_name, "Stop dataset generation before deleting its project"):
        if not os.path.exists(work_dir):
            raise HTTPException(status_code=404, detail="Dataset builder project not found")
        shutil.rmtree(work_dir)
        logger.info(f"Dataset builder project discarded: {name}")
        return {"status": "deleted", "name": name}


@router.delete("/api/dataset_builder/{name}")
async def dataset_builder_delete(name: str):
    return await asyncio.to_thread(_dataset_builder_delete_sync, name)
