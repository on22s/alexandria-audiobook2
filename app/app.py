import os
import sys
import asyncio
import json
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Literal
import time
import queue
import zipfile
import subprocess
import traceback
from datetime import datetime
from utils import atomic_json_write, safe_load_json, secure_filename, run_rocm_smi_json, system_has_gpu, rocm_smi_utilization as _rocm_smi_utilization, check_basic_auth
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from math import ceil

from default_prompts import load_default_prompts
from review_prompts import load_review_prompts
from persona_prompts import load_persona_prompts
from lmstudio_settings import (get_lmstudio_status, apply_lmstudio_settings, is_remote_llm,
                               apply_remote_lmstudio_settings, is_local_llm_endpoint,
                               get_current_status)
from review_script import clear_checkpoint

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AlexandriaUI")

app = FastAPI(title="Alexandria Audiobook")

from core import (
    API_LOG_DIR,
    BASE_DIR,
    BUILTIN_LORA_DIR,
    CHARACTER_ALIASES_PATH,
    CLONE_VOICES_DIR,
    CONFIG_PATH,
    DATASET_BUILDER_DIR,
    DATA_DIR,
    DESIGNED_VOICES_DIR,
    ETA_TASKS,
    GPU_TASKS,
    LLMConfigError,
    LORA_MODELS_DIR,
    NON_GPU_TASKS,
    REPORTS_DIR,
    ROOT_DIR,
    SCRIPTS_DIR,
    SCRIPT_PATH,
    STATIC_DIR,
    UPLOADS_DIR,
    VOICELINES_DIR,
    VOICE_CONFIG_PATH,
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
    _init_batch_state,
    _init_task_log,
    _insert_llm_summary,
    _load_llm_config,
    _markdown_aliases_lines,
    _markdown_book_pass_lines,
    _markdown_diff_highlights_lines,
    _markdown_heads_up_lines,
    _markdown_stats_table,
    _new_review_totals,
    _pause_task,
    _resume_task,
    _run_claimed_background_task,
    _require_safe_filename,
    _save_upload_limited,
    _stream_subprocess_to_logs,
    _task_log_path,
    _validate_local_llm_base_url,
    _warn_corrupted_json,
    check_global_gpu_lock,
    check_disk_space,
    claim_gpu_task,
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

from routers.voices import router as voices_router

app.include_router(voices_router)


from routers.editor import router as editor_router

app.include_router(editor_router)


from routers.scripts_library import router as scripts_library_router

app.include_router(scripts_library_router)

from routers.voice_library import router as voice_library_router

app.include_router(voice_library_router)


from routers.voice_design import router as voice_design_router

app.include_router(voice_design_router)

from routers.lora import router as lora_router

app.include_router(lora_router)


from routers.dataset_builder import router as dataset_builder_router

app.include_router(dataset_builder_router)


from routers.preparer import router as preparer_router

app.include_router(preparer_router)


from routers.voicelab import router as voicelab_router

app.include_router(voicelab_router)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("ALEXANDRIA_HOST", "127.0.0.1")
    port = int(os.environ.get("ALEXANDRIA_PORT", "4200"))
    uvicorn.run(app, host=host, port=port, access_log=False)
