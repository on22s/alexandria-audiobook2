import json
import asyncio
import contextvars
import functools
import logging
import math
import os
import queue
import re
import signal
import shutil
import subprocess
import sys

from subprocess_ownership import (SUBPROCESS_TERMINATE_GRACE_SECONDS,
                                 start_owned_subprocess, stop_owned_subprocess, send_subprocess_signal, get_subprocess_lease_options,
                                 terminate_windows_process_tree as terminate_owned_windows_process_tree)
from voicelab_settings import get_voicelab_python
import threading
import time
import uuid
from review_report import save_review_report
from typing import List, Optional, Tuple

import aiofiles
from fastapi import HTTPException, UploadFile

from character_evidence import aliases_for
from project import ProjectManager
from config_settings import load_app_config
from utils import (atomic_json_write, get_app_config_path, get_runtime_data_dir,
                   is_generic_speaker, is_path_inside, safe_load_json,
                   secure_filename)
from lmstudio_settings import (get_active_llm_config, get_current_status, get_effective_max_tokens,
                               is_local_llm_endpoint)
from hf_utils import fetch_builtin_manifest, is_adapter_downloaded
from run_history import finish_run, prune_runs, record_artifact, start_run
from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock
from task_ownership import acquire_task_lease


logger = logging.getLogger("AlexandriaUI")


def _warn_corrupted_json(kind: str, path: str, action: str, e: Exception) -> None:
    """Log a consistent warning for a corrupted/unreadable JSON file that's
    falling back to some default. Shared by every site that catches a JSON
    parse failure on a config/state/manifest file - keeps the message format
    in one place instead of over a dozen independently-written copies."""
    logger.warning(f"Corrupted {kind} at {path}, {action}: {e}")


def _load_manifest(path):
    """Load a JSON manifest file, returning [] on missing or corrupt file."""
    entries = safe_load_json(path, default=[])
    valid = [entry for entry in entries if isinstance(entry, dict)]
    if len(valid) != len(entries):
        logger.warning("Ignoring malformed entries in manifest %s", path)
    return valid


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
    if target == os.path.realpath(base_dir) or not is_path_inside(target, base_dir):
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
        return False, 0.0


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
RUN_HISTORY_DIR = os.path.join(DATA_DIR, "run_history")
# Human evaluation-review history + pending blind sessions (Phase 6). Defined
# here so both routers/lora.py and app.py's startup prune use one path.
EVALUATION_REVIEWS_DIR = os.path.join(DATA_DIR, "evaluation_reviews")
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
    from book_state_transaction import apply_book_input_selection
    apply_book_input_selection(DATA_DIR, input_path, secure_filename(book_id))


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


def add_known_label(known_as, name: str) -> list:
    """Return `known_as` with `name` appended unless an equivalent spelling is
    already there (compared with _norm_name). Generic labels ("Man 1") are not
    identities and are never remembered."""
    labels = [str(x) for x in (known_as or []) if str(x).strip()]
    if not name or not name.strip() or is_generic_speaker(name):
        return labels
    if _norm_name(name) in {_norm_name(x) for x in labels}:
        return labels
    return labels + [name.strip()]


def get_member_labels(entry: dict, aliases: Optional[dict] = None) -> list:
    """Every speaker label a library member answers to: its display name, the
    labels it was saved from or applied to (`known_as`), and any registered
    alias of those. The one place that decides what a member is "known as"."""
    labels = add_known_label(entry.get("known_as"), entry.get("name", ""))
    out = list(labels)
    if aliases:
        for label in labels:
            for alias in sorted(aliases_for(label, aliases)):
                out = add_known_label(out, alias)
    return out


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


def get_cast_adapter_usage(lib: dict, cast_name: Optional[str], adapter_id_map=None) -> dict:
    """Derive LoRA usage from distinct stored cast-member identities."""
    usage = {}
    if not cast_name or cast_name not in lib.get("casts", {}):
        return usage
    members = list(lib.get("shared", {}).items())
    members += list(lib["casts"][cast_name].get("members", {}).items())
    from voice_manifest import get_resolved_adapter_id_mapping
    adapter_ids = [(member.get("config") or {}).get("adapter_id")
                   for _, member in members]
    identities = adapter_id_map
    if identities is None:
        identities = {os.path.normcase(name): current for name, current in
                      get_resolved_adapter_id_mapping(
                          LORA_MODELS_DIR, [adapter_id for adapter_id in adapter_ids if adapter_id]).items()}
    for key, member in members:
        cfg = member.get("config") or {}
        adapter_id = cfg.get("adapter_id")
        if not adapter_id:
            continue
        adapter_id = identities.get(os.path.normcase(adapter_id), adapter_id)
        item = usage.setdefault(adapter_id, {"character_count": 0, "total_lines": 0, "characters": []})
        item["character_count"] += 1
        assignments = member.get("assignments") or {}
        total_lines = sum(get_stored_line_count(a) for a in assignments.values())
        if not assignments:
            total_lines = get_stored_line_count(member)
        item["total_lines"] += total_lines
        item["characters"].append(member.get("name", key))
    return usage


