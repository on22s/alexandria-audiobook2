import os
import sys
import gc
import asyncio
import json
import shutil
import signal
import logging
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Tuple, Literal
import re
import time
import queue
import difflib
import threading
import zipfile
import subprocess
import traceback
import aiofiles
from datetime import datetime
from utils import atomic_json_write, file_lock, safe_load_json, secure_filename, run_rocm_smi_json
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from math import ceil

# Import ProjectManager
from project import ProjectManager
from tts import voice_category
from default_prompts import load_default_prompts
from review_prompts import load_review_prompts
from persona_prompts import load_persona_prompts
from hf_utils import fetch_builtin_manifest, download_builtin_adapter, is_adapter_downloaded
from lmstudio_settings import (get_lmstudio_status, apply_lmstudio_settings, is_remote_llm,
                               apply_remote_lmstudio_settings, is_local_llm_endpoint,
                               get_current_status, persist_healed_base_url, decide_healed_urls)
from review_script import clear_checkpoint, _checkpoint_path

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AlexandriaUI")

app = FastAPI(title="Alexandria Audiobook")

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
VOICE_CONFIG_PATH = os.path.join(ROOT_DIR, "voice_config.json")
SCRIPT_PATH = os.path.join(ROOT_DIR, "annotated_script.json")
AUDIOBOOK_PATH = os.path.join(ROOT_DIR, "cloned_audiobook.mp3")
M4B_PATH = os.path.join(ROOT_DIR, "audiobook.m4b")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
BATCH_SCRIPT_STATE_PATH = os.path.join(SCRIPTS_DIR, ".batch_script_state.json")
BATCH_REVIEW_STATE_PATH = os.path.join(SCRIPTS_DIR, ".batch_review_state.json")
CHUNKS_PATH = os.path.join(ROOT_DIR, "chunks.json")
VOICE_LIBRARY_PATH = os.path.join(ROOT_DIR, "voice_library.json")
CHARACTER_ALIASES_PATH = os.path.join(ROOT_DIR, "character_aliases.json")
REPORTS_DIR = os.path.join(ROOT_DIR, "reports")
API_LOG_DIR = os.path.join(ROOT_DIR, "logs", "api")
os.makedirs(API_LOG_DIR, exist_ok=True)


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
DESIGNED_VOICES_DIR = os.path.join(ROOT_DIR, "designed_voices")
CLONE_VOICES_DIR = os.path.join(ROOT_DIR, "clone_voices")
LORA_MODELS_DIR = os.path.join(ROOT_DIR, "lora_models")
LORA_DATASETS_DIR = os.path.join(ROOT_DIR, "lora_datasets")
BUILTIN_LORA_DIR = os.path.join(ROOT_DIR, "builtin_lora")
DATASET_BUILDER_DIR = os.path.join(ROOT_DIR, "dataset_builder")
PREPARER_SCRIPT_PATH = os.path.join(ROOT_DIR, "alexandria_preparer_rocm_compatible.py")
PREPARER_OUTPUT_DIR = os.path.join(ROOT_DIR, "preparer_output")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(SCRIPTS_DIR, exist_ok=True)
os.makedirs(DESIGNED_VOICES_DIR, exist_ok=True)
os.makedirs(CLONE_VOICES_DIR, exist_ok=True)
os.makedirs(LORA_MODELS_DIR, exist_ok=True)
os.makedirs(LORA_DATASETS_DIR, exist_ok=True)
os.makedirs(DATASET_BUILDER_DIR, exist_ok=True)
os.makedirs(PREPARER_OUTPUT_DIR, exist_ok=True)

# Mount static files with absolute path
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Create voicelines directory if it doesn't exist to prevent startup error
VOICELINES_DIR = os.path.join(ROOT_DIR, "voicelines")
os.makedirs(VOICELINES_DIR, exist_ok=True)
app.mount("/voicelines", StaticFiles(directory=VOICELINES_DIR), name="voicelines")

# Designed voices directory for voice designer feature
app.mount("/designed_voices", StaticFiles(directory=DESIGNED_VOICES_DIR), name="designed_voices")

# Clone voices directory for user-uploaded reference audio
app.mount("/clone_voices", StaticFiles(directory=CLONE_VOICES_DIR), name="clone_voices")

# LoRA models directory for trained adapter test audio
app.mount("/lora_models", StaticFiles(directory=LORA_MODELS_DIR), name="lora_models")

# Built-in LoRA adapters directory
os.makedirs(BUILTIN_LORA_DIR, exist_ok=True)
app.mount("/builtin_lora", StaticFiles(directory=BUILTIN_LORA_DIR), name="builtin_lora")

# Dataset builder directory for preview audio
app.mount("/dataset_builder", StaticFiles(directory=DATASET_BUILDER_DIR), name="dataset_builder")

# Initialize Project Manager
project_manager = ProjectManager(ROOT_DIR)

# Reset any chunks stuck in "generating" from a prior interrupted session
_startup_chunks = project_manager.load_chunks()
if _startup_chunks:
    _reset_count = 0
    for chunk in _startup_chunks:
        if chunk.get("status") == "generating":
            chunk["status"] = "pending"
            _reset_count += 1
    if _reset_count:
        project_manager.save_chunks(_startup_chunks)
        print(f"Startup: reset {_reset_count} stuck 'generating' chunk(s) to 'pending'")
    del _startup_chunks, _reset_count

# CORS — allow configurable origins via env var, defaulting to localhost for security
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://127.0.0.1:4200,http://localhost:4200").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- System Helpers ---

def _get_torch():
    """Lazily import torch, returning None if it isn't installed."""
    try:
        import torch
        return torch
    except ImportError:
        return None


# GPU stats cache to avoid repeated torch imports and subprocess calls
_gpu_stats_cache = {"data": None, "timestamp": 0}
_GPU_STATS_CACHE_TTL = 5  # seconds

def _gpu_stats_via_rocm_smi():
    """VRAM/util from rocm-smi when torch can't see the GPU (e.g. a CUDA-only
    torch build on an AMD box). Returns the same dict shape as get_gpu_stats(),
    or None on any rocm-smi failure (missing binary, timeout, bad/empty JSON)."""
    data = run_rocm_smi_json(["--showmeminfo", "vram", "--showuse"],
                             rocm_smi_path="/opt/rocm/bin/rocm-smi", timeout=2)
    if not data:
        return None
    for card_data in data.values():
        if not isinstance(card_data, dict):
            continue
        total_b = card_data.get("VRAM Total Memory (B)")
        used_b = card_data.get("VRAM Total Used Memory (B)")
        if total_b is None:
            continue
        try:
            total = int(total_b) / 1e9
            used = int(used_b) / 1e9 if used_b is not None else 0.0
        except (ValueError, TypeError):
            continue
        if total <= 0:
            continue
        stats = {"allocated_gb": used, "reserved_gb": used, "total_gb": total,
                 "allocated_percent": used / total * 100,
                 "utilization_percent": None}
        for key in ("GPU use (%)", "GPU Use (%)", "GPU Activity"):
            v = card_data.get(key)
            if v not in (None, "N/A"):
                try:
                    stats["utilization_percent"] = float(v)
                    break
                except (ValueError, TypeError):
                    pass
        return stats
    return None

def get_gpu_stats():
    """Get current GPU memory and utilization stats with caching."""
    now = time.time()

    # Return cached stats if still fresh
    if (now - _gpu_stats_cache["timestamp"]) < _GPU_STATS_CACHE_TTL:
        return _gpu_stats_cache["data"]

    torch = _get_torch()
    if torch is None or not torch.cuda.is_available():
        # torch can't see the GPU (e.g. a CUDA-only build on an AMD box) —
        # fall back to rocm-smi so AMD GPUs are still detected.
        stats = _gpu_stats_via_rocm_smi()
        if stats is None:
            logger.debug("GPU stats: No GPU available (torch + rocm-smi both failed)")
        _gpu_stats_cache["data"] = stats
        _gpu_stats_cache["timestamp"] = now
        return stats

    stats = {}
    try:
        # Memory stats (works for both NVIDIA and AMD ROCm)
        allocated = torch.cuda.memory_allocated() / 1e9  # GB
        reserved = torch.cuda.memory_reserved() / 1e9    # GB
        total = torch.cuda.get_device_properties(0).total_memory / 1e9  # GB

        if total <= 0:
            logger.warning("GPU stats: Total memory reported as 0, skipping utilization check")
            _gpu_stats_cache["data"] = None
            _gpu_stats_cache["timestamp"] = now
            return None

        stats['allocated_gb'] = allocated
        stats['reserved_gb'] = reserved
        stats['total_gb'] = total
        stats['allocated_percent'] = allocated / total * 100

        # Try to get utilization via rocm-smi for AMD GPUs
        data = run_rocm_smi_json(["--showuse"], rocm_smi_path="/opt/rocm/bin/rocm-smi", timeout=2)
        if data is not None:
            for card_key, card_data in data.items():
                if not isinstance(card_data, dict):
                    continue
                for key in ('GPU use (%)', 'GPU Use (%)', 'GPU Activity'):
                    gpu_use_str = card_data.get(key)
                    if gpu_use_str is not None and gpu_use_str != 'N/A':
                        try:
                            stats['utilization_percent'] = float(gpu_use_str)
                            break
                        except (ValueError, TypeError):
                            continue
                break
        else:
            logger.debug("GPU stats: Failed to get utilization via rocm-smi")
            stats['utilization_percent'] = None

    except Exception as e:
        logger.debug(f"Could not get GPU stats: {e}")
        _gpu_stats_cache["data"] = None
        _gpu_stats_cache["timestamp"] = now
        return None

    # Cache the result
    _gpu_stats_cache["data"] = stats
    _gpu_stats_cache["timestamp"] = now
    return stats

def check_disk_space(path, required_gb):
    """Check if disk has enough space. Returns (has_space, free_gb)."""
    try:
        stat = shutil.disk_usage(path)
        free_gb = stat.free / (1024 ** 3)
        return free_gb >= required_gb, free_gb
    except (OSError, ValueError) as e:
        logger.warning(f"Could not check disk space for {path}: {e}")
        return True, 0.0

# Data Models
class LLMConfig(BaseModel):
    base_url: str
    api_key: str
    model_name: str

class TTSConfig(BaseModel):
    mode: str = "local"  # "local" or "external"
    url: str = "http://127.0.0.1:7860"  # external mode only
    device: str = "auto"  # local mode: "auto", "cuda:0", "cpu", etc.
    language: str = "English"  # TTS language
    parallel_workers: int = 2  # concurrent TTS workers
    batch_seed: Optional[int] = None  # Single seed for batch mode, None/-1 = random
    compile_codec: bool = False  # torch.compile the codec for ~3-4x batch throughput (slow first run)
    sub_batch_enabled: bool = True  # split batch by text length to reduce padding waste
    sub_batch_min_size: int = 4  # minimum chunks per sub-batch before allowing a split
    sub_batch_ratio: float = 5.0  # max longest/shortest length ratio before splitting
    sub_batch_max_items: int = 0  # hard cap on sequences per sub-batch (0 = auto from VRAM estimate)
    batch_group_by_type: bool = False  # group chunks by voice type for efficient batching
    pause_between_speakers_ms: int = 500  # silence (ms) between different speakers during merge
    pause_same_speaker_ms: int = 250  # silence (ms) when same speaker continues during merge

class GenerationConfig(BaseModel):
    chunk_size: int = 3000
    max_tokens: int = 4096
    temperature: float = 0.6
    top_p: float = 0.8
    top_k: int = 0
    min_p: float = 0
    presence_penalty: float = 0.0
    banned_tokens: List[str] = []
    merge_narrators: bool = False

class PromptConfig(BaseModel):
    system_prompt: Optional[str] = None
    user_prompt: Optional[str] = None
    review_system_prompt: Optional[str] = None
    review_user_prompt: Optional[str] = None
    persona_system_prompt: Optional[str] = None
    persona_user_prompt: Optional[str] = None
    persona_advanced_prompt: Optional[str] = None

class AppConfig(BaseModel):
    llm: LLMConfig  # active profile - mirrored from llm_local/llm_remote per llm_mode
    llm_mode: Literal["local", "remote"] = "local"  # remote = e.g. LM Studio on Thunder
    llm_local: Optional[LLMConfig] = None   # saved local profile
    llm_remote: Optional[LLMConfig] = None  # saved remote profile
    llm_remote_ssh: Optional[str] = None    # ssh host alias (e.g. "tnr-0") for remote optimize
    tts: TTSConfig
    prompts: Optional[PromptConfig] = None
    generation: Optional[GenerationConfig] = None

class VoiceConfigItem(BaseModel):
    type: str = "custom"
    voice: Optional[str] = "Ryan"
    character_style: Optional[str] = ""
    default_style: Optional[str] = ""  # backward compat, prefer character_style
    seed: Optional[str] = "-1"
    ref_audio: Optional[str] = None
    ref_text: Optional[str] = None
    adapter_id: Optional[str] = None
    adapter_path: Optional[str] = None
    description: Optional[str] = ""  # voice description (for design type)

class SuggestVoicesRequest(BaseModel):
    only_unset: bool = False  # only suggest for characters not already set to a lora/builtin_lora voice
    max_lines: int = 8        # how many sample dialogue lines per character to feed the matcher

class CastCreateRequest(BaseModel):
    name: str

class LibrarySaveRequest(BaseModel):
    cast: str
    characters: List[str]                     # current-book character names to save into the cast
    shared: Optional[List[str]] = None        # subset to force into the shared (cross-series) pool
    cast_specific: Optional[List[str]] = None # subset to force into the cast even if normally shared (e.g. a different narrator)

class LibraryApplyRequest(BaseModel):
    cast: str
    mapping: Dict[str, str]                   # current character name -> library member key to apply

class CastMatchBulkRequest(BaseModel):
    name: str                                 # cast name
    script_names: List[str]                   # saved scripts to union-match against the cast

class LibraryApplyBulkRequest(BaseModel):
    cast: str
    mapping: Dict[str, str]                   # character name -> library member key
    script_names: List[str]                   # saved scripts to apply the mapping to

class ChunkUpdate(BaseModel):
    text: Optional[str] = None
    instruct: Optional[str] = None
    speaker: Optional[str] = None
    pause_after: Optional[int] = None

class BatchGenerateRequest(BaseModel):
    indices: List[int]

class VoiceDesignPreviewRequest(BaseModel):
    description: str
    sample_text: str
    language: Optional[str] = None

class VoiceDesignSaveRequest(BaseModel):
    name: str
    description: str
    sample_text: str
    preview_file: str

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

class LoraTrainingRequest(BaseModel):
    name: str
    dataset_id: str
    epochs: int = 5
    lr: float = 5e-6
    batch_size: int = 1
    lora_r: int = 32
    lora_alpha: int = 128
    gradient_accumulation_steps: int = 8
    language: str = "english"

class LoraTestRequest(BaseModel):
    adapter_id: str
    text: str
    instruct: str = ""

class LoraDatasetSample(BaseModel):
    emotion: str = ""
    text: str

class LoraGenerateDatasetRequest(BaseModel):
    name: str
    description: str  # root voice description
    samples: Optional[List[LoraDatasetSample]] = None  # emotion+text pairs
    texts: Optional[List[str]] = None  # legacy: flat text list (no emotions)
    language: Optional[str] = None

class DatasetSampleGenRequest(BaseModel):
    description: str      # full voice description (root + emotion already combined by frontend)
    text: str
    dataset_name: str     # working directory name
    sample_index: int     # row number
    seed: int = -1        # -1 = random, >= 0 = manual seed

class DatasetBatchGenRequest(BaseModel):
    name: str
    description: str      # root voice description
    samples: List[LoraDatasetSample]
    indices: Optional[List[int]] = None  # which rows to generate (None = all)
    global_seed: int = -1 # -1 = random, >= 0 = same seed for all lines
    seeds: Optional[List[int]] = None  # per-line seeds (overrides global_seed)

class DatasetSaveRequest(BaseModel):
    name: str
    ref_index: int = 0    # which sample to use as ref.wav

class DatasetBuilderCreateRequest(BaseModel):
    name: str

class DatasetBuilderUpdateMetaRequest(BaseModel):
    name: str
    description: str = ""
    global_seed: str = ""

class DatasetBuilderUpdateRowsRequest(BaseModel):
    name: str
    rows: List[dict]  # [{emotion, text, seed}]

class GenerateScriptRequest(BaseModel):
    resume: bool = False

class ReviewRequest(BaseModel):
    dedupe_speakers: bool = True
    resume: bool = False

class ContextualReviewRequest(BaseModel):
    window_size: int = 4
    dedupe_speakers: bool = True
    resume: bool = False

class BatchReviewRequest(BaseModel):
    script_names: List[str]            # names from the Scripts library (without .json)
    context_window: int = 0            # >0 enables contextual review
    dedupe_speakers: bool = True       # merge same-character aliases, consistent across the batch
    find_nicknames: bool = True        # run nickname discovery per book first, into the shared series alias file
    bidirectional: bool = False        # after the forward pass, re-scan in reverse so early books get
                                       # discovery seeded with full-series hindsight (requires find_nicknames)
    resume: bool = False               # continue an interrupted batch from its saved pass/order plan

class GeneratePersonasRequest(BaseModel):
    advanced: bool = False
    batch_size: int = 40

class BatchPreparerTask(BaseModel):
    audio_filename: str
    output_filename: str

class BatchPreparerRequest(BaseModel):
    tasks: List[BatchPreparerTask]
    lang: str = "en"
    min_confidence: float = 0.85
    min_snr: int = 25

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
    "lora_training": {"running": False, "logs": []},
    "dataset_gen": {"running": False, "logs": []},
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
    """Prevent multiple GPU-intensive tasks from running concurrently and causing an OOM crash."""
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
        process_state[task_name]["running"] = True

def _init_batch_state(state: dict, logs: list, tasks: list) -> None:
    """Reset a process_state[...] entry for the start of a new batch run.

    Common initialization shared by review_script_batch_start,
    generate_script_batch_start, and voicelab_start's background _run().
    """
    state["running"] = True
    state["cancel"] = False
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
                # Show which book in the backward pass (reverse order)
                bwd_book_num = num_items - idx
                progress = f"item {bwd_book_num}/{num_items} (pass 2/2)"
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
    if "cancel"      in state: state["cancel"]      = False
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
            proc.send_signal(signal.SIGCONT)
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
        if proc is not None:
            proc.terminate()
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        raise HTTPException(status_code=400, detail=exited_msg)
    return {"status": "cancel signal sent", "pid": pid}



