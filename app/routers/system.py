import asyncio
import json
import logging
import os
import time
import traceback
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from config_settings import (AppConfig, GenerationConfig, LLMConfig, PromptConfig,
                             TTSConfig, backup_damaged_app_config, load_app_config,
                             load_app_config_result)

from default_prompts import load_default_prompts
from review_prompts import load_review_prompts
from persona_prompts import load_persona_prompts
from lmstudio_settings import (get_lmstudio_status, apply_lmstudio_settings, is_remote_llm,
                               apply_remote_lmstudio_settings, is_local_llm_endpoint,
                               get_current_status)

from core import (
    API_LOG_DIR,
    CONFIG_PATH,
    DATA_DIR,
    ETA_TASKS,
    LLMConfigError,
    ROOT_DIR,
    RUN_HISTORY_DIR,
    STATIC_DIR,
    _compute_eta,
    _load_llm_config,
    _validate_local_llm_base_url,
    _warn_corrupted_json,
    check_disk_space,
    claim_gpu_task,
    process_state,
    project_manager,
)

from utils import (atomic_json_write, file_lock,
                   rocm_smi_utilization as _rocm_smi_utilization,
                   run_rocm_smi_json, system_has_gpu)
from run_history import get_run, list_runs
from runtime_info import get_runtime_info


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


@router.get("/api/runs")
async def get_run_history(limit: int = 100):
    return {"runs": list_runs(RUN_HISTORY_DIR, limit=limit)}


@router.get("/api/runs/{run_id}")
async def get_run_history_entry(run_id: str):
    record = get_run(RUN_HISTORY_DIR, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return record

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
            for card_data in data.values():
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


@router.get("/api/system/stats")
def get_system_stats():
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
        "runtime": get_runtime_info(ROOT_DIR),
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


@router.get("/api/status/eta")
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


@router.get("/api/lmstudio/status")
async def lmstudio_status():
    """Report whether the loaded model is using ideal settings (VRAM-safe
    locally, large-context remotely) so the UI can show an at-a-glance indicator."""
    full_cfg = load_app_config(CONFIG_PATH)
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


@router.post("/api/lmstudio/optimize")
async def lmstudio_optimize(req: LMStudioOptimizeRequest):
    """Toggle the loaded model between VRAM-safe/best settings and LM Studio's
    defaults. Local endpoints use the local `lms` CLI; remote endpoints (e.g.
    LM Studio on Thunder) are driven over SSH via the configured host alias."""
    full_cfg = load_app_config(CONFIG_PATH)
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


@router.post("/api/llm/test")
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

@router.get("/")
async def read_index():
    # Stamp the page with the build it was served from so a tab left open across
    # a backend update can detect it is running stale frontend code (Phase 7).
    # Same runtime build source the backend reports at /api/system/stats.
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as handle:
        html = handle.read()
    build = get_runtime_info(ROOT_DIR).get("short_revision") or ""
    # Replace only the meta-tag stamp, not the JS placeholder literal the
    # frontend compares against (a blanket replace would rewrite that guard and
    # permanently disable stale detection on served pages).
    html = html.replace('content="__APP_BUILD__"', f'content="{build}"', 1)
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )

@router.get("/favicon.ico")
async def read_favicon():
    favicon_path = os.path.join(ROOT_DIR, "icon.png")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Favicon not found")

@router.get("/api/config")
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

    # A partial, empty, or corrupted config should not make the Setup page
    # unusable. The shared loader returns safe data plus warnings; merging here
    # supplies required sections without rewriting the damaged file during GET.
    load_result = load_app_config_result(CONFIG_PATH)
    loaded_config = load_result.data
    config = {**default_config, **loaded_config}

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
    config["config_warnings"] = [
        {"field": warning.field, "message": warning.message}
        for warning in load_result.warnings
    ]
    config["config_needs_backup"] = load_result.needs_backup

    return config

@router.get("/api/default_prompts")
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

def _normalize_and_validate_llm(profile: "LLMConfig") -> "LLMConfig":
    """Return a copy with a normalized, validated local/trusted base URL."""
    if not profile.base_url.strip():
        raise HTTPException(status_code=400, detail="LLM base_url is required")
    url = profile.base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    try:
        _validate_local_llm_base_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return profile.model_copy(update={"base_url": url}, deep=True)


@router.post("/api/config")
async def save_config(config: AppConfig):
    normalized_config = config.model_copy(deep=True)

    # Normalize/validate whichever profiles were sent. The local/remote profiles
    # are persisted side-by-side so toggling never loses the other one's settings.
    if normalized_config.llm_local is not None and normalized_config.llm_local.base_url.strip():
        normalized_config.llm_local = _normalize_and_validate_llm(normalized_config.llm_local)
    if normalized_config.llm_remote is not None and normalized_config.llm_remote.base_url.strip():
        normalized_config.llm_remote = _normalize_and_validate_llm(normalized_config.llm_remote)

    # Pick the active profile from the toggle, then mirror it into `llm` - the
    # section every consumer (review/generate/personas/nicknames) reads.
    active = (normalized_config.llm_remote if normalized_config.llm_mode == "remote"
              else normalized_config.llm_local)
    if active is None:
        raise HTTPException(status_code=400, detail=(
            f"llm_mode is '{normalized_config.llm_mode}' but no llm_{normalized_config.llm_mode} "
            f"profile was provided - refusing to save a config where llm_mode "
            f"and the active llm profile would disagree."))
    normalized_config.llm = _normalize_and_validate_llm(active)

    with file_lock(CONFIG_PATH):
        existing = load_app_config_result(CONFIG_PATH)
        if existing.needs_backup:
            try:
                backup_damaged_app_config(CONFIG_PATH)
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Could not preserve damaged config before saving: {exc}",
                ) from exc
        atomic_json_write(normalized_config.model_dump(), CONFIG_PATH)
    project_manager.invalidate_config_cache()
    # Reset engine so it picks up new TTS settings on next use
    project_manager.engine = None
    return {"status": "saved"}