def get_stored_line_count(entry) -> int:
    """Read a nonnegative count, reporting damaged persisted values."""
    try:
        return max(0, int(entry.get("line_count", 0) or 0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        logger.warning("Invalid stored line count; ignoring it")
        return 0


def _load_voice_library() -> dict:
    # favorites: adapter ids the user starred (#522 19.4); user data, so it
    # lives here rather than in the shipped built-in manifest.
    lib = {"shared": {}, "casts": {}, "favorites": []}
    if os.path.exists(VOICE_LIBRARY_PATH):
        try:
            with open(VOICE_LIBRARY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for section in ("shared", "casts", "favorites"):
                    value = data.get(section, lib[section])
                    if isinstance(value, type(lib[section])):
                        lib[section] = value
                    else:
                        logger.warning("Invalid voice library section %s; using default", section)
                lib["favorites"] = [str(a) for a in lib["favorites"] if a]
        except (json.JSONDecodeError, ValueError, OSError) as e:
            _warn_corrupted_json("voice library", VOICE_LIBRARY_PATH, "resetting to empty", e)
    return lib


def _script_line_counts(path: str = SCRIPT_PATH) -> dict:
    """Per-speaker line counts from the given annotated script (defaults to the current one)."""
    return get_script_line_counts(safe_load_json(path))


def get_cast_importance_order(names, line_counts):
    """-> names in importance order: the narrator, then most lines, then name.
    The one order for the Voices list and for voice casting, where earlier
    characters get first pick of unused library voices (Rule 15)."""
    return sorted(names, key=lambda name: (0 if _norm_name(name) == "narrator" else 1,
                                           -int(line_counts.get(name, 0) or 0), _norm_name(name), name))


def get_script_line_counts(script):
    """Count spoken rows consistently for whole books and scoped evidence."""
    counts = {}
    if isinstance(script, list):
        for entry in script:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("speaker") or entry.get("type") or "", str)
                    or not isinstance(entry.get("text") or "", str)):
                logger.warning("Ignoring malformed script entry when counting lines")
                continue
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


def get_portable_voice_config(config):
    """-> a copy of a character's voice config without what belongs to one
    book: its alias, its voice timeline (chunk indices), and character-state
    versions (bound to that book's script). Manual versions travel with the
    cast. The one definition for saving and applying casts (#1040 review C9)."""
    cfg = dict(config or {})
    cfg.pop("alias_of", None)
    cfg.pop("version_timeline", None)
    versions = {key: value for key, value in (cfg.get("versions") or {}).items()
                if not (isinstance(value, dict) and "persona_state" in value)}
    if "versions" in cfg:
        cfg["versions"] = versions
    if cfg.get("active_version") and cfg["active_version"] not in versions:
        cfg.pop("active_version", None)
    return cfg


def _make_library_entry(display_name: str, config: dict, line_count: int,
                        book_id: Optional[str] = None, casting: Optional[dict] = None,
                        existing: Optional[dict] = None) -> dict:
    cfg = get_portable_voice_config(config)  # book-specific parts never enter the library
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
        "known_as": add_known_label(entry.get("known_as"), display_name),
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
    # Interpreter that runs the Voice Lab ML stages. Since 2026-09-11 app/env
    # carries the whole stack (speechbrain, umap, matplotlib, seaborn, peft,
    # librosa) except llama_cpp, which the profile stage needs and which must
    # be the HIP build (see CLAUDE.md, "Voice Lab pipeline"). The default is
    # therefore the interpreter that is running - it exists by definition -
    # and the preflight probe reports per-stage what it lacks. A separate env
    # can still be configured here or via ALEXANDRIA_ROCM_PYTHON.
    "rocm_python": get_voicelab_python({}),
    # GGUF model voice_profiler.py uses for the prose descriptions ("" = its default)
    "profiler_model": os.environ.get("ALEXANDRIA_PROFILER_MODEL", ""),
    # Optional book folders used to enrich voice profiles with a prose sample.
    "epub_dirs": [p for p in os.environ.get("ALEXANDRIA_EPUB_DIRS", "").split(os.pathsep) if p],
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
    cfg = {**VOICELAB_DEFAULTS, "epub_dirs": list(VOICELAB_DEFAULTS["epub_dirs"])}
    if os.path.exists(VOICELAB_CONFIG_PATH):
        try:
            with open(VOICELAB_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg["rocm_python"] = get_voicelab_python(data, cfg["rocm_python"])
                for key in ("profiler_model", "zips_dir"):
                    if isinstance(data.get(key), str):
                        cfg[key] = data[key]
                epub_dirs = data.get("epub_dirs")
                if isinstance(epub_dirs, list) and all(
                        isinstance(path, str) for path in epub_dirs):
                    cfg["epub_dirs"] = list(epub_dirs)
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
    "chapter_export": {"running": False, "logs": [], "cancel": False},
    "drift_check": {"running": False, "logs": []},
    "speaker_review": {"running": False, "logs": [], "cancel": False, "status": "idle"},
    "report_explanation": {"running": False, "logs": [], "cancel": False, "status": "idle"},
    "review": {"running": False, "logs": [], "cancel": False, "pid": None, "process": None, "paused": False, "start_time": None},
    "batch_review": {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1, "process": None, "pid": None, "paused": False, "start_time": None, "bidirectional": False,
                     "totals_fwd": {"text_changed": 0, "speaker_changed": 0, "instruct_changed": 0, "entries_added": 0, "entries_removed": 0, "narrators_merged": 0, "speakers_merged": 0, "batches_failed": 0, "batches_skipped_vram": 0, "total_changes": 0, "books_done": 0},
                     "totals_bwd": {"text_changed": 0, "speaker_changed": 0, "instruct_changed": 0, "entries_added": 0, "entries_removed": 0, "narrators_merged": 0, "speakers_merged": 0, "batches_failed": 0, "batches_skipped_vram": 0, "total_changes": 0, "books_done": 0},
                     "aliases_fwd": [], "aliases_bwd": []},
    "nicknames": {"running": False, "logs": [], "cancel": False, "pid": None, "process": None, "paused": False, "start_time": None},
    "cast_list": {"running": False, "logs": [], "cancel": False, "pid": None, "process": None, "paused": False, "start_time": None},
    "lora_training": {"running": False, "logs": [], "cancel": False, "process": None, "pid": None, "start_time": None},
    "lora_test": {"running": False, "logs": []},
    "voice_design": {"running": False, "logs": []},
    "lmstudio_optimize": {"running": False, "logs": []},
    "llm_test": {"running": False, "logs": [], "start_time": None},
    "dataset_builder": {"running": False, "logs": [], "cancel": False},
    "preparer": {"running": False, "logs": [], "cancel": False, "process": None, "status": "idle", "output_file": None},
    "batch_preparer": {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1},
    "batch_script":   {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1, "process": None, "pid": None, "paused": False, "start_time": None},
    "voicelab":       {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1, "process": None, "pid": None, "paused": False, "status": "idle", "start_time": None},
    "benchmark":      {"running": False, "logs": [], "cancel": False, "tasks": [], "current_task_idx": -1, "process": None, "pid": None, "paused": False, "status": "idle", "start_time": None},
}

# Tasks that don't touch the GPU/LLM and are exempt from the global GPU lock.
# "voices" (suggest_voices) is intentionally NOT here: it runs local LLM
# inference, so it must respect the GPU lock to avoid OOM alongside TTS/review.
# "drift_check" scores audio with ECAPA under the sibling interpreter, which
# pins CPU on purpose (see voice_reference.py) - it never touches the GPU.
NON_GPU_TASKS = {"audacity_export", "m4b_export", "drift_check", "chapter_export"}
GPU_TASKS = set(process_state.keys()) - NON_GPU_TASKS
# Tasks whose only accelerator use is the LLM endpoint. When the active LLM
# profile is not on this machine's GPU (hosted API, a CPU-served model, a
# box across the network), these contend with each other for the endpoint
# but not with the TTS/LoRA tasks for the card.
LLM_TASKS = {"speaker_review", "report_explanation", "script", "batch_script", "review", "batch_review", "persona",
             "voices", "nicknames", "cast_list"} & set(process_state.keys())


def is_llm_on_this_gpu(config) -> bool:
    """The active LLM profile's `on_this_gpu`, or - when unset - whether its
    endpoint is local to this machine. Conservative on any failure: True."""
    try:
        from lmstudio_settings import get_active_llm_config, is_remote_llm
        llm = get_active_llm_config(config) or {}
        if llm.get("transport") == "manual":
            return False   # the user is the model; no GPU involved
        explicit = llm.get("on_this_gpu")
        if explicit is not None:
            return bool(explicit)
        return not is_remote_llm(config.get("llm_mode", "local"), llm.get("base_url", ""))
    except Exception:
        return True


def llm_is_on_this_gpu() -> bool:
    """Read the active config through the shared local-GPU dispatch policy."""
    try:
        return is_llm_on_this_gpu(load_app_config(CONFIG_PATH))
    except Exception:
        return True


def gpu_lock_conflicts(new_task_name: str, *, cpu_only=False) -> set:
    """The tasks that must not be running for `new_task_name` to start."""
    if cpu_only or new_task_name in NON_GPU_TASKS:
        return set()
    others = {name for name in GPU_TASKS - {new_task_name}
              if not is_task_cpu_only(name)}
    if llm_is_on_this_gpu():
        return others
    if new_task_name in LLM_TASKS:
        return others & LLM_TASKS
    return others - LLM_TASKS


def is_task_running(task_name, states=None):
    """Owned pending/started work stays admitted through callback cleanup."""
    owner = _task_claims.get(task_name)
    states = process_state if states is None else states
    return bool(states.get(task_name, {}).get("running")
                or (owner and owner["phase"] in ("pending", "started")))


def is_task_cpu_only(task_name):
    """Only an owned CPU claim exempts a running task from GPU conflicts."""
    return bool(_task_claims.get(task_name, {}).get("cpu_only"))


def check_global_gpu_lock(new_task_name: str, *, cpu_only=False):
    """Prevent multiple GPU-intensive tasks from running concurrently and causing an OOM crash.

    Raises HTTPException on conflict (every caller relies on this propagating
    straight out of the route handler) - unlike check_disk_space/
    check_text_loss's return-a-value convention.
    """
    if is_task_running(new_task_name):
        raise HTTPException(
            status_code=400,
            detail=f"{new_task_name.replace('_', ' ').capitalize()} is already running."
        )
    for task_name in gpu_lock_conflicts(new_task_name, cpu_only=cpu_only):
        if is_task_running(task_name):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start {new_task_name.replace('_', ' ')}: {task_name.replace('_', ' ')} is currently running. Please wait for it to finish or cancel it to free up GPU VRAM."
            )

