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
from pydantic import BaseModel, Field
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
from utils import atomic_json_write, atomic_json_write_pair, file_lock, safe_load_json, secure_filename, run_rocm_smi_json, extract_json_object, is_path_inside, is_generic_speaker, system_has_gpu, rocm_smi_utilization as _rocm_smi_utilization, check_basic_auth
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from math import ceil

from tts import voice_category
from default_prompts import load_default_prompts
from review_prompts import load_review_prompts
from persona_prompts import load_persona_prompts
from hf_utils import fetch_builtin_manifest, download_builtin_adapter, is_adapter_downloaded, builtin_hf_name
from lmstudio_settings import (get_lmstudio_status, apply_lmstudio_settings, is_remote_llm,
                               apply_remote_lmstudio_settings, is_local_llm_endpoint,
                               get_current_status, get_effective_max_tokens)
from review_script import clear_checkpoint, _checkpoint_path

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AlexandriaUI")

app = FastAPI(title="Alexandria Audiobook")

from core import (
    API_LOG_DIR,
    AUDIOBOOK_PATH,
    BASE_DIR,
    BUILTIN_LORA_DIR,
    CHARACTER_ALIASES_PATH,
    CHUNKS_PATH,
    CLONE_VOICES_DIR,
    CONFIG_PATH,
    DATASET_BUILDER_DIR,
    DATA_DIR,
    DESIGNED_VOICES_DIR,
    ETA_TASKS,
    GPU_TASKS,
    LLMConfigError,
    LORA_DATASETS_DIR,
    LORA_MODELS_DIR,
    LORA_MODELS_MANIFEST,
    M4B_PATH,
    NON_GPU_TASKS,
    PREPARER_OUTPUT_DIR,
    PREPARER_SCRIPT_PATH,
    REPORTS_DIR,
    ROOT_DIR,
    SCRIPTS_DIR,
    SCRIPT_PATH,
    STATIC_DIR,
    UPLOADS_DIR,
    VOICELINES_DIR,
    VOICE_CONFIG_PATH,
    VOICE_LIBRARY_PATH,
    _REVIEW_SUMMARY_PATTERNS,
    _batch_cancel_helper,
    _cancel_task,
    _combine_pass_stats,
    _combine_pass_totals,
    _compute_eta,
    _extract_diff_highlights,
    _extract_new_aliases,
    _extract_review_stats,
    _format_book_summary,
    _format_pass_summary,
    _get_saved_book_id,
    _init_batch_state,
    _init_task_log,
    _insert_llm_summary,
    _load_llm_config,
    _load_manifest,
    _load_voicelab_config,
    _make_llm_client,
    _markdown_aliases_lines,
    _markdown_book_pass_lines,
    _markdown_diff_highlights_lines,
    _markdown_heads_up_lines,
    _markdown_stats_table,
    _new_review_totals,
    _pause_task,
    _resume_task,
    _run_claimed_background_task,
    _save_active_book_id,
    _saved_book_meta_path,
    _send_signal_tree,
    _stream_subprocess_to_logs,
    _task_log_path,
    _validate_local_llm_base_url,
    _validate_voicelab_path,
    _revalidate_voicelab_paths,
    _warn_corrupted_json,
    check_global_gpu_lock,
    claim_gpu_task,
    get_active_book_id,
    process_state,
    project_manager,
    run_process,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Create voicelines directory if it doesn't exist to prevent startup error
app.mount("/voicelines", StaticFiles(directory=VOICELINES_DIR), name="voicelines")

# Designed voices directory for voice designer feature
app.mount("/designed_voices", StaticFiles(directory=DESIGNED_VOICES_DIR), name="designed_voices")

# Clone voices directory for user-uploaded reference audio
app.mount("/clone_voices", StaticFiles(directory=CLONE_VOICES_DIR), name="clone_voices")

app.mount("/lora_models", StaticFiles(directory=LORA_MODELS_DIR), name="lora_models")

# Built-in LoRA adapters directory
app.mount("/builtin_lora", StaticFiles(directory=BUILTIN_LORA_DIR), name="builtin_lora")

# Dataset builder directory for preview audio
app.mount("/dataset_builder", StaticFiles(directory=DATASET_BUILDER_DIR), name="dataset_builder")

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

# Optional HTTP Basic Auth gate. OFF by default: only registered when
# ALEXANDRIA_AUTH_PASSWORD is set, so the local Pinokio flow is unchanged and
# pays no per-request cost. When enabled, every request must carry valid Basic
# credentials — the browser stores them once (native dialog) and re-sends on
# fetch, download links, and <audio> loads alike. Set this before exposing the
# app beyond localhost (e.g. the Docker image binds 0.0.0.0).
_AUTH_USERNAME = os.environ.get("ALEXANDRIA_AUTH_USERNAME", "alexandria")
_AUTH_PASSWORD = os.environ.get("ALEXANDRIA_AUTH_PASSWORD", "")
if _AUTH_PASSWORD:
    from starlette.responses import Response as _StarletteResponse

    @app.middleware("http")
    async def _basic_auth_gate(request, call_next):
        # CORS preflight carries no credentials by design; let it through so the
        # CORS middleware can answer it.
        if request.method == "OPTIONS":
            return await call_next(request)
        if check_basic_auth(request.headers.get("Authorization", ""),
                             _AUTH_USERNAME, _AUTH_PASSWORD):
            return await call_next(request)
        return _StarletteResponse(
            "Authentication required", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Alexandria"'})
    print("Auth: HTTP Basic Auth enabled (ALEXANDRIA_AUTH_PASSWORD is set)")

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
                 "utilization_percent": _rocm_smi_utilization(card_data)}
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
    except Exception as e:
        logger.debug(f"Could not get GPU memory stats: {e}")
        _gpu_stats_cache["data"] = None
        _gpu_stats_cache["timestamp"] = now
        return None

    # Utilization via rocm-smi is a separate, independent try - same reason
    # as alexandria_preparer_rocm_compatible.py's twin get_gpu_stats(): an
    # odd/unparseable value here shouldn't discard the memory stats already
    # computed successfully above.
    stats['utilization_percent'] = None
    try:
        data = run_rocm_smi_json(["--showuse"], rocm_smi_path="/opt/rocm/bin/rocm-smi", timeout=2)
        if data is not None:
            for card_key, card_data in data.items():
                if not isinstance(card_data, dict):
                    continue
                stats['utilization_percent'] = _rocm_smi_utilization(card_data)
                break
        else:
            logger.debug("GPU stats: Failed to get utilization via rocm-smi")
    except Exception as e:
        logger.debug(f"Could not get GPU utilization via rocm-smi: {e}")

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
    cast: Optional[str] = None

class VoiceSuggestionApplyRequest(BaseModel):
    character: str
    cast: Optional[str] = None
    suggestion: Dict

class VoiceSuggestionApplyBulkRequest(BaseModel):
    cast: Optional[str] = None
    suggestions: Dict[str, Dict]

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
    epochs: int = Field(default=5, ge=1)
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

class DatasetSampleGenRequest(BaseModel):
    description: str      # full voice description (root + emotion already combined by frontend)
    text: str
    dataset_name: str     # working directory name
    sample_index: int = Field(ge=0, le=4999)  # row number
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

class ReviewRequest(BaseModel):
    dedupe_speakers: bool = True

class ContextualReviewRequest(BaseModel):
    window_size: int = 4
    dedupe_speakers: bool = True

class BatchReviewRequest(BaseModel):
    script_names: List[str]            # names from the Scripts library (without .json)
    context_window: int = 0            # >0 enables contextual review
    dedupe_speakers: bool = True       # merge same-character aliases, consistent across the batch
    find_nicknames: bool = True        # run nickname discovery per book first, into the shared series alias file
    bidirectional: bool = False        # after the forward pass, re-scan in reverse so early books get
                                       # discovery seeded with full-series hindsight (requires find_nicknames)

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
    torch_cuda_ok = False
    try:
        torch = _get_torch()
        if torch is not None and torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            torch_cuda_ok = True
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

    # A GPU physically present (rocm-smi/nvidia-smi/Apple Silicon) but torch
    # unable to use it means generation/training will silently run on CPU -
    # surfaced here so the UI can show this prominently instead of it only
    # showing up as "everything is slow" with no clear cause.
    gpu_mismatch = False
    gpu_vendor = None
    if not torch_cuda_ok:
        has_gpu, gpu_vendor = system_has_gpu()
        gpu_mismatch = has_gpu

    return {
        "gpu": gpu,
        "gpu_name": gpu_name,
        "gpu_mismatch": gpu_mismatch,
        "gpu_mismatch_vendor": gpu_vendor,
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
    status = await asyncio.to_thread(get_current_status, llm_mode, base_url, model_name, ssh_alias, use_cache=True)
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
    LM Studio on Thunder) are driven over SSH via the configured host alias."""
    full_cfg = safe_load_json(CONFIG_PATH, default={})
    cfg = full_cfg.get("llm", {})
    model_name = cfg.get("model_name")
    if not model_name:
        raise HTTPException(status_code=400, detail="No LLM model configured")

    # Reloading the model is a real VRAM operation - keep it from racing any
    # other GPU_TASKS member (review/audio/script/etc.) holding VRAM against
    # the same model. See FINDINGS.md F-029.
    claim_gpu_task("lmstudio_optimize")
    try:
        if not is_remote_llm(full_cfg.get("llm_mode", "local"), cfg.get("base_url", "")):
            ok, msg = await asyncio.to_thread(apply_lmstudio_settings, model_name, ideal=req.enable)
            if not ok:
                raise HTTPException(status_code=502, detail=msg)
            status = await asyncio.to_thread(get_lmstudio_status, model_name)
            status["model"] = model_name
            status["message"] = msg
            return status

        # Remote: needs the SSH host alias (e.g. "tnr-0" from `tnr connect`).
        ssh_alias = (full_cfg.get("llm_remote_ssh") or "").strip()
        if not ssh_alias:
            raise HTTPException(status_code=400, detail=(
                "Remote optimize needs an SSH host alias (e.g. 'tnr-0'). Set it in "
                "the Setup tab's Remote LLM settings (run `tnr connect <id>` once first)."))
        ok, msg = await asyncio.to_thread(apply_remote_lmstudio_settings, ssh_alias, model_name, req.enable)
        if not ok:
            log_path = _log_llm_failure("optimize", f"alias={ssh_alias} model={model_name}\n\n{msg}")
            raise HTTPException(status_code=502, detail=f"{msg}" + (f" (log: {log_path})" if log_path else ""))
        return {"model": model_name, "message": msg, "remote": True, "optimized": req.enable}
    finally:
        process_state["lmstudio_optimize"]["running"] = False


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
        return {"ok": False, "step": "models", "error": str(e),
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
            "models": models, "model_present": model_present, "reply": reply,
            # Computed from the base_url actually tested (not the saved
            # llm_mode) so the caller can label this result correctly even
            # when testing a not-yet-saved profile.
            "is_remote": not is_local_llm_endpoint(base_url)}


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

@app.get("/api/config")
async def get_config():
    default_config = {
        "llm": {
            "base_url": "http://localhost:11434/v1",
            "api_key": "local",
            "model_name": "richardyoung/qwen3-14b-abliterated:Q8_0"
        },
        "llm_mode": "local",
        "tts": TTSConfig().model_dump(),
        "prompts": {
            "system_prompt": "",
            "user_prompt": ""
        }
    }

    if not os.path.exists(CONFIG_PATH):
        sys_prompt, usr_prompt = load_default_prompts()
        default_config["prompts"]["system_prompt"] = sys_prompt
        default_config["prompts"]["user_prompt"] = usr_prompt
        try:
            rev_sys, rev_usr = load_review_prompts()
            default_config["prompts"]["review_system_prompt"] = rev_sys
            default_config["prompts"]["review_user_prompt"] = rev_usr
        except RuntimeError:
            pass
        try:
            per_sys, per_usr, per_adv = load_persona_prompts()
            default_config["prompts"]["persona_system_prompt"] = per_sys
            default_config["prompts"]["persona_user_prompt"] = per_usr
            default_config["prompts"]["persona_advanced_prompt"] = per_adv
        except RuntimeError:
            pass
        config = default_config
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

    # Backfill any TTSConfig field missing from an existing on-disk config.json
    # (e.g. saved before pause_between_speakers_ms/pause_same_speaker_ms or some
    # other field existed) with that field's model default. save_config() always
    # writes every field via the pydantic model, so this only matters for a file
    # that hasn't been re-saved since a field was added - without it, GET would
    # silently omit fields the TTSConfig model promises always have a default.
    config["tts"] = {**TTSConfig().model_dump(), **(config.get("tts") or {})}

    # Ensure prompts section exists with defaults from file. Treat an explicit
    # null (config saved without a prompts field) the same as a missing key so
    # the dict-access branches below don't crash on None.
    if not config.get("prompts"):
        sys_prompt, usr_prompt = load_default_prompts()
        prompts = {"system_prompt": sys_prompt, "user_prompt": usr_prompt}
        try:
            rev_sys, rev_usr = load_review_prompts()
            prompts["review_system_prompt"] = rev_sys
            prompts["review_user_prompt"] = rev_usr
        except RuntimeError:
            pass
        try:
            per_sys, per_usr, per_adv = load_persona_prompts()
            prompts["persona_system_prompt"] = per_sys
            prompts["persona_user_prompt"] = per_usr
            prompts["persona_advanced_prompt"] = per_adv
        except RuntimeError:
            pass
        config["prompts"] = prompts
    else:
        if not config["prompts"].get("system_prompt") or not config["prompts"].get("user_prompt"):
            sys_prompt, usr_prompt = load_default_prompts()
            if not config["prompts"].get("system_prompt"):
                config["prompts"]["system_prompt"] = sys_prompt
            if not config["prompts"].get("user_prompt"):
                config["prompts"]["user_prompt"] = usr_prompt
        if not config["prompts"].get("review_system_prompt") or not config["prompts"].get("review_user_prompt"):
            try:
                rev_sys, rev_usr = load_review_prompts()
                if not config["prompts"].get("review_system_prompt"):
                    config["prompts"]["review_system_prompt"] = rev_sys
                if not config["prompts"].get("review_user_prompt"):
                    config["prompts"]["review_user_prompt"] = rev_usr
            except RuntimeError:
                pass  # review_prompts.txt missing or malformed — leave fields empty
        if not config["prompts"].get("persona_system_prompt") or not config["prompts"].get("persona_user_prompt") or not config["prompts"].get("persona_advanced_prompt"):
            try:
                per_sys, per_usr, per_adv = load_persona_prompts()
                if not config["prompts"].get("persona_system_prompt"):
                    config["prompts"]["persona_system_prompt"] = per_sys
                if not config["prompts"].get("persona_user_prompt"):
                    config["prompts"]["persona_user_prompt"] = per_usr
                if not config["prompts"].get("persona_advanced_prompt"):
                    config["prompts"]["persona_advanced_prompt"] = per_adv
            except RuntimeError:
                pass

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
    state_path = os.path.join(DATA_DIR, "state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as sf:
                state = json.load(sf)
            input_path = state.get("input_file_path", "")
            if input_path and os.path.exists(input_path):
                config["current_file"] = os.path.basename(input_path)
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("state", state_path, "ignoring current_file", e)

    # Precomputed drift-aware answer to "is the active LLM endpoint remote?"
    # so the frontend doesn't have to re-derive it from llm_mode alone (which
    # can drift from the actual active base_url) - see lmstudio_settings.is_remote_llm.
    config["is_remote"] = is_remote_llm(config["llm_mode"], config.get("llm", {}).get("base_url", ""))

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


def _claim_unique_path(directory: str, filename: str) -> str:
    """Atomically reserve a unique path in directory for filename, returning the
    path to a newly-created empty file the caller should now write/truncate into.

    A directory scan picks a good starting candidate (avoiding O(n) O_EXCL
    failures when the directory is large), then os.O_EXCL claims it -
    closing the TOCTOU race a scan-then-write approach has under concurrent
    uploads of the same filename. Caps at 1000 attempts to prevent a DoS
    from a maliciously pre-populated directory.
    """
    existing = {e.name for e in os.scandir(directory) if e.is_file()}
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while True:
        if candidate not in existing:
            path = os.path.join(directory, candidate)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.close(fd)
                return path
            except FileExistsError:
                pass  # lost the race - fall through and try the next candidate
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


def _safe_extractall(zf: "zipfile.ZipFile", dest_dir: str) -> None:
    """zipfile.extractall, but reject members that would escape dest_dir
    (Zip-Slip path traversal via '../' entries or absolute paths)."""
    # Resolve dest_dir's realpath once, not per member - is_path_inside
    # re-resolves its base_dir argument on every call (even when given an
    # already-canonical path, realpath still re-walks/lstats it), which
    # would otherwise mean one extra realpath syscall per ZIP entry. Inlined
    # rather than routed through is_path_inside for that reason.
    dest = os.path.realpath(dest_dir)
    members = zf.infolist()
    if len(members) > 100000:
        raise HTTPException(status_code=400, detail="Archive contains too many files.")
    if sum(member.file_size for member in members) > 20 * 1024**3:
        raise HTTPException(status_code=400, detail="Archive expands beyond the 20 GB limit.")
    for member in members:
        target = os.path.realpath(os.path.join(dest_dir, member.filename))
        if target != dest and not target.startswith(dest + os.sep):
            raise HTTPException(status_code=400, detail="Archive contains an unsafe path.")
    zf.extractall(dest_dir)


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


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    # Validate and sanitize filename to prevent path traversal
    safe_name = _require_safe_filename(file.filename or "", "Invalid or empty filename")
    file_path = await asyncio.to_thread(_claim_unique_path, UPLOADS_DIR, safe_name)
    await _save_upload_limited(file, file_path, 512 * 1024**2)

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
    state_path = os.path.join(DATA_DIR, "state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                _warn_corrupted_json("state", state_path, "overwriting with new data", e)

    state["input_file_path"] = file_path
    state["active_book_id"] = secure_filename(os.path.splitext(os.path.basename(file_path))[0])
    atomic_json_write(state, state_path)

    return {"filename": file.filename, "stored_filename": os.path.basename(file_path), "path": file_path}

@app.post("/api/generate_script")
async def generate_script(background_tasks: BackgroundTasks):
    # Get input file from state.json
    state_path = os.path.join(DATA_DIR, "state.json")
    if not os.path.exists(state_path):
        raise HTTPException(status_code=400, detail="No input file selected")

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
        input_file = state.get("input_file_path")

    if not input_file:
         raise HTTPException(status_code=400, detail="No input file found in state")

    check_global_gpu_lock("script")

    claim_gpu_task("script")
    background_tasks.add_task(run_process, [sys.executable, "-u", "generate_script.py", input_file], "script")
    return {"status": "started"}

@app.post("/api/generate_script/cancel")
async def generate_script_cancel():
    return _cancel_task("script", "No script generation is currently running.", "Script generation process already exited.")



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

    window_size = max(1, min(int(request.window_size or 4), 12))
    total_entries = 0
    try:
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            total_entries = len(json.load(f))
    except (json.JSONDecodeError, ValueError, OSError) as e:
        _warn_corrupted_json("script", SCRIPT_PATH, "estimated_calls will read 0", e)
        total_entries = 0

    review_batch_size = 25
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                review_batch_size = max(1, int((cfg.get("generation") or {}).get("review_batch_size", 25)))
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as e:
            _warn_corrupted_json("config", CONFIG_PATH, "using default review_batch_size", e)
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
                if stats is None:
                    # rc == 0 but the summary line is missing/malformed - we
                    # genuinely don't know whether this book finished cleanly
                    # or hit a VRAM abort with no recorded summary. Treat as
                    # incomplete rather than silently calling it "done".
                    state["tasks"][i]["status"] = "incomplete"
                elif stats.get("batches_skipped_vram", 0) > 0:
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
                    # Only combine stats_bwd in if this run is actually
                    # bidirectional - otherwise stats_bwd is never populated
                    # by design (no backward pass ever runs), and combining
                    # it in would mark every single-pass book "partial" even
                    # when that book's one-and-only pass succeeded cleanly.
                    pass_stats = [state["tasks"][i].get("stats_fwd")]
                    if state.get("bidirectional"):
                        pass_stats.append(state["tasks"][i].get("stats_bwd"))
                    state["tasks"][i]["stats"] = _combine_pass_stats(*pass_stats)
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

        # Forward pass (reading order)
        state["current_pass"] = "fwd"
        if bidirectional:
            state["logs"].append("=== Forward pass (reading order) ===")
        for i, name in enumerate(names):
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
            for i in range(total - 1, -1, -1):
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
        state["logs"].append("Batch review finished.")

    claim_gpu_task("batch_review")
    background_tasks.add_task(_run_claimed_background_task, "batch_review", _run)
    return {"status": "started", "task_count": total, "bidirectional": bidirectional}




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

@app.post("/api/generate_script/batch/start")
async def generate_script_batch_start(request: BatchScriptRequest, background_tasks: BackgroundTasks):
    """Process multiple text/EPUB files sequentially through generate_script.py."""
    check_global_gpu_lock("batch_script")
    if not request.tasks:
        raise HTTPException(status_code=400, detail="No files provided.")

    def _run():
        state = process_state["batch_script"]
        _init_batch_state(state,
                          [f"Starting batch of {len(request.tasks)} file(s)..."],
                          [{"filename": t.filename, "status": "pending"} for t in request.tasks])

        # One full on-disk log for the whole batch (in-memory list is a capped tail)
        log_path = _init_task_log("batch_script")

        for i, task in enumerate(request.tasks):
            if state["cancel"]:
                state["logs"].append("Batch cancelled.")
                break

            state["current_task_idx"] = i
            state["tasks"][i]["status"] = "running"

            # Resolve upload path — handle epub→txt conversion
            safe_filename = secure_filename(task.filename)
            if not safe_filename:
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — invalid filename: {task.filename}")
                state["tasks"][i]["status"] = "failed"
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
            rc, _ = _stream_subprocess_to_logs(cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ", log_file=log_path)

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                break
            elif rc == 0:
                state["tasks"][i]["status"] = "done"
                state["tasks"][i]["saved_as"] = safe_stem
                state["logs"].append(f"[{i+1}] Saved as '{safe_stem}' in Scripts library.")
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{i+1}] Failed (exit {rc}): {task.filename}")

        state["running"] = False
        state["logs"].append("Batch script generation finished.")

    claim_gpu_task("batch_script")
    background_tasks.add_task(_run_claimed_background_task, "batch_script", _run)
    return {"status": "started", "task_count": len(request.tasks)}


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
    """Return the current working annotated_script.json.

    No SPA caller - intentionally kept as a programmatic/curl-accessible
    read endpoint (exercised by test_api.py's test_get_annotated_script).
    """
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
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("script", SCRIPT_PATH, "returning empty voice list", e)

    if not voices_list:
        return []

    # Combine with config
    voice_config = {}
    if os.path.exists(VOICE_CONFIG_PATH):
        try:
            with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                voice_config = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("voice config", VOICE_CONFIG_PATH, "ignoring", e)
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
            _send_signal_tree(proc, signal.SIGTERM)
        except (ProcessLookupError, OSError) as e:
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
                    except (json.JSONDecodeError, ValueError) as e:
                        _warn_corrupted_json("voice config", VOICE_CONFIG_PATH, "overwriting with new data", e)

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


AGE_GROUPS = ("child", "teen", "young_adult", "adult", "middle_aged", "elderly")


def _age_group_from_years(age):
    age = int(age)
    if age <= 12:
        return "child"
    if age <= 19:
        return "teen"
    if age <= 29:
        return "young_adult"
    if age <= 39:
        return "adult"
    if age <= 59:
        return "middle_aged"
    return "elderly"


def _infer_age_group(text):
    """Best-effort normalized apparent age from names, profiles, or dialogue."""
    value = (text or "").lower().replace("-", " ").replace("_", " ")
    numeric = re.search(r"\b(?:aged?\s+(\d{1,3})|(\d{1,3})\s+years?\s+old)\b", value)
    if not numeric and re.fullmatch(r"\s*\d{1,3}\s*", value):
        numeric = re.match(r"\s*(\d{1,3})", value)
    if numeric:
        years = next(group for group in numeric.groups() if group is not None)
        if 1 <= int(years) <= 120:
            return _age_group_from_years(years)
    decade = re.search(r"\b([2-8])0s\b", value)
    if decade:
        return _age_group_from_years(int(decade.group(1)) * 10 + 5)
    patterns = (
        ("child", r"\b(child|kid|little boy|little girl|preteen|under ?1[0-2])\b"),
        ("teen", r"\b(teen|teenage|adolescent)\b"),
        ("young_adult", r"\b(young adult|young man|young woman|twent(?:y|ies))\b"),
        ("middle_aged", r"\b(middle aged|middle age|forties|fifties)\b"),
        ("elderly", r"\b(elderly|old man|old woman|senior|sixties|seventies|eighties)\b"),
        ("adult", r"\b(adult|grown man|grown woman|thirties)\b"),
    )
    return next((group for group, pattern in patterns if re.search(pattern, value)), "unknown")


def _infer_lora_age(model):
    explicit = str(model.get("age_group") or model.get("age") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if explicit in AGE_GROUPS:
        return explicit
    evidence = " ".join(str(model.get(k) or "") for k in ("age", "name", "id", "description", "voice_profile"))
    return _infer_age_group(evidence)


def _infer_character_traits(name, profile, lines):
    """Infer traits with evidence priority: label, persona, then dialogue."""
    sources = (("character label", name, "high"),
               ("existing persona/style", profile, "medium"),
               ("representative dialogue", " ".join(lines), "low"))
    result = {"gender": "unknown", "gender_confidence": "unknown",
              "age_group": "unknown", "age_confidence": "unknown", "trait_evidence": ""}
    evidence = []
    for source, text, confidence in sources:
        if result["gender"] == "unknown":
            gender = _infer_character_gender(text)
            if gender != "unknown":
                result.update(gender=gender, gender_confidence=confidence)
                evidence.append(f"{source}: {gender}")
        if result["age_group"] == "unknown":
            age = _infer_age_group(text)
            if age != "unknown":
                result.update(age_group=age, age_confidence=confidence)
                evidence.append(f"{source}: {age.replace('_', ' ')}")
    result["trait_evidence"] = "; ".join(evidence) or "No explicit gender or age evidence"
    result["local_trait_evidence"] = result["trait_evidence"]
    result["llm_trait_evidence"] = ""
    return result


def _age_distance(character_age, voice_age):
    if character_age == "unknown" or voice_age == "unknown":
        return 2
    return abs(AGE_GROUPS.index(character_age) - AGE_GROUPS.index(voice_age))


def _is_authoritative_confidence(confidence):
    return confidence in ("high", "medium")


def _is_stronger_authoritative_confidence(current, proposed):
    confidence_rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
    return (_is_authoritative_confidence(proposed)
            and confidence_rank.get(proposed, 0) > confidence_rank.get(current, 0))


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
            "age_group": _infer_lora_age(m),
            "description": m.get("description") or m.get("voice_profile") or "",
        })
    for m in _load_manifest(LORA_MODELS_MANIFEST):
        candidates.append({
            "adapter_id": m["id"],
            "name": m.get("name") or m["id"],
            "type": "lora",
            "gender": _infer_lora_gender(m),
            "age_group": _infer_lora_age(m),
            "description": m.get("description") or m.get("voice_profile") or "",
        })
    return candidates


def _select_representative_lines(lines: List[str], limit: int) -> List[str]:
    """Sample dialogue across the whole book rather than only its beginning."""
    if len(lines) <= limit:
        return lines
    if limit <= 1:
        return [lines[0]]
    indices = [round(i * (len(lines) - 1) / (limit - 1)) for i in range(limit)]
    return [lines[i] for i in dict.fromkeys(indices)]


def _rank_heuristic_candidates(profile: str, candidates: List[dict], preferred_gender=None,
                               preferred_age="unknown", filter_gender=True) -> List[str]:
    gender = preferred_gender if preferred_gender in ("male", "female") else _infer_character_gender(profile)
    pool = candidates
    if filter_gender and gender != "unknown":
        pool = [c for c in candidates if c.get("gender") == gender] or candidates
    words = set(re.findall(r"[a-z]{4,}", profile.lower()))
    ranked = sorted(pool, key=lambda c: (
        _age_distance(preferred_age, c.get("age_group", "unknown")),
        -len(words & set(re.findall(r"[a-z]{4,}", c.get("description", "").lower()))),
        c["adapter_id"]))
    return [c["adapter_id"] for c in ranked]


def get_voice_allocation(profile, candidates, initial_ranked, traits,
                         existing_adapter, usage, priority):
    """Pure compatibility/reuse decision; the caller owns usage mutation."""
    cand_by_id = {c["adapter_id"]: c for c in candidates}
    hard_gender = (traits["gender"] != "unknown"
                   and _is_authoritative_confidence(traits["gender_confidence"]))
    hard_age = _is_authoritative_confidence(traits["age_confidence"])
    gender_matches = [c for c in candidates if c.get("gender") == traits["gender"]]
    gender_fallback = hard_gender and not gender_matches
    ranked = [adapter_id for adapter_id in initial_ranked if adapter_id in cand_by_id]
    for adapter_id in _rank_heuristic_candidates(
            profile, candidates, traits["gender"],
            traits["age_group"] if hard_age else "unknown",
            filter_gender=False):
        if adapter_id not in ranked:
            ranked.append(adapter_id)
    rank_order = {adapter_id: index for index, adapter_id in enumerate(ranked)}

    def allocation_score(adapter_id):
        candidate = cand_by_id[adapter_id]
        candidate_gender = candidate.get("gender", "unknown")
        hard_gender_tier = 0
        soft_gender_penalty = 0
        if hard_gender:
            if candidate_gender != traits["gender"]:
                hard_gender_tier = 1 if candidate_gender == "unknown" else 2
        elif traits["gender"] != "unknown" and candidate_gender != traits["gender"]:
            soft_gender_penalty = 1 if candidate_gender == "unknown" else 3
        distance = _age_distance(traits["age_group"], candidate.get("age_group", "unknown"))
        age_penalty = distance * (100 if hard_age else 1)
        reuse_penalty = 100 if priority == "major" else 2
        compatibility_and_reuse = (
            rank_order[adapter_id] * 10 + age_penalty + soft_gender_penalty
            + usage.get(adapter_id, {}).get("character_count", 0) * reuse_penalty)
        return hard_gender_tier, compatibility_and_reuse, adapter_id

    ranked.sort(key=allocation_score)
    if existing_adapter in cand_by_id:
        chosen_id, is_new_identity = existing_adapter, False
    else:
        chosen_id = ranked[0]
        is_new_identity = True
    chosen = cand_by_id[chosen_id]
    existing_trait_mismatch = bool(existing_adapter and (
        (_is_authoritative_confidence(traits["gender_confidence"])
         and traits["gender"] != "unknown"
         and chosen.get("gender") not in (traits["gender"], "unknown"))
        or (_is_authoritative_confidence(traits["age_confidence"])
            and _age_distance(traits["age_group"], chosen.get("age_group", "unknown")) >= 3)))
    return chosen_id, ranked, is_new_identity, gender_fallback, existing_trait_mismatch


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

    # Collect every per-character dialogue line so counts are accurate; sample
    # representative lines across the book only when building the prompt.
    samples = {}
    for entry in script:
        speaker = (entry.get("speaker") or entry.get("type") or "").strip()
        text = (entry.get("text") or "").strip()
        if not speaker or not text:
            continue
        lines = samples.setdefault(speaker, [])
        if text not in lines:
            lines.append(text)
    if not samples:
        return {"method": "none", "suggestions": {}, "message": "No characters found in script."}

    # Existing config (for persona descriptions/styles + only_unset filtering)
    voice_config = {}
    if os.path.exists(VOICE_CONFIG_PATH):
        try:
            with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                voice_config = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("voice config", VOICE_CONFIG_PATH, "treating as empty", e)
            voice_config = {}

    candidates = _build_lora_candidates()
    if not candidates:
        raise HTTPException(status_code=400, detail="No downloaded LoRA voices available. Download a built-in voice or train an adapter first.")

    line_limit = max(1, min(int(request.max_lines or 8), 30))
    book_id = get_active_book_id()
    lib = _load_voice_library()
    cast_name = (request.cast or "").strip() or None
    if cast_name and cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")
    usage = get_cast_adapter_usage(lib, cast_name)
    line_counts = _script_line_counts()

    # Build profiles in importance order: narrator, then most dialogue lines.
    characters = {}
    ordered_names = sorted(samples, key=lambda n: (0 if _norm_name(n) == "narrator" else 1, -len(samples[n]), _norm_name(n)))
    for speaker in ordered_names:
        lines = samples[speaker]
        if request.only_unset:
            existing = voice_config.get(speaker, {})
            if voice_category(existing) == "lora" and existing.get("adapter_id"):
                continue
        cfg = voice_config.get(speaker, {})
        persona_bits = [cfg.get("description") or "", cfg.get("character_style") or "", cfg.get("default_style") or ""]
        profile = " ".join(b for b in persona_bits if b)
        traits = _infer_character_traits(speaker, profile, lines)
        count = line_counts.get(speaker, len(lines))
        try:
            member_key = get_cast_member_key(speaker, book_id)
        except ValueError:
            member_key = None
        characters[speaker] = {
            "profile": profile,
            "lines": _select_representative_lines(lines, line_limit),
            "line_count": count,
            "priority": "major" if _norm_name(speaker) == "narrator" or count >= CAST_MAJOR_LINE_THRESHOLD else "minor",
            "member_key": member_key,
            **traits,
        }

    if not characters:
        return {"method": "none", "suggestions": {}, "message": "No characters to suggest (all already set)."}

    # When only filling unset roles, already-configured current-book roles are
    # fixed assignments and must contribute to reuse pressure unless the same
    # identity is already represented in the selected cast.
    if request.only_unset:
        cast_members = (lib.get("casts", {}).get(cast_name, {}).get("members", {})
                        if cast_name else {})
        for name, cfg in voice_config.items():
            adapter_id = (cfg or {}).get("adapter_id")
            if not adapter_id or voice_category(cfg) != "lora":
                continue
            try:
                key = get_cast_member_key(name, book_id)
            except ValueError:
                continue
            if key in cast_members:
                continue
            item = usage.setdefault(adapter_id, {"character_count": 0, "total_lines": 0, "characters": []})
            item["character_count"] += 1
            item["total_lines"] += line_counts.get(name, 0)
            item["characters"].append(name)

    cand_by_id = {c["adapter_id"]: c for c in candidates}
    suggestions = {}
    rankings = {}
    style_by_name = {}
    reason_by_name = {}
    method = "heuristic"
    llm_warning = None

    # --- Try LLM ranking first ---
    llm_ok = False
    try:
        # don't let a stuck model hang the worker thread forever
        client, model_name = _make_llm_client(timeout=120)

        voice_catalog = "\n".join(
            f'- id="{c["adapter_id"]}" | name="{c["name"][:50]}" | gender={c.get("gender", "unknown")} | age={c.get("age_group", "unknown")} | series_use={usage.get(c["adapter_id"], {}).get("character_count", 0)} | description: {(c["description"] or "(none)")[:80]}'
            for c in candidates
        )
        system_prompt = (
            "You are a casting director matching narrated audiobook characters to available LoRA TTS voices. "
            "For each character, rank up to three fitting voice ids and write concise TTS delivery guidance based only on the book text. "
            "The style should describe cadence, energy, formality, confidence, and supported emotion; do not invent biography or accent. "
            "Infer gender and broad apparent age only when supported by the supplied book evidence. "
            "Known character gender must match voice gender; prefer the closest available age group. "
            "Only use provided voice ids. Return every requested character in the structured response."
        )
        casting_schema = {
            "name": "audiobook_casting",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "characters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "ranked_adapter_ids": {
                                    "type": "array", "items": {"type": "string"},
                                    "minItems": 1, "maxItems": 3,
                                },
                                "character_style": {"type": "string"},
                                "reason": {"type": "string"},
                                "character_gender": {"type": "string", "enum": ["male", "female", "unknown"]},
                                "age_group": {"type": "string", "enum": ["child", "teen", "young_adult", "adult", "middle_aged", "elderly", "unknown"]},
                                "trait_evidence": {"type": "string"},
                                "trait_confidence": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                            },
                            "required": ["name", "ranked_adapter_ids", "character_style", "reason", "character_gender", "age_group", "trait_evidence", "trait_confidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["characters"],
                "additionalProperties": False,
            },
        }
        character_items = list(characters.items())
        full_cfg = safe_load_json(CONFIG_PATH, default={})
        llm_cfg = full_cfg.get("llm") or {}
        status = get_current_status(
            full_cfg.get("llm_mode", "local"), llm_cfg.get("base_url", ""),
            model_name, (full_cfg.get("llm_remote_ssh") or "").strip(),
            use_cache=True)
        for start in range(0, len(character_items), 2):
            batch = character_items[start:start + 2]
            char_block = "\n\n".join(
                f'CHARACTER: {name}\nLines: {info["line_count"]} ({info["priority"]})\nCurrent trait estimate: gender={info["gender"]}, age={info["age_group"]}\nPersona/style: {(info["profile"] or "(none)")[:200]}\nSample lines:\n'
                + "\n".join(f'  - "{ln[:140]}"' for ln in info["lines"])
                for name, info in batch
            )
            user_prompt = f"AVAILABLE VOICES:\n{voice_catalog}\n\nCHARACTERS:\n{char_block}"
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            effective_max = get_effective_max_tokens(
                2600, status.get("context_length"), messages, hard_max=12000)
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                response_format={"type": "json_schema", "json_schema": casting_schema},
                temperature=0.3,
                max_tokens=effective_max,
                timeout=120,
            )
            raw = response.choices[0].message.content or ""
            parsed = extract_json_object(raw)
            if parsed is None:
                finish_reason = response.choices[0].finish_reason
                logger.warning("Unparseable casting response (%s) preview: %s", finish_reason, raw[:500])
                raise ValueError(f"Could not parse a JSON object from casting batch ({len(raw)} chars)")
            parsed_items = parsed.get("characters", []) if isinstance(parsed, dict) else []
            parsed_by_name = {
                item.get("name"): item for item in parsed_items
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            for name, _info in batch:
                pick = parsed_by_name.get(name)
                if isinstance(pick, dict):
                    ranked = pick.get("ranked_adapter_ids") or ([pick.get("adapter_id")] if pick.get("adapter_id") else [])
                    rankings[name] = list(dict.fromkeys(i for i in ranked if i in cand_by_id))
                    style_by_name[name] = (pick.get("character_style") or "").strip()[:500]
                    reason_by_name[name] = (pick.get("reason") or "").strip()[:240]
                    info = characters[name]
                    llm_confidence = pick.get("trait_confidence", "unknown")
                    llm_gender = pick.get("character_gender")
                    llm_age = pick.get("age_group")
                    accepted_traits = []
                    rejected_conflict = False
                    if (llm_gender in ("male", "female")
                            and _is_stronger_authoritative_confidence(
                                info["gender_confidence"], llm_confidence)):
                        info["gender"] = llm_gender
                        info["gender_confidence"] = llm_confidence
                        accepted_traits.append(f"gender={llm_gender}")
                    elif llm_gender in ("male", "female") and llm_gender != info["gender"]:
                        rejected_conflict = True
                    if (llm_age in AGE_GROUPS
                            and _is_stronger_authoritative_confidence(
                                info["age_confidence"], llm_confidence)):
                        info["age_group"] = llm_age
                        info["age_confidence"] = llm_confidence
                        accepted_traits.append(f"age={llm_age.replace('_', ' ')}")
                    elif llm_age in AGE_GROUPS and llm_age != info["age_group"]:
                        rejected_conflict = True
                    info["llm_trait_evidence"] = (pick.get("trait_evidence") or "")[:300]
                    if accepted_traits and info["llm_trait_evidence"]:
                        llm_evidence = ("LM accepted " + ", ".join(accepted_traits)
                                        if rejected_conflict else f"LM: {info['llm_trait_evidence']}")
                        info["trait_evidence"] = (
                            f"Local: {info['local_trait_evidence']}; {llm_evidence}")[:300]
    except LLMConfigError as e:
        # Config issue (e.g. base_url rejected by _validate_local_llm_base_url) -
        # surface to the UI instead of silently falling back to heuristic.
        llm_warning = str(e)
    except Exception as e:
        logger.warning(f"LLM voice suggestion failed, falling back to heuristic: {e}")

    if rankings:
        llm_ok = True
        method = "llm"

    # Fill missing rankings/styles deterministically, then allocate in priority
    # order while updating reuse counts after every new distinct character.
    for name, info in characters.items():
        profile_text = " ".join([name, info["profile"]] + info["lines"])
        if not rankings.get(name):
            rankings[name] = _rank_heuristic_candidates(
                profile_text, candidates, info["gender"],
                info["age_group"] if _is_authoritative_confidence(info["age_confidence"]) else "unknown",
                filter_gender=_is_authoritative_confidence(info["gender_confidence"]))
        if not style_by_name.get(name):
            style_by_name[name] = info["profile"] or "Natural delivery matching the character's dialogue and role in this book."
        if not reason_by_name.get(name):
            reason_by_name[name] = "Deterministic compatibility and series-diversity ranking"

        existing_member = None
        if cast_name and info["member_key"]:
            existing_member = (lib["casts"][cast_name].get("members", {}).get(info["member_key"])
                               or lib.get("shared", {}).get(info["member_key"]))
        existing_adapter = ((existing_member or {}).get("config") or {}).get("adapter_id")
        (chosen_id, ranked, is_new_identity, gender_fallback,
         existing_trait_mismatch) = get_voice_allocation(
            profile_text, candidates, rankings[name], info, existing_adapter,
            usage, info["priority"])
        before = usage.get(chosen_id, {}).get("character_count", 0)
        if is_new_identity:
            usage.setdefault(chosen_id, {"character_count": 0, "total_lines": 0, "characters": []})
            usage[chosen_id]["character_count"] += 1
            usage[chosen_id]["total_lines"] += info["line_count"]
            usage[chosen_id]["characters"].append(name)
        chosen = cand_by_id[chosen_id]
        suggestions[name] = {
            "adapter_id": chosen_id, "adapter_name": chosen["name"], "type": chosen["type"],
            "character_style": style_by_name[name], "reason": reason_by_name[name],
            "line_count": info["line_count"], "priority": info["priority"], "book_id": book_id,
            "cast_member_key": info["member_key"], "reuse_count_before": before,
            "reuse_count_after": before + (1 if is_new_identity else 0),
            "reused": before > 0 and is_new_identity,
            "forced_reuse": info["priority"] == "major" and before > 0 and all(usage.get(i, {}).get("character_count", 0) > 0 for i in ranked),
            "character_gender": info["gender"], "character_age_group": info["age_group"],
            "voice_gender": chosen.get("gender", "unknown"),
            "voice_age_group": chosen.get("age_group", "unknown"),
            "trait_evidence": info["trait_evidence"],
            "local_trait_evidence": info["local_trait_evidence"],
            "llm_trait_evidence": info["llm_trait_evidence"],
            "gender_confidence": info["gender_confidence"],
            "age_confidence": info["age_confidence"],
            "gender_fallback": gender_fallback,
            "existing_trait_mismatch": existing_trait_mismatch,
        }

    if not llm_ok and suggestions:
        method = "heuristic"

    return {"method": method, "suggestions": suggestions, "candidate_count": len(candidates),
            "adapter_usage": usage, "book_id": book_id, "cast": cast_name,
            "major_line_threshold": CAST_MAJOR_LINE_THRESHOLD, "llm_warning": llm_warning}


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


def _apply_voice_suggestions(suggestions: Dict[str, dict], cast_name: Optional[str]) -> dict:
    candidates = {c["adapter_id"]: c for c in _build_lora_candidates()}
    counts = _script_line_counts()
    book_id = get_active_book_id()
    if cast_name and not book_id:
        raise HTTPException(status_code=400, detail="Active book identity is required to save suggestions to a cast.")

    with file_lock(VOICE_LIBRARY_PATH), file_lock(VOICE_CONFIG_PATH):
        voice_config = safe_load_json(VOICE_CONFIG_PATH, default={})
        lib = _load_voice_library()
        if cast_name and cast_name not in lib["casts"]:
            raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")
        usage = get_cast_adapter_usage(lib, cast_name)
        applied = []
        for character, suggestion in suggestions.items():
            if character not in counts:
                continue
            suggestion_book_id = secure_filename(suggestion.get("book_id") or "")
            if suggestion_book_id != secure_filename(book_id or ""):
                raise HTTPException(status_code=409, detail=(
                    f"Suggestion for '{character}' belongs to a different book. Generate suggestions again."))
            adapter_id = suggestion.get("adapter_id")
            candidate = candidates.get(adapter_id)
            if not candidate:
                raise HTTPException(status_code=400, detail=f"Unknown or unavailable LoRA adapter: {adapter_id}")
            style = (suggestion.get("character_style") or "").strip()[:500]
            cfg = dict(voice_config.get(character) or {})
            cfg.update({
                "type": candidate["type"], "adapter_id": adapter_id,
                "adapter_path": (f"builtin_lora/{adapter_id}" if candidate["type"] == "builtin_lora"
                                 else f"lora_models/{adapter_id}"),
                "character_style": style, "seed": "-1",
                **get_trait_assignment_metadata(suggestion),
            })
            voice_config[character] = cfg

            if cast_name:
                try:
                    key = get_cast_member_key(character, book_id)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                members = get_cast_storage_pool(lib, cast_name, character)
                casting = {
                    "priority": suggestion.get("priority"),
                    "suggestion_reason": (suggestion.get("reason") or "")[:240],
                    "reuse_count_when_assigned": usage.get(adapter_id, {}).get("character_count", 0),
                    **get_trait_assignment_metadata(suggestion),
                }
                members[key] = _make_library_entry(
                    character, cfg, counts[character], book_id, casting, members.get(key))
                usage = get_cast_adapter_usage(lib, cast_name)
            applied.append(character)

        if cast_name:
            atomic_json_write_pair(voice_config, VOICE_CONFIG_PATH,
                                   lib, VOICE_LIBRARY_PATH)
        else:
            atomic_json_write(voice_config, VOICE_CONFIG_PATH)
    return {"applied": applied, "count": len(applied), "cast": cast_name,
            "book_id": book_id, "adapter_usage": get_cast_adapter_usage(lib, cast_name)}


@app.post("/api/suggest_voices/apply")
async def apply_voice_suggestion(request: VoiceSuggestionApplyRequest):
    return await asyncio.to_thread(
        _apply_voice_suggestions, {request.character: request.suggestion},
        (request.cast or "").strip() or None)


@app.post("/api/suggest_voices/apply_bulk")
async def apply_voice_suggestions_bulk(request: VoiceSuggestionApplyBulkRequest):
    return await asyncio.to_thread(
        _apply_voice_suggestions, request.suggestions,
        (request.cast or "").strip() or None)


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
        try:
            project_manager.generate_chunk_audio(index)
        finally:
            process_state["audio"]["running"] = False

    # Same GPU resource as /api/generate_batch - must not race it. See F-032.
    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started"}

@app.post("/api/merge")
async def merge_audio_endpoint(background_tasks: BackgroundTasks):
    # Reuse audio process state for merge if possible, or just background it
    # For simplicity, we just background it and frontend will assume it works
    # Or we can link it to process_state["audio"]

    def task():
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

    # Claim the GPU/TTS slot atomically on the request thread: a merge shares
    # process_state["audio"] with generation, so without this two rapid POSTs (or
    # a merge started during generation) both pass and clobber each other, and a
    # merge's early finally would free the lock while TTS is still in flight.
    claim_gpu_task("audio")
    background_tasks.add_task(task)
    return {"status": "started"}

@app.post("/api/export_audacity")
async def export_audacity_endpoint(background_tasks: BackgroundTasks):
    # Atomic check-and-set on the request thread (closes the double-start TOCTOU
    # where two rapid POSTs both pass a plain running check before either sets it).
    # audacity_export is a NON_GPU_TASK, so this only guards against self-double-start.
    claim_gpu_task("audacity_export")

    def task():
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

@app.post("/api/merge_m4b")
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
    cover_path = os.path.join(DATA_DIR, "m4b_cover.jpg")
    cover_tmp = cover_path + ".upload"
    await _save_upload_limited(file, cover_tmp, 25 * 1024**2)
    os.replace(cover_tmp, cover_path)
    return {"status": "uploaded", "path": cover_path}

@app.delete("/api/m4b_cover")
async def delete_m4b_cover():
    """Remove the uploaded cover image."""
    cover_path = os.path.join(DATA_DIR, "m4b_cover.jpg")
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
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("config", CONFIG_PATH, "using default worker count", e)

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
        except (json.JSONDecodeError, ValueError) as e:
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
    any known companion/internal suffix (voice_config, metadata, checkpoint, etc.).
    """
    scripts = []
    companion_suffixes = (".voice_config.json", ".meta.json", ".review_checkpoint.json", ".checkpoint.jsonl")
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

    safe_name = _require_safe_filename(request.name, "Invalid script name.")

    dest = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    shutil.copy2(SCRIPT_PATH, dest)

    if os.path.exists(VOICE_CONFIG_PATH):
        shutil.copy2(VOICE_CONFIG_PATH, os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json"))
    atomic_json_write({"book_id": get_active_book_id() or safe_name},
                      _saved_book_meta_path(safe_name))

    logger.info(f"Script saved as '{safe_name}'")
    return {"status": "saved", "name": safe_name}

class ScriptLoadRequest(BaseModel):
    name: str

@app.post("/api/scripts/load")
async def load_script(request: ScriptLoadRequest):
    """Load a saved script, replacing the current annotated_script.json and chunks."""
    # Block while ANY task that writes annotated_script.json / voice_config.json
    # is running — not just audio. A script/review/persona/nicknames run finishes
    # by writing those files and would silently overwrite the book we load here.
    busy = [k for k in ("audio", "script", "review", "persona", "nicknames")
            if process_state.get(k, {}).get("running")]
    if busy:
        raise HTTPException(status_code=409,
            detail=f"Cannot load a script while these tasks are running: {', '.join(busy)}.")

    safe_name = _require_safe_filename(request.name, "Invalid script name.")

    src = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail=f"Saved script '{request.name}' not found.")

    shutil.copy2(src, SCRIPT_PATH)
    _save_active_book_id(_get_saved_book_id(safe_name), src)

    companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
    if os.path.exists(companion):
        shutil.copy2(companion, VOICE_CONFIG_PATH)
    elif os.path.exists(VOICE_CONFIG_PATH):
        os.remove(VOICE_CONFIG_PATH)

    # Delete chunks so they regenerate from the loaded script
    if os.path.exists(CHUNKS_PATH):
        os.remove(CHUNKS_PATH)

    # Clear any review checkpoint left over from the PREVIOUS active book. The
    # checkpoint is keyed to SCRIPT_PATH, not to a book identity (load_checkpoint
    # validates only batch_size/context_window), so a resume after this load would
    # otherwise splice the old book's corrected entries into the one just loaded.
    clear_checkpoint(SCRIPT_PATH)

    logger.info(f"Script '{request.name}' loaded")
    return {"status": "loaded", "name": request.name}

@app.delete("/api/scripts/{name}")
async def delete_script(name: str):
    """Delete a saved script."""
    safe_name = _require_safe_filename(name, "Invalid script name.")

    filepath = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"Saved script '{name}' not found.")

    os.remove(filepath)
    companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
    if os.path.exists(companion):
        os.remove(companion)
    meta_path = _saved_book_meta_path(safe_name)
    if os.path.exists(meta_path):
        os.remove(meta_path)
    checkpoint = _checkpoint_path(filepath)
    if os.path.exists(checkpoint):
        os.remove(checkpoint)

    logger.info(f"Script '{name}' deleted")
    return {"status": "deleted", "name": name}

## ── Series Voice Library (cross-book cast) ──────────────────────

# Character names that belong to the shared cross-series pool by default
# (a series usually keeps the same narrator unless it explicitly uses a different one).
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
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("voice library", VOICE_LIBRARY_PATH, "resetting to empty", e)
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


def _cast_match_pool(lib: dict, cast_name: str, book_id: Optional[str] = None,
                     include_all_generic: bool = False) -> dict:
    """Build the candidate pool for matching against a cast: shared first, cast
    members override on key collision (a cast-specific narrator beats the
    shared narrator = "different narrator")."""
    pool = {}
    for k, m in lib["shared"].items():
        pool[k] = {"key": k, "name": m.get("name", k), "source": "shared",
                   "type": (m.get("config") or {}).get("type")}
    for k, m in lib["casts"][cast_name].get("members", {}).items():
        if m.get("generic") and not include_all_generic and m.get("book_id") != book_id:
            continue
        if is_generic_speaker(m.get("name", k)) and not m.get("book_id"):
            continue  # legacy ambiguous generic entry
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
                         current_config: dict, chars: Optional[dict] = None,
                         book_id: Optional[str] = None) -> Tuple[dict, List[str]]:
    """Apply a confirmed character -> library member mapping onto a voice_config
    dict, returning a new dict (current_config is not mutated) along with the
    list of characters that were actually applied.

    If `chars` is given (the per-speaker line counts of a specific book), only
    characters present in it are considered — used by the bulk endpoint so a
    book only receives entries for characters that actually appear in it."""
    def resolve_entry(key):
        # cast members win over shared on collision
        return lib["casts"][cast_name].get("members", {}).get(key) or lib["shared"].get(key)

    result_config = dict(current_config)
    applied = []
    for char, key in mapping.items():
        if chars is not None and char not in chars:
            continue
        if book_id and is_generic_speaker(char):
            scoped_key = get_cast_member_key(char, book_id)
            if resolve_entry(scoped_key):
                key = scoped_key
        entry = resolve_entry(key)
        if not entry:
            continue
        cfg = dict(entry.get("config") or {})
        assignment = (entry.get("assignments") or {}).get(book_id or "", {})
        if assignment.get("character_style"):
            cfg["character_style"] = assignment["character_style"]
        for field in get_trait_assignment_metadata({}):
            if field in assignment:
                cfg[field] = assignment[field]
        cfg.pop("alias_of", None)
        # Preserve an existing alias_of on the current character (book-specific)
        if isinstance(result_config.get(char), dict) and result_config[char].get("alias_of"):
            cfg["alias_of"] = result_config[char]["alias_of"]
        result_config[char] = cfg
        applied.append(char)
    return result_config, applied