def _stream_subprocess_to_logs(command: List[str], cwd: str, state: dict, log_prefix: str = "", max_logs: int = 20000, log_file: str = None) -> Tuple[int, List[str]]:
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
        env=os.environ.copy(),
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
            if state.get("cancel") and not terminate_requested:
                terminate_requested = True
                try:
                    process.terminate()
                except (ProcessLookupError, OSError):
                    # Process already exited on its own - nothing to terminate.
                    # Let the reader thread drain the rest of the output; the
                    # real exit code below reflects how it actually finished.
                    pass
            continue
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
    combined totals rather than only whichever pass ran last."""
    combined = {key: 0 for key in _REVIEW_SUMMARY_PATTERNS}
    for stats in stat_dicts:
        if not stats:
            continue
        for key in combined:
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
            except (json.JSONDecodeError, AttributeError):
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
    return safe_load_json(CONFIG_PATH, default={}).get("llm", {})


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
    # Thunder Compute forwards instance ports via *.thundercompute.net, so allow
    # that trusted remote host for running LM Studio on a Thunder GPU instance.
    if _is_thunder_host(base_url):
        return
    raise LLMConfigError(f"LLM base_url '{base_url}' is not local. Only local/trusted LLM endpoints are permitted.")


def _is_thunder_host(base_url: str) -> bool:
    """True if base_url points at a Thunder Compute port-forward host
    (*.thundercompute.net). Single source for the Thunder-host check shared by
    base_url validation and the test-connection "server not serving" hint."""
    from urllib.parse import urlparse
    hostname = (urlparse(base_url).hostname or "").lower()
    return hostname == "thundercompute.net" or hostname.endswith(".thundercompute.net")


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

        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": _REPORT_SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": markdown_body},
            ],
            temperature=0.4,
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


def _write_batch_review_report(state: dict, names: List[str], bidirectional: bool, discover: bool) -> Optional[str]:
    """Write one plain-language Markdown summary covering an entire batch review run
    (whether it was 1 book or many).

    Returns the path to the written file, or None if it couldn't be written.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(REPORTS_DIR, f"batch_review_{timestamp}.md")

    tasks = state.get("tasks", [])
    total_books = len(names)
    if bidirectional:
        # The bare "stats"/"diffs" keys hold whichever pass ran last, so a book
        # that only completed the forward pass before cancellation would still
        # look "done" via the bare key. Require both passes' stats and a
        # "done" status (not "incomplete", which means VRAM cut a pass short).
        done = [t for t in tasks if t.get("stats_fwd") and t.get("stats_bwd") and t.get("status") == "done"]
    else:
        done = [t for t in tasks if t.get("stats_fwd") and t.get("status") == "done"]
    incomplete = [t for t in tasks if t.get("status") == "incomplete"]
    failed = [t for t in tasks if t.get("status") == "failed"]
    cancelled = [t for t in tasks if t.get("status") == "cancelled"]

    book_word = "book" if total_books == 1 else "books"
    intro = [
        "# Batch Review Report",
        "",
        f"*Generated {time.strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
        f"The AI reviewer checked **{total_books} {book_word}** for mistakes — like the wrong "
        "character speaking a line, awkward wording, or repeated narration — and fixed what "
        "it found.",
    ]

    if bidirectional:
        intro += [
            "",
            "It went through the books twice: once in reading order, then a second "
            '"hindsight" pass from the last book back to the first, so things learned about '
            "characters later in the series could also be applied to earlier books.",
        ]

    if cancelled or state.get("cancel"):
        intro += ["", f"**Note:** this run was stopped early — {len(done)} of {total_books} "
                       f"{book_word} finished before it was cancelled."]
    if failed:
        names_list = ", ".join(f"*{t['name']}*" for t in failed)
        intro += ["", f"**Note:** {len(failed)} {'book' if len(failed) == 1 else 'books'} "
                       f"could not be reviewed (an error occurred): {names_list}"]
    if incomplete:
        names_list = ", ".join(f"*{t['name']}*" for t in incomplete)
        intro += ["", f"**Note:** {len(incomplete)} {'book' if len(incomplete) == 1 else 'books'} "
                       f"{'was' if len(incomplete) == 1 else 'were'} only partially reviewed — the "
                       f"GPU ran low on memory and the run stopped early for "
                       f"{'it' if len(incomplete) == 1 else 'them'}: {names_list}. "
                       "Re-run the review to finish the rest."]

    if bidirectional:
        overall = _combine_pass_totals(state)
    else:
        overall = state["totals_fwd"]

    lines = list(intro)
    lines += ["", "## Overall totals", ""]
    lines += _markdown_stats_table(overall)

    if bidirectional:
        lines += ["", "### First pass (reading order)", ""]
        lines += _markdown_stats_table(state["totals_fwd"])
        lines += ["", "### Second pass (hindsight)", ""]
        lines += _markdown_stats_table(state["totals_bwd"])

    diff_pool = state.get("diff_pool", {"text": [], "speaker": []})
    overall_highlights = {
        "text_rewrites": sorted(diff_pool["text"], key=lambda h: h["magnitude"], reverse=True)[:5],
        "speaker_changes": diff_pool["speaker"][:5],
    }
    hl_lines = _markdown_diff_highlights_lines(overall_highlights, max_each=5)
    if hl_lines:
        lines += ["", "## Highlights", ""]
        lines += hl_lines

    heads_up = _markdown_heads_up_lines(overall)
    if heads_up:
        lines += ["", "## Things to check", ""]
        lines += heads_up

    if discover:
        aliases_fwd = state.get("aliases_fwd", [])
        aliases_bwd = state.get("aliases_bwd", [])
        lines += ["", "## New character names discovered", ""]
        if not aliases_fwd and not aliases_bwd:
            lines.append("- No new character names were found.")
        elif bidirectional:
            if aliases_fwd:
                lines += _markdown_aliases_lines(aliases_fwd, pass_label=" — first pass")
            if aliases_bwd:
                lines += _markdown_aliases_lines(aliases_bwd, pass_label=" — second/hindsight pass")
        else:
            lines += _markdown_aliases_lines(aliases_fwd)

    # Ask the LLM for a plain-English summary of the report so far, before appending
    # the (potentially very long) book-by-book breakdown.
    lines = _insert_llm_summary(lines, len(intro))

    if total_books > 1:
        lines += ["", "## Book-by-book breakdown", ""]
        for t in tasks:
            name = t.get("name", "?")
            status = t.get("status")
            lines += [f"### {name}", ""]
            if bidirectional:
                stats_fwd = t.get("stats_fwd")
                stats_bwd = t.get("stats_bwd")
                if stats_fwd or stats_bwd:
                    if stats_fwd:
                        lines += ["#### First pass (reading order)", ""]
                        lines += _markdown_book_pass_lines(stats_fwd, t.get("diffs_fwd"), heading="#####")
                    if stats_bwd:
                        if stats_fwd:
                            lines.append("")
                        lines += ["#### Second pass (hindsight)", ""]
                        lines += _markdown_book_pass_lines(stats_bwd, t.get("diffs_bwd"), heading="#####")
                elif status == "cancelled":
                    lines.append("- Not reviewed — the run was cancelled before reaching this book.")
                elif status == "failed":
                    lines.append("- Not reviewed — an error occurred for this book.")
                else:
                    lines.append("- Not reviewed.")
            else:
                stats = t.get("stats")
                if stats:
                    lines += _markdown_book_pass_lines(stats, t.get("diffs"))
                elif status == "cancelled":
                    lines.append("- Not reviewed — the run was cancelled before reaching this book.")
                elif status == "failed":
                    lines.append("- Not reviewed — an error occurred for this book.")
                else:
                    lines.append("- Not reviewed.")
            lines.append("")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return None
    return path


@app.get("/api/system/stats")
async def get_system_stats():
    """Return GPU memory, disk, and basic hardware statistics."""
    gpu = get_gpu_stats()
    has_space, free_gb = check_disk_space(ROOT_DIR, 1.0)

    cpu_count = os.cpu_count()

    ram_gb = None
    try:
        with open("/proc/meminfo") as _mf:
            for _line in _mf:
                if _line.startswith("MemTotal:"):
                    ram_gb = round(int(_line.split()[1]) / 1_048_576, 1)
                    break
    except (OSError, ValueError):
        pass

    gpu_name = None
    try:
        torch = _get_torch()
        if torch is not None and torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
    except (RuntimeError, OSError, AttributeError):
        pass
    if gpu_name is None:
        # torch can't see the GPU — ask rocm-smi for the product name (AMD).
        name_data = run_rocm_smi_json(["--showproductname"],
                                      rocm_smi_path="/opt/rocm/bin/rocm-smi", timeout=2)
        if name_data:
            for card_data in name_data.values():
                if isinstance(card_data, dict):
                    name = card_data.get("Card Series") or card_data.get("Card Model")
                    if name and name != "N/A":
                        gpu_name = name
                        break

    return {
        "gpu": gpu,
        "gpu_name": gpu_name,
        "disk": {
            "free_gb": round(free_gb, 2),
            "low_space": not has_space
        },
        "cpu_count": cpu_count,
        "ram_gb": ram_gb,
    }


@app.get("/api/status/eta")
async def get_eta_status():
    """Return progress/ETA for the most relevant currently-running task, if any."""
    for key, label in ETA_TASKS:
        state = process_state.get(key)
        if state and state.get("running"):
            eta = _compute_eta(state)
            eta.update({"running": True, "task": key, "label": label})
            return eta
    return {"running": False}


class LMStudioOptimizeRequest(BaseModel):
    enable: bool


@app.get("/api/lmstudio/status")
async def lmstudio_status():
    """Report whether the loaded model is using ideal settings (VRAM-safe
    locally, large-context remotely) so the UI can show an at-a-glance indicator."""
    full_cfg = safe_load_json(CONFIG_PATH, default={})
    llm_cfg = full_cfg.get("llm") or {}
    base_url = llm_cfg.get("base_url", "")
    model_name = llm_cfg.get("model_name")
    if not model_name:
        return {"available": False, "loaded": False, "context_length": None,
                "parallel": None, "optimized": False, "model": None}
    llm_mode = full_cfg.get("llm_mode", "local")
    ssh_alias = (full_cfg.get("llm_remote_ssh") or "").strip()
    status = await asyncio.to_thread(get_current_status, llm_mode, base_url, model_name, ssh_alias)
    status["model"] = model_name
    if is_remote_llm(llm_mode, base_url):
        status["remote"] = True
    return status


def _log_llm_failure(kind: str, detail: str) -> str:
    """Write an LLM connection/optimize failure to logs/api/ and return the path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(API_LOG_DIR, f"llm_{kind}_{ts}.log")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] LLM {kind} failure\n\n{detail}\n")
    except OSError:
        return ""
    return path


@app.post("/api/lmstudio/optimize")
async def lmstudio_optimize(req: LMStudioOptimizeRequest):
    """Toggle the loaded model between VRAM-safe/best settings and LM Studio's
    defaults. Local endpoints use the local `lms` CLI; remote endpoints (e.g.
    LM Studio on Thunder) are driven over SSH via the configured host alias -
    a `tnr-<id>`-shaped alias is resolved live each call (see
    lmstudio_settings.resolve_thunder_target), so this self-heals across
    Thunder instance recreation without needing `tnr connect` re-run."""
    full_cfg = safe_load_json(CONFIG_PATH, default={})
    cfg = full_cfg.get("llm", {})
    model_name = cfg.get("model_name")
    if not model_name:
        raise HTTPException(status_code=400, detail="No LLM model configured")

    if not is_remote_llm(full_cfg.get("llm_mode", "local"), cfg.get("base_url", "")):
        res = await asyncio.to_thread(
            apply_lmstudio_settings, model_name, ideal=req.enable)
        if not res.ok:
            raise HTTPException(status_code=502, detail=res.message)
        status = await asyncio.to_thread(get_lmstudio_status, model_name)
        status["model"] = model_name
        status["message"] = res.message
        return status

    # Remote: needs the SSH host alias (e.g. "tnr-0" from `tnr connect`).
    ssh_alias = (full_cfg.get("llm_remote_ssh") or "").strip()
    if not ssh_alias:
        raise HTTPException(status_code=400, detail=(
            "Remote optimize needs an SSH host alias (e.g. 'tnr-0'). Set it in "
            "the Setup tab's Remote LLM settings (run `tnr connect <id>` once first)."))
    res = await asyncio.to_thread(
        apply_remote_lmstudio_settings, ssh_alias, model_name, req.enable)

    # decide_healed_urls is the single keep-persist policy shared with
    # ensure_ideal_settings (so the two can't drift): persist a freshly-resolved
    # URL that differs from the cached one even when verify failed - it's the new
    # truth and the next run re-resolves. last_synced is recorded only on success
    # (pass target only when ok), straight from the resolved Thunder target
    # (uuid/ip/ssh_port) instead of reverse-parsing the URL. We're past the local
    # early-return, so this branch is provably remote.
    cached = full_cfg.get("llm_remote", {}).get("base_url")
    persist_url, _ = decide_healed_urls(res.ok, res.base_url, cached)
    if persist_url:
        persist_healed_base_url(full_cfg, CONFIG_PATH, True, persist_url,
                                target=res.target if res.ok else None)

    if not res.ok:
        log_path = _log_llm_failure(res.log_kind or "optimize", f"alias={ssh_alias} model={model_name}\n\n{res.message}")
        detail = f"{res.message}" + (f" (log: {log_path})" if log_path else "")
        hint = _ssh_key_hint(res.message, ssh_alias)
        if hint:
            detail += f" — {hint}"
        raise HTTPException(status_code=502, detail=detail)
    return {"model": model_name, "message": res.message, "remote": True, "optimized": req.enable, "base_url": res.base_url}


def _ssh_key_hint(error_text: str, ssh_alias: str) -> Optional[str]:
    """If a remote optimize fails SSH auth (missing/wrong key - e.g. the Thunder
    instance was recreated and `tnr connect` was never re-run, so no key exists
    at the per-uuid path resolve_thunder_target expects), point the user at the
    one command that restores access. Returns a one-line hint, else None."""
    text = error_text or ""
    if "Permission denied (publickey" not in text and "Identity file" not in text:
        return None
    m = re.match(r"tnr-(\d+)$", (ssh_alias or "").strip())
    cmd = f"tnr connect {m.group(1)}" if m else "tnr connect <id>"
    return (f"SSH key for this instance is missing - run `{cmd}` to restore access "
            "(the instance was likely recreated), then click Optimize again.")


def _thunder_not_serving_hint(base_url: str, error_text: str) -> Optional[str]:
    """If a Thunder forwarding URL returns the proxy's "Nothing running here"
    page, the instance is up but no LLM server is answering on the forwarded
    port - not started, or bound to 127.0.0.1 so the forward can't see it. The
    Optimize button SSHes in and runs `lms server start --bind 0.0.0.0`, which
    fixes exactly this. Returns a one-line hint, else None."""
    if _is_thunder_host(base_url) and "Nothing running here" in (error_text or ""):
        return ('Remote instance is up but no LLM server is answering on it. '
                'Click "Optimize" to start LM Studio on the remote box over SSH '
                "(binds 0.0.0.0 so Thunder's port-forward can see it), then Test again.")
    return None


def _run_llm_test(base_url: str, api_key: str, model_name: str) -> dict:
    """Probe an OpenAI-compatible endpoint: list models, then a tiny completion.

    Returns a dict describing each step. On failure, writes a log file and
    includes its path so the user can hand it back for debugging.
    """
    from openai import OpenAI
    try:
        _validate_local_llm_base_url(base_url)
    except LLMConfigError as e:
        return {"ok": False, "step": "validate", "error": str(e)}

    client = OpenAI(base_url=base_url, api_key=api_key or "local", timeout=30)
    # Step 1: list models (cheap reachability + model-id check)
    try:
        models = [m.id for m in client.models.list().data]
    except Exception as e:
        detail = f"base_url={base_url}\nGET /models failed:\n{traceback.format_exc()}"
        hint = _thunder_not_serving_hint(base_url, str(e))
        return {"ok": False, "step": "models",
                "error": "Endpoint reachable but no LLM server is running on it." if hint else str(e),
                "hint": hint,
                "log_file": _log_llm_failure("test", detail)}
    model_present = model_name in models if model_name else None
    # Step 2: tiny chat completion
    try:
        resp = client.chat.completions.create(
            model=model_name or (models[0] if models else ""),
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
            max_tokens=8, temperature=0,
        )
        reply = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        detail = (f"base_url={base_url}\nmodel={model_name}\navailable={models}\n"
                  f"chat completion failed:\n{traceback.format_exc()}")
        return {"ok": False, "step": "completion", "error": str(e),
                "models": models, "model_present": model_present,
                "log_file": _log_llm_failure("test", detail)}
    return {"ok": True, "base_url": base_url, "model": model_name,
            "models": models, "model_present": model_present, "reply": reply}


@app.post("/api/llm/test")
async def llm_test(profile: Optional[LLMConfig] = None):
    """Test LLM connectivity. Uses the posted profile if given (so the Setup tab
    can test before saving), otherwise the active config. Writes a log on failure."""
    if profile is not None and profile.base_url.strip():
        url = profile.base_url.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        base_url, api_key, model_name = url, profile.api_key, profile.model_name
    else:
        cfg = _load_llm_config()
        base_url = cfg.get("base_url", "")
        api_key = cfg.get("api_key", "local")
        model_name = cfg.get("model_name", "")
    if not base_url:
        raise HTTPException(status_code=400, detail="No LLM base_url configured")
    return await asyncio.to_thread(_run_llm_test, base_url, api_key, model_name)

# Endpoints

@app.get("/")
async def read_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )

@app.get("/favicon.ico")
async def read_favicon():
    favicon_path = os.path.join(ROOT_DIR, "icon.png")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Favicon not found")

def _fill_missing_prompt_defaults(prompts: dict) -> None:
    """Fill in any missing/empty prompt fields from the on-disk templates,
    in place. Used by get_config()'s "need defaults" cases (no config.json
    yet, prompts key entirely missing, prompts present but some fields
    empty) so the system/review/persona default-loading logic - including
    review/persona's RuntimeError handling for missing prompt-template
    files, and skipping a loader entirely when nothing in its category is
    missing - exists in exactly one place instead of three copies that can
    drift out of sync with each other.
    """
    if not prompts.get("system_prompt") or not prompts.get("user_prompt"):
        sys_prompt, usr_prompt = load_default_prompts()
        if not prompts.get("system_prompt"):
            prompts["system_prompt"] = sys_prompt
        if not prompts.get("user_prompt"):
            prompts["user_prompt"] = usr_prompt
    if not prompts.get("review_system_prompt") or not prompts.get("review_user_prompt"):
        try:
            rev_sys, rev_usr = load_review_prompts()
            if not prompts.get("review_system_prompt"):
                prompts["review_system_prompt"] = rev_sys
            if not prompts.get("review_user_prompt"):
                prompts["review_user_prompt"] = rev_usr
        except RuntimeError:
            pass  # review_prompts.txt missing or malformed — leave fields empty
    if not all(prompts.get(k) for k in
               ("persona_system_prompt", "persona_user_prompt", "persona_advanced_prompt")):
        try:
            per_sys, per_usr, per_adv = load_persona_prompts()
            if not prompts.get("persona_system_prompt"):
                prompts["persona_system_prompt"] = per_sys
            if not prompts.get("persona_user_prompt"):
                prompts["persona_user_prompt"] = per_usr
            if not prompts.get("persona_advanced_prompt"):
                prompts["persona_advanced_prompt"] = per_adv
        except RuntimeError:
            pass


@app.get("/api/config")
async def get_config():
    default_config = {
        "llm": {
            "base_url": "http://localhost:11434/v1",
            "api_key": "local",
            "model_name": "richardyoung/qwen3-14b-abliterated:Q8_0"
        },
        "llm_mode": "local",
        "tts": {
            "mode": "local",
            "url": "http://127.0.0.1:7860",
            "device": "auto"
        },
        "prompts": {
            "system_prompt": "",
            "user_prompt": ""
        }
    }

    if not os.path.exists(CONFIG_PATH):
        _fill_missing_prompt_defaults(default_config["prompts"])
        config = default_config
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

    # Ensure prompts section exists with defaults from file. Treat an explicit
    # null (config saved without a prompts field) the same as a missing key so
    # the dict-access branches below don't crash on None.
    if not config.get("prompts"):
        config["prompts"] = {}
    _fill_missing_prompt_defaults(config["prompts"])

    # Local/Remote LLM toggle: ensure mode + both profiles are present so the UI
    # can populate the toggle. Migrate older config.json (only had `llm`) by
    # seeding the local profile from the active section.
    config.setdefault("llm_mode", "local")
    if not config.get("llm_local"):
        config["llm_local"] = dict(config.get("llm", {}))
    config.setdefault("llm_remote", None)
    config.setdefault("llm_remote_ssh", None)

    # Always include current_file (null when no state or file missing)
    config["current_file"] = None
    state_path = os.path.join(ROOT_DIR, "state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as sf:
                state = json.load(sf)
            input_path = state.get("input_file_path", "")
            if input_path and os.path.exists(input_path):
                config["current_file"] = os.path.basename(input_path)
        except (json.JSONDecodeError, ValueError):
            pass

    return config

@app.get("/api/default_prompts")
async def get_default_prompts():
    system_prompt, user_prompt = load_default_prompts()
    result = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt
    }
    try:
        review_sys, review_usr = load_review_prompts()
        result["review_system_prompt"] = review_sys
        result["review_user_prompt"] = review_usr
    except RuntimeError:
        pass
    try:
        persona_sys, persona_usr, persona_adv = load_persona_prompts()
        result["persona_system_prompt"] = persona_sys
        result["persona_user_prompt"] = persona_usr
        result["persona_advanced_prompt"] = persona_adv
    except RuntimeError:
        pass
    return result

def _normalize_and_validate_llm(profile: "LLMConfig") -> None:
    """Append /v1 to the base_url (in place) and ensure it's a local/trusted host."""
    if not profile.base_url.strip():
        raise HTTPException(status_code=400, detail="LLM base_url is required")
    url = profile.base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    profile.base_url = url
    try:
        _validate_local_llm_base_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/config")