_gpu_lock = threading.Lock()
_gpu_leases = {}
_task_claims = {}
_request_task_claims = contextvars.ContextVar("alexandria_request_task_claims", default=None)


def _reap_gpu_leases():
    while True:
        with _gpu_lock:
            for task_name, handle in list(_gpu_leases.items()):
                if not is_task_running(task_name):
                    release_gpu_lock(handle)
                    del _gpu_leases[task_name]
            if not _gpu_leases:
                return
        time.sleep(0.1)

def claim_gpu_task(task_name: str, *, llm_config=None, cpu_only=False):
    """Atomically re-check and reserve the GPU lock for task_name.

    check_global_gpu_lock() alone has a TOCTOU race: two requests for
    different GPU tasks can both pass the check before either's
    process_state[...]["running"] flag is set, and both start. Call this
    immediately before scheduling the background task (after all validation
    that could fail has already happened) to atomically perform the final
    check and mark the task as running.
    """
    with _gpu_lock:
        for old_task, handle in list(_gpu_leases.items()):
            if not is_task_running(old_task):
                release_gpu_lock(handle)
                del _gpu_leases[old_task]
        if cpu_only:
            check_global_gpu_lock(task_name, cpu_only=True)
        else:
            check_global_gpu_lock(task_name)
        needs_local_gpu = (not cpu_only and task_name not in NON_GPU_TASKS
                           and (task_name not in LLM_TASKS
                                or (llm_is_on_this_gpu() if llm_config is None
                                    else is_llm_on_this_gpu(llm_config))))
        task_lease = None
        handle = None
        try:
            conflicts = gpu_lock_conflicts(task_name, cpu_only=cpu_only)
            if cpu_only:
                task_lease = acquire_task_lease(DATA_DIR, task_name, conflicts, cpu_only=True)
            else:
                task_lease = acquire_task_lease(DATA_DIR, task_name, conflicts)
            if needs_local_gpu:
                handle = acquire_gpu_lock()
            if handle is not None:
                _gpu_leases[task_name] = handle
                if len(_gpu_leases) == 1:
                    threading.Thread(target=_reap_gpu_leases, daemon=True).start()
            if "cancel" in process_state[task_name]:
                process_state[task_name]["cancel"] = False
            process_state[task_name]["running"] = True
            claim_id = uuid.uuid4().hex
            _task_claims[task_name] = {"id": claim_id, "phase": "claimed", "lease": task_lease,
                                      "cpu_only": cpu_only}
            process_state[task_name]["cpu_only"] = cpu_only
            return claim_id
        except BaseException as error:
            if task_name in process_state:
                process_state[task_name]["running"] = False
            _task_claims.pop(task_name, None)
            _gpu_leases.pop(task_name, None)
            if handle is not None:
                release_gpu_lock(handle)
            if task_lease is not None:
                task_lease.close()
            if isinstance(error, (OSError, RuntimeError)):
                raise HTTPException(status_code=400, detail=str(error)) from error
            raise


