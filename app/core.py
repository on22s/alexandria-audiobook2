import json
import logging
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from typing import List, Optional, Tuple

import aiofiles
from fastapi import HTTPException, UploadFile

from project import ProjectManager
from config_settings import load_app_config
from utils import (atomic_json_write, get_app_config_path, get_runtime_data_dir,
                   is_generic_speaker, is_path_inside, safe_load_json,
                   secure_filename)
from lmstudio_settings import (get_current_status, get_effective_max_tokens,
                               is_local_llm_endpoint)
from hf_utils import fetch_builtin_manifest, is_adapter_downloaded


logger = logging.getLogger("AlexandriaUI")


def _warn_corrupted_json(kind: str, path: str, action: str, e: Exception) -> None:
    """Log a consistent warning for a corrupted/unreadable JSON file that's
    falling back to some default. Shared by every site that catches a JSON
    parse failure on a config/state/manifest file - keeps the message format
    in one place instead of over a dozen independently-written copies."""
    logger.warning(f"Corrupted {kind} at {path}, {action}: {e}")


def _load_manifest(path):
    """Load a JSON manifest file, returning [] on missing or corrupt file."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("manifest", path, "returning empty list", e)
    return []


def _load_builtin_lora_manifest():
    """Load built-in LoRA manifest from HF (with local fallback). Returns ALL entries with download status."""
    entries = fetch_builtin_manifest(BUILTIN_LORA_DIR)
    result = []
    for entry in entries:
        entry = dict(entry)  # avoid mutating cached list
        local_id = entry["id"] if entry["id"].startswith("builtin_") else f"builtin_{entry['id']}"
        downloaded = is_adapter_downloaded(local_id, BUILTIN_LORA_DIR)
        entry["id"] = local_id
        entry["builtin"] = True
        entry["downloaded"] = downloaded
        entry["adapter_path"] = f"builtin_lora/{local_id}" if downloaded else None
        result.append(entry)
    return result


def _save_manifest(path, manifest):
    """Write a JSON manifest file."""
    atomic_json_write(manifest, path)


def _safe_subpath(base_dir: str, name: str) -> str:
    """Resolve `name` under `base_dir`, rejecting path traversal (e.g. '..' or
    absolute paths). Returns the realpath; raises HTTP 400 if it escapes.

    Guards endpoints that build a filesystem path from a user-supplied name and
    then delete/extract it, so a value like '..' can't reach outside base_dir.
    """
    target = os.path.realpath(os.path.join(base_dir, name))
    if not is_path_inside(target, base_dir):
        raise HTTPException(status_code=400, detail="Invalid name.")
    return target


def _require_safe_filename(raw_name: str, detail: str) -> str:
    """secure_filename(raw_name), raising HTTPException 400 with `detail` if
    it sanitizes to empty. Factors out the same 2-line sanitize-or-reject
    pattern repeated across the many endpoints below that take a
    user-supplied name/filename and reject the request outright if it's
    invalid (loops that skip/log a single bad item instead of rejecting the
    whole request build the same check inline, since their failure handling
    differs per call site)."""
    safe = secure_filename(raw_name)
    if not safe:
        raise HTTPException(status_code=400, detail=detail)
    return safe


def check_disk_space(path, required_gb):
    """Check if disk has enough space. Returns (has_space, free_gb)."""
    try:
        stat = shutil.disk_usage(path)
        free_gb = stat.free / (1024 ** 3)
        return free_gb >= required_gb, free_gb
    except (OSError, ValueError) as e:
        logger.warning(f"Could not check disk space for {path}: {e}")
        return True, 0.0


async def _save_upload_limited(file: UploadFile, path: str, max_bytes: int) -> None:
    """Stream an upload to disk and remove it if it exceeds max_bytes."""
    written = 0
    try:
        async with aiofiles.open(path, "wb") as out_file:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="Uploaded file is too large.")
                await out_file.write(chunk)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
DATA_DIR = get_runtime_data_dir(ROOT_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = get_app_config_path(DATA_DIR, ROOT_DIR, BASE_DIR)
VOICE_CONFIG_PATH = os.path.join(DATA_DIR, "voice_config.json")
SCRIPT_PATH = os.path.join(DATA_DIR, "annotated_script.json")
AUDIOBOOK_PATH = os.path.join(DATA_DIR, "cloned_audiobook.mp3")
M4B_PATH = os.path.join(DATA_DIR, "audiobook.m4b")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads") if DATA_DIR != ROOT_DIR else os.path.join(BASE_DIR, "uploads")
SCRIPTS_DIR = os.path.join(DATA_DIR, "scripts")
CHUNKS_PATH = os.path.join(DATA_DIR, "chunks.json")
VOICE_LIBRARY_PATH = os.path.join(DATA_DIR, "voice_library.json")
CHARACTER_ALIASES_PATH = os.path.join(DATA_DIR, "character_aliases.json")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
API_LOG_DIR = os.path.join(DATA_DIR, "logs", "api")
os.makedirs(API_LOG_DIR, exist_ok=True)


def get_active_book_id() -> Optional[str]:
    """Return the stable active-book id stored in state.json, if available."""
    state = safe_load_json(os.path.join(DATA_DIR, "state.json"), default={})
    book_id = secure_filename(state.get("active_book_id") or "")
    if book_id:
        return book_id
    input_path = state.get("input_file_path") or ""
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return secure_filename(stem) or None


def _save_active_book_id(book_id: str, input_path: Optional[str] = None) -> None:
    state_path = os.path.join(DATA_DIR, "state.json")
    state = safe_load_json(state_path, default={})
    state["active_book_id"] = secure_filename(book_id)
    if input_path is not None:
        state["input_file_path"] = input_path
    atomic_json_write(state, state_path)


def _saved_book_meta_path(name: str) -> str:
    return os.path.join(SCRIPTS_DIR, f"{name}.meta.json")


def _get_saved_book_id(name: str) -> str:
    meta = safe_load_json(_saved_book_meta_path(name), default={})
    return secure_filename(meta.get("book_id") or name)


SHARED_DEFAULT_NAMES = {"narrator"}
CAST_MAJOR_LINE_THRESHOLD = 25


def _norm_name(name: str) -> str:
    """Normalize a character name for matching: lowercase, trimmed, collapsed spaces."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def get_cast_member_key(name: str, book_id: Optional[str]) -> str:
    """Return a cross-book key, scoping generic labels to one book."""
    key = _norm_name(name)
    if is_generic_speaker(name):
        if not book_id:
            raise ValueError(f"Book identity is required for generic character '{name}'.")
        return f"{key}::{secure_filename(book_id)}"
    return key


def get_cast_storage_pool(lib: dict, cast_name: str, name: str,
                          cast_specific: bool = False) -> dict:
    """Return the single authoritative storage pool for a cast member."""
    if _norm_name(name) in SHARED_DEFAULT_NAMES and not cast_specific:
        return lib["shared"]
    return lib["casts"][cast_name].setdefault("members", {})