async def save_config(config: AppConfig):
    # Normalize/validate whichever profiles were sent. The local/remote profiles
    # are persisted side-by-side so toggling never loses the other one's settings.
    if config.llm_local is not None and config.llm_local.base_url.strip():
        _normalize_and_validate_llm(config.llm_local)
    if config.llm_remote is not None and config.llm_remote.base_url.strip():
        _normalize_and_validate_llm(config.llm_remote)

    # Pick the active profile from the toggle, then mirror it into `llm` - the
    # section every consumer (review/generate/personas/nicknames) reads.
    active = config.llm_remote if config.llm_mode == "remote" else config.llm_local
    if active is None:
        raise HTTPException(status_code=400, detail=(
            f"llm_mode is '{config.llm_mode}' but no llm_{config.llm_mode} "
            f"profile was provided - refusing to save a config where llm_mode "
            f"and the active llm profile would disagree."))
    config.llm = active.model_copy(deep=True)
    _normalize_and_validate_llm(config.llm)

    atomic_json_write(config.model_dump(), CONFIG_PATH)
    # Reset engine so it picks up new TTS settings on next use
    project_manager.engine = None
    return {"status": "saved"}

class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags from EPUB content, preserving block-level structure."""
    BLOCK_TAGS = frozenset({
        'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'li', 'blockquote', 'br', 'hr', 'tr', 'section', 'article',
    })
    SKIP_TAGS = frozenset({'style', 'script'})

    def __init__(self):
        super().__init__()
        self.parts = []
        self._pending_newline = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._pending_newline = True

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._pending_newline and self.parts:
            self.parts.append('\n')
            self._pending_newline = False
        self.parts.append(data)

    def get_text(self):
        return ''.join(self.parts)


def extract_epub_text(epub_path: str) -> str:
    """Extract plain text from an EPUB file, ordered by spine (reading order).

    Parses the EPUB ZIP structure directly using stdlib only:
    META-INF/container.xml -> .opf manifest+spine -> XHTML content files.
    """
    with zipfile.ZipFile(epub_path, 'r') as zf:
        # 1. Find the OPF file path from container.xml
        container_xml = zf.read('META-INF/container.xml')
        container = ET.fromstring(container_xml)
        ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
        rootfile_el = container.find('.//c:rootfile', ns)
        if rootfile_el is None:
            raise ValueError("Invalid EPUB: no rootfile found in container.xml")
        opf_path = rootfile_el.get('full-path')

        # 2. Parse the OPF to get manifest (id->href) and spine (reading order)
        opf_xml = zf.read(opf_path)
        opf = ET.fromstring(opf_xml)
        # Detect OPF namespace (varies between EPUB 2 and 3)
        opf_ns = opf.tag.split('}')[0] + '}' if '}' in opf.tag else ''

        # Build manifest: id -> href (resolve relative to OPF directory)
        opf_dir = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''
        manifest = {}
        for item in opf.findall(f'.//{opf_ns}item'):
            item_id = item.get('id')
            href = item.get('href')
            media_type = item.get('media-type', '')
            if item_id and href and 'html' in media_type:
                manifest[item_id] = opf_dir + href

        # Get spine order
        spine_ids = []
        for itemref in opf.findall(f'.//{opf_ns}itemref'):
            idref = itemref.get('idref')
            if idref:
                spine_ids.append(idref)

        # 3. Extract text from each spine item in order
        chapters = []
        for item_id in spine_ids:
            href = manifest.get(item_id)
            if href is None:
                continue
            try:
                html_bytes = zf.read(href)
            except KeyError:
                continue
            html_content = html_bytes.decode('utf-8', errors='replace')
            extractor = _HTMLTextExtractor()
            extractor.feed(html_content)
            text = extractor.get_text().strip()
            if text:
                chapters.append(text)

    return '\n\n'.join(chapters)


def _claim_unique_path(directory: str, filename: str, sibling_suffixes=()) -> str:
    """Atomically reserve a unique path in directory for filename, returning the
    path to a newly-created empty file the caller should now write/truncate into.

    A directory scan picks a good starting candidate (avoiding O(n) O_EXCL
    failures when the directory is large), then os.O_EXCL claims it -
    closing the TOCTOU race a scan-then-write approach has under concurrent
    uploads of the same filename. Caps at 1000 attempts to prevent a DoS
    from a maliciously pre-populated directory.

    sibling_suffixes: when given (e.g. (".voice_config.json",)), each
    {stem}{suffix} companion is O_EXCL-reserved too (an empty placeholder the
    caller overwrites), so a concurrent claimant or out-of-band creator can't
    slip a sibling in between this claim and the caller's companion write. If any
    companion is already taken, the whole claim is released and the next
    candidate tried. A caller that ends up not writing a companion should remove
    the leftover placeholder.
    """
    existing = {e.name for e in os.scandir(directory) if e.is_file()}
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while True:
        stem = os.path.splitext(candidate)[0]
        siblings_clear = all(f"{stem}{s}" not in existing for s in sibling_suffixes)
        if candidate not in existing and siblings_clear:
            path = os.path.join(directory, candidate)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.close(fd)
            except FileExistsError:
                pass  # lost the race on the primary - try the next candidate
            else:
                # Primary claimed; now atomically reserve every companion too.
                claimed = [path]
                try:
                    for suffix in sibling_suffixes:
                        sib = os.path.join(directory, f"{stem}{suffix}")
                        fd = os.open(sib, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                        os.close(fd)
                        claimed.append(sib)
                except FileExistsError:
                    for p in claimed:  # a companion was taken - release the lot
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
                else:
                    return path
        if counter > 1000:
            raise RuntimeError(f"Too many collisions for filename: {filename}")
        counter += 1
        candidate = f"{base}_{counter}{ext}"


def _safe_subpath(base_dir: str, name: str) -> str:
    """Resolve `name` under `base_dir`, rejecting path traversal (e.g. '..' or
    absolute paths). Returns the realpath; raises HTTP 400 if it escapes.

    Guards endpoints that build a filesystem path from a user-supplied name and
    then delete/extract it, so a value like '..' can't reach outside base_dir.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base_dir, name))
    if target != base and not target.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="Invalid name.")
    return target


def _safe_extractall(zf: "zipfile.ZipFile", dest_dir: str) -> None:
    """zipfile.extractall, but reject members that would escape dest_dir
    (Zip-Slip path traversal via '../' entries or absolute paths)."""
    dest = os.path.realpath(dest_dir)
    for member in zf.namelist():
        target = os.path.realpath(os.path.join(dest_dir, member))
        if target != dest and not target.startswith(dest + os.sep):
            raise HTTPException(status_code=400, detail="Archive contains an unsafe path.")
    zf.extractall(dest_dir)


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    # Validate and sanitize filename to prevent path traversal
    safe_name = secure_filename(file.filename or "")
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid or empty filename")
    file_path = await asyncio.to_thread(_claim_unique_path, UPLOADS_DIR, safe_name)
    async with aiofiles.open(file_path, 'wb') as out_file:
        content = await file.read()
        await out_file.write(content)

    # Convert EPUB to plain text
    if file_path.lower().endswith('.epub'):
        try:
            text = extract_epub_text(file_path)
        except Exception as e:
            os.remove(file_path)
            raise HTTPException(status_code=400, detail=f"Failed to process EPUB: {e}")
        if not text.strip():
            os.remove(file_path)
            raise HTTPException(status_code=400, detail="No readable text content found in EPUB.")
        txt_name = os.path.basename(file_path).rsplit('.', 1)[0] + '.txt'
        txt_path = await asyncio.to_thread(_claim_unique_path, UPLOADS_DIR, txt_name)
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(text)
        # The original .epub is no longer needed once its text is extracted;
        # leaving it behind leaks disk space as books accumulate.
        try:
            os.remove(file_path)
        except OSError:
            pass
        file_path = txt_path

    # Save input path to state.json to be compatible with original scripts if needed
    state_path = os.path.join(ROOT_DIR, "state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass

    state["input_file_path"] = file_path
    atomic_json_write(state, state_path)

    return {"filename": file.filename, "stored_filename": os.path.basename(file_path), "path": file_path}

@app.post("/api/generate_script")
async def generate_script(background_tasks: BackgroundTasks,
                          request: Optional[GenerateScriptRequest] = None):
    if request is None:
        request = GenerateScriptRequest()
    # Get input file from state.json
    state_path = os.path.join(ROOT_DIR, "state.json")
    if not os.path.exists(state_path):
        raise HTTPException(status_code=400, detail="No input file selected")

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
        input_file = state.get("input_file_path")

    if not input_file:
         raise HTTPException(status_code=400, detail="No input file found in state")

    check_global_gpu_lock("script")

    cmd = [sys.executable, "-u", "generate_script.py", input_file]
    if request.resume:
        cmd.append("--resume")
    claim_gpu_task("script")
    background_tasks.add_task(run_process, cmd, "script")
    return {"status": "started", "resume": request.resume}


@app.get("/api/generate_script/checkpoint")
async def generate_script_checkpoint():
    """Detect an unfinished single-script generation (read-only)."""
    s = _summarize_script_checkpoint(SCRIPT_PATH + ".script_checkpoint.json")
    return s or {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}

@app.post("/api/generate_script/cancel")
async def generate_script_cancel():
    return _cancel_task("script", "No script generation is currently running.", "Script generation process already exited.")

def _posix_signal(proc, signame):
    """Send a POSIX signal by name (e.g. "SIGSTOP"). Raises 501 on Windows,
    where SIGSTOP/SIGCONT don't exist on the signal module — the name is
    resolved here, after the platform check, so callers never reference the
    constant directly and crash with AttributeError on Windows."""
    if sys.platform == "win32":
        raise HTTPException(status_code=501, detail="Pause/resume is not supported on Windows.")
    try:
        sig = getattr(signal, signame)
        proc.send_signal(sig)
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


@app.post("/api/generate_script/pause")
async def generate_script_pause():
    return _pause_task("script", "No script generation is currently running.",
                        "Script generation is starting up, retry in a moment.",
                        "Script generation")

@app.post("/api/generate_script/resume")
async def generate_script_resume():
    return _resume_task("script", "No script generation is currently running.",
                         "Script generation")


@app.post("/api/review_script")
async def review_script(background_tasks: BackgroundTasks, request: Optional[ReviewRequest] = None):
    """Review the current annotated script. Accepts empty POST or JSON body."""
    if request is None:
        request = ReviewRequest()  # Use defaults
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=400, detail="No annotated script found. Generate a script first.")

    check_global_gpu_lock("review")
    if not request.resume:
        clear_checkpoint(SCRIPT_PATH)

    cmd = [sys.executable, "-u", "review_script.py"]
    if request.dedupe_speakers:
        cmd += ["--dedupe-speakers", "--remap-voice-config", VOICE_CONFIG_PATH,
                "--alias-registry", CHARACTER_ALIASES_PATH]
    claim_gpu_task("review")
    background_tasks.add_task(run_process, cmd, "review")
    return {"status": "started", "dedupe_speakers": request.dedupe_speakers}

@app.post("/api/review_script_contextual")
async def review_script_contextual(request: ContextualReviewRequest, background_tasks: BackgroundTasks):
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=400, detail="No annotated script found. Generate a script first.")

    check_global_gpu_lock("review")
    if not request.resume:
        clear_checkpoint(SCRIPT_PATH)

    window_size = max(1, min(int(request.window_size or 4), 12))
    total_entries = 0
    try:
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            total_entries = len(json.load(f))
    except (json.JSONDecodeError, ValueError, OSError):
        total_entries = 0

    review_batch_size = 25
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                review_batch_size = max(1, int((cfg.get("generation") or {}).get("review_batch_size", 25)))
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            review_batch_size = 25

    estimated_calls = ceil(total_entries / review_batch_size) if total_entries else 0
    cmd = [sys.executable, "-u", "review_script.py", "--context-window", str(window_size)]
    if request.dedupe_speakers:
        cmd += ["--dedupe-speakers", "--remap-voice-config", VOICE_CONFIG_PATH,
                "--alias-registry", CHARACTER_ALIASES_PATH]
    claim_gpu_task("review")
    background_tasks.add_task(
        run_process,
        cmd,
        "review"
    )
    return {
        "status": "started",
        "mode": "contextual",
        "window_size": window_size,
        "batch_size": review_batch_size,
        "total_entries": total_entries,
        "estimated_calls": estimated_calls,
        "dedupe_speakers": request.dedupe_speakers,
    }


@app.post("/api/review_script/cancel")
async def review_script_cancel():
    return _cancel_task("review", "No script review is currently running.", "Script review process already exited.")


@app.post("/api/review_script/pause")
async def review_script_pause():
    return _pause_task("review", "No script review is currently running.",
                        "Script review is starting up, retry in a moment.",
                        "Script review")


@app.post("/api/review_script/resume")
async def review_script_resume():
    return _resume_task("review", "No script review is currently running.",
                         "Script review")


@app.get("/api/review_script/checkpoint")
async def review_script_checkpoint():
    """Detect an unfinished single-book review (read-only)."""
    s = _summarize_review_checkpoint(SCRIPT_PATH + ".review_checkpoint.json")
    if not s:
        return {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}
    return {"exists": True, "done": s["completed_batches"], "total": s["total_batches"],
            "label": f"{s['completed_batches']}/{s['total_batches']} batches", "mode": {}}


@app.post("/api/find_nicknames")
async def find_nicknames_endpoint(background_tasks: BackgroundTasks):
    """Scan the working script for character nicknames/aliases and write character_aliases.json."""
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=400, detail="No annotated script found. Generate a script first.")
    # nicknames runs the LLM, so it must claim the GPU lock (this also guards
    # against a duplicate start, replacing the old running-flag check).
    claim_gpu_task("nicknames")
    cmd = [sys.executable, "-u", "find_nicknames.py",
           "--aliases-file", CHARACTER_ALIASES_PATH, "--append"]
    background_tasks.add_task(run_process, cmd, "nicknames")
    return {"status": "started"}


@app.post("/api/find_nicknames/cancel")
async def find_nicknames_cancel():
    return _cancel_task("nicknames", "No nickname discovery is currently running.", "Nickname discovery already exited.")


@app.post("/api/find_nicknames/pause")
async def find_nicknames_pause():
    return _pause_task("nicknames", "No nickname discovery is currently running.",
                        "Nickname discovery is starting up, retry in a moment.",
                        "Nickname discovery")


@app.post("/api/find_nicknames/resume")
async def find_nicknames_resume():
    return _resume_task("nicknames", "No nickname discovery is currently running.",
                         "Nickname discovery")


@app.get("/api/character_aliases")
async def get_character_aliases():
    """Return the current alias map { alias: canonical }."""
    aliases = safe_load_json(CHARACTER_ALIASES_PATH, default={})
    if not isinstance(aliases, dict):
        return {}
    # Hide identity rows (NAME -> NAME) — they're inert and only clutter the editor.
    # Exact-match only, so a legitimate case-fix alias (kenji -> KENJI) stays visible.
    return {k: v for k, v in aliases.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() != v.strip()}


@app.post("/api/character_aliases")
async def save_character_aliases(aliases: Dict[str, str]):
    """Overwrite the alias map (lets the user correct discovered nicknames before review)."""
    cleaned = {k.strip(): v.strip() for k, v in aliases.items() if k.strip() and v.strip()}
    atomic_json_write(cleaned, CHARACTER_ALIASES_PATH)
    return {"status": "saved", "count": len(cleaned)}


def _save_batch_review_state(state: dict, names: list, settings: dict) -> None:
    try:
        atomic_json_write({
            "names": names,
            "current_pass": state.get("current_pass"),
            "current_task_idx": state.get("current_task_idx", 0),
            "bidirectional": settings.get("bidirectional", False),
            "window": settings.get("window", 0),
            "dedupe": settings.get("dedupe", False),
            "discover": settings.get("discover", False),
            "tasks": [dict(t) for t in state.get("tasks", [])],
        }, BATCH_REVIEW_STATE_PATH)
    except OSError as e:
        state["logs"].append(f"WARNING: could not save batch review state: {e}")


def _clear_batch_review_state(names: list) -> None:
    """Fresh start: drop the plan and every per-book review checkpoint."""
    if os.path.exists(BATCH_REVIEW_STATE_PATH):
        try:
            os.remove(BATCH_REVIEW_STATE_PATH)
        except OSError:
            pass
    for name in names:
        safe = secure_filename(name)
        if not safe:
            continue
        clear_checkpoint(os.path.join(SCRIPTS_DIR, f"{safe}.json"))