def release_gpu_task_claim(task_name: str, claim_id=None, pending_only=False):
    """Release a claim whose worker could not be started."""
    with _gpu_lock:
        owner = _task_claims.get(task_name)
        if claim_id is not None and (owner is None or owner["id"] != claim_id):
            return False
        if pending_only and (owner is None or owner["phase"] != "pending"):
            return False
        process_state[task_name]["running"] = False
        _task_claims.pop(task_name, None)
        handle = _gpu_leases.pop(task_name, None)
        try:
            if handle is not None:
                release_gpu_lock(handle)
        finally:
            if owner is not None and owner.get("lease") is not None:
                owner["lease"].close()
        return True


class _RequestTaskClaims(list):
    """Shared request admission remains closed after ASGI cancellation."""
    closed = False


def reserve_background_task(task_name, *, cpu_only=False):
    """Reserve unstarted work and bind its token to the current request."""
    claim_id = claim_gpu_task(task_name, cpu_only=True) if cpu_only else claim_gpu_task(task_name)
    try:
        with _gpu_lock:
            claims = _request_task_claims.get()
            if claims is not None and getattr(claims, "closed", False):
                raise HTTPException(status_code=409, detail="The initiating request is no longer active")
            _task_claims[task_name]["phase"] = "pending"
            if claims is not None:
                claims.append((task_name, claim_id))
    except BaseException:
        release_gpu_task_claim(task_name, claim_id)
        raise
    return claim_id


def register_claimed_background_task(background_tasks, task_name, claim_id, callback, *args, **kwargs):
    """Adopt the same pending reservation; registration never double-claims."""
    try:
        with _gpu_lock:
            owner = _task_claims.get(task_name)
            if owner is None or owner["id"] != claim_id or owner["phase"] != "pending":
                raise HTTPException(status_code=409, detail="Task reservation is no longer active")
        background_tasks.add_task(_run_owned_background_task, task_name, claim_id,
                                  functools.partial(callback, *args, **kwargs))
    except BaseException:
        release_gpu_task_claim(task_name, claim_id, pending_only=True)
        raise
    return claim_id


def schedule_claimed_background_task(background_tasks, task_name, callback, *args, cpu_only=False, **kwargs):
    """Reserve and register one owned worker; failed registration rolls back."""
    claim_id = reserve_background_task(task_name, cpu_only=True) if cpu_only else reserve_background_task(task_name)
    return register_claimed_background_task(background_tasks, task_name, claim_id, callback, *args, **kwargs)


async def run_claimed_task_worker(task_name, callback, *args, **kwargs):
    """Keep a started thread's exact claim until its callback exits after cancellation."""
    claim_id = reserve_background_task(task_name)
    try:
        return await asyncio.to_thread(_run_owned_background_task, task_name, claim_id,
                                       functools.partial(callback, *args, **kwargs))
    finally:
        release_gpu_task_claim(task_name, claim_id, pending_only=True)


def _ensure_owned_task_started(task_name, claim_id, already_started=False):
    with _gpu_lock:
        owner = _task_claims.get(task_name)
        phase = "started" if already_started else "pending"
        if owner is None or owner["id"] != claim_id or owner["phase"] != phase:
            return False
        owner["phase"] = "started"
        return True


def _run_owned_background_task(task_name, claim_id, callback, already_started=False):
    if not _ensure_owned_task_started(task_name, claim_id, already_started):
        return
    try:
        return callback()
    finally:
        release_gpu_task_claim(task_name, claim_id)


def start_claimed_task_thread(task_name, claim_id, callback, *args, **kwargs):
    """Adopt a reservation before OS thread start; an alive worker keeps its claim."""
    worker = None
    try:
        worker = threading.Thread(target=_run_owned_background_task,
                                  args=(task_name, claim_id, functools.partial(callback, *args, **kwargs), True),
                                  daemon=True)
        if not _ensure_owned_task_started(task_name, claim_id):
            raise HTTPException(status_code=409, detail="Task reservation is no longer active")
        worker.start()
    except BaseException:
        if worker is None or not worker.is_alive():
            release_gpu_task_claim(task_name, claim_id)
        raise
    return worker


def release_pending_task_claims():
    """Shutdown releases reservations only; started workers retain admission."""
    with _gpu_lock:
        pending = [(name, owner["id"]) for name, owner in _task_claims.items()
                   if owner["phase"] == "pending"]
    for name, claim_id in pending:
        release_gpu_task_claim(name, claim_id, pending_only=True)