def get_cast_adapter_usage(lib: dict, cast_name: Optional[str]) -> dict:
    """Derive LoRA usage from distinct stored cast-member identities."""
    usage = {}
    if not cast_name or cast_name not in lib.get("casts", {}):
        return usage
    members = list(lib.get("shared", {}).items())
    members += list(lib["casts"][cast_name].get("members", {}).items())
    for key, member in members:
        cfg = member.get("config") or {}
        adapter_id = cfg.get("adapter_id")
        if not adapter_id:
            continue
        item = usage.setdefault(adapter_id, {"character_count": 0, "total_lines": 0, "characters": []})
        item["character_count"] += 1
        assignments = member.get("assignments") or {}
        total_lines = sum(max(0, int(a.get("line_count", 0) or 0)) for a in assignments.values())
        if not assignments:
            total_lines = max(0, int(member.get("line_count", 0) or 0))
        item["total_lines"] += total_lines
        item["characters"].append(member.get("name", key))
    return usage


def _load_voice_library() -> dict:
    lib = {"shared": {}, "casts": {}}
    if os.path.exists(VOICE_LIBRARY_PATH):
        try:
            with open(VOICE_LIBRARY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                lib["shared"] = data.get("shared", {}) or {}
                lib["casts"] = data.get("casts", {}) or {}
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("voice library", VOICE_LIBRARY_PATH, "resetting to empty", e)
    return lib


def _script_line_counts(path: str = SCRIPT_PATH) -> dict:
    """Per-speaker line counts from the given annotated script (defaults to the current one)."""
    counts = {}
    script = safe_load_json(path)
    if isinstance(script, list):
        for entry in script:
            speaker = (entry.get("speaker") or entry.get("type") or "").strip()
            if speaker and (entry.get("text") or "").strip():
                counts[speaker] = counts.get(speaker, 0) + 1
    return counts


def get_trait_assignment_metadata(source):
    """Normalize the single persisted shape for casting trait decisions."""
    return {
        "character_gender": source.get("character_gender", "unknown"),
        "character_age_group": source.get("character_age_group", "unknown"),
        "voice_gender": source.get("voice_gender", "unknown"),
        "voice_age_group": source.get("voice_age_group", "unknown"),
        "trait_evidence": (source.get("trait_evidence") or "")[:300],
        "local_trait_evidence": (source.get("local_trait_evidence") or "")[:300],
        "llm_trait_evidence": (source.get("llm_trait_evidence") or "")[:300],
        "gender_confidence": source.get("gender_confidence", "unknown"),
        "age_confidence": source.get("age_confidence", "unknown"),
        "gender_fallback": bool(source.get("gender_fallback")),
        "existing_trait_mismatch": bool(source.get("existing_trait_mismatch")),
    }


def _make_library_entry(display_name: str, config: dict, line_count: int,
                        book_id: Optional[str] = None, casting: Optional[dict] = None,
                        existing: Optional[dict] = None) -> dict:
    cfg = dict(config or {})
    cfg.pop("alias_of", None)  # aliases are book-specific; don't carry across books
    entry = dict(existing or {})
    assignments = dict(entry.get("assignments") or {})
    if book_id:
        assignments[book_id] = {
            "line_count": line_count,
            "character_style": cfg.get("character_style", ""),
            "suggestion_reason": (casting or {}).get("suggestion_reason", ""),
            "priority": (casting or {}).get("priority", "major" if line_count >= CAST_MAJOR_LINE_THRESHOLD else "minor"),
            "reuse_count_when_assigned": (casting or {}).get("reuse_count_when_assigned", 0),
            "assigned_at": time.time(),
            **get_trait_assignment_metadata(casting or {}),
        }
    entry.update({
        "name": display_name,
        "config": cfg,
        "line_count": line_count,
        "generic": is_generic_speaker(display_name),
        "book_id": book_id if is_generic_speaker(display_name) else None,
        "assignments": assignments,
        "saved_at": time.time(),
    })
    return entry



def _task_log_path(task_name: str) -> str:
    """Full on-disk log for a task. The in-memory state['logs'] is a capped live tail;
    this file keeps the complete history so nothing is lost on long/batch runs."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", task_name)
    return os.path.join(API_LOG_DIR, f"{safe}-latest.log")


def _init_task_log(task_name: str, extra_header: str = "") -> str:
    """Start a fresh on-disk log for a task run with a header banner, swallowing
    any OSError (e.g. read-only filesystem). Returns the log path regardless."""
    log_path = _task_log_path(task_name)
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# {task_name} log — {time.strftime('%Y-%m-%d %H:%M:%S')}\n{extra_header}")
    except OSError:
        pass
    return log_path


DESIGNED_VOICES_DIR = os.path.join(DATA_DIR, "designed_voices")
CLONE_VOICES_DIR = os.path.join(DATA_DIR, "clone_voices")
LORA_MODELS_DIR = os.path.join(DATA_DIR, "lora_models")
LORA_MODELS_MANIFEST = os.path.join(LORA_MODELS_DIR, "manifest.json")
LORA_DATASETS_DIR = os.path.join(DATA_DIR, "lora_datasets")
BUILTIN_LORA_DIR = os.path.join(ROOT_DIR, "builtin_lora")
DATASET_BUILDER_DIR = os.path.join(DATA_DIR, "dataset_builder")
PREPARER_SCRIPT_PATH = os.path.join(ROOT_DIR, "alexandria_preparer_rocm_compatible.py")
PREPARER_OUTPUT_DIR = os.path.join(DATA_DIR, "preparer_output")
VOICELAB_CONFIG_PATH = os.path.join(DATA_DIR, "voicelab_config.json")

VOICELAB_DEFAULTS = {
    # Interpreter with the full Voice Lab ML stack, including speechbrain and
    # llama_cpp. No default: this lives outside the repo, so any path derived
    # from ROOT_DIR is a guess that cannot resolve. Empty means "not configured".
    "rocm_python": os.environ.get("ALEXANDRIA_ROCM_PYTHON", ""),
    # GGUF model voice_profiler.py uses for the prose descriptions ("" = its default)
    "profiler_model": os.environ.get("ALEXANDRIA_PROFILER_MODEL", ""),
    # Default zips2 root (folder of narrator subfolders) the dedup stage reads
    "zips_dir": os.environ.get("ALEXANDRIA_ZIPS_DIR", os.path.join(DATA_DIR, "zips2")),
}

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(SCRIPTS_DIR, exist_ok=True)
os.makedirs(DESIGNED_VOICES_DIR, exist_ok=True)
os.makedirs(CLONE_VOICES_DIR, exist_ok=True)
os.makedirs(LORA_MODELS_DIR, exist_ok=True)
os.makedirs(LORA_DATASETS_DIR, exist_ok=True)
os.makedirs(DATASET_BUILDER_DIR, exist_ok=True)
os.makedirs(PREPARER_OUTPUT_DIR, exist_ok=True)

# Static and generated asset directories
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

VOICELINES_DIR = os.path.join(DATA_DIR, "voicelines")
os.makedirs(VOICELINES_DIR, exist_ok=True)

os.makedirs(BUILTIN_LORA_DIR, exist_ok=True)

project_manager = ProjectManager(DATA_DIR)


# Directories this app writes user/attacker-suppliable content into (uploads,
# extracted dataset ZIPs, generated samples/previews, preparer output).
# voicelab's rocm_python/profiler_model must never resolve
# inside one of these - otherwise anyone who can upload a file (via
# /api/upload, /api/lora/upload_dataset, etc.) or run the preparer (which
# writes to PREPARER_OUTPUT_DIR with an attacker-chosen filename) could point
# voicelab at content they just planted and have it executed as the
# "trusted" interpreter or pipeline script.
_VOICELAB_FORBIDDEN_DIRS = [
    UPLOADS_DIR, LORA_DATASETS_DIR, LORA_MODELS_DIR, BUILTIN_LORA_DIR,
    DATASET_BUILDER_DIR, DESIGNED_VOICES_DIR, CLONE_VOICES_DIR, VOICELINES_DIR,
    PREPARER_OUTPUT_DIR,
]


def _validate_voicelab_path(path: str, what: str) -> None:
    """Raise HTTPException 400 if `path` resolves inside a directory this app
    writes uploaded/generated content into - see _VOICELAB_FORBIDDEN_DIRS."""
    for forbidden in _VOICELAB_FORBIDDEN_DIRS:
        if is_path_inside(path, forbidden):
            raise HTTPException(
                status_code=400,
                detail=f"{what} cannot be inside {forbidden} - that directory holds "
                       f"uploaded/generated content, not trusted pipeline code.")


def _load_voicelab_config() -> dict:
    cfg = dict(VOICELAB_DEFAULTS)
    if os.path.exists(VOICELAB_CONFIG_PATH):
        try:
            with open(VOICELAB_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in VOICELAB_DEFAULTS})
        except (json.JSONDecodeError, ValueError, OSError) as e:
            _warn_corrupted_json("voicelab config", VOICELAB_CONFIG_PATH, "using defaults", e)
    return cfg


def _revalidate_voicelab_paths(*path_label_pairs: Tuple[Optional[str], str]) -> Optional[HTTPException]:
    """Run _validate_voicelab_path on each (path, label) pair, skipping falsy
    paths, and return the first HTTPException raised (or None if all pass).

    Every caller of this is a background_tasks.add_task closure re-checking
    a value that was already validated synchronously before the task was
    scheduled - the deferral until after the HTTP response is sent leaves a
    window where the on-disk target could be repointed in between. Shared
    here specifically because preparer_start, preparer_batch_start, and
    voicelab_start each used to hand-roll this same try/except, and one of
    those three copies (preparer_batch_start) silently covered less than
    its own comment claimed - one canonical implementation can't drift out
    of sync with itself the way three independent copies already did.
    Callers still apply their own state-dict's abort contract (which fields
    to set, return vs. break a loop) since those genuinely differ.
    """
    for path, label in path_label_pairs:
        if path:
            try:
                _validate_voicelab_path(path, label)
            except HTTPException as e:
                return e
    return None


# Global state for process tracking
process_state = {
    "script": {"running": False, "logs": [], "cancel": False, "pid": None, "process": None, "paused": False, "start_time": None},
    "voices": {"running": False, "logs": []},
    "persona": {"running": False, "logs": [], "cancel": False, "process": None},
    "audio": {"running": False, "logs": [], "cancel": False, "start_time": None},
    "audacity_export": {"running": False, "logs": []},
    "m4b_export": {"running": False, "logs": []},
    "review": {"running": False, "logs": [], "cancel": False, "pid": None, "process": None, "paused": False, "start_time": None},
    "batch_review": {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1, "process": None, "pid": None, "paused": False, "start_time": None, "bidirectional": False,
                     "totals_fwd": {"text_changed": 0, "speaker_changed": 0, "instruct_changed": 0, "entries_added": 0, "entries_removed": 0, "narrators_merged": 0, "speakers_merged": 0, "batches_failed": 0, "batches_skipped_vram": 0, "total_changes": 0, "books_done": 0},
                     "totals_bwd": {"text_changed": 0, "speaker_changed": 0, "instruct_changed": 0, "entries_added": 0, "entries_removed": 0, "narrators_merged": 0, "speakers_merged": 0, "batches_failed": 0, "batches_skipped_vram": 0, "total_changes": 0, "books_done": 0},
                     "aliases_fwd": [], "aliases_bwd": []},
    "nicknames": {"running": False, "logs": [], "cancel": False, "pid": None, "process": None, "paused": False, "start_time": None},
    "lora_training": {"running": False, "logs": [], "cancel": False, "process": None, "pid": None, "start_time": None},
    "lora_test": {"running": False, "logs": []},
    "voice_design": {"running": False, "logs": []},
    "lmstudio_optimize": {"running": False, "logs": []},
    "dataset_builder": {"running": False, "logs": [], "cancel": False},
    "preparer": {"running": False, "logs": [], "cancel": False, "process": None, "status": "idle", "output_file": None},
    "batch_preparer": {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1},
    "batch_script":   {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1, "process": None, "pid": None, "paused": False, "start_time": None},
    "voicelab":       {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1, "process": None, "pid": None, "paused": False, "status": "idle", "start_time": None},
}

# Tasks that don't touch the GPU/LLM and are exempt from the global GPU lock.
# "voices" (suggest_voices) is intentionally NOT here: it runs local LLM
# inference, so it must respect the GPU lock to avoid OOM alongside TTS/review.
NON_GPU_TASKS = {"audacity_export", "m4b_export"}
GPU_TASKS = set(process_state.keys()) - NON_GPU_TASKS

def check_global_gpu_lock(new_task_name: str):
    """Prevent multiple GPU-intensive tasks from running concurrently and causing an OOM crash.

    Raises HTTPException on conflict (every caller relies on this propagating
    straight out of the route handler) - unlike check_disk_space/
    check_text_loss's return-a-value convention.
    """
    if process_state.get(new_task_name, {}).get("running"):
        raise HTTPException(
            status_code=400,
            detail=f"{new_task_name.replace('_', ' ').capitalize()} is already running."
        )
    if new_task_name in NON_GPU_TASKS:
        # NON_GPU_TASKS are exempt from the global GPU lock - don't block them
        # on other GPU tasks' running state, only guard against double-starting
        # themselves (handled above).
        return
    for task_name in GPU_TASKS:
        if task_name != new_task_name and process_state.get(task_name, {}).get("running"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start {new_task_name.replace('_', ' ')}: {task_name.replace('_', ' ')} is currently running. Please wait for it to finish or cancel it to free up GPU VRAM."
            )

_gpu_lock = threading.Lock()

def claim_gpu_task(task_name: str):
    """Atomically re-check and reserve the GPU lock for task_name.

    check_global_gpu_lock() alone has a TOCTOU race: two requests for
    different GPU tasks can both pass the check before either's
    process_state[...]["running"] flag is set, and both start. Call this
    immediately before scheduling the background task (after all validation
    that could fail has already happened) to atomically perform the final
    check and mark the task as running.
    """
    with _gpu_lock:
        check_global_gpu_lock(task_name)
        if "cancel" in process_state[task_name]:
            process_state[task_name]["cancel"] = False
        process_state[task_name]["running"] = True

def _init_batch_state(state: dict, logs: list, tasks: list) -> None:
    """Reset a process_state[...] entry for the start of a new batch run.

    Common initialization shared by review_script_batch_start,
    generate_script_batch_start, and voicelab_start's background _run().
    """
    state["running"] = True
    state["paused"] = False
    state["start_time"] = time.time()
    state["_last_eta_fraction"] = 0.0
    state["logs"] = logs
    state["tasks"] = tasks
    state["current_task_idx"] = -1
    # Clear any stale process/pid left over from a previous run so an early
    # cancel (before the first subprocess is spawned) can't signal a dead or
    # recycled PID.
    if "process" in state:
        state["process"] = None
    if "pid" in state:
        state["pid"] = None


_PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')

# Tasks worth surfacing a progress/ETA estimate for, most-relevant first.
ETA_TASKS = [
    ("batch_review", "Batch review"),
    ("batch_script", "Batch script generation"),
    ("voicelab", "Voice Lab"),
    ("script", "Script generation"),
    ("review", "Script review"),
    ("nicknames", "Nickname discovery"),
    ("audio", "Audio generation"),
]


def _compute_eta(state: dict) -> dict:
    """Best-effort progress fraction + ETA for a running task.

    Combines wall-clock elapsed time since the task started with the most
    recent "current/total" marker found in its logs (e.g. "Reviewing batch
    71/104", "Progress: 12/40"). For batch tasks (tasks + current_task_idx),
    the per-item log progress is folded in as a fraction of the current item,
    so a 58-book batch reports overall progress rather than just the current
    book's.
    """
    start = state.get("start_time")
    if not start:
        return {"elapsed_seconds": None, "eta_seconds": None, "progress": None, "fraction": None}
    elapsed = time.time() - start

    sub_fraction = 0.0
    sub_progress = None
    for line in reversed(state.get("logs", [])[-30:]):
        if "VRAM" in line:
            # The VRAM watchdog prints lines like "(10.5/12.0 GB)" which can
            # otherwise be mistaken for a "current/total" progress marker.
            continue
        stripped = line.strip()
        if stripped.startswith("---") or stripped.startswith(">>>") or stripped.startswith("==="):
            # Per-book banner/summary lines (e.g. "--- [4/10] Reviewing
            # '...' ---" or ">>> [4/10] '...' done: ... <<<") also match
            # N/M but describe book-level progress, not the current item's
            # sub-batch progress.
            continue
        m = _PROGRESS_RE.search(line)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            if 0 < cur <= tot:
                sub_fraction = cur / tot
                sub_progress = f"{cur}/{tot}"
                break
            # Not a valid "current/total" marker (e.g. cur == 0 or cur > tot) -
            # keep scanning earlier lines for one that is.

    tasks = state.get("tasks")
    idx = state.get("current_task_idx")
    if tasks and idx is not None and idx >= 0:
        num_items = len(tasks)
        if state.get("bidirectional"):
            # A bidirectional batch processes every book twice (forward, then
            # backward). current_task_idx counts down during the backward
            # pass, so map it onto the second half of the overall range to
            # keep progress/ETA monotonically increasing.
            total_items = num_items * 2
            if state.get("current_pass") == "bwd":
                position = total_items - 1 - idx
                # Book number matches the per-book log lines elsewhere
                # (e.g. "--- [{i+1}/{total}] Reviewing ... ---"), which use
                # idx + 1 in both passes - not a reversed countdown.
                progress = f"item {idx + 1}/{num_items} (pass 2/2)"
            else:
                position = idx
                progress = f"item {idx + 1}/{num_items} (pass 1/2)"
        else:
            total_items = num_items
            position = idx
            progress = f"item {idx + 1}/{total_items}"
        fraction = (position + sub_fraction) / total_items
        fraction = min(1.0, max(0.0, fraction))
        # Ensure fraction never decreases (protects against unexpected idx behavior)
        last_fraction = state.get("_last_eta_fraction", 0.0)
        if fraction < last_fraction:
            # If fraction went backwards, clamp it to the last known value
            fraction = last_fraction
        state["_last_eta_fraction"] = fraction
        if sub_progress:
            progress += f" ({sub_progress})"
    else:
        fraction = sub_fraction or None
        progress = sub_progress

    eta_seconds = None
    if fraction and fraction > 0.001:  # Avoid enormous ETAs at start
        eta_seconds = elapsed * (1 - fraction) / fraction
    return {"elapsed_seconds": elapsed, "eta_seconds": eta_seconds, "progress": progress, "fraction": fraction}

def run_process(command: List[str], task_name: str, cwd: str = None):
    """Run a subprocess and stream its output into process_state logs."""
    state = process_state[task_name]

    # NOTE: do NOT bail out here if state["running"] is already True. GPU tasks
    # reserve their slot via claim_gpu_task() on the request thread (which sets
    # running=True before this background task is scheduled), and the few
    # non-GPU callers (e.g. nicknames) guard against double-starts at their own
    # endpoint. A guard here would see claim_gpu_task's own reservation and
    # abort every GPU task, deadlocking the queue.
    state["running"] = True
    state["logs"] = []
    if "paused"      in state: state["paused"]      = False
    if "status"      in state: state["status"]      = "running"
    if "return_code" in state: state["return_code"] = None
    if "process"     in state: state["process"]     = None
    if "pid"         in state: state["pid"]         = None
    if "start_time"  in state: state["start_time"]  = time.time()

    logger.info(f"Starting task {task_name}: {' '.join(command)}")

    # Start a fresh on-disk log for this run (full history; in-memory list is a capped tail)
    log_path = _init_task_log(task_name, extra_header=f"# {' '.join(command)}\n")

    return_code = None
    try:
        return_code, _ = _stream_subprocess_to_logs(command, cwd or BASE_DIR, state, log_file=log_path)

        if state.get("cancel"):
            state["logs"].append(f"Task {task_name} cancelled.")
            if "status" in state: state["status"] = "cancelled"
        elif return_code == 0:
            if "status" in state: state["status"] = "done"
            completion_note = f"Task {task_name} completed successfully."
            report_path = None
            if task_name == "review":
                stats = _extract_review_stats(state["logs"])
                if stats:
                    if stats.get("batches_skipped_vram"):
                        completion_note = (
                            f"Task {task_name} completed, but {stats['batches_skipped_vram']} "
                            f"section(s) were skipped because the GPU ran low on memory. "
                            f"Re-run the review to finish the rest."
                        )
                    highlights = _extract_diff_highlights(state["logs"])
                    report_path = _write_single_review_report(stats, highlights)
            state["logs"].append(completion_note)
            if report_path:
                state["logs"].append(f"Wrote review report: {os.path.relpath(report_path, ROOT_DIR)}")
        else:
            # Check if process was killed by a signal (negative return code on Unix)
            if return_code < 0:
                sig_name = signal.Signals(-return_code).name if hasattr(signal, 'Signals') else f"signal {-return_code}"
                state["logs"].append(f"Task {task_name} was cancelled ({sig_name}).")
                if "status" in state: state["status"] = "cancelled"
            else:
                state["logs"].append(f"Task {task_name} failed with return code {return_code}.")
                if "status" in state: state["status"] = "failed"

    except Exception as e:
        # Catch-all so we always clean up the task state even if the exception
        # is unexpected (e.g. AttributeError, KeyError from internal bugs).
        logger.exception(f"Error running {task_name}: {e}")
        state["logs"].append(f"Error: {str(e)}")
        if "status" in state:
            state["status"] = "cancelled" if state.get("cancel") else "failed"
    finally:
        if "process"     in state: state["process"]     = None
        if "pid"         in state: state["pid"]         = None
        state["running"] = False
        if "return_code" in state: state["return_code"] = return_code







def _resume_if_paused(state: dict, proc):
    """Wake a SIGSTOP'd process so a subsequent SIGTERM/cancel actually takes effect.

    A stopped process ignores SIGTERM until resumed, so any cancel path that
    might be cancelling a paused task must SIGCONT it first. Shared by
    _cancel_task and generate_script_batch_cancel so the two stay in sync.
    """
    if proc is not None and state.get("paused") and sys.platform != "win32":
        try:
            _send_signal_tree(proc, signal.SIGCONT)
        except (ProcessLookupError, OSError):
            pass
        state["paused"] = False


def _cancel_task(state_key: str, not_running_msg: str, exited_msg: str):
    """Terminate a running subprocess task, or queue cancel if Popen hasn't run yet."""
    state = process_state[state_key]
    if not state["running"]:
        raise HTTPException(status_code=400, detail=not_running_msg)

    proc = state.get("process")
    _resume_if_paused(state, proc)

    pid = state.get("pid")
    if not pid:
        # Pre-Popen race window: flag is checked by run_process immediately after Popen
        state["cancel"] = True
        return {"status": "cancel queued"}
    try:
        # Signal the whole group (grandchildren too); proc when we have it, else pid.
        _send_signal_tree(proc if proc is not None else pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        raise HTTPException(status_code=400, detail=exited_msg)
    return {"status": "cancel signal sent", "pid": pid}


def _batch_cancel_helper(state_key: str):
    state = process_state[state_key]
    state["cancel"] = True
    _resume_if_paused(state, state.get("process"))

    # Also terminate the current subprocess (whole group) if it's running
    proc = state.get("process")
    if proc and proc.poll() is None:
        try:
            _send_signal_tree(proc, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    return {"status": "cancel_requested"}


def _run_claimed_background_task(task_name: str, callback) -> None:
    """Run a claimed callback and always release its process-state slot."""
    state = process_state[task_name]
    try:
        callback()
    except Exception as e:
        logger.exception("Background task %s failed: %s", task_name, e)
        state.setdefault("logs", []).append(f"Error: {e}")
        if "status" in state:
            state["status"] = "failed"
    finally:
        if "process" in state:
            state["process"] = None
        if "pid" in state:
            state["pid"] = None
        state["running"] = False



def _stream_subprocess_to_logs(command: List[str], cwd: str, state: dict, log_prefix: str = "", max_logs: int = 20000, log_file: str = None, env: dict = None) -> Tuple[int, List[str]]:
    """Run a subprocess, appending its merged stdout/stderr into state['logs'].

    Uses a reader thread + Queue so the drain loop can check state['cancel']
    between reads without any platform-specific I/O multiplexing (e.g. no
    select.select(), which does not work on Windows pipes).

    state['logs'] is a capped in-memory tail (last `max_logs` lines) for the live
    UI; when `log_file` is given the *complete* output is also appended there so
    long single runs and multi-book batches never lose earlier lines.

    Returns (exit code, lines this call appended). The returned lines are this
    call's own output regardless of any cap-driven truncation of state['logs'],
    so callers can safely scan them for per-run markers (stats, diffs, aliases).
    """
    log_fh = None
    log_lines_since_flush = 0
    last_flush_time = time.time()
    if log_file:
        try:
            log_fh = open(log_file, "a", encoding="utf-8")
        except OSError:
            log_fh = None
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=env if env is not None else os.environ.copy(),
        # Own process group (pgid == pid) so cancel/pause can signal the whole
        # tree — grandchildren (e.g. Voice Lab's batch_train_lora → train_lora,
        # or the profiler's llama child) would otherwise survive. POSIX-only;
        # ignored on Windows.
        start_new_session=True,
    )

    if "process" in state:
        state["process"] = process
    if "pid" in state:
        state["pid"] = process.pid

    log_queue: queue.Queue = queue.Queue()

    def _reader(stream, q):
        try:
            for line in stream:
                q.put(line)
        except Exception as e:
            # Log the error so we know why the reader died
            logger.error(f"Subprocess reader thread failed: {e}")
        finally:
            # Always put None sentinel even if iteration failed
            try:
                q.put(None)
            except Exception:
                pass  # Queue might be closed, nothing we can do

    reader = threading.Thread(target=_reader, args=(process.stdout, log_queue), daemon=True)
    reader.start()

    own_lines: List[str] = []
    terminate_requested = False
    max_idle_cycles = 600  # Max consecutive Empty polls before assuming reader died (600 * 0.2s = 120s)
    idle_cycles = 0

    def _honor_cancel():
        # Signal the whole process group once, so a cancel reaches grandchildren
        # (the direct child's SIGTERM handler, if any, decides how to stop them).
        nonlocal terminate_requested
        if state.get("cancel") and not terminate_requested:
            terminate_requested = True
            try:
                _send_signal_tree(process, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                # Already exited — reader will deliver its None sentinel next.
                pass

    while True:
        try:
            line = log_queue.get(timeout=0.2)  # Increased from 0.05 to reduce CPU spinning
            idle_cycles = 0  # Reset on successful get
        except queue.Empty:
            if not state.get("paused"):
                idle_cycles += 1
            else:
                idle_cycles = 0  # Reset during pause to prevent false-positive reader thread timeouts
            if idle_cycles > max_idle_cycles:
                if process.poll() is not None:
                    # Process has exited but the reader thread hasn't delivered
                    # its output/None sentinel - it may have crashed.
                    logger.warning(f"Queue polling timed out after {max_idle_cycles * 0.2}s after process exit - assuming reader thread died")
                    break
                # Process is still running (e.g. a slow LLM call with no stdout
                # output) - keep waiting rather than dropping output that
                # arrives later.
                idle_cycles = 0
            _honor_cancel()
            continue
        # Also honor cancel when output is flowing continuously — otherwise a
        # chatty stage never reaches the Empty branch and can't be terminated
        # until it happens to go quiet.
        _honor_cancel()
        if line is None:
            break
        log_line = line.strip()
        if log_line:
            entry = f"{log_prefix}{log_line}" if log_prefix else log_line
            own_lines.append(entry)
            state["logs"].append(entry)
            if len(state["logs"]) > max_logs:
                state["logs"].pop(0)
            if log_fh:
                try:
                    log_fh.write(entry + "\n")
                    log_lines_since_flush += 1
                    now = time.time()
                    # Flush on whichever comes first: a burst of 50 lines, or ~1s
                    # since the last flush — so /api/logs/{task_name} (served
                    # directly from this file) doesn't lag the live in-memory
                    # log by much during slow-running tasks.
                    if log_lines_since_flush >= 50 or now - last_flush_time >= 1:
                        log_fh.flush()
                        log_lines_since_flush = 0
                        last_flush_time = now
                except OSError as e:
                    # Log write failed (e.g., disk full). Notify user and close file handle.
                    state["logs"].append(f"WARNING: Log file write failed ({e}). Subsequent logs will only appear in memory.")
                    try:
                        log_fh.close()
                    except OSError:
                        pass
                    log_fh = None  # Stop trying to write to disk

    reader.join()
    process.wait()
    if log_fh:
        try:
            log_fh.close()  # flushes any remaining buffered lines
        except OSError:
            pass
    return process.returncode, own_lines

_REVIEW_ENTRIES_RE = re.compile(r'Review complete:\s*(\d+)\s*->\s*(\d+)\s*entries')
_REVIEW_SUMMARY_PATTERNS = {
    "text_changed": re.compile(r'Text changed:\s*(\d+)'),
    "speaker_changed": re.compile(r'Speaker changed:\s*(\d+)'),
    "instruct_changed": re.compile(r'Instruct changed:\s*(\d+)'),
    "entries_added": re.compile(r'Entries added:\s*(\d+)'),
    "entries_removed": re.compile(r'Entries removed:\s*(\d+)'),
    "narrators_merged": re.compile(r'Narrators merged:\s*(\d+)'),
    "speakers_merged": re.compile(r'Speakers merged:\s*(\d+)'),
    "batches_failed": re.compile(r'Batches failed:\s*(\d+)'),
    "batches_skipped_vram": re.compile(r'Batches skipped \(low GPU VRAM\):\s*(\d+)'),
    "total_changes": re.compile(r'Total changes:\s*(\d+)'),
}
_ALIAS_HEADER_RE = re.compile(r'Found \d+ nickname/alias mapping')
_ALIAS_LINE_RE = re.compile(r"'(.+?)'\s*->\s*'(.+?)'(?:\s*\((.*)\))?\s*$")
_DIFF_PREVIEW_RE = re.compile(r'DIFF_PREVIEW_JSON:\s*(\{.*\})\s*$')


def _new_review_totals() -> dict:
    """Zeroed accumulator matching the keys _extract_review_stats() may set."""
    totals = {key: 0 for key in _REVIEW_SUMMARY_PATTERNS}
    totals["books_done"] = 0
    return totals


def _extract_review_stats(lines: List[str]) -> Optional[dict]:
    """Parse the 'Review complete: X -> Y entries ... Total changes: N' block that
    review_script.py prints at the end of each book's review. Returns None if the
    block isn't present (e.g. the subprocess crashed before finishing)."""
    entries_match = next((m for l in lines if (m := _REVIEW_ENTRIES_RE.search(l))), None)
    if not entries_match:
        return None
    stats = {key: 0 for key in _REVIEW_SUMMARY_PATTERNS}
    stats["entries_before"] = int(entries_match.group(1))
    stats["entries_after"] = int(entries_match.group(2))
    for line in lines:
        for key, pattern in _REVIEW_SUMMARY_PATTERNS.items():
            m = pattern.search(line)
            if m:
                stats[key] = int(m.group(1))
    return stats


def _combine_pass_stats(*stat_dicts: Optional[dict]) -> dict:
    """Sum per-pass review stats (e.g. forward + backward) into one dict, for
    displays — like a per-book badge tooltip — that should reflect a book's
    combined totals rather than only whichever pass ran last.

    Sets combined["partial"] = True if any of the given stat_dicts is None/
    falsy (a pass that crashed or hasn't run yet), so a consumer can tell
    "both passes ran and together found little" apart from "only one pass
    actually contributed to this total"."""
    combined = {key: 0 for key in _REVIEW_SUMMARY_PATTERNS}
    combined["partial"] = False
    for stats in stat_dicts:
        if not stats:
            combined["partial"] = True
            continue
        for key in combined:
            if key != "partial":
                combined[key] += stats.get(key, 0)
    return combined


def _combine_pass_totals(state: dict) -> dict:
    """Sum the forward and backward run-wide totals into one "Overall" dict, for
    the combined summary of a bidirectional batch review's two passes.

    books_done is special-cased: both passes process the same set of books, so
    summing would double-count; take the max so "Overall" reports how many
    distinct books completed at least one pass."""
    combined = _combine_pass_stats(state["totals_fwd"], state["totals_bwd"])
    combined["books_done"] = max(state["totals_fwd"]["books_done"], state["totals_bwd"]["books_done"])
    return combined


def _extract_new_aliases(lines: List[str]) -> List[dict]:
    """Parse the "Found N nickname/alias mapping(s): 'X' -> 'Y' (evidence)" block that
    find_nicknames.py prints when it discovers new aliases for a book."""
    aliases = []
    capturing = False
    for line in lines:
        if _ALIAS_HEADER_RE.search(line):
            capturing = True
            continue
        if capturing:
            m = _ALIAS_LINE_RE.search(line)
            if m:
                aliases.append({"variant": m.group(1), "canonical": m.group(2), "evidence": m.group(3) or ""})
            else:
                capturing = False
    return aliases


def _extract_diff_highlights(lines: List[str]) -> dict:
    """Parse the 'DIFF_PREVIEW_JSON: {...}' line that review_script.py prints after
    its final summary, containing the highest-impact before/after examples for the
    "diff preview" report section. Returns empty lists if not present or unparseable."""
    for line in reversed(lines):
        m = _DIFF_PREVIEW_RE.search(line)
        if m:
            try:
                data = json.loads(m.group(1))
                return {
                    "text_rewrites": data.get("text_rewrites", []),
                    "speaker_changes": data.get("speaker_changes", []),
                }
            except (json.JSONDecodeError, AttributeError) as e:
                logger.warning(f"Malformed DIFF_PREVIEW_JSON line, returning empty diff preview: {e}")
                break
    return {"text_rewrites": [], "speaker_changes": []}


def _format_book_summary(i: int, total: int, tag: str, name: str, stats: dict) -> str:
    """One-line, easy-to-spot summary of a single book's review changes for the live log."""
    bits = [f"{stats['total_changes']} changes",
            f"{stats['text_changed']} text", f"{stats['speaker_changed']} speaker",
            f"{stats['instruct_changed']} instruct",
            f"+{stats['entries_added']}/-{stats['entries_removed']} entries"]
    if stats["narrators_merged"]:
        bits.append(f"{stats['narrators_merged']} narrators merged")
    if stats["speakers_merged"]:
        bits.append(f"{stats['speakers_merged']} speakers merged")
    if stats["batches_failed"]:
        bits.append(f"{stats['batches_failed']} batch(es) failed")
    if stats["batches_skipped_vram"]:
        bits.append(f"{stats['batches_skipped_vram']} batch(es) skipped (VRAM)")
    return f">>> [{i+1}/{total}]{tag} '{name}' done: {', '.join(bits)} <<<"


def _format_pass_summary(label: str, totals: dict, aliases: List[dict], show_aliases: bool) -> str:
    """Roll-up summary line(s) for a finished pass (or the whole run) over the live log."""
    lines = [f"=== {label}: {totals['books_done']} book(s), {totals['total_changes']} total change(s) ===",
             f"  Text: {totals['text_changed']}, Speaker: {totals['speaker_changed']}, "
             f"Instruct: {totals['instruct_changed']}, Entries: +{totals['entries_added']}/-{totals['entries_removed']}"]
    if totals["narrators_merged"] or totals["speakers_merged"]:
        lines.append(f"  Narrators merged: {totals['narrators_merged']}, Speakers merged: {totals['speakers_merged']}")
    if totals["batches_failed"] or totals["batches_skipped_vram"]:
        lines.append(f"  Batches failed: {totals['batches_failed']}, skipped (VRAM): {totals['batches_skipped_vram']}")
    if show_aliases:
        if aliases:
            lines.append(f"  New alias(es) found ({len(aliases)}):")
            for a in aliases:
                ev = f"  ({a['evidence']})" if a.get("evidence") else ""
                lines.append(f"    '{a['variant']}' -> '{a['canonical']}'  [{a['book']}]{ev}")
        else:
            lines.append("  New aliases found: none")
    return "\n".join(lines)


_STAT_LABELS = [
    ("text_changed", "Lines with reworded text"),
    ("speaker_changed", "Lines where the speaker was corrected"),
    ("instruct_changed", "Lines with updated voice direction"),
    ("entries_added", "New lines added"),
    ("entries_removed", "Lines removed"),
    ("narrators_merged", "Narration lines merged together for smoother flow"),
    ("speakers_merged", "Lines updated for renamed/merged characters"),
]


def _markdown_stats_table(stats: dict) -> List[str]:
    """Plain-language bullet list of what changed, skipping anything that was zero."""
    lines = [f"- **Total changes:** {stats['total_changes']}"]
    for key, label in _STAT_LABELS:
        if stats.get(key):
            lines.append(f"- **{label}:** {stats[key]}")
    return lines


def _markdown_heads_up_lines(stats: dict) -> List[str]:
    """Plain-language notes about anything the reviewer couldn't finish."""
    lines = []
    if stats.get("batches_failed"):
        lines.append(f"- {stats['batches_failed']} section(s) ran into an error and were left "
                      f"unchanged. Running the review again may fix these.")
    if stats.get("batches_skipped_vram"):
        lines.append(f"- {stats['batches_skipped_vram']} section(s) were skipped because the "
                      f"graphics card was running low on memory. Running the review again may "
                      f"catch these.")
    return lines


def _markdown_aliases_lines(aliases: List[dict], pass_label: str = "") -> List[str]:
    """Plain-language bullet list of newly-discovered character name variants."""
    lines = []
    for a in aliases:
        evidence = f" — {a['evidence']}" if a.get("evidence") else ""
        book = f" (in *{a['book']}*)" if a.get("book") else ""
        lines.append(f"- **{a['variant']}** is also known as **{a['canonical']}**{book}{pass_label}{evidence}")
    return lines


def _markdown_diff_highlights_lines(highlights: dict, max_each: int = 3, heading: str = "###") -> List[str]:
    """Plain-language 'before vs after' examples for the most notable changes."""
    def _clean(s: str) -> str:
        return " ".join(s.split())

    lines = []
    rewrites = highlights.get("text_rewrites", [])[:max_each]
    if rewrites:
        lines += [f"{heading} Biggest rewrites", ""]
        for r in rewrites:
            book = f" (in *{r['book']}*)" if r.get("book") else ""
            lines.append(f"- **{r.get('speaker') or 'Narrator'}**{book}")
            lines.append(f"  - Before: “{_clean(r.get('before', ''))}”")
            lines.append(f"  - After: “{_clean(r.get('after', ''))}”")

    changes = highlights.get("speaker_changes", [])[:max_each]
    if changes:
        if rewrites:
            lines.append("")
        lines += [f"{heading} Speaker corrections", ""]
        for c in changes:
            book = f" (in *{c['book']}*)" if c.get("book") else ""
            lines.append(f"- “{_clean(c.get('text', ''))}” — was **{c.get('before') or '?'}**, "
                          f"corrected to **{c.get('after') or '?'}**{book}")
    return lines


def _markdown_book_pass_lines(stats: dict, diffs: Optional[dict], heading: str = "####") -> List[str]:
    """Stats table + heads-up notes + top diff highlights for one book (or one
    pass of a book, in a bidirectional run)."""
    lines = _markdown_stats_table(stats)
    hu = _markdown_heads_up_lines(stats)
    if hu:
        lines += [""] + hu
    if diffs:
        hl = _markdown_diff_highlights_lines(diffs, max_each=2, heading=heading)
        if hl:
            lines += [""] + hl
    return lines


_REPORT_SUMMARY_SYSTEM_PROMPT = (
    "You explain audiobook script-review results to someone with no technical or "
    "programming background. You will be given a Markdown report full of statistics "
    "about an automated review pass over a book script. Write a short summary "
    "(3-6 sentences, plain prose, no headings, no markdown formatting, no field names "
    "verbatim) that a non-technical person could read to understand what happened, "
    "what kinds of things were fixed, and whether anything needs their attention. "
    "Be warm and concrete, and mention any new character names that were discovered, "
    "if listed. Respond with the summary text only."
)


def _load_llm_config() -> dict:
    """Return the `llm` section of config.json, or {} if missing/unreadable."""
    return load_app_config(CONFIG_PATH).get("llm", {})


class LLMConfigError(ValueError):
    """Raised when the configured LLM base_url fails the local/trusted-host check.

    A subclass of ValueError so existing `except ValueError` handlers (e.g. in
    save_config) keep working, while callers that want to distinguish "LLM is
    misconfigured" from other failures (transient connection errors, bad JSON
    responses, etc.) can catch this specifically and surface it to the user.
    """


def _validate_local_llm_base_url(base_url: str) -> None:
    """Raise LLMConfigError if base_url is set but doesn't point to a local/trusted host.

    Enforced both when an LLM client is constructed (_make_llm_client) and when
    config is saved (save_config), so config.json can never persist a non-local
    endpoint and every consumer of the `llm` config section is protected.
    """
    if not base_url:
        return
    if is_local_llm_endpoint(base_url):
        return
    from urllib.parse import urlparse
    hostname = (urlparse(base_url).hostname or "").lower()
    # Thunder Compute forwards instance ports via *.thundercompute.net, so allow
    # that trusted remote host for running LM Studio on a Thunder GPU instance.
    if hostname == "thundercompute.net" or hostname.endswith(".thundercompute.net"):
        return
    raise LLMConfigError(f"LLM base_url '{base_url}' is not local. Only local/trusted LLM endpoints are permitted.")


# LLM client cache to avoid creating new HTTP sessions for every request.
# Keyed by config (base_url/api_key/model_name/timeout) since different call
# sites use different timeouts - a single-slot cache would thrash between them.
_llm_client_cache: dict = {}

def _make_llm_client(timeout: float = 60):
    """Build an OpenAI-compatible client + model name from config.json's `llm` section.

    Validates that the base_url is local/trusted.
    Reuses cached client if config hasn't changed to avoid connection pool leaks.
    """
    from openai import OpenAI
    llm_cfg = _load_llm_config()

    base_url = llm_cfg.get("base_url", "http://localhost:11434/v1")
    _validate_local_llm_base_url(base_url)

    # Create a hash of the config to detect changes
    config_key = f"{llm_cfg.get('base_url')}:{llm_cfg.get('api_key')}:{llm_cfg.get('model_name')}:{timeout}"

    # Reuse cached client if config hasn't changed
    cached_client = _llm_client_cache.get(config_key)
    if cached_client is not None:
        return cached_client, llm_cfg.get("model_name", "")

    client = OpenAI(
        base_url=base_url,
        api_key=llm_cfg.get("api_key", "local"),
        timeout=timeout,
    )
    model_name = llm_cfg.get("model_name", "")

    # Cache the client, bounding the cache size to avoid memory growth
    if len(_llm_client_cache) >= 10:
        oldest_key = next(iter(_llm_client_cache))
        evicted = _llm_client_cache.pop(oldest_key, None)
        if evicted is not None:
            evicted.close()
    _llm_client_cache[config_key] = client

    return client, model_name


def _llm_summarize_report(markdown_body: str) -> Optional[str]:
    """Ask the local LLM (the same one used for script review) to write a short,
    friendly plain-language summary of a review report.

    Best-effort: returns None (and the caller falls back to the plain-language
    report on its own) if the LLM is unavailable or the request fails.
    Validates that the configured base_url is local/trusted before sending data."""
    try:
        client, model_name = _make_llm_client(timeout=60)

        messages = [
            {"role": "system", "content": _REPORT_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": markdown_body},
        ]
        full_cfg = load_app_config(CONFIG_PATH)
        llm_cfg = full_cfg.get("llm") or {}
        status = get_current_status(
            full_cfg.get("llm_mode", "local"), llm_cfg.get("base_url", ""),
            model_name, (full_cfg.get("llm_remote_ssh") or "").strip(), use_cache=True)
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.4,
            max_tokens=get_effective_max_tokens(
                800, status.get("context_length"), messages, hard_max=4000),
        )
        text = (response.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        logger.warning(f"LLM report summary failed, continuing without it: {e}")
        return None


def _insert_llm_summary(lines: List[str], intro_len: int) -> List[str]:
    """Ask the local LLM to summarize `lines` in plain English and, if it answers,
    splice that summary in as an "In Plain English" section right after the intro
    (the first `intro_len` lines). Returns `lines` unchanged if the LLM is
    unavailable."""
    summary = _llm_summarize_report("\n".join(lines))
    if not summary:
        return lines
    return lines[:intro_len] + ["", "## In Plain English", "", summary] + lines[intro_len:]


def _write_single_review_report(stats: dict, highlights: Optional[dict] = None) -> Optional[str]:
    """Write a plain-language Markdown summary of a single (non-batch) review run.

    Returns the path to the written file, or None if it couldn't be written.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(REPORTS_DIR, f"review_{timestamp}.md")

    intro = [
        "# Script Review Report",
        "",
        f"*Generated {time.strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
        "The AI reviewer checked your script for mistakes — like the wrong character "
        "speaking a line, awkward wording, or repeated narration — and fixed what it found.",
        "",
        f"Your script went from **{stats['entries_before']}** lines to "
        f"**{stats['entries_after']}** lines.",
    ]

    if stats.get("batches_skipped_vram"):
        intro += ["", f"**Note:** this script was only partially reviewed — the GPU ran low "
                       f"on memory and {stats['batches_skipped_vram']} section(s) were skipped. "
                       "Re-run the review to finish the rest."]

    lines = list(intro)
    if stats["total_changes"] == 0:
        lines += ["", "No changes were needed — your script looked good!"]
    else:
        lines += ["", "## What changed", ""]
        lines += _markdown_stats_table(stats)

        if highlights:
            hl_lines = _markdown_diff_highlights_lines(highlights)
            if hl_lines:
                lines += ["", "## Highlights", ""]
                lines += hl_lines

    heads_up = _markdown_heads_up_lines(stats)
    if heads_up:
        lines += ["", "## Things to check", ""]
        lines += heads_up

    lines = _insert_llm_summary(lines, len(intro))

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return None
    return path


def _send_signal_tree(proc_or_pid, sig) -> None:
    """Send `sig` to the whole process group, so grandchildren are signalled too.

    Every cancelable/pausable subprocess is started with start_new_session=True
    (see _stream_subprocess_to_logs), which makes each the leader of its own
    process group (pgid == pid). Signalling only the direct child (proc.terminate/
    send_signal) misses grandchildren — e.g. Voice Lab's `train` stage runs
    batch_train_lora.py which spawns train_lora.py, and profiling spawns a llama
    process — so a cancel would leave the real GPU worker running orphaned and a
    pause would freeze only the idle wrapper. Kill the group instead.

    Accepts a Popen or a bare pid. Raises ProcessLookupError/OSError like
    proc.send_signal so existing callers' handlers still catch an already-exited
    process. Falls back to the direct process (Windows, or if the group can't be
    resolved)."""
    pid = proc_or_pid.pid if hasattr(proc_or_pid, "pid") else proc_or_pid
    if sys.platform != "win32":
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except ProcessLookupError:
            raise  # process/group already gone — let caller treat as exited
        except OSError:
            pass  # couldn't resolve/signal the group; fall back to direct
    if hasattr(proc_or_pid, "send_signal"):
        proc_or_pid.send_signal(sig)
    else:
        os.kill(pid, sig)


def _posix_signal(proc, signame):
    """Send a POSIX signal by name (e.g. "SIGSTOP") to the process's whole group.
    Raises 501 on Windows, where SIGSTOP/SIGCONT don't exist on the signal module
    — the name is resolved here, after the platform check, so callers never
    reference the constant directly and crash with AttributeError on Windows."""
    if sys.platform == "win32":
        raise HTTPException(status_code=501, detail="Pause/resume is not supported on Windows.")
    try:
        sig = getattr(signal, signame)
        _send_signal_tree(proc, sig)
    except AttributeError:
        raise HTTPException(status_code=400, detail=f"Invalid signal name: {signame}")
    except (ProcessLookupError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"Signal failed: {e}")


def _pause_task(state_key: str, not_running_msg: str, starting_up_msg: str, log_label: str):
    """Pause a running subprocess task by sending SIGSTOP."""
    state = process_state[state_key]
    if not state["running"]:
        raise HTTPException(status_code=400, detail=not_running_msg)
    proc = state.get("process")
    if proc is None:
        raise HTTPException(status_code=503, detail=starting_up_msg)
    _posix_signal(proc, "SIGSTOP")
    state["paused"] = True
    state["logs"].append(f"[PAUSED] {log_label} paused.")
    return {"status": "paused"}


def _resume_task(state_key: str, not_running_msg: str, log_label: str):
    """Resume a paused subprocess task by sending SIGCONT."""
    state = process_state[state_key]
    if not state["running"]:
        raise HTTPException(status_code=400, detail=not_running_msg)
    proc = state.get("process")
    if proc is None:
        raise HTTPException(status_code=400, detail="Process not available.")
    _posix_signal(proc, "SIGCONT")
    state["paused"] = False
    state["logs"].append(f"[RESUMED] {log_label} resumed.")
    return {"status": "resumed"}