@app.post("/api/review_script/batch/start")
async def review_script_batch_start(request: BatchReviewRequest, background_tasks: BackgroundTasks):
    """Review multiple saved scripts from the Scripts library, in place.
    A shared alias registry keeps merged character names consistent across the batch."""
    check_global_gpu_lock("batch_review")
    if not request.script_names:
        raise HTTPException(status_code=400, detail="No scripts selected.")

    window = max(0, min(int(request.context_window or 0), 12))
    dedupe = bool(request.dedupe_speakers)
    discover = bool(request.find_nicknames) and dedupe
    # A backward pass only adds value when discovery is on (it re-scans early books with the
    # now-complete registry as hindsight context). With discovery off it would be a pure re-apply.
    bidirectional = bool(request.bidirectional) and discover

    names = request.script_names
    total = len(names)

    resume = bool(request.resume)
    resume_pass = "fwd"
    resume_idx = 0
    if resume:
        prev = safe_load_json(BATCH_REVIEW_STATE_PATH)
        if isinstance(prev, dict) and prev.get("names") == names:
            resume_pass = prev.get("current_pass") or "fwd"
            resume_idx = prev.get("current_task_idx", 0) or 0
        else:
            resume = False  # plan changed (different books/order) -> fresh
    if not resume:
        _clear_batch_review_state(names)
    settings = {"bidirectional": bidirectional, "window": window,
                "dedupe": dedupe, "discover": discover}

    def _run():
        state = process_state["batch_review"]
        prefix = "bidirectional " if bidirectional else ""
        _init_batch_state(state,
                          [f"Starting {prefix}batch review of {total} script(s)..."],
                          [{"name": n, "status": "pending"} for n in names])
        state["bidirectional"] = bidirectional
        state["totals_fwd"] = _new_review_totals()
        state["totals_bwd"] = _new_review_totals()
        state["aliases_fwd"] = []
        state["aliases_bwd"] = []
        state["diff_pool"] = {"text": [], "speaker": []}

        # One full on-disk log for the whole batch (in-memory list is a capped tail)
        log_path = _init_task_log("batch_review")

        # One shared registry for the whole batch so canonical names align across books
        registry_path = os.path.join(SCRIPTS_DIR, ".series_aliases.json") if dedupe else None

        def _process_book(i: int, name: str, tag: str = "") -> bool:
            """Discover + review one book in place. Returns False to stop the batch (cancel)."""
            state["current_task_idx"] = i
            orig_status = state["tasks"][i].get("status")
            state["tasks"][i]["status"] = "running"
            # Persist position before the (long) per-book work so a power outage
            # mid-book leaves an accurate pass + index for resume.
            _save_batch_review_state(state, names, settings)

            safe_name = secure_filename(name)
            if not safe_name:
                state["logs"].append(f"--- [{i+1}/{total}]{tag} Skipping — invalid name: {name} ---")
                state["tasks"][i]["status"] = "failed"
                return True
            script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
            if not os.path.exists(script_path):
                state["logs"].append(f"--- [{i+1}/{total}]{tag} Skipping — not found: {name} ---")
                state["tasks"][i]["status"] = "failed"
                return True

            state["logs"].append(f"--- [{i+1}/{total}]{tag} Reviewing '{name}' ---")

            # Optional nickname discovery first, accumulating into the shared series registry
            if discover and registry_path:
                state["logs"].append(f"[{i+1}]{tag} Discovering nicknames...")
                nick_cmd = [
                    sys.executable, "-u",
                    os.path.join(BASE_DIR, "find_nicknames.py"),
                    "--input", script_path,
                    "--aliases-file", registry_path,
                    "--append",
                ]
                _, nick_lines = _stream_subprocess_to_logs(nick_cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ", log_file=log_path)
                if state.get("cancel"):
                    state["tasks"][i]["status"] = "cancelled"
                    return False
                new_aliases = _extract_new_aliases(nick_lines)
                if new_aliases:
                    state["tasks"][i]["aliases_found"] = new_aliases
                    bucket = state["aliases_bwd"] if tag == " [bwd]" else state["aliases_fwd"]
                    for a in new_aliases:
                        bucket.append({**a, "book": name})
                    state["logs"].append(
                        f"[{i+1}]{tag} New alias(es): " +
                        ", ".join(f"'{a['variant']}' -> '{a['canonical']}'" for a in new_aliases)
                    )

            # Only clear checkpoint at the start of the first pass (forward).
            # For bidirectional reviews, preserve the forward pass checkpoint
            # so if the backward pass crashes, we can resume from where forward left off.
            should_clear = True
            if state.get("bidirectional") and state.get("current_pass") == "bwd":
                # Don't clear checkpoint during backward pass - preserve forward progress
                should_clear = False
                if orig_status == "incomplete":
                    # The forward pass on this book was VRAM-aborted and left behind
                    # its own partial checkpoint (forward-pass progress/aliases). That
                    # checkpoint isn't valid for the backward pass - reusing it would
                    # silently splice forward-pass output into the backward result.
                    should_clear = True

            # On a resumed batch, the one book that was interrupted mid-review keeps
            # its checkpoint so review_script.py continues it from the saved batch.
            if resume and state.get("current_pass") == resume_pass and i == resume_idx:
                should_clear = False

            if should_clear:
                clear_checkpoint(script_path)

            cmd = [
                sys.executable, "-u",
                os.path.join(BASE_DIR, "review_script.py"),
                "--input", script_path,
                "--output", script_path,
            ]
            if window > 0:
                cmd += ["--context-window", str(window)]
            if dedupe:
                cmd += ["--dedupe-speakers", "--alias-registry", registry_path]
                companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
                if os.path.exists(companion):
                    cmd += ["--remap-voice-config", companion]

            rc, own_lines = _stream_subprocess_to_logs(cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ", log_file=log_path)

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                return False
            elif rc == 0:
                # Bidirectional runs review each book twice (forward, then backward); keep
                # each pass's stats/diffs separate so the per-book breakdown doesn't lose
                # the first pass's results when the second pass overwrites them.
                pass_key = "bwd" if tag == " [bwd]" else "fwd"
                stats = _extract_review_stats(own_lines)
                if stats and stats.get("batches_skipped_vram", 0) > 0:
                    # The reviewer bailed out early to avoid an OOM; entries past the
                    # abort point were left unreviewed and a checkpoint may remain on
                    # disk for a future resume. Don't report this book as "done".
                    state["tasks"][i]["status"] = "incomplete"
                else:
                    state["tasks"][i]["status"] = "done"
                if stats:
                    state["tasks"][i][f"stats_{pass_key}"] = stats
                    # "stats" is the combined fwd+bwd total used by the per-book
                    # badge tooltip — recompute it from whichever pass(es) have
                    # run so far rather than letting the last pass overwrite it.
                    state["tasks"][i]["stats"] = _combine_pass_stats(
                        state["tasks"][i].get("stats_fwd"), state["tasks"][i].get("stats_bwd"))
                    totals = state["totals_bwd"] if tag == " [bwd]" else state["totals_fwd"]
                    for key in totals:
                        if key != "books_done":
                            totals[key] += stats[key]
                    totals["books_done"] += 1
                    state["logs"].append(_format_book_summary(i, total, tag, name, stats))
                else:
                    # The subprocess exited 0 but its "Review complete: X -> Y
                    # entries" summary line wasn't found - surface this rather
                    # than silently recording no stats for an otherwise "done" book.
                    state["logs"].append(
                        f"[{i+1}]{tag} Warning: '{name}' finished but no summary "
                        "stats were found in its output."
                    )
                highlights = _extract_diff_highlights(own_lines)
                if highlights["text_rewrites"] or highlights["speaker_changes"]:
                    state["tasks"][i][f"diffs_{pass_key}"] = highlights
                    # Combine diffs from both passes for the UI badge tooltip
                    existing_diffs = state["tasks"][i].get("diffs", {})
                    combined = {
                        "text_rewrites": existing_diffs.get("text_rewrites", []) + highlights["text_rewrites"],
                        "speaker_changes": existing_diffs.get("speaker_changes", []) + highlights["speaker_changes"],
                    }
                    state["tasks"][i]["diffs"] = combined
                    for item in highlights["text_rewrites"]:
                        state["diff_pool"]["text"].append({**item, "book": name})
                    for item in highlights["speaker_changes"]:
                        state["diff_pool"]["speaker"].append({**item, "book": name})
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{i+1}]{tag} Failed (exit {rc}): {name}")
            return True

        # Forward pass (reading order). Skipped entirely if resuming directly into
        # the backward pass (forward was already complete before the interruption).
        state["current_pass"] = "fwd"
        _save_batch_review_state(state, names, settings)
        if resume_pass == "fwd":
            if bidirectional:
                state["logs"].append("=== Forward pass (reading order) ===")
            for i, name in enumerate(names):
                if i < resume_idx:
                    continue
                if state["cancel"]:
                    state["logs"].append("Batch review cancelled.")
                    break
                if not _process_book(i, name, tag=" [fwd]" if bidirectional else ""):
                    break

            state["logs"].append(_format_pass_summary(
                "Forward pass" if bidirectional else "Batch review",
                state["totals_fwd"], state["aliases_fwd"], show_aliases=discover))

        # Backward pass — re-scan from the end so early books get discovery seeded with the
        # now-complete series registry (catches references that only resolve later in the series).
        if bidirectional and not state["cancel"]:
            state["logs"].append("=== Backward pass (hindsight: re-scanning from the end) ===")
            state["current_pass"] = "bwd"
            _save_batch_review_state(state, names, settings)
            bwd_start = resume_idx if resume_pass == "bwd" else total - 1
            for i in range(bwd_start, -1, -1):
                if state["cancel"]:
                    state["logs"].append("Batch review cancelled.")
                    break
                if not _process_book(i, names[i], tag=" [bwd]"):
                    break

            state["logs"].append(_format_pass_summary(
                "Backward pass (hindsight)", state["totals_bwd"], state["aliases_bwd"], show_aliases=discover))

            overall_totals = _combine_pass_totals(state)
            overall_aliases = state["aliases_fwd"] + state["aliases_bwd"]
            state["logs"].append(_format_pass_summary("Overall", overall_totals, overall_aliases, show_aliases=discover))

        report_path = _write_batch_review_report(state, names, bidirectional, discover)
        if report_path:
            state["logs"].append(f"Wrote batch review report: {os.path.relpath(report_path, ROOT_DIR)}")

        state["running"] = False
        if not state["cancel"]:
            _clear_batch_review_state(names)
        state["logs"].append("Batch review finished.")

    def _run_guarded():
        # Guarantee the GPU lock is released even if _run raises before reaching
        # its own cleanup — otherwise process_state stays running=True and every
        # later GPU task is rejected until restart. Mirrors run_process's finally.
        # On an unexpected crash the checkpoint is deliberately left intact (the
        # non-cancel clear lives inside _run's success path), so the batch stays
        # resumable.
        try:
            _run()
        except Exception as e:
            logger.exception(f"batch_review task crashed: {e}")
            process_state["batch_review"]["logs"].append(f"Batch review error: {e}")
        finally:
            process_state["batch_review"]["running"] = False

    claim_gpu_task("batch_review")
    background_tasks.add_task(_run_guarded)
    return {"status": "started", "task_count": total, "bidirectional": bidirectional, "resume": resume}


@app.get("/api/review_script/batch/checkpoint")
async def review_script_batch_checkpoint():
    """Detect an unfinished batch review and report its pass/order plan (read-only)."""
    prev = safe_load_json(BATCH_REVIEW_STATE_PATH)
    if not isinstance(prev, dict) or not prev.get("names"):
        return {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}
    tasks = prev.get("tasks", [])
    done = sum(1 for t in tasks if t.get("status") == "done")
    total = len(prev["names"])
    pass_label = "backward" if prev.get("current_pass") == "bwd" else "forward"
    kind = "front-to-back" if prev.get("bidirectional") else "forward-only"
    return {
        "exists": True, "done": done, "total": total,
        "label": f"{kind}, {pass_label} pass, {done}/{total} books",
        "mode": {
            "bidirectional": prev.get("bidirectional", False),
            "current_pass": prev.get("current_pass"),
            "current_task_idx": prev.get("current_task_idx", 0),
            "names": prev["names"], "tasks": tasks,
        },
    }


def _batch_cancel_helper(state_key: str):
    state = process_state[state_key]
    state["cancel"] = True
    _resume_if_paused(state, state.get("process"))

    # Also terminate the current subprocess if it's running
    proc = state.get("process")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    return {"status": "cancel_requested"}


@app.post("/api/review_script/batch/cancel")
async def review_script_batch_cancel():
    return _batch_cancel_helper("batch_review")


@app.post("/api/review_script/batch/pause")
async def review_script_batch_pause():
    return _pause_task("batch_review", "No batch review is currently running.",
                        "Batch review is starting up, retry in a moment.",
                        "Batch review")


@app.post("/api/review_script/batch/resume")
async def review_script_batch_resume():
    return _resume_task("batch_review", "No batch review is currently running.",
                         "Batch review")


class BatchScriptTask(BaseModel):
    filename: str  # filename inside uploads/

class BatchScriptRequest(BaseModel):
    tasks: List[BatchScriptTask]
    resume: bool = False


def _save_batch_script_state(state: dict) -> None:
    try:
        atomic_json_write({
            "files": [dict(t) for t in state.get("tasks", [])],
            "current_idx": state.get("current_task_idx", 0),
        }, BATCH_SCRIPT_STATE_PATH)
    except OSError as e:
        state["logs"].append(f"WARNING: could not save batch state: {e}")


def _clear_batch_script_state(tasks) -> None:
    """Fresh start: drop the batch plan and every per-file script checkpoint."""
    if os.path.exists(BATCH_SCRIPT_STATE_PATH):
        try:
            os.remove(BATCH_SCRIPT_STATE_PATH)
        except OSError:
            pass
    for t in tasks:
        stem = secure_filename(os.path.splitext(t.filename)[0]) or ""
        if not stem:
            continue
        ckpt = os.path.join(SCRIPTS_DIR, f"{stem}.json.script_checkpoint.json")
        if os.path.exists(ckpt):
            try:
                os.remove(ckpt)
            except OSError:
                pass


@app.post("/api/generate_script/batch/start")
async def generate_script_batch_start(request: BatchScriptRequest, background_tasks: BackgroundTasks):
    """Process multiple text/EPUB files sequentially through generate_script.py."""
    check_global_gpu_lock("batch_script")
    if not request.tasks:
        raise HTTPException(status_code=400, detail="No files provided.")

    resume = bool(request.resume)
    done_filenames = set()
    if resume:
        prev = safe_load_json(BATCH_SCRIPT_STATE_PATH)
        if isinstance(prev, dict):
            done_filenames = {f["filename"] for f in prev.get("files", [])
                              if isinstance(f, dict) and f.get("status") == "done"}
    else:
        _clear_batch_script_state(request.tasks)

    def _run():
        state = process_state["batch_script"]
        _init_batch_state(state,
                          [f"Starting batch of {len(request.tasks)} file(s)..."],
                          [{"filename": t.filename,
                            "status": "done" if t.filename in done_filenames else "pending"}
                           for t in request.tasks])
        _save_batch_script_state(state)

        # One full on-disk log for the whole batch (in-memory list is a capped tail)
        log_path = _init_task_log("batch_script")

        for i, task in enumerate(request.tasks):
            if state["cancel"]:
                state["logs"].append("Batch cancelled.")
                break

            state["current_task_idx"] = i
            if state["tasks"][i]["status"] == "done":
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — already done: {task.filename}")
                continue
            state["tasks"][i]["status"] = "running"

            # Resolve upload path — handle epub→txt conversion
            safe_filename = secure_filename(task.filename)
            if not safe_filename:
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — invalid filename: {task.filename}")
                state["tasks"][i]["status"] = "failed"
                _save_batch_script_state(state)
                continue
            input_path = os.path.join(UPLOADS_DIR, safe_filename)
            if not os.path.exists(input_path):
                stem, ext = os.path.splitext(safe_filename)
                if ext.lower() == ".epub":
                    txt_path = os.path.join(UPLOADS_DIR, stem + ".txt")
                    if os.path.exists(txt_path):
                        input_path = txt_path
            if not os.path.exists(input_path):
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — file not found: {task.filename}")
                state["tasks"][i]["status"] = "failed"
                _save_batch_script_state(state)
                continue

            stem = os.path.splitext(os.path.basename(input_path))[0]
            safe_stem = secure_filename(stem) or f"batch_{i+1}"
            output_path = os.path.join(SCRIPTS_DIR, f"{safe_stem}.json")

            state["logs"].append(f"--- [{i+1}/{len(request.tasks)}] {task.filename} ---")

            cmd = [
                sys.executable, "-u",
                os.path.join(BASE_DIR, "generate_script.py"),
                input_path,
                "--output", output_path,
            ]
            if resume:
                cmd.append("--resume")
            rc, _ = _stream_subprocess_to_logs(cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ", log_file=log_path)

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                _save_batch_script_state(state)
                break
            elif rc == 0:
                state["tasks"][i]["status"] = "done"
                state["tasks"][i]["saved_as"] = safe_stem
                state["logs"].append(f"[{i+1}] Saved as '{safe_stem}' in Scripts library.")
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{i+1}] Failed (exit {rc}): {task.filename}")
            _save_batch_script_state(state)

        state["running"] = False
        if not state["cancel"]:
            _clear_batch_script_state(request.tasks)
        state["logs"].append("Batch script generation finished.")

    def _run_guarded():
        # Release the GPU lock even if _run raises before its own cleanup (see
        # batch_review for rationale); crash leaves the checkpoint intact.
        try:
            _run()
        except Exception as e:
            logger.exception(f"batch_script task crashed: {e}")
            process_state["batch_script"]["logs"].append(f"Batch script error: {e}")
        finally:
            process_state["batch_script"]["running"] = False

    claim_gpu_task("batch_script")
    background_tasks.add_task(_run_guarded)
    return {"status": "started", "task_count": len(request.tasks), "resume": resume}


@app.get("/api/generate_script/batch/checkpoint")
async def generate_script_batch_checkpoint():
    """Detect an unfinished batch generation (read-only)."""
    prev = safe_load_json(BATCH_SCRIPT_STATE_PATH)
    if not isinstance(prev, dict) or not prev.get("files"):
        return {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}
    files = prev["files"]
    done = sum(1 for f in files if f.get("status") == "done")
    total = len(files)
    return {"exists": done < total, "done": done, "total": total,
            "label": f"{done}/{total} files", "mode": {"files": files}}


@app.post("/api/generate_script/batch/cancel")
async def generate_script_batch_cancel():
    return _batch_cancel_helper("batch_script")


@app.post("/api/generate_script/batch/pause")
async def generate_script_batch_pause():
    return _pause_task("batch_script", "No batch script generation is currently running.",
                        "Batch script generation is starting up, retry in a moment.",
                        "Batch script generation")


@app.post("/api/generate_script/batch/resume")
async def generate_script_batch_resume():
    return _resume_task("batch_script", "No batch script generation is currently running.",
                         "Batch script generation")


@app.get("/api/annotated_script")
async def get_annotated_script():
    """Return the current working annotated_script.json."""
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=404, detail="No annotated script found")
    with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

@app.get("/api/status/{task_name}")
async def get_status(task_name: str):
    if task_name not in process_state:
        raise HTTPException(status_code=404, detail="Task not found")
    state = dict(process_state[task_name])
    state.pop("process", None)
    return state


@app.get("/api/logs/{task_name}")
async def get_task_log(task_name: str, download: bool = False):
    """Serve the complete on-disk log for a task (the in-memory status only keeps a
    capped tail). Use ?download=true to download the file."""
    if task_name not in process_state:
        raise HTTPException(status_code=404, detail="Task not found")
    log_path = _task_log_path(task_name)
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="No log file for this task yet.")
    filename = f"{task_name}.log"
    return FileResponse(
        log_path,
        media_type="text/plain",
        filename=filename if download else None,
    )

@app.get("/api/voices")
async def get_voices():
    # Parse voices directly from the current script (no stale cache)
    voices_list = []
    if os.path.exists(SCRIPT_PATH):
        try:
            with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
                script_data = json.load(f)
            voices_set = set()
            for entry in script_data:
                speaker = (entry.get("speaker") or entry.get("type") or "").strip()
                if speaker:
                    voices_set.add(speaker)
            voices_list = sorted(voices_set)
        except (json.JSONDecodeError, ValueError):
            pass

    if not voices_list:
        return []

    # Combine with config
    voice_config = {}
    if os.path.exists(VOICE_CONFIG_PATH):
        try:
            with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                voice_config = json.load(f)
        except (json.JSONDecodeError, ValueError):
            voice_config = {}

    missing_speakers = {voice_name for voice_name in voices_list if voice_name not in voice_config}

    result = []
    for voice_name in voices_list:
        config = voice_config.get(voice_name, {})
        result.append({
            "name": voice_name,
            "config": config,
            "persona_pending": voice_name in missing_speakers
        })
    return result