class TaskClaimMiddleware:
    """ASGI request lifetime includes response sending and background execution."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        claims = _RequestTaskClaims()
        token = _request_task_claims.set(claims)
        try:
            await self.app(scope, receive, send)
        finally:
            with _gpu_lock:
                claims.closed = True
                pending = list(claims)
            for name, claim_id in pending:
                release_gpu_task_claim(name, claim_id, pending_only=True)
            _request_task_claims.reset(token)

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
    state["artifacts"] = []
    state["current_task_idx"] = -1
    # Clear any stale process/pid left over from a previous run so an early
    # cancel (before the first subprocess is spawned) can't signal a dead or
    # recycled PID.
    if "process" in state:
        state["process"] = None
    if "pid" in state:
        state["pid"] = None
    state["processes"] = []


_PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')
# The three-pass pipeline prints its own estimate after every finished unit
# (three_pass_generate.RunProgress.eta_line): it knows the plan, the actual
# counts and the call rate, which this tail scan cannot. Its fixed tail is
# what is read; the text before it is shown as the progress string.
_ETA_LINE_RE = re.compile(r'^ETA: (.*?) \[eta_seconds=(\d+) fraction=([0-9.]+)\]\s*$')

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
    pipeline_eta = None
    tail = state.get("logs", [])[-30:]
    # The pipeline's own line wins over any N/M marker after it ("chunk 5/5
    # done" would otherwise read as 100% between two ETA lines).
    for line in reversed(tail):
        m = _ETA_LINE_RE.match(line.strip())
        if m:
            sub_progress = m.group(1)
            sub_fraction = min(1.0, max(0.0, float(m.group(3))))
            pipeline_eta = float(m.group(2))
            break
    for line in reversed(tail if pipeline_eta is None else []):
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
    if pipeline_eta is not None and not (tasks and idx is not None and idx >= 0):
        eta_seconds = pipeline_eta
    elif fraction and fraction > 0.001:  # Avoid enormous ETAs at start
        eta_seconds = elapsed * (1 - fraction) / fraction
    return {"elapsed_seconds": elapsed, "eta_seconds": eta_seconds, "progress": progress, "fraction": fraction}

def run_process(command: List[str], task_name: str, cwd: str = None):
    """Run a subprocess and stream its output into process_state logs."""
    state = process_state[task_name]

    # NOTE: do NOT bail out here if state["running"] is already True. GPU tasks
    # reserve their slot via claim_gpu_task() on the request thread (which sets
    # running=True before this background task is scheduled). Nicknames also
    # runs LLM work and reserves its slot at the endpoint. A guard here would
    # see claim_gpu_task's own reservation and
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
        env = os.environ.copy()
        if state.get("run_id"):
            env["ALEXANDRIA_RUN_ID"] = state["run_id"]
        return_code, _ = _stream_subprocess_to_logs(
            command, cwd or BASE_DIR, state, log_file=log_path, env=env)

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
                    if stats.get("batches_failed"):
                        completion_note = (
                            f"Task {task_name} completed, but {stats['batches_failed']} "
                            f"section(s) could not be reviewed. The original entries were "
                            f"preserved; see the report for exact ranges and retry guidance."
                        )
                    elif stats.get("batches_skipped_vram"):
                        completion_note = (
                            f"Task {task_name} completed, but {stats['batches_skipped_vram']} "
                            f"section(s) were skipped because the GPU ran low on memory. "
                            f"Re-run the review to finish the rest."
                        )
                    highlights = _extract_diff_highlights(state["logs"])
                    failures = _extract_failed_sections(state["logs"])
                    report_path = _write_single_review_report(stats, highlights, failures)
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
    return return_code







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

    state["cancel"] = True
    pid = state.get("pid")
    if not pid:
        # Pre-Popen race window: flag is checked by run_process immediately after Popen
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
    processes = list(state.get("processes") or ([state.get("process")] if state.get("process") else []))
    for proc in processes:
        _resume_if_paused(state, proc)

    # Also terminate the current subprocess (whole group) if it's running
    for proc in processes:
        if not proc or proc.poll() is not None:
            continue
        try:
            _send_signal_tree(proc, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    return {"status": "cancel_requested"}


CANCEL_TERMINATE_GRACE_SECONDS = SUBPROCESS_TERMINATE_GRACE_SECONDS


def apply_cancel_escalation(process, terminate_requested_at: Optional[float],
                            kill_sent: bool) -> Tuple[Optional[float], bool]:
    """Signal a cancelled process group, escalating from TERM to KILL after grace."""
    now = time.monotonic()
    if terminate_requested_at is None:
        try:
            _send_signal_tree(process, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        return now, kill_sent
    if not kill_sent and now - terminate_requested_at >= CANCEL_TERMINATE_GRACE_SECONDS:
        try:
            if sys.platform == "win32":
                terminate_windows_process_tree(process, force=True)
            else:
                _send_signal_tree(process, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        return terminate_requested_at, True
    return terminate_requested_at, kill_sent


def _run_claimed_background_task(task_name: str, callback) -> None:
    """Run a claimed callback and always release its process-state slot."""
    state = process_state[task_name]
    run_id = None
    error = None
    with _gpu_lock:
        owner = _task_claims.get(task_name)
        direct_claim = owner["id"] if owner is not None and owner["phase"] == "claimed" else None
    try:
        try:
            prune_runs(RUN_HISTORY_DIR)
            run_id = start_run(RUN_HISTORY_DIR, task_name)
            state["run_id"] = run_id
        except Exception:
            logger.exception("Could not start run history for %s", task_name)
        callback()
    except Exception as e:
        error = str(e)
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
        state.pop("run_id", None)
        if run_id:
            for artifact in state.get("artifacts", []):
                try:
                    record_artifact(
                        RUN_HISTORY_DIR, run_id, data_dir=DATA_DIR, **artifact)
                except Exception:
                    logger.exception("Could not record artifact for run %s", run_id)
            if error is not None or state.get("status") == "failed":
                final_status = "failed"
            elif state.get("cancel") or state.get("status") == "cancelled":
                final_status = "cancelled"
            else:
                final_status = "completed"
            try:
                finish_run(RUN_HISTORY_DIR, run_id, final_status, error=error)
            except Exception:
                logger.exception("Could not finish run history record %s", run_id)
        if direct_claim is not None:
            release_gpu_task_claim(task_name, direct_claim)



def ensure_failed_subprocess_stopped(process, state):
    """Stop owned work before exceptional stream cleanup releases its handles."""
    _resume_if_paused(state, process)
    stop_owned_subprocess(process, timeout=CANCEL_TERMINATE_GRACE_SECONDS,
                          force_after_grace=True)


def get_gpu_task_environment(state: dict, env: dict = None) -> dict:
    """Copy subprocess settings and describe this task's held kernel lease."""
    environment = dict(os.environ if env is None else env)
    with _gpu_lock:
        for task_name, task_state in process_state.items():
            handle = _gpu_leases.get(task_name)
            if task_state is state and handle is not None and is_task_running(task_name):
                environment.update(
                    GPU_LOCK=os.fspath(handle.name),
                    ALEXANDRIA_GPU_LOCK_HELD="1",
                    ALEXANDRIA_GPU_LOCK_PID=str(os.getpid()),
                    ALEXANDRIA_GPU_LOCK_FD=str(handle.fileno()),
                )
                break
    return environment