def _apply_cast_to_config_file(config_path: str, lib: dict, cast_name: str,
                                mapping: Dict[str, str], chars: Optional[dict] = None,
                                book_id: Optional[str] = None) -> List[str]:
    """Load a voice_config.json (if present), apply the cast mapping under a file
    lock, write it back atomically if anything changed, and return the list of
    characters that were applied.

    Raises TimeoutError if the lock can't be acquired - callers should map that
    to a 503 (single-book) or a per-book error entry (bulk).
    """
    with file_lock(config_path):
        current_config = safe_load_json(config_path, default={})

        current_config, applied = _apply_cast_mapping(
            lib, cast_name, mapping, current_config, chars=chars, book_id=book_id)

        if applied:
            atomic_json_write(current_config, config_path)
    return applied


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


@app.get("/api/voice_library")
async def voice_library_get():
    """Return the full library plus the current book's characters with line counts."""
    lib = _load_voice_library()
    counts = _script_line_counts()

    casts = []
    for cast_name, cast in sorted(lib["casts"].items()):
        members = cast.get("members", {})
        adapter_usage = get_cast_adapter_usage(lib, cast_name)
        casts.append({
            "name": cast_name,
            "member_count": len(members),
            "members": [
                {"key": k, "name": m.get("name", k), "type": (m.get("config") or {}).get("type"),
                 "adapter_id": (m.get("config") or {}).get("adapter_id"),
                 "character_style": (m.get("config") or {}).get("character_style", ""),
                 "line_count": m.get("line_count", 0), "generic": bool(m.get("generic")),
                 "book_id": m.get("book_id"), "assignments": m.get("assignments", {})}
                for k, m in sorted(members.items())
            ],
            "adapter_usage": adapter_usage,
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

    return {"casts": casts, "shared": shared, "current_characters": current_characters,
            "active_book_id": get_active_book_id(), "major_line_threshold": CAST_MAJOR_LINE_THRESHOLD}


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
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("voice config", VOICE_CONFIG_PATH, "ignoring", e)
            voice_config = {}

    counts = _script_line_counts()
    book_id = get_active_book_id()
    shared_override = {_norm_name(n) for n in (request.shared or [])}
    cast_specific = {_norm_name(n) for n in (request.cast_specific or [])}

    saved = {"cast": [], "shared": []}
    for char in request.characters:
        config = voice_config.get(char)
        if not config:
            continue  # nothing configured for this character; skip
        try:
            key = get_cast_member_key(char, book_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Narrator (or explicitly flagged) goes to the shared cross-series pool,
        # unless this book uses a different narrator (forced cast-specific).
        is_shared = (key in SHARED_DEFAULT_NAMES or key in shared_override) and key not in cast_specific
        if is_shared:
            pool = get_cast_storage_pool(lib, cast_name, char)
            entry = _make_library_entry(char, config, counts.get(char, 0), book_id,
                                        existing=pool.get(key))
            pool[key] = entry
            saved["shared"].append(char)
        else:
            members = lib["casts"][cast_name].setdefault("members", {})
            entry = _make_library_entry(char, config, counts.get(char, 0), book_id,
                                        existing=members.get(key))
            members[key] = entry
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

    pool = _cast_match_pool(lib, cast_name, get_active_book_id())

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

    pool = _cast_match_pool(lib, cast_name, include_all_generic=True)

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
            _apply_cast_to_config_file, VOICE_CONFIG_PATH, lib, cast_name, request.mapping,
            None, get_active_book_id())
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
            book_id = _get_saved_book_id(safe_name)
            chars = _script_line_counts(os.path.join(SCRIPTS_DIR, f"{safe_name}.json"))

            config_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
            # Hold the lock across the read-modify-write so this can't race a batch
            # review's concurrent speaker-rename remap of the same companion file.
            try:
                applied = _apply_cast_to_config_file(
                    config_path, lib, cast_name, request.mapping, chars=chars, book_id=book_id)
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


def _save_manifest(path, manifest):
    """Write a JSON manifest file."""
    atomic_json_write(manifest, path)

@app.post("/api/voice_design/preview")
async def voice_design_preview(request: VoiceDesignPreviewRequest):
    """Generate a preview voice from a text description."""
    claim_gpu_task("voice_design")
    try:
        # Model initialization allocates VRAM too, so it belongs inside the same
        # reservation as inference rather than happening before the lock check.
        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")
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
    finally:
        process_state["voice_design"]["running"] = False

@app.post("/api/voice_design/save")
async def voice_design_save(request: VoiceDesignSaveRequest):
    """Save a preview voice as a permanent designed voice."""
    previews_dir = os.path.join(DESIGNED_VOICES_DIR, "previews")
    # Constrain to the previews dir so preview_file can't traverse out and copy
    # an arbitrary host file (e.g. ../../etc/passwd) into the web-served dir.
    preview_path = _safe_subpath(previews_dir, request.preview_file)

    if not os.path.exists(preview_path):
        raise HTTPException(status_code=404, detail="Preview file not found")

    safe_name = _require_safe_filename(request.name, "Invalid voice name")

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
    safe_name = _require_safe_filename(base_name, "Invalid filename")

    voice_id = f"{safe_name}_{int(time.time())}"
    dest_filename = f"{voice_id}{ext}"
    dest_path = os.path.join(CLONE_VOICES_DIR, dest_filename)

    await _save_upload_limited(file, dest_path, 512 * 1024**2)

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


def _extract_lora_dataset_archive(archive_path, dataset_dir):
    """Perform potentially multi-gigabyte extraction outside the event loop."""
    os.makedirs(dataset_dir, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        _safe_extractall(archive, dataset_dir)

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

@app.post("/api/lora/train/cancel")
async def lora_cancel_training():
    """Cancel a running LoRA training subprocess (it holds the global GPU lock for
    hours, so without this the only way to stop it was killing the whole server)."""
    return _cancel_task("lora_training",
                        "No LoRA training is currently running.",
                        "LoRA training already exited.")


@app.post("/api/lora/train")
async def lora_start_training(request: LoraTrainingRequest, background_tasks: BackgroundTasks):
    """Start LoRA training as a subprocess."""
    check_global_gpu_lock("lora_training")

    # Validate dataset exists
    dataset_dir = _safe_subpath(LORA_DATASETS_DIR, request.dataset_id)
    if not os.path.isdir(dataset_dir):
        raise HTTPException(status_code=400, detail=f"Dataset '{request.dataset_id}' not found")

    # Build output directory
    safe_name = _require_safe_filename(request.name, "Invalid adapter name")

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
        logger.error(f"Download failed for {adapter_id}: {e}")
        raise HTTPException(status_code=500, detail="Built-in adapter download failed — see server logs for details.")

@app.post("/api/lora/test")
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
                logger.error(f"Auto-download failed for {request.adapter_id}: {e}")
                raise HTTPException(status_code=500, detail="Adapter auto-download failed — see server logs for details.")

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
        logger.error(f"LoRA test generation failed: {e}")
        raise HTTPException(status_code=500, detail="LoRA test generation failed — see server logs for details.")
    finally:
        process_state["lora_test"]["running"] = False

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
                logger.error(f"Auto-download failed for {adapter_id}: {e}")
                raise HTTPException(status_code=500, detail="Adapter auto-download failed — see server logs for details.")

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
        logger.error(f"LoRA preview generation failed: {e}")
        raise HTTPException(status_code=500, detail="LoRA preview generation failed — see server logs for details.")
    finally:
        process_state["lora_test"]["running"] = False

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
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    if os.path.exists(work_dir):
        raise HTTPException(status_code=400, detail=f"Project '{safe_name}' already exists")
    _save_builder_state(safe_name, {"description": "", "global_seed": "", "samples": []})
    return {"name": safe_name}

@app.post("/api/dataset_builder/update_meta")
async def dataset_builder_update_meta(request: DatasetBuilderUpdateMetaRequest):
    """Update project description and global seed without touching samples."""
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
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
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")
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
    safe_name = _require_safe_filename(request.dataset_name, "Invalid dataset name")

    # Same "dataset_builder" slot as the sibling /generate_batch route -
    # fail fast before any setup work below. See F-043.
    check_global_gpu_lock("dataset_builder")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    os.makedirs(work_dir, exist_ok=True)

    claim_gpu_task("dataset_builder")
    try:
        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

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
    except HTTPException:
        raise
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
    finally:
        process_state["dataset_builder"]["running"] = False

@app.post("/api/dataset_builder/generate_batch")
async def dataset_builder_generate_batch(request: DatasetBatchGenRequest):
    """Batch generate dataset samples as a background task."""
    check_global_gpu_lock("dataset_builder")

    if not request.samples or len(request.samples) == 0:
        raise HTTPException(status_code=400, detail="No samples provided")

    safe_name = _require_safe_filename(request.name, "Invalid dataset name")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    os.makedirs(work_dir, exist_ok=True)
    root_desc = request.description.strip()

    # Determine which indices to generate
    if request.indices is not None:
        to_generate = request.indices
    else:
        to_generate = list(range(len(request.samples)))

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
        except Exception as e:
            logger.error(f"Dataset builder batch generation crashed: {e}")
            process_state["dataset_builder"]["logs"].append(f"[ERROR] Batch generation crashed: {e}")
        finally:
            process_state["dataset_builder"]["running"] = False

    claim_gpu_task("dataset_builder")
    threading.Thread(target=task, daemon=True).start()
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
    safe_name = _require_safe_filename(name, "Invalid dataset name")
    state = _load_builder_state(safe_name)
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
    safe_name = _require_safe_filename(request.name, "Invalid dataset name")

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
    # Same denylist voicelab_save_config/voicelab_start enforce on this exact
    # config value - the preparer endpoints execute it too and must not skip
    # the check just because they read it through a different function.
    _validate_voicelab_path(interpreter, "rocm_python")
    return interpreter


@app.post("/api/preparer/start")
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


@app.post("/api/preparer/cancel")
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


@app.post("/api/preparer/batch/cancel")
async def preparer_batch_cancel():
    process_state["batch_preparer"]["cancel"] = True
    return {"status": "cancel_requested"}


from routers.voicelab import router as voicelab_router

app.include_router(voicelab_router)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("ALEXANDRIA_HOST", "127.0.0.1")
    port = int(os.environ.get("ALEXANDRIA_PORT", "4200"))
    uvicorn.run(app, host=host, port=port, access_log=False)