@app.post("/api/generate_personas")
async def generate_personas(background_tasks: BackgroundTasks, request: GeneratePersonasRequest = GeneratePersonasRequest()):
    """Generate LLM-derived voice persona descriptions and VoiceDesign previews.

    This runs `app/generate_personas.py` which:
    - reads `annotated_script.json`,
    - asks the configured LLM to produce a short `description` and `ref_text` for each character,
    - uses the VoiceDesign model to synthesize a preview and saves it,
    - updates `voice_config.json` with a clone-style reference for each character.
    """
    check_global_gpu_lock("persona")

    process_state["persona"]["cancel"] = False

    # Unload TTS engine to free GPU for the subprocess
    if project_manager.engine is not None:
        logger.info("Unloading TTS engine for persona generation...")
        project_manager.engine = None
        gc.collect()

    command = [sys.executable, "-u", "generate_personas.py"]
    if request.advanced:
        batch_size = max(1, min(int(request.batch_size or 40), 200))
        command.extend(["--advanced", "--batch-size", str(batch_size)])
    claim_gpu_task("persona")
    background_tasks.add_task(run_process, command, "persona")
    return {"status": "started", "advanced": request.advanced}


@app.post("/api/cancel_persona")
async def cancel_persona():
    if not process_state["persona"]["running"]:
        return {"status": "idle"}

    process_state["persona"]["cancel"] = True
    process_state["persona"]["logs"].append("[CANCEL] Cancellation requested")

    proc = process_state["persona"].get("process")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception as e:
            logger.warning(f"Failed to terminate persona process cleanly: {e}")

    return {"status": "cancelling"}

@app.post("/api/save_voice_config")
async def save_voice_config(config_data: Dict[str, VoiceConfigItem]):
    def _save():
        # Hold the lock across the read-modify-write so this can't race a batch
        # review's concurrent speaker-rename remap of the same file.
        with file_lock(VOICE_CONFIG_PATH):
            current_config = {}
            if os.path.exists(VOICE_CONFIG_PATH):
                with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                    try:
                        current_config = json.load(f)
                    except (json.JSONDecodeError, ValueError):
                        pass

            # Update current config with new data
            for voice_name, config in config_data.items():
                # Convert Pydantic model to dict
                current_config[voice_name] = config.model_dump()

            atomic_json_write(current_config, VOICE_CONFIG_PATH)

    # Offload to a worker thread so file_lock's wait loop can't block the event loop.
    try:
        await asyncio.to_thread(_save)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice config is busy (locked by another operation); please try again.")

    return {"status": "saved"}


# --- Auto-suggest best LoRA voice per character -------------------------------

def _infer_lora_gender(model):
    """Best-effort gender for a LoRA candidate: explicit field, then name suffix,
    then description keywords, then mean f0 from voice_features."""
    g = (model.get("gender") or "").strip().lower()
    if g in ("male", "female"):
        return g
    name_id = f"{model.get('name', '')} {model.get('id', '')}".lower()
    if re.search(r"(_|\b)f(\b|_|\d|emale)", name_id):
        return "female"
    if re.search(r"(_|\b)m(\b|_|\d|ale)", name_id):
        return "male"
    desc = (model.get("description") or model.get("voice_profile") or "").lower()
    if any(w in desc for w in ("alto", "soprano", "mezzo", "feminine", "woman", "girl")):
        return "female"
    if any(w in desc for w in ("baritone", "tenor", "bass", "masculine", "man", "boy")):
        return "male"
    f0 = (model.get("voice_features") or {}).get("mean_f0")
    if isinstance(f0, (int, float)) and f0 > 0:
        return "female" if f0 >= 165 else "male"
    return "unknown"


def _infer_character_gender(text):
    """Rough gender guess for a character from persona/style/sample text via pronoun counts."""
    t = (text or "").lower()
    male = len(re.findall(r"\b(he|him|his|himself|man|men|boy|male|sir|mr|lord|king|father)\b", t))
    female = len(re.findall(r"\b(she|her|hers|herself|woman|women|girl|female|lady|mrs|ms|miss|queen|mother)\b", t))
    if male > female and male > 0:
        return "male"
    if female > male and female > 0:
        return "female"
    return "unknown"


def _build_lora_candidates():
    """Downloaded built-in + user-trained adapters with normalized fields for matching."""
    candidates = []
    for m in _load_builtin_lora_manifest():
        if not m.get("downloaded", False):
            continue
        candidates.append({
            "adapter_id": m["id"],
            "name": m.get("name") or m["id"],
            "type": "builtin_lora",
            "gender": _infer_lora_gender(m),
            "description": m.get("description") or m.get("voice_profile") or "",
        })
    for m in _load_manifest(LORA_MODELS_MANIFEST):
        candidates.append({
            "adapter_id": m["id"],
            "name": m.get("name") or m["id"],
            "type": "lora",
            "gender": _infer_lora_gender(m),
            "description": m.get("description") or m.get("voice_profile") or "",
        })
    return candidates


def _heuristic_match(char_profile, candidates, preferred_gender=None):
    """Gender-filter then keyword-overlap rank. Returns (candidate, reason) or (None, reason).
    `preferred_gender` (e.g. inferred from an explicit gender word in the character name)
    overrides the pronoun-count guess when provided."""
    if not candidates:
        return None, "No downloaded LoRA voices available"
    
    # Determine gender: use preferred if known, otherwise infer from profile
    cgender = preferred_gender if preferred_gender in ("male", "female") else _infer_character_gender(char_profile)
    
    # If still unknown after inference, skip gender filtering entirely
    pool = candidates
    if cgender != "unknown":
        gender_match = [c for c in candidates if c["gender"] == cgender]
        if gender_match:
            pool = gender_match
        else:
            # No matches for inferred gender - fall back to all candidates
            cgender = "unknown"
    words = set(re.findall(r"[a-z]{4,}", char_profile.lower()))
    best, best_score = None, -1
    for c in pool:
        cwords = set(re.findall(r"[a-z]{4,}", c["description"].lower()))
        score = len(words & cwords)
        if score > best_score:
            best, best_score = c, score
    if best is None:
        return None, "No candidate after filtering"
    reason = f"Heuristic match ({cgender or 'unknown'} gender)"
    if best_score > 0:
        reason += f", {best_score} description keyword(s) in common"
    return best, reason


@app.post("/api/suggest_voices")
async def suggest_voices(request: SuggestVoicesRequest = SuggestVoicesRequest()):
    """Suggest the best-matching downloaded LoRA voice for each character based on
    the character's dialogue + persona, ranked by the configured LLM (heuristic fallback).
    
    Offloaded to threadpool via asyncio.to_thread to avoid blocking the event loop."""
    # Reserve the GPU slot for the duration of the (local-LLM) suggestion so it
    # can't run concurrently with TTS/review and trigger a VRAM OOM. Released in
    # finally since this is a synchronous request, not a run_process task.
    claim_gpu_task("voices")
    try:
        return await asyncio.to_thread(_suggest_voices_impl, request)
    finally:
        process_state["voices"]["running"] = False


def _suggest_voices_impl(request: SuggestVoicesRequest):
    # Sync implementation that makes a blocking LLM call and file I/O.
    # Called via asyncio.to_thread from the async endpoint above.
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=400, detail="No script found. Generate a script first.")

    try:
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            script = json.load(f)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Script is not valid JSON.")

    # Collect per-character sample dialogue lines (in order, deduped)
    samples = {}
    for entry in script:
        speaker = (entry.get("speaker") or entry.get("type") or "").strip()
        text = (entry.get("text") or "").strip()
        if not speaker or not text:
            continue
        lines = samples.setdefault(speaker, [])
        if text not in lines and len(lines) < max(1, min(int(request.max_lines or 8), 30)):
            lines.append(text)
    if not samples:
        return {"method": "none", "suggestions": {}, "message": "No characters found in script."}

    # Existing config (for persona descriptions/styles + only_unset filtering)
    voice_config = {}
    if os.path.exists(VOICE_CONFIG_PATH):
        try:
            with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                voice_config = json.load(f)
        except (json.JSONDecodeError, ValueError):
            voice_config = {}

    candidates = _build_lora_candidates()
    if not candidates:
        raise HTTPException(status_code=400, detail="No downloaded LoRA voices available. Download a built-in voice or train an adapter first.")

    # Build per-character profile text (persona description/style + sample lines)
    characters = {}
    for speaker, lines in samples.items():
        if request.only_unset:
            existing = voice_config.get(speaker, {})
            if voice_category(existing) == "lora" and existing.get("adapter_id"):
                continue
        cfg = voice_config.get(speaker, {})
        persona_bits = [cfg.get("description") or "", cfg.get("character_style") or "", cfg.get("default_style") or ""]
        profile = " ".join(b for b in persona_bits if b)
        characters[speaker] = {"profile": profile, "lines": lines}

    if not characters:
        return {"method": "none", "suggestions": {}, "message": "No characters to suggest (all already set)."}

    cand_by_id = {c["adapter_id"]: c for c in candidates}
    suggestions = {}
    method = "heuristic"
    llm_warning = None

    # --- Try LLM ranking first ---
    llm_ok = False
    try:
        # don't let a stuck model hang the worker thread forever
        client, model_name = _make_llm_client(timeout=120)

        voice_catalog = "\n".join(
            f'- id="{c["adapter_id"]}" | name="{c["name"]}" | gender={c["gender"]} | description: {c["description"] or "(none)"}'
            for c in candidates
        )
        char_block = "\n\n".join(
            f'CHARACTER: {name}\nPersona/style: {info["profile"] or "(none)"}\nSample lines:\n'
            + "\n".join(f'  - "{ln}"' for ln in info["lines"][:8])
            for name, info in characters.items()
        )
        system_prompt = (
            "You are a casting director matching narrated audiobook characters to available LoRA TTS voices. "
            "For each character, pick the single best-fitting voice id from the catalog, considering gender, "
            "age, timbre, and personality implied by the character's dialogue and persona. "
            "Only choose from the provided voice ids. Respond with ONLY a JSON object mapping each character "
            'name to {"adapter_id": "<id>", "reason": "<short reason>"}. No prose, no markdown.'
        )
        user_prompt = f"AVAILABLE VOICES:\n{voice_catalog}\n\nCHARACTERS:\n{char_block}\n\nReturn the JSON object now."

        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            timeout=120,  # Hard timeout on the actual API call to prevent hanging
        )
        raw = response.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(m.group(0)) if m else {}

        for name in characters:
            pick = parsed.get(name) if isinstance(parsed, dict) else None
            if isinstance(pick, dict) and pick.get("adapter_id") in cand_by_id:
                c = cand_by_id[pick["adapter_id"]]
                suggestions[name] = {
                    "adapter_id": c["adapter_id"],
                    "adapter_name": c["name"],
                    "type": c["type"],
                    "reason": (pick.get("reason") or "").strip()[:240] or "LLM recommendation",
                }
        if suggestions:
            llm_ok = True
            method = "llm"
    except LLMConfigError as e:
        # Config issue (e.g. base_url rejected by _validate_local_llm_base_url) -
        # surface to the UI instead of silently falling back to heuristic.
        llm_warning = str(e)
    except Exception as e:
        logger.warning(f"LLM voice suggestion failed, falling back to heuristic: {e}")

    # --- Heuristic fallback for any character the LLM didn't cover ---
    for name, info in characters.items():
        if name in suggestions:
            continue
        profile_text = " ".join([name, info["profile"]] + info["lines"][:5])
        # An explicit gender word in the character's name/label is authoritative
        name_gender = _infer_character_gender(name)
        best, reason = _heuristic_match(profile_text, candidates, preferred_gender=name_gender)
        if best:
            suggestions[name] = {
                "adapter_id": best["adapter_id"],
                "adapter_name": best["name"],
                "type": best["type"],
                "reason": reason,
            }

    if not llm_ok and suggestions:
        method = "heuristic"

    return {"method": method, "suggestions": suggestions, "candidate_count": len(candidates), "llm_warning": llm_warning}


@app.get("/api/audiobook")
async def get_audiobook():
    if not os.path.exists(AUDIOBOOK_PATH):
        raise HTTPException(status_code=404, detail="Audiobook not found")
    return FileResponse(AUDIOBOOK_PATH, filename="audiobook.mp3", media_type="audio/mpeg")

# --- Chunk Management Endpoints ---

@app.get("/api/chunks")
async def get_chunks():
    chunks = project_manager.load_chunks()
    return chunks

class ChunkRestoreRequest(BaseModel):
    chunk: dict
    at_index: int

@app.post("/api/chunks/restore")
async def restore_chunk(request: ChunkRestoreRequest):
    """Re-insert a previously deleted chunk at a specific index."""
    chunks = project_manager.restore_chunk(request.at_index, request.chunk)
    if chunks is None:
        raise HTTPException(status_code=400, detail="Failed to restore chunk")
    return {"status": "ok", "total": len(chunks)}

@app.post("/api/chunks/{index}")
async def update_chunk(index: int, update: ChunkUpdate):
    updates = update.model_dump(exclude_unset=True)
    logger.info(f"Updating chunk {index} with data: {updates}")
    chunk = project_manager.update_chunk(index, updates)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    logger.info(f"Chunk {index} updated, instruct is now: '{chunk.get('instruct', '')}'")
    return chunk

@app.post("/api/chunks/{index}/insert")
async def insert_chunk(index: int):
    """Insert an empty chunk after the given index."""
    chunks = project_manager.insert_chunk(index)
    if chunks is None:
        raise HTTPException(status_code=404, detail="Invalid chunk index")
    return {"status": "ok", "total": len(chunks)}

@app.delete("/api/chunks/{index}")
async def delete_chunk(index: int):
    """Delete a chunk at the given index."""
    result = project_manager.delete_chunk(index)
    if result is None:
        raise HTTPException(status_code=400, detail="Cannot delete chunk (invalid index or last remaining chunk)")
    deleted, chunks = result
    return {"status": "ok", "deleted": deleted, "total": len(chunks)}

@app.post("/api/chunks/{index}/generate")
async def generate_chunk_endpoint(index: int, background_tasks: BackgroundTasks):
    chunks = project_manager.load_chunks()
    if not (0 <= index < len(chunks)):
        raise HTTPException(status_code=404, detail="Invalid chunk index")
    if not chunks[index].get("text", "").strip():
        raise HTTPException(status_code=400, detail="Cannot generate audio for an empty line")

    def task():
        project_manager.generate_chunk_audio(index)

    background_tasks.add_task(task)
    return {"status": "started"}

@app.post("/api/merge")
async def merge_audio_endpoint(background_tasks: BackgroundTasks):
    # Reuse audio process state for merge if possible, or just background it
    # For simplicity, we just background it and frontend will assume it works
    # Or we can link it to process_state["audio"]

    def task():
        process_state["audio"]["running"] = True
        process_state["audio"]["start_time"] = time.time()
        process_state["audio"]["logs"] = ["Starting merge..."]
        try:
            success, msg = project_manager.merge_audio()
            if success:
                process_state["audio"]["logs"].append(f"Merge complete: {msg}")
            else:
                process_state["audio"]["logs"].append(f"Merge failed: {msg}")
        except Exception as e:
            process_state["audio"]["logs"].append(f"Merge error: {e}")
        finally:
            process_state["audio"]["running"] = False

    background_tasks.add_task(task)
    return {"status": "started"}

@app.post("/api/export_audacity")
async def export_audacity_endpoint(background_tasks: BackgroundTasks):
    if process_state["audacity_export"]["running"]:
        raise HTTPException(status_code=400, detail="Audacity export already running")

    def task():
        process_state["audacity_export"]["running"] = True
        process_state["audacity_export"]["logs"] = ["Starting Audacity export..."]
        try:
            success, msg = project_manager.export_audacity()
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

@app.get("/api/export_audacity")
async def get_audacity_export():
    zip_path = os.path.join(ROOT_DIR, "audacity_export.zip")
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