def get_task_lease_descriptor(state):
    """Read the current claim's borrowed descriptor for its outside command owner."""
    with _gpu_lock:
        for task_name, task_state in process_state.items():
            owner = _task_claims.get(task_name)
            if task_state is state and owner is not None and owner.get("lease") is not None:
                return owner["lease"].fileno()
    return None


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
    process = None
    reader = None
    try:
        subprocess_env = get_gpu_task_environment(state, env)
        process = start_owned_subprocess(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            cwd=cwd,
            env=subprocess_env,
            **get_subprocess_lease_options(subprocess_env, get_task_lease_descriptor(state)),
            termination_grace=CANCEL_TERMINATE_GRACE_SECONDS,
            # Linux uses a reaper; macOS a private task job; Windows a Job Object.
            start_new_session=True,
        )

        if sys.platform != "win32":
            process._alexandria_pgid = process.pid

        if "process" in state:
            state["process"] = process
        if "pid" in state:
            state["pid"] = process.pid
        if "processes" in state:
            state["processes"].append(process)

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
        terminate_requested_at = None
        kill_sent = False
        max_idle_cycles = 600  # Max consecutive Empty polls before assuming reader died (600 * 0.2s = 120s)
        idle_cycles = 0

        def _honor_cancel():
            # Graceful group termination first; force-kill the same group if a child
            # or grandchild ignores SIGTERM and keeps the output pipe open.
            nonlocal terminate_requested_at, kill_sent
            if state.get("cancel"):
                terminate_requested_at, kill_sent = apply_cancel_escalation(
                    process, terminate_requested_at, kill_sent)

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
            # Drain only lines already available, without delaying slow output.
            pending_lines = [line]
            while pending_lines[-1] is not None and len(pending_lines) < 128:
                try:
                    pending_lines.append(log_queue.get_nowait())
                except queue.Empty:
                    break
            log_entries = []
            for line in pending_lines:
                # Cancellation remains per line even under continuous output.
                _honor_cancel()
                if line is None:
                    break
                log_line = line.strip()
                if log_line:
                    entry = f"{log_prefix}{log_line}" if log_prefix else log_line
                    own_lines.append(entry)
                    log_entries.append(entry)
                    # The child froze itself (generate_script.pause_for_operator) after
                    # exhausting API retries. Mirror that here so the UI shows Paused
                    # and its Resume button - SIGCONT via _resume_task - wakes it.
                    if log_line.startswith(AUTO_PAUSE_MARKER):
                        state["paused"] = "resumed" not in log_line
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
                            log_entries.append(f"WARNING: Log file write failed ({e}). Subsequent logs will only appear in memory.")
                            try:
                                log_fh.close()
                            except OSError:
                                pass
                            log_fh = None  # Stop trying to write to disk

            if log_entries:
                # Preserve the shared list and publish the exact bounded tail once
                # per queued burst instead of shifting it for every emitted line.
                logs = state["logs"]
                entries = log_entries[-max_logs:] if max_logs > 0 else []
                overflow = max(0, len(logs) + len(entries) - max_logs)
                if overflow:
                    del logs[:overflow]
                logs.extend(entries)
                # Another control path may append while this burst is published.
                overflow = max(0, len(logs) - max_logs)
                if overflow:
                    del logs[:overflow]
            if pending_lines[-1] is None:
                break

        reader.join(timeout=1)
        if reader.is_alive():
            # The owner has exited, but a failed reader may still need cleanup.
            logger.warning("Subprocess reader did not finish; closing inherited stdout pipe")
        try:
            process.stdout.close()
        except OSError:
            pass
        reader.join(timeout=1)
        while True:
            _honor_cancel()
            try:
                process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        return process.returncode, own_lines

    except BaseException:
        if process is not None:
            try:
                ensure_failed_subprocess_stopped(process, state)
            except BaseException:
                logger.exception("Failed to stop subprocess after stream failure")
        raise
    finally:
        if process is not None:
            if "processes" in state and process in state["processes"]:
                state["processes"].remove(process)
            if state.get("process") is process:
                state["process"] = None
            if state.get("pid") == process.pid:
                state["pid"] = None
            if reader is not None and reader.is_alive():
                reader.join(timeout=1)
            if process.stdout is not None and (reader is None or not reader.is_alive()):
                process.stdout.close()
        if process is not None:
            control = getattr(process, "_alexandria_control", None)
            if control is not None:
                control.close()
        if log_fh is not None:
            log_fh.close()