@app.post("/api/merge_m4b")
async def merge_m4b_endpoint(request: M4bExportRequest, background_tasks: BackgroundTasks):
    if process_state["m4b_export"]["running"]:
        raise HTTPException(status_code=400, detail="M4B export already running")

    def task():
        process_state["m4b_export"]["running"] = True
        process_state["m4b_export"]["logs"] = ["Starting M4B export..."]
        try:
            meta = {
                "title": request.title,
                "author": request.author,
                "narrator": request.narrator,
                "year": request.year,
                "description": request.description,
                "cover_path": os.path.join(ROOT_DIR, "m4b_cover.jpg") if os.path.exists(os.path.join(ROOT_DIR, "m4b_cover.jpg")) else "",
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

@app.get("/api/audiobook_m4b")
async def get_audiobook_m4b():
    if not os.path.exists(M4B_PATH):
        raise HTTPException(status_code=404, detail="M4B audiobook not found. Export it first.")
    return FileResponse(M4B_PATH, filename="audiobook.m4b", media_type="audio/mp4")

@app.post("/api/m4b_cover")
async def upload_m4b_cover(file: UploadFile = File(...)):
    """Upload a cover image for M4B export."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    cover_path = os.path.join(ROOT_DIR, "m4b_cover.jpg")
    content = await file.read()
    with open(cover_path, "wb") as f:
        f.write(content)
    return {"status": "uploaded", "path": cover_path}

@app.delete("/api/m4b_cover")
async def delete_m4b_cover():
    """Remove the uploaded cover image."""
    cover_path = os.path.join(ROOT_DIR, "m4b_cover.jpg")
    if os.path.exists(cover_path):
        os.remove(cover_path)
    return {"status": "removed"}

@app.post("/api/generate_batch")
async def generate_batch_endpoint(request: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """Generate multiple chunks in parallel using configured worker count."""
    check_global_gpu_lock("audio")

    # Load worker count from config
    workers = 2
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                workers = max(1, cfg.get("tts", {}).get("parallel_workers", 2))
        except (json.JSONDecodeError, ValueError):
            pass

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
        process_state["audio"]["cancel"] = False
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
            logger.error(f"Batch generation error: {e}")
            process_state["audio"]["logs"].append(f"Batch generation error: {e}")
        finally:
            process_state["audio"]["running"] = False
            process_state["audio"]["cancel"] = False

    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started", "workers": workers, "total_chunks": total}

@app.post("/api/generate_batch_fast")
async def generate_batch_fast_endpoint(request: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """Generate multiple chunks using batch TTS API with single seed. Faster but less flexible.
    Requires custom Qwen3-TTS with /generate_batch endpoint."""
    check_global_gpu_lock("audio")

    # Load batch_seed and batch_size from config
    batch_seed = -1
    batch_size = 4
    batch_group_by_type = False
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                tts_cfg = cfg.get("tts", {})
                seed_val = tts_cfg.get("batch_seed")
                if seed_val is not None and seed_val != "":
                    batch_seed = int(seed_val)
                batch_size = max(1, tts_cfg.get("parallel_workers", 4))
                batch_group_by_type = tts_cfg.get("batch_group_by_type", False)
        except (json.JSONDecodeError, ValueError):
            pass

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
        process_state["audio"]["cancel"] = False
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
            logger.error(f"Batch generation error: {e}")
            process_state["audio"]["logs"].append(f"Batch generation error: {e}")
        finally:
            process_state["audio"]["running"] = False
            process_state["audio"]["cancel"] = False

    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started", "batch_seed": batch_seed, "batch_size": batch_size, "total_chunks": total}

@app.post("/api/cancel_audio")
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

@app.get("/api/reports")
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


@app.get("/api/reports/{filename}")
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


def _summarize_script_checkpoint(path: str) -> Optional[dict]:
    """Uniform detect descriptor for a *.script_checkpoint.json. None if unusable."""
    data = safe_load_json(path)
    if not isinstance(data, dict) or "completed_chunks" not in data:
        return None
    done = data.get("completed_chunks", 0) or 0
    total = data.get("total_chunks", 0) or 0
    return {
        "exists": True,
        "done": done,
        "total": total,
        "label": f"{done}/{total} chunks",
        "mode": {},
        "mtime": os.path.getmtime(path) if os.path.exists(path) else None,
    }


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


@app.get("/api/review/checkpoints")
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


@app.get("/api/scripts")
async def list_saved_scripts():
    """List all saved scripts in the scripts/ directory.

    Uses a whitelist approach: only includes .json files that do NOT end with
    any known companion/internal suffix (voice_config, review_checkpoint, etc.).
    Any new companion file type added later is safely excluded by this rule.
    """
    scripts = []
    companion_suffixes = (".voice_config.json", ".review_checkpoint.json", ".checkpoint.jsonl")
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

@app.post("/api/scripts/save")
async def save_script(request: ScriptSaveRequest):
    """Save the current annotated_script.json (and voice_config.json) under a name."""
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=404, detail="No annotated script to save. Generate a script first.")

    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid script name.")

    # Claim a non-colliding path so saving a name that sanitizes to an existing
    # file (or re-saving the same name) auto-numbers instead of silently
    # overwriting another saved book. Derive the companion voice_config name
    # from the claimed stem so the two never mismatch, and avoid a stem whose
    # companion already exists as an orphan (else the copy below clobbers it).
    dest = _claim_unique_path(SCRIPTS_DIR, f"{safe_name}.json",
                              sibling_suffixes=(".voice_config.json",))
    claimed_name = os.path.splitext(os.path.basename(dest))[0]
    shutil.copy2(SCRIPT_PATH, dest)

    companion = os.path.join(SCRIPTS_DIR, f"{claimed_name}.voice_config.json")
    if os.path.exists(VOICE_CONFIG_PATH):
        shutil.copy2(VOICE_CONFIG_PATH, companion)
    else:
        # _claim_unique_path reserved an empty companion placeholder; with no
        # voice_config to write, remove it so we don't leave an empty orphan.
        try:
            os.remove(companion)
        except OSError:
            pass

    logger.info(f"Script saved as '{claimed_name}'")
    return {"status": "saved", "name": claimed_name}

class ScriptLoadRequest(BaseModel):
    name: str

@app.post("/api/scripts/load")
async def load_script(request: ScriptLoadRequest):
    """Load a saved script, replacing the current annotated_script.json and chunks."""
    if process_state["audio"]["running"]:
        raise HTTPException(status_code=409, detail="Cannot load a script while audio generation is running.")

    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid script name.")

    src = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail=f"Saved script '{request.name}' not found.")

    shutil.copy2(src, SCRIPT_PATH)

    companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
    if os.path.exists(companion):
        shutil.copy2(companion, VOICE_CONFIG_PATH)

    # Delete chunks so they regenerate from the loaded script
    if os.path.exists(CHUNKS_PATH):
        os.remove(CHUNKS_PATH)

    logger.info(f"Script '{request.name}' loaded")
    return {"status": "loaded", "name": request.name}

@app.delete("/api/scripts/{name}")
async def delete_script(name: str):
    """Delete a saved script."""
    safe_name = secure_filename(name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid script name.")

    filepath = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")

    os.remove(filepath)
    companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
    if os.path.exists(companion):
        os.remove(companion)
    checkpoint = _checkpoint_path(filepath)
    if os.path.exists(checkpoint):
        os.remove(checkpoint)

    logger.info(f"Script '{name}' deleted")
    return {"status": "deleted", "name": name}

## ── Series Voice Library (cross-book cast) ──────────────────────

# Character names that belong to the shared cross-series pool by default
# (a series usually keeps the same narrator unless it explicitly uses a different one).
SHARED_DEFAULT_NAMES = {"narrator"}


def _norm_name(name: str) -> str:
    """Normalize a character name for matching: lowercase, trimmed, collapsed spaces."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _name_similarity(a: str, b: str) -> float:
    """Similarity in [0,1] combining sequence ratio and token overlap on normalized names."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0
    # Containment bonus: "kenji" vs "kenji sato"
    contain = 1.0 if (ta and tb and (ta <= tb or tb <= ta)) else 0.0
    return max(ratio, jaccard, contain * 0.9)


def _load_voice_library() -> dict:
    lib = {"shared": {}, "casts": {}}
    if os.path.exists(VOICE_LIBRARY_PATH):
        try:
            with open(VOICE_LIBRARY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                lib["shared"] = data.get("shared", {}) or {}
                lib["casts"] = data.get("casts", {}) or {}
        except (json.JSONDecodeError, ValueError):
            pass
    return lib


def _save_voice_library(lib: dict):
    """Save voice library with file_lock to prevent race conditions."""
    try:
        with file_lock(VOICE_LIBRARY_PATH):
            atomic_json_write(lib, VOICE_LIBRARY_PATH)
    except TimeoutError as e:
        logger.warning(f"Could not acquire lock to save voice library: {e}")
        raise  # Re-raise so caller knows the save failed


async def _save_voice_library_async(lib: dict):
    """Offload _save_voice_library to a worker thread so file_lock's wait loop
    can't block the event loop; turns lock-contention timeouts into a 503."""
    try:
        await asyncio.to_thread(_save_voice_library, lib)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice library is busy (locked by another operation); please try again.")


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


def _cast_match_pool(lib: dict, cast_name: str) -> dict:
    """Build the candidate pool for matching against a cast: shared first, cast
    members override on key collision (a cast-specific narrator beats the
    shared narrator = "different narrator")."""
    pool = {}
    for k, m in lib["shared"].items():
        pool[k] = {"key": k, "name": m.get("name", k), "source": "shared",
                   "type": (m.get("config") or {}).get("type")}
    for k, m in lib["casts"][cast_name].get("members", {}).items():
        pool[k] = {"key": k, "name": m.get("name", k), "source": "cast",
                   "type": (m.get("config") or {}).get("type")}
    return pool


def _build_match_proposals(counts: Dict[str, int], pool: dict) -> List[dict]:
    """Fuzzy-match each character in `counts` against `pool`, returning proposals
    sorted by line count descending. Shared by /match and /match_bulk."""
    proposals = []
    for char in sorted(counts, key=lambda n: counts[n], reverse=True):
        best, best_score = None, 0.0
        for cand in pool.values():
            score = _name_similarity(char, cand["name"])
            if score > best_score:
                best, best_score = cand, score
        match = None
        if best and best_score >= 0.6:
            match = {
                "key": best["key"], "name": best["name"], "source": best["source"],
                "type": best["type"], "score": round(best_score, 3),
                "exact": best_score >= 0.999,
            }
        proposals.append({"character": char, "line_count": counts[char], "match": match})
    return proposals


def _apply_cast_mapping(lib: dict, cast_name: str, mapping: Dict[str, str],
                         current_config: dict, chars: Optional[dict] = None) -> Tuple[dict, List[str]]:
    """Apply a confirmed character -> library member mapping onto a voice_config
    dict, returning a new dict (the input is not mutated) along with the list
    of characters that were actually applied.

    If `chars` is given (the per-speaker line counts of a specific book), only
    characters present in it are considered — used by the bulk endpoint so a
    book only receives entries for characters that actually appear in it."""
    def resolve_entry(key):
        # cast members win over shared on collision
        return lib["casts"][cast_name].get("members", {}).get(key) or lib["shared"].get(key)

    result = dict(current_config)
    applied = []
    for char, key in mapping.items():
        if chars is not None and char not in chars:
            continue
        entry = resolve_entry(key)
        if not entry:
            continue
        cfg = dict(entry.get("config") or {})
        cfg.pop("alias_of", None)
        # Preserve an existing alias_of on the current character (book-specific)
        if isinstance(result.get(char), dict) and result[char].get("alias_of"):
            cfg["alias_of"] = result[char]["alias_of"]
        result[char] = cfg
        applied.append(char)
    return result, applied


def _apply_cast_to_config_file(config_path: str, lib: dict, cast_name: str,
                                mapping: Dict[str, str], chars: Optional[dict] = None) -> List[str]:
    """Load a voice_config.json (if present), apply the cast mapping under a file
    lock, write it back atomically if anything changed, and return the list of
    characters that were applied.

    Raises TimeoutError if the lock can't be acquired - callers should map that
    to a 503 (single-book) or a per-book error entry (bulk).
    """
    with file_lock(config_path):
        current_config = safe_load_json(config_path, default={})

        current_config, applied = _apply_cast_mapping(lib, cast_name, mapping, current_config, chars=chars)

        if applied:
            atomic_json_write(current_config, config_path)
    return applied


def _make_library_entry(display_name: str, config: dict, line_count: int) -> dict:
    cfg = dict(config or {})
    cfg.pop("alias_of", None)  # aliases are book-specific; don't carry across books
    return {
        "name": display_name,
        "config": cfg,
        "line_count": line_count,
        "saved_at": time.time(),
    }


@app.get("/api/voice_library")
async def voice_library_get():
    """Return the full library plus the current book's characters with line counts."""
    lib = _load_voice_library()
    counts = _script_line_counts()

    casts = []
    for cast_name, cast in sorted(lib["casts"].items()):
        members = cast.get("members", {})
        casts.append({
            "name": cast_name,
            "member_count": len(members),
            "members": [
                {"key": k, "name": m.get("name", k), "type": (m.get("config") or {}).get("type"),
                 "line_count": m.get("line_count", 0)}
                for k, m in sorted(members.items())
            ],
        })

    shared = [
        {"key": k, "name": m.get("name", k), "type": (m.get("config") or {}).get("type"),
         "line_count": m.get("line_count", 0)}
        for k, m in sorted(lib["shared"].items())
    ]

    current_characters = [
        {"name": name, "line_count": counts[name]}
        for name in sorted(counts, key=lambda n: counts[n], reverse=True)
    ]

    return {"casts": casts, "shared": shared, "current_characters": current_characters}


@app.post("/api/voice_library/casts")
async def voice_library_create_cast(request: CastCreateRequest):
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Cast name is required.")
    if name == "__shared__":
        # Reserved sentinel: other endpoints treat this name as the global
        # shared pool, so a real cast by this name would be unaddressable.
        raise HTTPException(status_code=400, detail="'__shared__' is a reserved name.")
    lib = _load_voice_library()
    if name in lib["casts"]:
        raise HTTPException(status_code=409, detail=f"Cast '{name}' already exists.")
    lib["casts"][name] = {"members": {}}
    await _save_voice_library_async(lib)
    return {"status": "created", "name": name}


@app.delete("/api/voice_library/casts/{cast}")
async def voice_library_delete_cast(cast: str):
    lib = _load_voice_library()
    if cast not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast}' not found.")
    del lib["casts"][cast]
    await _save_voice_library_async(lib)
    return {"status": "deleted", "name": cast}


@app.delete("/api/voice_library/casts/{cast}/members/{key}")
async def voice_library_delete_member(cast: str, key: str):
    lib = _load_voice_library()
    if cast == "__shared__":
        pool = lib["shared"]
    else:
        if cast not in lib["casts"]:
            raise HTTPException(status_code=404, detail=f"Cast '{cast}' not found.")
        pool = lib["casts"][cast].setdefault("members", {})
    if key not in pool:
        raise HTTPException(status_code=404, detail=f"Member '{key}' not found.")
    del pool[key]
    await _save_voice_library_async(lib)
    return {"status": "deleted", "cast": cast, "key": key}


@app.post("/api/voice_library/save")
async def voice_library_save(request: LibrarySaveRequest):
    """Save selected current-book characters into a cast (NARRATOR -> shared by default)."""
    cast_name = request.cast.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found. Create it first.")

    voice_config = {}
    if os.path.exists(VOICE_CONFIG_PATH):
        try:
            with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                voice_config = json.load(f)
        except (json.JSONDecodeError, ValueError):
            voice_config = {}

    counts = _script_line_counts()
    shared_override = {_norm_name(n) for n in (request.shared or [])}
    cast_specific = {_norm_name(n) for n in (request.cast_specific or [])}

    saved = {"cast": [], "shared": []}
    for char in request.characters:
        config = voice_config.get(char)
        if not config:
            continue  # nothing configured for this character; skip
        key = _norm_name(char)
        entry = _make_library_entry(char, config, counts.get(char, 0))
        # Narrator (or explicitly flagged) goes to the shared cross-series pool,
        # unless this book uses a different narrator (forced cast-specific).
        is_shared = (key in SHARED_DEFAULT_NAMES or key in shared_override) and key not in cast_specific
        if is_shared:
            lib["shared"][key] = entry
            saved["shared"].append(char)
        else:
            lib["casts"][cast_name].setdefault("members", {})[key] = entry
            saved["cast"].append(char)

    await _save_voice_library_async(lib)
    return {"status": "saved", "cast": cast_name, "saved": saved}


@app.post("/api/voice_library/match")
async def voice_library_match(request: CastCreateRequest):
    """Fuzzy-match the current book's characters against a cast (+shared pool).
    Returns proposals for the user to confirm before applying. `name` = cast name."""
    cast_name = request.name.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

    pool = _cast_match_pool(lib, cast_name)

    counts = _script_line_counts()
    if not counts:
        raise HTTPException(status_code=400, detail="No characters in the current book. Generate a script first.")

    proposals = _build_match_proposals(counts, pool)

    return {"cast": cast_name, "proposals": proposals}


@app.post("/api/voice_library/match_bulk")
async def voice_library_match_bulk(request: CastMatchBulkRequest):
    """Fuzzy-match the union of characters across several saved books against a
    cast (+shared pool). Same proposal shape as /api/voice_library/match, but
    `line_count` is the sum across all selected books."""
    cast_name = request.name.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

    pool = _cast_match_pool(lib, cast_name)

    def _collect_counts():
        counts = {}
        for name in request.script_names:
            safe_name = secure_filename(name)
            if not safe_name:
                continue
            script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
            for char, n in _script_line_counts(script_path).items():
                counts[char] = counts.get(char, 0) + n
        return counts

    # Offload the per-book file reads to a worker thread so reading a large
    # series doesn't block the event loop (and other in-flight requests).
    counts = await asyncio.to_thread(_collect_counts)

    if not counts:
        raise HTTPException(status_code=400, detail="No characters found in the selected books.")

    proposals = _build_match_proposals(counts, pool)

    return {"cast": cast_name, "proposals": proposals, "book_count": len(request.script_names)}


@app.post("/api/voice_library/apply")
async def voice_library_apply(request: LibraryApplyRequest):
    """Apply confirmed cast members onto the current voice_config by the given mapping."""
    cast_name = request.cast.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

    # Offload to a worker thread so file_lock's wait loop can't block the event loop.
    # Hold the lock across the read-modify-write so this can't race a batch
    # review's concurrent speaker-rename remap of the same file.
    try:
        applied = await asyncio.to_thread(
            _apply_cast_to_config_file, VOICE_CONFIG_PATH, lib, cast_name, request.mapping)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice config is busy (locked by another operation); please try again.")

    return {"status": "applied", "cast": cast_name, "applied": applied, "count": len(applied)}


@app.post("/api/voice_library/apply_bulk")
async def voice_library_apply_bulk(request: LibraryApplyBulkRequest):
    """Apply confirmed cast members onto several saved books' voice_config.json
    files at once. Each book only receives entries for characters that actually
    appear in that book."""
    cast_name = request.cast.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

    def _apply_all():
        results = []
        for name in request.script_names:
            safe_name = secure_filename(name)
            if not safe_name:
                results.append({"name": name, "applied": [], "count": 0, "error": "Invalid script name"})
                continue
            chars = _script_line_counts(os.path.join(SCRIPTS_DIR, f"{safe_name}.json"))

            config_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
            # Hold the lock across the read-modify-write so this can't race a batch
            # review's concurrent speaker-rename remap of the same companion file.
            try:
                applied = _apply_cast_to_config_file(config_path, lib, cast_name, request.mapping, chars=chars)
            except TimeoutError as e:
                results.append({"name": name, "applied": [], "count": 0, "error": str(e)})
                continue

            results.append({"name": name, "applied": applied, "count": len(applied)})
        return results

    # Offload the per-book locking/read/write loop to a worker thread so
    # applying a cast to a long series doesn't block the event loop.
    results = await asyncio.to_thread(_apply_all)

    return {"cast": cast_name, "results": results}


## ── Voice Designer ──────────────────────────────────────────────

DESIGNED_VOICES_MANIFEST = os.path.join(DESIGNED_VOICES_DIR, "manifest.json")

def _load_manifest(path):
    """Load a JSON manifest file, returning [] on missing or corrupt file."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return []

def _save_manifest(path, manifest):
    """Write a JSON manifest file."""
    atomic_json_write(manifest, path)

@app.post("/api/voice_design/preview")
async def voice_design_preview(request: VoiceDesignPreviewRequest):
    """Generate a preview voice from a text description."""
    engine = project_manager.get_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    try:
        wav_path, sr = engine.generate_voice_design(
            description=request.description,
            sample_text=request.sample_text,
            language=request.language,
        )
        # Return relative URL for the static mount
        filename = os.path.basename(wav_path)
        return {"status": "ok", "audio_url": f"/designed_voices/previews/{filename}"}
    except Exception as e:
        logger.error(f"Voice design preview failed: {e}")
        raise HTTPException(status_code=500, detail="Voice design preview failed — see server logs for details.")

@app.post("/api/voice_design/save")
async def voice_design_save(request: VoiceDesignSaveRequest):
    """Save a preview voice as a permanent designed voice."""
    previews_dir = os.path.join(DESIGNED_VOICES_DIR, "previews")
    # Constrain to the previews dir so preview_file can't traverse out and copy
    # an arbitrary host file (e.g. ../../etc/passwd) into the web-served dir.
    preview_path = _safe_subpath(previews_dir, request.preview_file)

    if not os.path.exists(preview_path):
        raise HTTPException(status_code=404, detail="Preview file not found")

    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid voice name")

    # Generate unique ID
    voice_id = f"{safe_name}_{int(time.time())}"
    dest_filename = f"{voice_id}.wav"
    dest_path = os.path.join(DESIGNED_VOICES_DIR, dest_filename)

    shutil.copy2(preview_path, dest_path)

    # Update manifest
    manifest = _load_manifest(DESIGNED_VOICES_MANIFEST)
    manifest.append({
        "id": voice_id,
        "name": request.name,
        "description": request.description,
        "sample_text": request.sample_text,
        "filename": dest_filename,
    })
    _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)

    logger.info(f"Designed voice saved: '{request.name}' as {dest_filename}")
    return {"status": "saved", "voice_id": voice_id}

@app.get("/api/voice_design/list")
async def voice_design_list():
    """List all saved designed voices."""
    return _load_manifest(DESIGNED_VOICES_MANIFEST)

@app.delete("/api/voice_design/{voice_id}")
async def voice_design_delete(voice_id: str):
    """Delete a saved designed voice."""
    manifest = _load_manifest(DESIGNED_VOICES_MANIFEST)
    entry = next((v for v in manifest if v["id"] == voice_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Voice not found")

    # Delete WAV file
    wav_path = os.path.join(DESIGNED_VOICES_DIR, entry["filename"])
    if os.path.exists(wav_path):
        os.remove(wav_path)

    # Remove from manifest
    manifest = [v for v in manifest if v["id"] != voice_id]
    _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)

    logger.info(f"Designed voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}

## ── Clone Voice Uploads ───────────────────────────────────────

CLONE_VOICES_MANIFEST = os.path.join(CLONE_VOICES_DIR, "manifest.json")
ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg"}

@app.get("/api/clone_voices/list")
async def clone_voices_list():
    """List all uploaded clone voices."""
    return _load_manifest(CLONE_VOICES_MANIFEST)

@app.post("/api/clone_voices/upload")
async def clone_voices_upload(file: UploadFile = File(...)):
    """Upload an audio file for voice cloning."""
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported format. Use: {', '.join(ALLOWED_AUDIO_EXTS)}")

    base_name = os.path.splitext(file.filename)[0]
    safe_name = secure_filename(base_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    voice_id = f"{safe_name}_{int(time.time())}"
    dest_filename = f"{voice_id}{ext}"
    dest_path = os.path.join(CLONE_VOICES_DIR, dest_filename)

    async with aiofiles.open(dest_path, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)

    manifest = _load_manifest(CLONE_VOICES_MANIFEST)
    manifest.append({
        "id": voice_id,
        "name": base_name,
        "filename": dest_filename,
    })
    _save_manifest(CLONE_VOICES_MANIFEST, manifest)

    logger.info(f"Clone voice uploaded: '{base_name}' as {dest_filename}")
    return {"status": "uploaded", "voice_id": voice_id, "filename": dest_filename}

@app.delete("/api/clone_voices/{voice_id}")
async def clone_voices_delete(voice_id: str):
    """Delete an uploaded clone voice."""
    manifest = _load_manifest(CLONE_VOICES_MANIFEST)
    entry = next((v for v in manifest if v["id"] == voice_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Clone voice not found")

    wav_path = os.path.join(CLONE_VOICES_DIR, entry["filename"])
    if os.path.exists(wav_path):
        os.remove(wav_path)

    manifest = [v for v in manifest if v["id"] != voice_id]
    _save_manifest(CLONE_VOICES_MANIFEST, manifest)

    logger.info(f"Clone voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}

## ── LoRA Training ──────────────────────────────────────────────

LORA_MODELS_MANIFEST = os.path.join(LORA_MODELS_DIR, "manifest.json")

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

@app.post("/api/lora/upload_dataset")
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
        async with aiofiles.open(tmp_path, "wb") as out_file:
            content = await file.read()
            await out_file.write(content)

        os.makedirs(dataset_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_path, "r") as zf:
            _safe_extractall(zf, dataset_dir)

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
        missing_audio = []
        with open(metadata_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                sample_count += 1
                try:
                    entry = json.loads(line)
                    audio_rel = entry.get("audio_filepath") or entry.get("audio", "")
                    if audio_rel and not os.path.exists(os.path.join(dataset_dir, audio_rel)):
                        missing_audio.append(audio_rel)
                except (json.JSONDecodeError, KeyError):
                    pass

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

        return {"status": "uploaded", "dataset_id": dataset_name, "sample_count": sample_count}

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.post("/api/lora/generate_dataset")
async def lora_generate_dataset(request: LoraGenerateDatasetRequest, background_tasks: BackgroundTasks):
    """Generate a LoRA training dataset using Voice Designer.

    Generates multiple audio samples with the same voice description,
    saving them as a ready-to-train dataset.
    """
    check_global_gpu_lock("dataset_gen")

    # Build unified sample list from either format
    sample_list = []
    if request.samples:
        for s in request.samples:
            if s.text.strip():
                sample_list.append({"emotion": s.emotion.strip(), "text": s.text.strip()})
    elif request.texts:
        for t in request.texts:
            if t.strip():
                sample_list.append({"emotion": "", "text": t.strip()})

    if not sample_list:
        raise HTTPException(status_code=400, detail="Provide at least one sample text")

    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    dataset_dir = os.path.join(LORA_DATASETS_DIR, safe_name)
    if os.path.exists(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{safe_name}' already exists")

    total = len(sample_list)
    root_description = request.description.strip()

    def task():
        process_state["dataset_gen"]["running"] = True
        process_state["dataset_gen"]["logs"] = [
            f"Generating {total} samples with VoiceDesign..."
        ]
        try:
            engine = project_manager.get_engine()
            if not engine:
                process_state["dataset_gen"]["logs"].append("Error: TTS engine not initialized")
                return

            os.makedirs(dataset_dir, exist_ok=True)
            metadata_lines = []
            completed = 0

            for i, sample in enumerate(sample_list):
                text = sample["text"]
                emotion = sample["emotion"]
                # Build full description: root + emotion if provided
                description = f"{root_description}, {emotion}" if emotion else root_description

                process_state["dataset_gen"]["logs"].append(
                    f"[{i+1}/{total}] {('[' + emotion + '] ' if emotion else '')}\"{ text[:60]}{'...' if len(text) > 60 else ''}\""
                )
                try:
                    wav_path, sr = engine.generate_voice_design(
                        description=description,
                        sample_text=text,
                        language=request.language,
                    )
                    # Copy to dataset dir with sequential name
                    dest_filename = f"sample_{i:03d}.wav"
                    dest_path = os.path.join(dataset_dir, dest_filename)
                    shutil.copy2(wav_path, dest_path)

                    # Save first successful sample as ref.wav for consistent speaker embedding
                    if completed == 0:
                        shutil.copy2(wav_path, os.path.join(dataset_dir, "ref.wav"))

                    metadata_lines.append(json.dumps({
                        "audio_filepath": dest_filename,
                        "text": text,
                        "ref_audio": "ref.wav",
                    }, ensure_ascii=False))
                    completed += 1
                    process_state["dataset_gen"]["logs"].append(
                        f"  Saved {dest_filename}"
                    )
                except Exception as e:
                    process_state["dataset_gen"]["logs"].append(
                        f"  Failed: {e}"
                    )

            # Write metadata.jsonl
            metadata_path = os.path.join(dataset_dir, "metadata.jsonl")
            with open(metadata_path, "w", encoding="utf-8") as f:
                f.write("\n".join(metadata_lines) + "\n")

            process_state["dataset_gen"]["logs"].append(
                f"Dataset '{safe_name}' complete: {completed}/{total} samples generated."
            )
            logger.info(f"LoRA dataset generated: '{safe_name}' ({completed} samples)")

        except Exception as e:
            process_state["dataset_gen"]["logs"].append(f"Error: {e}")
            logger.error(f"Dataset generation error: {e}")
            # Clean up partial dataset on failure
            if os.path.exists(dataset_dir):
                shutil.rmtree(dataset_dir)
        finally:
            process_state["dataset_gen"]["running"] = False

    claim_gpu_task("dataset_gen")
    background_tasks.add_task(task)
    return {"status": "started", "dataset_id": safe_name, "total": total}

@app.get("/api/lora/datasets")
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

@app.delete("/api/lora/datasets/{dataset_id}")
async def lora_delete_dataset(dataset_id: str):
    """Delete an uploaded dataset."""
    dataset_dir = _safe_subpath(LORA_DATASETS_DIR, dataset_id)
    if not os.path.isdir(dataset_dir):
        raise HTTPException(status_code=404, detail="Dataset not found")

    shutil.rmtree(dataset_dir)
    logger.info(f"LoRA dataset deleted: {dataset_id}")
    return {"status": "deleted", "dataset_id": dataset_id}

@app.post("/api/lora/train")
async def lora_start_training(request: LoraTrainingRequest, background_tasks: BackgroundTasks):
    """Start LoRA training as a subprocess."""
    check_global_gpu_lock("lora_training")

    # Validate dataset exists
    dataset_dir = _safe_subpath(LORA_DATASETS_DIR, request.dataset_id)
    if not os.path.isdir(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{request.dataset_id}' not found")

    # Build output directory
    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid adapter name")

    adapter_id = f"{safe_name}_{int(time.time())}"
    output_dir = os.path.join(LORA_MODELS_DIR, adapter_id)

    # Log dataset details and effective settings before training
    try:
        meta_path = os.path.join(dataset_dir, "metadata.jsonl")
        dataset_sample_count = sum(1 for l in open(meta_path, encoding="utf-8") if l.strip())
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

@app.get("/api/lora/models")
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

@app.delete("/api/lora/models/{adapter_id}")
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

@app.post("/api/lora/download/{adapter_id}")
async def lora_download_builtin(adapter_id: str):
    """Download a built-in LoRA adapter from HuggingFace."""
    manifest = fetch_builtin_manifest(BUILTIN_LORA_DIR)
    hf_name = adapter_id.replace("builtin_", "", 1)
    entry = next((e for e in manifest if e["id"] == hf_name or e["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown built-in adapter: {adapter_id}")

    if is_adapter_downloaded(adapter_id, BUILTIN_LORA_DIR):
        return {"status": "already_downloaded", "adapter_id": adapter_id}

    try:
        download_builtin_adapter(adapter_id, BUILTIN_LORA_DIR)
        logger.info(f"Built-in adapter downloaded: {adapter_id}")
        return {"status": "downloaded", "adapter_id": adapter_id}
    except Exception as e:
        logger.error(f"Download failed for {adapter_id}: {e}")
        raise HTTPException(status_code=500, detail="Built-in adapter download failed — see server logs for details.")

@app.post("/api/lora/test")
async def lora_test_model(request: LoraTestRequest):
    """Generate test audio using a LoRA adapter (built-in or user-trained)."""
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

    if not os.path.isdir(adapter_dir) and is_builtin:
        try:
            download_builtin_adapter(request.adapter_id, BUILTIN_LORA_DIR)
            adapter_dir = os.path.join(BUILTIN_LORA_DIR, request.adapter_id)
        except Exception as e:
            logger.error(f"Auto-download failed for {request.adapter_id}: {e}")
            raise HTTPException(status_code=500, detail="Adapter auto-download failed — see server logs for details.")
    elif not os.path.isdir(adapter_dir):
        raise HTTPException(status_code=404, detail="Adapter files not found")

    engine = project_manager.get_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    try:
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
    except Exception as e:
        logger.error(f"LoRA test generation failed: {e}")
        raise HTTPException(status_code=500, detail="LoRA test generation failed — see server logs for details.")

LORA_PREVIEW_TEXT = "The ancient library stood at the crossroads of two forgotten paths, its weathered stone walls covered in ivy that had been growing for centuries."

@app.post("/api/lora/preview/{adapter_id}")
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

    if not os.path.isdir(adapter_dir) and is_builtin:
        try:
            download_builtin_adapter(adapter_id, BUILTIN_LORA_DIR)
            adapter_dir = os.path.join(BUILTIN_LORA_DIR, adapter_id)
        except Exception as e:
            logger.error(f"Auto-download failed for {adapter_id}: {e}")
            raise HTTPException(status_code=500, detail="Adapter auto-download failed — see server logs for details.")
    elif not os.path.isdir(adapter_dir):
        raise HTTPException(status_code=404, detail="Adapter files not found")

    preview_path = os.path.join(adapter_dir, "preview_sample.wav")

    # Return cached if exists
    if os.path.exists(preview_path):
        return {"status": "cached", "audio_url": f"{url_prefix}/preview_sample.wav"}

    # Generate preview
    engine = project_manager.get_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    try:
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
    except Exception as e:
        logger.error(f"LoRA preview generation failed: {e}")
        raise HTTPException(status_code=500, detail="LoRA preview generation failed — see server logs for details.")

## ── Dataset Builder ──────────────────────────────────────────

def _load_builder_state(name):
    """Load project state from dataset builder working directory."""
    # Sanitize the name into the path here too (defense-in-depth): callers pass
    # an already-secured name, and secure_filename is idempotent on those, so
    # this only hardens any future/raw caller against `..` traversal.
    name = secure_filename(name)
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
    name = secure_filename(name)  # defense-in-depth; idempotent on secured names
    work_dir = os.path.join(DATASET_BUILDER_DIR, name)
    os.makedirs(work_dir, exist_ok=True)
    atomic_json_write(state, os.path.join(work_dir, "state.json"))

@app.get("/api/dataset_builder/list")
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

@app.post("/api/dataset_builder/create")
async def dataset_builder_create(request: DatasetBuilderCreateRequest):
    """Create a new dataset builder project."""
    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if os.path.exists(work_dir):
        raise HTTPException(status_code=400, detail=f"Project '{safe_name}' already exists")
    _save_builder_state(safe_name, {"description": "", "global_seed": "", "samples": []})
    return {"name": safe_name}

@app.post("/api/dataset_builder/update_meta")
async def dataset_builder_update_meta(request: DatasetBuilderUpdateMetaRequest):
    """Update project description and global seed without touching samples."""
    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    state = _load_builder_state(safe_name)
    state["description"] = request.description
    state["global_seed"] = request.global_seed
    _save_builder_state(safe_name, state)
    return {"status": "ok"}

@app.post("/api/dataset_builder/update_rows")
async def dataset_builder_update_rows(request: DatasetBuilderUpdateRowsRequest):
    """Update row definitions, preserving existing generation status/audio."""
    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    state = _load_builder_state(safe_name)
    existing = state.get("samples", [])
    # Merge: keep status/audio_url from existing samples where text unchanged
    new_samples = []
    for i, row in enumerate(request.rows):
        sample = {
            "emotion": row.get("emotion", ""),
            "text": row.get("text", "").strip(),
            "seed": row.get("seed", ""),
            "status": "pending",
            "audio_url": None,
        }
        if i < len(existing):
            old = existing[i]
            # Preserve generation state if text unchanged (trimmed comparison)
            if old.get("text", "").strip() == sample["text"]:
                sample["status"] = old.get("status", "pending")
                sample["audio_url"] = old.get("audio_url")
        new_samples.append(sample)
    state["samples"] = new_samples
    _save_builder_state(safe_name, state)
    return {"status": "ok", "sample_count": len(new_samples)}

@app.post("/api/dataset_builder/generate_sample")
async def dataset_builder_generate_sample(request: DatasetSampleGenRequest):
    """Generate a single dataset sample using VoiceDesign."""
    engine = project_manager.get_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    safe_name = secure_filename(request.dataset_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    os.makedirs(work_dir, exist_ok=True)

    try:
        wav_path, sr = engine.generate_voice_design(
            description=request.description,
            sample_text=request.text,
            seed=request.seed,
        )

        dest_filename = f"sample_{request.sample_index:03d}.wav"
        dest_path = os.path.join(work_dir, dest_filename)
        shutil.copy2(wav_path, dest_path)

        # Update state (cache-bust URL so browser loads fresh audio on regen)
        cache_bust = int(time.time())
        audio_url = f"/dataset_builder/{safe_name}/{dest_filename}?t={cache_bust}"
        state = _load_builder_state(safe_name)
        samples = state.get("samples", [])
        # Ensure list is large enough
        while len(samples) <= request.sample_index:
            samples.append({"status": "pending"})
        existing_sample = samples[request.sample_index] if request.sample_index < len(samples) else {}
        samples[request.sample_index] = {
            **existing_sample,
            "status": "done",
            "audio_url": audio_url,
            "text": request.text.strip(),
            "description": request.description,
        }
        state["samples"] = samples
        _save_builder_state(safe_name, state)

        return {
            "status": "done",
            "sample_index": request.sample_index,
            "audio_url": audio_url,
        }
    except Exception as e:
        logger.error(f"Dataset builder sample generation failed: {e}")
        # Mark as error in state
        state = _load_builder_state(safe_name)
        samples = state.get("samples", [])
        while len(samples) <= request.sample_index:
            samples.append({"status": "pending"})
        samples[request.sample_index] = {"status": "error", "error": str(e)}
        state["samples"] = samples
        _save_builder_state(safe_name, state)
        raise HTTPException(status_code=500, detail="Sample generation failed — see server logs for details.")

@app.post("/api/dataset_builder/generate_batch")
async def dataset_builder_generate_batch(request: DatasetBatchGenRequest):
    """Batch generate dataset samples as a background task."""
    check_global_gpu_lock("dataset_builder")

    if not request.samples or len(request.samples) == 0:
        raise HTTPException(status_code=400, detail="No samples provided")

    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    os.makedirs(work_dir, exist_ok=True)
    root_desc = request.description.strip()

    # Determine which indices to generate
    if request.indices is not None:
        # Validate client-supplied indices up front: an out-of-range value would
        # raise inside the background thread (samples_snapshot[idx]) and, without
        # the guard below, wedge the GPU lock.
        if any(not (0 <= i < len(request.samples)) for i in request.indices):
            raise HTTPException(status_code=400, detail="indices out of range for samples")
        to_generate = request.indices
    else:
        to_generate = list(range(len(request.samples)))

    total = len(to_generate)

    # Snapshot request data for the thread (request object may not survive)
    samples_snapshot = [(s.emotion.strip(), s.text.strip()) for s in request.samples]
    global_seed = request.global_seed
    per_seeds = request.seeds

    def task():
        process_state["dataset_builder"]["running"] = True
        process_state["dataset_builder"]["logs"] = []
        process_state["dataset_builder"]["cancel"] = False

        engine = project_manager.get_engine()
        if not engine:
            process_state["dataset_builder"]["logs"].append("[ERROR] Failed to initialize TTS engine")
            process_state["dataset_builder"]["running"] = False
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
                seed = -1
                if per_seeds and idx < len(per_seeds) and per_seeds[idx] >= 0:
                    seed = per_seeds[idx]
                elif global_seed >= 0:
                    seed = global_seed

                wav_path, sr = engine.generate_voice_design(
                    description=description,
                    sample_text=text,
                    seed=seed,
                )
                dest_filename = f"sample_{idx:03d}.wav"
                dest_path = os.path.join(work_dir, dest_filename)
                shutil.copy2(wav_path, dest_path)

                samples_state[idx] = {
                    **samples_state[idx],
                    "status": "done",
                    "audio_url": f"/dataset_builder/{safe_name}/{dest_filename}?t={int(time.time())}",
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
        process_state["dataset_builder"]["running"] = False

    def _task_guarded():
        # Release the GPU lock even if task() raises before its own cleanup —
        # otherwise process_state stays running=True and blocks every later GPU
        # task until restart. Mirrors run_process's finally.
        try:
            task()
        except Exception as e:
            logger.exception(f"dataset_builder task crashed: {e}")
            process_state["dataset_builder"]["logs"].append(f"[ERROR] {e}")
        finally:
            process_state["dataset_builder"]["running"] = False

    claim_gpu_task("dataset_builder")
    threading.Thread(target=_task_guarded, daemon=True).start()
    return {"status": "started", "dataset_name": safe_name, "total": total}

@app.post("/api/dataset_builder/cancel")
async def dataset_builder_cancel():
    """Cancel ongoing batch dataset generation."""
    if process_state["dataset_builder"]["running"]:
        process_state["dataset_builder"]["cancel"] = True
        return {"status": "cancelling"}
    return {"status": "not_running"}

@app.get("/api/dataset_builder/status/{name}")
async def dataset_builder_status(name: str):
    """Get per-sample generation status for a dataset builder project."""
    state = _load_builder_state(name)
    return {
        "description": state.get("description", ""),
        "global_seed": state.get("global_seed", ""),
        "samples": state.get("samples", []),
        "running": process_state["dataset_builder"]["running"],
        "logs": process_state["dataset_builder"]["logs"],
    }

@app.post("/api/dataset_builder/save")
async def dataset_builder_save(request: DatasetSaveRequest):
    """Finalize dataset builder project as a training dataset."""
    safe_name = secure_filename(request.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Dataset builder project not found")

    state = _load_builder_state(safe_name)
    samples = state.get("samples", [])

    # Collect completed samples
    done_samples = [(i, s) for i, s in enumerate(samples) if s.get("status") == "done"]
    if not done_samples:
        raise HTTPException(status_code=400, detail="No completed samples to save")

    # Check ref_index is valid
    ref_idx = request.ref_index
    ref_sample = next((s for i, s in done_samples if i == ref_idx), None)
    if ref_sample is None:
        # Fall back to first completed sample
        ref_idx = done_samples[0][0]
        ref_sample = done_samples[0][1]

    # Create training dataset directory
    dataset_dir = os.path.join(LORA_DATASETS_DIR, safe_name)
    if os.path.exists(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{safe_name}' already exists in training datasets")

    os.makedirs(dataset_dir, exist_ok=True)

    try:
        metadata_lines = []
        for i, sample in done_samples:
            src_filename = f"sample_{i:03d}.wav"
            src_path = os.path.join(work_dir, src_filename)
            if not os.path.exists(src_path):
                continue

            dest_filename = f"sample_{i:03d}.wav"
            shutil.copy2(src_path, os.path.join(dataset_dir, dest_filename))

            metadata_lines.append(json.dumps({
                "audio_filepath": dest_filename,
                "text": sample.get("text", ""),
                "ref_audio": "ref.wav",
            }, ensure_ascii=False))

        # Copy ref sample and save its text for correct clone prompt alignment
        ref_src = os.path.join(work_dir, f"sample_{ref_idx:03d}.wav")
        if os.path.exists(ref_src):
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
    except Exception as e:
        # Clean up on failure
        if os.path.exists(dataset_dir):
            shutil.rmtree(dataset_dir, ignore_errors=True)
        logger.error(f"Dataset save failed: {e}")
        raise HTTPException(status_code=500, detail="Dataset save failed — see server logs for details.")

@app.delete("/api/dataset_builder/{name}")
async def dataset_builder_delete(name: str):
    """Discard a dataset builder working project."""
    work_dir = _safe_subpath(DATASET_BUILDER_DIR, name)
    if not os.path.exists(work_dir):
        raise HTTPException(status_code=404, detail="Dataset builder project not found")
    shutil.rmtree(work_dir, ignore_errors=True)
    logger.info(f"Dataset builder project discarded: {name}")
    return {"status": "deleted", "name": name}

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
    return interpreter


@app.post("/api/preparer/start")
async def preparer_start(
    background_tasks: BackgroundTasks,
    config_json: str = Form(...),
    audio_file: UploadFile = File(...),
):
    """Upload audio and run the preparer to generate a voice training dataset."""
    interpreter = _resolve_preparer_interpreter()
    check_global_gpu_lock("preparer")

    try:
        config = PreparerConfig(**json.loads(config_json))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid config: {e}")

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
    async with aiofiles.open(audio_path, "wb") as f:
        while chunk := await audio_file.read(1024 * 1024):
            await f.write(chunk)

    def _run():
        state = process_state["preparer"]
        state["running"] = True
        state["logs"] = []
        state["cancel"] = False
        state["status"] = "running"
        state["output_file"] = None
        state["process"] = None

        cmd = [interpreter, "-u", PREPARER_SCRIPT_PATH,
               "--audio", audio_path,
               "--output", os.path.join(PREPARER_OUTPUT_DIR, output_filename),
               "--lang", config.lang,
               "--min-confidence", str(config.min_confidence),
               "--min-snr", str(config.min_snr)]

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

    def _run_guarded():
        # Release the GPU lock even if _run raises before its own cleanup (e.g.
        # Popen fails to spawn the interpreter) — otherwise the lock stays held.
        try:
            _run()
        except Exception as e:
            logger.exception(f"preparer task crashed: {e}")
            process_state["preparer"]["logs"].append(f"Preparer error: {e}")
            process_state["preparer"]["status"] = "failed"
        finally:
            process_state["preparer"]["running"] = False
            process_state["preparer"]["process"] = None

    claim_gpu_task("preparer")
    background_tasks.add_task(_run_guarded)
    return {"status": "started"}


@app.post("/api/preparer/cancel")
async def preparer_cancel():
    state = process_state["preparer"]
    if not state["running"]:
        raise HTTPException(status_code=400, detail="No preparer is currently running.")
    proc = state.get("process")
    if proc:
        try:
            proc.terminate()
        except OSError:
            pass
    state["cancel"] = True
    return {"status": "cancel_requested"}


@app.get("/api/preparer/list")
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


@app.get("/api/preparer/download/{filename:path}")
async def preparer_download(filename: str):
    """Download a generated dataset ZIP."""
    root = os.path.realpath(PREPARER_OUTPUT_DIR)
    file_path = os.path.realpath(os.path.join(PREPARER_OUTPUT_DIR, filename))
    if not file_path.startswith(root + os.sep) and file_path != root:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, media_type="application/zip", filename=os.path.basename(file_path))


@app.post("/api/preparer/batch/start")
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
        state["cancel"] = False
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

    def _run_guarded():
        # Release the GPU lock even if _run raises before its own cleanup.
        try:
            _run()
        except Exception as e:
            logger.exception(f"batch_preparer task crashed: {e}")
            process_state["batch_preparer"]["logs"].append(f"Batch preparer error: {e}")
        finally:
            process_state["batch_preparer"]["running"] = False

    claim_gpu_task("batch_preparer")
    background_tasks.add_task(_run_guarded)
    return {"status": "started", "task_count": len(request.tasks)}


@app.post("/api/preparer/batch/cancel")
async def preparer_batch_cancel():
    process_state["batch_preparer"]["cancel"] = True
    return {"status": "cancel_requested"}


## ── Voice Lab: end-to-end audiobook → named LoRA pipeline ─────────────────────
#
# Orchestrates the four post-preparer stages as a single sequential job:
#   dedup   → voice_analysis.py --phase dedup   (this repo, ROCm env)
#   train   → batch_train_lora.py               (sibling repo, ROCm env)
#   profile → voice_profiler.py                 (sibling repo, ROCm env)
#   name    → name_voices.py                    (this repo, pure stdlib)
#
# Stages 1-3 need the ROCm ML env (torch/librosa/speechbrain), which the web app's
# own venv does NOT have — so they run under a configurable interpreter. The paths
# are machine-specific (the user's established setup) and live in a small editable
# config file rather than being hardcoded, so another machine can point them
# elsewhere without code changes.

VOICELAB_CONFIG_PATH = os.path.join(ROOT_DIR, "voicelab_config.json")

VOICELAB_DEFAULTS = {
    # Interpreter with torch/librosa/speechbrain (NOT the web app's env)
    "rocm_python": os.environ.get("ALEXANDRIA_ROCM_PYTHON", os.path.join(ROOT_DIR, "env", "bin", "python")),
    # Repo holding batch_train_lora.py + voice_profiler.py
    "pipeline_repo": os.environ.get("ALEXANDRIA_PIPELINE_REPO", ROOT_DIR),
    # GGUF model voice_profiler.py uses for the prose descriptions ("" = its default)
    "profiler_model": os.environ.get("ALEXANDRIA_PROFILER_MODEL", ""),
    # Default zips2 root (folder of narrator subfolders) the dedup stage reads
    "zips_dir": os.environ.get("ALEXANDRIA_ZIPS_DIR", os.path.join(ROOT_DIR, "zips2")),
}

VOICELAB_STAGES = ("dedup", "train", "profile", "name")


def _load_voicelab_config() -> dict:
    cfg = dict(VOICELAB_DEFAULTS)
    if os.path.exists(VOICELAB_CONFIG_PATH):
        try:
            with open(VOICELAB_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in VOICELAB_DEFAULTS})
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return cfg


class VoiceLabConfig(BaseModel):
    rocm_python: Optional[str] = None
    pipeline_repo: Optional[str] = None
    profiler_model: Optional[str] = None
    zips_dir: Optional[str] = None


class VoiceLabRequest(BaseModel):
    zips_dir: Optional[str] = None                 # narrator-subfolder root; default from config
    stages: List[str] = list(VOICELAB_STAGES)      # which stages to run, in pipeline order
    device: Optional[str] = None                   # cuda/cpu for the dedup stage (auto if unset)
    target_loss: float = 4.15                      # batch-train early-stop target
    max_epochs: int = 6
    lora_r: int = 64
    profiler_model: Optional[str] = None           # override GGUF for the profile stage
    name_apply: bool = True                        # name stage: actually rename (else dry-run)
    name_overwrite: bool = False                   # also re-name already-named adapters


@app.get("/api/voicelab/config")
async def voicelab_get_config():
    """Return the pipeline paths plus whether each resolves on this machine."""
    cfg = _load_voicelab_config()
    zips_dir_ok = False
    try:
        resolved_zips = _resolve_zips_dir(cfg["zips_dir"])
        zips_dir_ok = os.path.isdir(resolved_zips)
    except Exception:
        pass

    return {
        "config": cfg,
        "checks": {
            "rocm_python": os.path.isfile(cfg["rocm_python"]),
            "pipeline_repo": os.path.isdir(cfg["pipeline_repo"]),
            "batch_train_lora": os.path.isfile(os.path.join(cfg["pipeline_repo"], "batch_train_lora.py")),
            "voice_profiler": os.path.isfile(os.path.join(cfg["pipeline_repo"], "voice_profiler.py")),
            "voice_analysis": os.path.isfile(os.path.join(ROOT_DIR, "voice_analysis.py")),
            "name_voices": os.path.isfile(os.path.join(ROOT_DIR, "name_voices.py")),
            "profiler_model": (not cfg["profiler_model"]) or os.path.isfile(cfg["profiler_model"]),
            "zips_dir": zips_dir_ok,
        },
        "defaults": VOICELAB_DEFAULTS,
    }


@app.post("/api/voicelab/config")
async def voicelab_save_config(request: VoiceLabConfig):
    cfg = _load_voicelab_config()
    for k, v in request.model_dump(exclude_none=True).items():
        cfg[k] = v.strip() if isinstance(v, str) else v
    atomic_json_write(cfg, VOICELAB_CONFIG_PATH)
    return {"status": "saved", "config": cfg}


def _resolve_zips_dir(raw: str) -> str:
    """Resolve a (possibly relative) zips_dir the same way voicelab_start does.

    zips_dir is a machine-specific path (like rocm_python/pipeline_repo) and may be
    absolute; relative values are resolved against the project root.
    """
    resolved = os.path.normpath(raw)
    if not os.path.isabs(resolved):
        resolved = os.path.abspath(os.path.join(ROOT_DIR, resolved))
    else:
        resolved = os.path.abspath(resolved)
    return resolved


@app.get("/api/voicelab/inspect")
async def voicelab_inspect(zips_dir: Optional[str] = None):
    """Preview what a dedup input folder contains so the UI can show readiness."""
    cfg = _load_voicelab_config()
    root = (zips_dir or cfg["zips_dir"]).strip()
    if not root:
        raise HTTPException(status_code=400, detail="zips_dir is not configured. Set it in Voice Lab settings.")
    root = _resolve_zips_dir(root)
    if not os.path.isdir(root):
        raise HTTPException(status_code=400, detail=f"Folder not found: {root}")

    narrators = []
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if name.startswith("_") or not os.path.isdir(sub):
            continue
        zips = [f for f in os.listdir(sub) if f.lower().endswith(".zip")]
        narrators.append({"name": name, "zip_count": len(zips)})

    deduped_dir = os.path.join(root, "_deduped")
    deduped_zips = (
        [f for f in os.listdir(deduped_dir) if f.lower().endswith(".zip")]
        if os.path.isdir(deduped_dir) else []
    )

    manifest = _load_manifest(LORA_MODELS_MANIFEST)
    trained = sum(1 for e in manifest if e.get("zip_source"))
    unnamed = sum(1 for e in manifest
                  if e.get("zip_source") and e.get("dataset_id") and e.get("id") == e.get("dataset_id"))
    profiled = sum(1 for e in manifest if e.get("voice_profile"))

    return {
        "zips_dir": root,
        "narrator_count": len(narrators),
        "narrators": narrators,
        "deduped_exists": os.path.isdir(deduped_dir),
        "deduped_count": len(deduped_zips),
        "manifest": {"trained": trained, "profiled": profiled, "unnamed": unnamed},
    }


def _voicelab_build_commands(req: VoiceLabRequest, cfg: dict, zips_dir: str):
    """Build the (stage_name, command, cwd) tuples for the requested stages."""
    rocm = cfg["rocm_python"]
    repo = cfg["pipeline_repo"]
    deduped_dir = os.path.join(zips_dir, "_deduped")
    profiler_model = (req.profiler_model or cfg["profiler_model"]).strip()

    steps = []
    if "dedup" in req.stages:
        cmd = [rocm, "-u", os.path.join(ROOT_DIR, "voice_analysis.py"),
               "--phase", "dedup", "--zips2", zips_dir]
        if req.device:
            cmd += ["--device", req.device]
        steps.append(("dedup", cmd, ROOT_DIR))
    if "train" in req.stages:
        cmd = [rocm, "-u", os.path.join(repo, "batch_train_lora.py"),
               "--zips_dir", deduped_dir,
               "--models_dir", LORA_MODELS_DIR,
               "--manifest", LORA_MODELS_MANIFEST,
               "--target_loss", str(req.target_loss),
               "--max_epochs", str(req.max_epochs),
               "--lora_r", str(req.lora_r)]
        steps.append(("train", cmd, repo))
    if "profile" in req.stages:
        cmd = [rocm, "-u", os.path.join(repo, "voice_profiler.py"),
               "--manifest", LORA_MODELS_MANIFEST]
        if profiler_model:
            cmd += ["--model", profiler_model]
        steps.append(("profile", cmd, repo))
    if "name" in req.stages:
        # Pure stdlib — safe to run under the web app's own interpreter
        cmd = [sys.executable, "-u", os.path.join(ROOT_DIR, "name_voices.py"),
               "--manifest", LORA_MODELS_MANIFEST, "--models-dir", LORA_MODELS_DIR]
        if req.name_apply:
            cmd.append("--apply")
        if req.name_overwrite:
            cmd.append("--overwrite")
        steps.append(("name", cmd, ROOT_DIR))
    return steps


@app.post("/api/voicelab/start")
async def voicelab_start(request: VoiceLabRequest, background_tasks: BackgroundTasks):
    """Run the selected pipeline stages in sequence as one cancel/pausable job."""
    check_global_gpu_lock("voicelab")

    bad = [s for s in request.stages if s not in VOICELAB_STAGES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown stage(s): {', '.join(bad)}")
    # Keep canonical pipeline order regardless of how the request listed them
    request.stages = [s for s in VOICELAB_STAGES if s in request.stages]
    if not request.stages:
        raise HTTPException(status_code=400, detail="No stages selected.")

    cfg = _load_voicelab_config()
    zips_dir_raw = (request.zips_dir or cfg["zips_dir"]).strip()
    if not zips_dir_raw:
        raise HTTPException(status_code=400, detail="zips_dir is not configured. Set it in Voice Lab settings.")
    zips_dir = _resolve_zips_dir(zips_dir_raw)

    # Validate prerequisites up front with actionable errors
    needs_rocm = any(s in request.stages for s in ("dedup", "train", "profile"))
    if needs_rocm and not os.path.isfile(cfg["rocm_python"]):
        raise HTTPException(status_code=400,
                            detail=f"ROCm interpreter not found: {cfg['rocm_python']}. Set it in Voice Lab settings.")
    if "dedup" in request.stages and not os.path.isdir(zips_dir):
        raise HTTPException(status_code=400, detail=f"Input folder not found: {zips_dir}")
    if "train" in request.stages and not os.path.isdir(os.path.join(zips_dir, "_deduped")) and "dedup" not in request.stages:
        raise HTTPException(status_code=400,
                            detail=f"No _deduped folder in {zips_dir}; run the dedup stage first.")
    for s, fname, base in (("train", "batch_train_lora.py", cfg["pipeline_repo"]),
                           ("profile", "voice_profiler.py", cfg["pipeline_repo"])):
        if s in request.stages and not os.path.isfile(os.path.join(base, fname)):
            raise HTTPException(status_code=400,
                                detail=f"{fname} not found in {base}. Check the pipeline repo path in Voice Lab settings.")

    steps = _voicelab_build_commands(request, cfg, zips_dir)

    def _run():
        state = process_state["voicelab"]
        _init_batch_state(state,
                          [f"Voice Lab: {len(steps)} stage(s) — {', '.join(s[0] for s in steps)}"],
                          [{"name": s[0], "status": "pending"} for s in steps])
        state["status"] = "running"
        state["process"] = None
        state["pid"] = None

        log_path = _init_task_log("voicelab", extra_header=f"# zips_dir={zips_dir}\n")

        failed = False
        for i, (stage, cmd, cwd) in enumerate(steps):
            if state["cancel"]:
                state["logs"].append("Pipeline cancelled.")
                break
            state["current_task_idx"] = i
            state["tasks"][i]["status"] = "running"
            state["logs"].append(f"--- [{i+1}/{len(steps)}] {stage} ---")

            try:
                rc, _ = _stream_subprocess_to_logs(cmd, cwd, state, log_prefix=f"[{stage}] ", log_file=log_path)
            except Exception as e:
                state["logs"].append(f"[{stage}] error launching: {e}")
                state["tasks"][i]["status"] = "failed"
                failed = True
                break

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                break
            if rc == 0:
                state["tasks"][i]["status"] = "done"
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{stage}] failed (exit {rc}) — stopping pipeline.")
                failed = True
                break  # later stages depend on earlier ones; don't continue on failure

        state["process"] = None
        state["pid"] = None
        state["running"] = False
        if state.get("cancel"):
            state["status"] = "cancelled"
        elif failed:
            state["status"] = "failed"
        else:
            state["status"] = "done"
            state["logs"].append("Voice Lab pipeline finished.")

    claim_gpu_task("voicelab")
    background_tasks.add_task(_run)
    return {"status": "started", "stages": request.stages, "zips_dir": zips_dir}


@app.post("/api/voicelab/cancel")
async def voicelab_cancel():
    return _batch_cancel_helper("voicelab")


@app.post("/api/voicelab/pause")
async def voicelab_pause():
    return _pause_task("voicelab", "No Voice Lab pipeline is currently running.",
                        "Voice Lab is between stages, retry in a moment.",
                        "Voice Lab")


@app.post("/api/voicelab/resume")
async def voicelab_resume():
    return _resume_task("voicelab", "No Voice Lab pipeline is currently running.",
                         "Voice Lab")


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("ALEXANDRIA_HOST", "127.0.0.1")
    port = int(os.environ.get("ALEXANDRIA_PORT", "4200"))
    uvicorn.run(app, host=host, port=port, access_log=False)