_REVIEW_ENTRIES_RE = re.compile(r'Review complete:\s*(\d+)\s*->\s*(\d+)\s*entries')
_REVIEW_SUMMARY_PATTERNS = {
    "text_changed": re.compile(r'Text changed:\s*(\d+)'),
    "speaker_changed": re.compile(r'Speaker changed:\s*(\d+)'),
    "instruct_changed": re.compile(r'Instruct changed:\s*(\d+)'),
    "entries_changed": re.compile(r'Entries changed:\s*(\d+)'),
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
_FAILED_SECTIONS_RE = re.compile(r'FAILED_SECTIONS_JSON:\s*(\{.*\})\s*$')


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


def _extract_failed_sections(lines: List[str]) -> dict:
    """Parse structured failed-batch details emitted by review_script.py."""
    for line in reversed(lines):
        match = _FAILED_SECTIONS_RE.search(line)
        if match:
            try:
                data = json.loads(match.group(1))
                sections = data.get("sections")
                if not isinstance(sections, list):
                    break
                return {
                    "sections": sections,
                    "original_entries_preserved": data.get("original_entries_preserved") is True,
                    "checkpoint_retained": data.get("checkpoint_retained") is True,
                    "retry_from_batch": data.get("retry_from_batch"),
                }
            except (json.JSONDecodeError, AttributeError) as e:
                logger.warning(f"Malformed FAILED_SECTIONS_JSON line: {e}")
                break
    return {"sections": []}


def _markdown_failed_sections_lines(failures: Optional[dict]) -> List[str]:
    """Format failed review ranges and their safe retry disposition."""
    if not failures or not failures.get("sections"):
        return []
    labels = {
        "review_failed": "the review request or response failed",
        "text_length_mismatch": "text-loss protection rejected the response",
    }
    lines = []
    for section in failures["sections"]:
        reason = labels.get(section.get("category"), "the section could not be reviewed")
        if section.get("category") == "text_length_mismatch" and section.get("word_ratio") is not None:
            reason += f" (word-count ratio {section['word_ratio']:.2f})"
        lines.append(
            f"- Batch {section.get('batch', '?')}, entries "
            f"{section.get('entry_start', '?')}–{section.get('entry_end', '?')}: {reason}."
        )
    if failures.get("original_entries_preserved"):
        lines.append("- The original entries for these sections were preserved unchanged.")
    if failures.get("checkpoint_retained") and failures.get("retry_from_batch") is not None:
        lines.append(
            f"- A checkpoint was retained at batch {failures['retry_from_batch']}. "
            "Running the single-book review again with the same settings will retry from there."
        )
    return lines


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
    ("entries_changed", "Existing lines with one or more edits"),
    ("text_changed", "Lines with reworded text"),
    ("speaker_changed", "Lines with a changed speaker"),
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


_HIGH_CHANGE_DENSITY_MIN_ENTRIES = 100
_HIGH_CHANGE_DENSITY_THRESHOLD = 0.20


def _markdown_change_density_lines(stats: dict) -> List[str]:
    """Warn about broad review passes without treating change volume as failure."""
    entries_before = stats.get("entries_before", 0)
    entries_changed = stats.get("entries_changed", 0)
    if entries_before < _HIGH_CHANGE_DENSITY_MIN_ENTRIES:
        return []
    density = entries_changed / entries_before
    if density < _HIGH_CHANGE_DENSITY_THRESHOLD:
        return []
    return [
        f"- **High-change review:** {entries_changed} of {entries_before} existing lines "
        f"({density:.1%}) had text, speaker, or direction edits. Structural changes were "
        f"+{stats.get('entries_added', 0)}/-{stats.get('entries_removed', 0)} lines. "
        "Inspect the detailed counts and highlights before generating audio."
    ]


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
        lines += [f"{heading} Speaker changes to verify", ""]
        for c in changes:
            book = f" (in *{c['book']}*)" if c.get("book") else ""
            entry = f"Entry {c['entry_number']}: " if c.get("entry_number") is not None else ""
            lines.append(f"- {entry}“{_clean(c.get('text', ''))}” — changed from "
                          f"**{c.get('before') or '?'}** to **{c.get('after') or '?'}**{book}")
            if c.get("context_before"):
                lines.append(f"  - Previous: “{_clean(c['context_before'])}”")
            if c.get("context_after"):
                lines.append(f"  - Next: “{_clean(c['context_after'])}”")
            if c.get("manual_review_reason"):
                lines.append(f"  - **Manual check recommended:** {c['manual_review_reason']}")
    return lines


def _markdown_book_pass_lines(stats: dict, diffs: Optional[dict], failures: Optional[dict] = None,
                              heading: str = "####") -> List[str]:
    """Stats table + heads-up notes + top diff highlights for one book (or one
    pass of a book, in a bidirectional run)."""
    lines = _markdown_stats_table(stats)
    hu = _markdown_heads_up_lines(stats)
    if hu:
        lines += [""] + hu
    density_lines = _markdown_change_density_lines(stats)
    if density_lines:
        lines += [""] + density_lines
    failure_lines = _markdown_failed_sections_lines(failures)
    if failure_lines:
        lines += [""] + failure_lines
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
    "what kinds of changes were reported, and whether anything needs their attention. "
    "Use only facts explicitly present in the report. Describe edits as reported changes, "
    "not proven fixes or improvements. Never judge the script's quality, praise the reviewer, "
    "or claim that every issue was found. Mention any new character names that were discovered, "
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

# HOW LONG MAY ONE LLM REQUEST HANG? One answer, imported by every caller.
#
# generate_script built its client with no timeout at all while this module
# defaulted to 60s. On 2026-08-18 that cost two hours: a request never
# returned, nothing bounded it, and the job slept holding the GPU while the
# queue waited behind it. Ten other constructions in this app had the same
# hole.
#
# 600s, not core's old 60s, because a generation batch on a 14B model
# legitimately takes minutes and a timeout shorter than the work turns slow
# into failed. The number matters less than its existence: a finite timeout
# converts a dead request into an error a retry loop can act on.
# Printed by a generation subprocess that has frozen itself after exhausting
# API retries (generate_script.pause_for_operator); the output reader below
# turns it into the task's `paused` flag. Lives here, not in generate_script,
# because generate_script imports core and not the other way round.
AUTO_PAUSE_MARKER = "[AUTO-PAUSE]"


def llm_timeout_seconds():
    """-> seconds any single LLM request may take before it is an error."""
    try:
        timeout = float(os.environ.get("ALEXANDRIA_LLM_TIMEOUT", 600))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        return timeout
    except (TypeError, ValueError):
        logger.warning("Invalid ALEXANDRIA_LLM_TIMEOUT; using the 600-second deadline")
        return 600.0


def _make_llm_client(timeout: float = None):
    """Build an OpenAI-compatible client + model name from config.json's `llm` section.

    Validates that the base_url is local/trusted.
    Reuses cached client if config hasn't changed to avoid connection pool leaks.
    """
    from llm_provider import make_llm_client
    # None means "the shared answer", so this function stops being a second
    # opinion on how long a request may hang. An explicit argument still wins:
    # a caller that knows its request is quick may want a tighter bound.
    timeout = llm_timeout_seconds() if timeout is None else timeout
    llm_cfg = _load_llm_config()

    base_url = llm_cfg.get("base_url", "http://localhost:11434/v1")
    _validate_local_llm_base_url(base_url)

    # Create a hash of the config to detect changes
    config_key = json.dumps({"llm": llm_cfg, "timeout": timeout}, sort_keys=True)

    # Reuse cached client if config hasn't changed
    cached_client = _llm_client_cache.get(config_key)
    if cached_client is not None:
        return cached_client, llm_cfg.get("model_name", "")

    client = make_llm_client(llm_cfg, timeout)
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
        llm_cfg = get_active_llm_config(full_cfg)
        status = get_current_status(
            full_cfg.get("llm_mode", "local"), llm_cfg.get("base_url", ""),
            model_name, (full_cfg.get("llm_remote_ssh") or "").strip(), use_cache=True,
            api_key=llm_cfg.get("api_key"))
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


_UNSUPPORTED_SUMMARY_CLAIMS = (
    "everything looks great", "fantastic job", "perfect", "flawless",
    "all issues", "all mistakes", "successfully fixed", "fully polished",
)


def _get_deterministic_review_summary(stats: dict, incomplete: bool) -> str:
    """Describe verified review status without making a quality judgment."""
    total_changes = stats.get("total_changes", 0)
    if incomplete:
        failed = stats.get("batches_failed", 0)
        skipped = stats.get("batches_skipped_vram", 0)
        details = []
        if failed:
            details.append(f"{failed} section(s) failed")
        if skipped:
            details.append(f"{skipped} section(s) were skipped for VRAM safety")
        reason = "; ".join(details) or "some work did not finish"
        return (
            f"This review was incomplete: {reason}. Completed sections reported "
            f"{total_changes} change(s). Check the warnings below before generating audio."
        )
    return (
        "The automated review completed without recorded failed or skipped sections. "
        f"It reported {total_changes} change(s); the detailed counts and examples below "
        "show what changed."
    )


def _is_evidence_bound_summary(summary: str) -> bool:
    """Reject common unsupported quality and completeness claims."""
    lowered = summary.casefold()
    return not any(claim in lowered for claim in _UNSUPPORTED_SUMMARY_CLAIMS)


def _insert_llm_summary(lines: List[str], intro_len: int, stats: Optional[dict] = None,
                        incomplete: bool = False, *, allow_llm: bool = False) -> List[str]:
    """Insert an evidence-bound plain-language summary after the report intro."""
    stats = stats or {}
    summary = None
    if allow_llm and not incomplete:
        candidate = _llm_summarize_report("\n".join(lines))
        if candidate and _is_evidence_bound_summary(candidate):
            summary = candidate
        elif candidate:
            logger.warning("LLM report summary made an unsupported quality claim; using deterministic summary")
    if not summary:
        summary = _get_deterministic_review_summary(stats, incomplete)
    return lines[:intro_len] + ["", "## In Plain English", "", summary] + lines[intro_len:]


def _write_single_review_report(stats: dict, highlights: Optional[dict] = None,
                                failures: Optional[dict] = None) -> Optional[str]:
    """Write a plain-language Markdown summary of a single (non-batch) review run.

    Returns the path to the written file, or None if it couldn't be written.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(REPORTS_DIR, f"review_{timestamp}_{uuid.uuid4().hex}.md")

    intro = [
        "# Script Review Report",
        "",
        f"*Generated {time.strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
        "The AI reviewer checked your script for possible mistakes — like the wrong character "
        "speaking a line, awkward wording, or repeated narration — and recorded its changes.",
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
        lines += ["", "No changes were reported during this pass."]
    else:
        lines += ["", "## What changed", ""]
        lines += _markdown_stats_table(stats)

        if highlights:
            hl_lines = _markdown_diff_highlights_lines(highlights)
            if hl_lines:
                lines += ["", "## Highlights", ""]
                lines += hl_lines

    heads_up = _markdown_heads_up_lines(stats)
    density_lines = _markdown_change_density_lines(stats)
    if heads_up or density_lines:
        lines += ["", "## Things to check", ""]
        lines += heads_up
        lines += _markdown_failed_sections_lines(failures)
        lines += density_lines

    incomplete = bool(stats.get("batches_failed") or stats.get("batches_skipped_vram"))
    lines = _insert_llm_summary(lines, len(intro), stats, incomplete=incomplete)

    try:
        save_review_report(path, "\n".join(lines) + "\n",
                           _get_deterministic_review_summary(stats, incomplete), incomplete)
    except OSError:
        return None
    return path


def terminate_windows_process_tree(proc_or_pid, force=False):
    """Dispatch Windows termination through the shared owner backend."""
    return terminate_owned_windows_process_tree(
        proc_or_pid, force=force, timeout=CANCEL_TERMINATE_GRACE_SECONDS)


def _send_signal_tree(proc_or_pid, sig) -> None:
    """Dispatch through the shared subprocess owner, retaining the task grace."""
    return send_subprocess_signal(
        proc_or_pid, sig, termination_grace=CANCEL_TERMINATE_GRACE_SECONDS)


def pause_resume_supported():
    """Pause is SIGSTOP/SIGCONT on the worker's process group, which Windows
    does not have. The one answer for the 501 below and for GET /api/config's
    `capabilities.pause_resume`, which the page reads to disable the Pause
    buttons instead of letting every click fail (issue #588)."""
    return sys.platform != "win32"


def _posix_signal(proc, signame):
    """Send a POSIX signal by name (e.g. "SIGSTOP") to the process's whole group.
    Raises 501 on Windows, where SIGSTOP/SIGCONT don't exist on the signal module
    — the name is resolved here, after the platform check, so callers never
    reference the constant directly and crash with AttributeError on Windows."""
    if not pause_resume_supported():
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
    processes = list(state.get("processes") or ([state.get("process")] if state.get("process") else []))
    if not processes:
        raise HTTPException(status_code=503, detail=starting_up_msg)
    for proc in processes:
        _posix_signal(proc, "SIGSTOP")
    state["paused"] = True
    state["logs"].append(f"[PAUSED] {log_label} paused.")
    return {"status": "paused"}


def _resume_task(state_key: str, not_running_msg: str, log_label: str):
    """Resume a paused subprocess task by sending SIGCONT."""
    state = process_state[state_key]
    if not state["running"]:
        raise HTTPException(status_code=400, detail=not_running_msg)
    processes = list(state.get("processes") or ([state.get("process")] if state.get("process") else []))
    if not processes:
        raise HTTPException(status_code=400, detail="Process not available.")
    for proc in processes:
        _posix_signal(proc, "SIGCONT")
    state["paused"] = False
    state["logs"].append(f"[RESUMED] {log_label} resumed.")
    return {"status": "resumed"}
