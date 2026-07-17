import asyncio
import logging
import json
import os
import hashlib
import shutil
import subprocess
import sys
import datetime
import time
from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from pydantic import field_validator

from core import (
    API_LOG_DIR,
    CONFIG_PATH,
    DATA_DIR,
    LORA_DATASETS_DIR,
    LORA_MODELS_DIR,
    LORA_MODELS_MANIFEST,
    ROOT_DIR,
    RUN_HISTORY_DIR,
    VOICELAB_CONFIG_PATH,
    VOICELAB_DEFAULTS,
    _batch_cancel_helper,
    _init_batch_state,
    _init_task_log,
    _load_manifest,
    _load_voicelab_config,
    _pause_task,
    _revalidate_voicelab_paths,
    _resume_task,
    _run_claimed_background_task,
    _stream_subprocess_to_logs,
    _validate_voicelab_path,
    check_global_gpu_lock,
    claim_gpu_task,
    process_state,
)
from device_utils import normalize_device
from utils import atomic_json_write, safe_load_json
from voicelab_settings import get_profiler_paths
from run_history import list_runs, update_run
from runtime_info import get_runtime_info
from routers.lora import list_adapters_needing_recovery
from config_settings import load_app_config
from lmstudio_settings import is_remote_llm
import diagnostics as diagnostics_module


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


## ── Voice Lab: end-to-end audiobook → named LoRA pipeline ─────────────────────
#
# Orchestrates the six post-preparer stages as a single sequential job:
#   quality → audit_voice_datasets.py            (this repo, ROCm env)
#   dedup   → voice_analysis.py --phase dedup   (this repo, ROCm env)
#   train   → batch_train_lora.py               (this repo, ROCm env)
#   evaluate→ evaluate_lora.py                  (this repo, ROCm env)
#   profile → voice_profiler.py                 (this repo, ROCm env)
#   name    → name_voices.py                    (this repo, pure stdlib)
#
# Dedup needs packages such as speechbrain that are not installed in the web
# app's venv, so the ML stages run under a configurable ROCm interpreter. The
# interpreter path is machine-specific and lives in a small editable config file
# rather than being hardcoded.

VOICELAB_STAGES = ("quality", "dedup", "train", "evaluate", "profile", "name")


def _rocm_env(rocm_python: str) -> dict:
    """Environment for the ROCm pipeline stages.

    The web app runs inside its own venv (app/env), so os.environ has VIRTUAL_ENV
    and a PATH that front-loads app/env/bin. Dedup needs packages such as
    speechbrain that are available only through rocm_python; without scrubbing,
    any bare `python`/`pip`/tool the ML scripts invoke would resolve back into the
    web environment. Drop VIRTUAL_ENV and front-load the configured interpreter's
    own bin directory instead."""
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    bin_dir = os.path.dirname(os.path.abspath(rocm_python))
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def _run_profiler_preflight(command: list[str], env: dict) -> dict:
    """Run voice_profiler's canonical lightweight check under its real env."""
    try:
        result = subprocess.run(command + ["--check"], capture_output=True, text=True,
                                env=env, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="Voice profiler preflight timed out after 30 seconds.")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Voice profiler preflight could not start: {e}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        report = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        report = {}
    if result.returncode or report.get("status") != "passed":
        detail = "; ".join(report.get("errors", [])) or result.stderr.strip() or "unknown preflight failure"
        raise HTTPException(status_code=400, detail=f"Voice profiler is not ready: {detail}")
    return report


def _build_profiler_command(rocm_python: str, profiler_model: str,
                            epub_dirs: List[str]) -> list[str]:
    """Build the one canonical profile/preflight command."""
    paths = get_profiler_paths(ROOT_DIR, DATA_DIR)
    command = [rocm_python, "-u", os.path.join(ROOT_DIR, "voice_profiler.py"),
               "--manifest", paths["manifest"],
               "--model", profiler_model or paths["model"],
               "--output_csv", paths["output_csv"]]
    for epub_dir in epub_dirs:
        command += ["--epub-dir", epub_dir]
    return command


class VoiceLabConfig(BaseModel):
    rocm_python: Optional[str] = None
    profiler_model: Optional[str] = None
    epub_dirs: Optional[List[str]] = None
    zips_dir: Optional[str] = None


class VoiceLabRequest(BaseModel):
    zips_dir: Optional[str] = None                 # narrator-subfolder root; default from config
    stages: List[str] = Field(default=list(VOICELAB_STAGES), max_length=6)
    device: Optional[Literal["auto", "cpu", "cuda", "mps", "rocm", "hip"]] = None
    target_loss: float = Field(4.15, gt=0, le=100)
    max_epochs: int = Field(6, ge=1, le=100)
    lora_r: int = Field(64, ge=1, le=1024)
    candidate_checkpoints: int = Field(2, ge=0, le=2)
    profiler_model: Optional[str] = None           # override GGUF for the profile stage
    name_apply: bool = True                        # name stage: actually rename (else dry-run)
    name_overwrite: bool = False                   # also re-name already-named adapters
    preflight_id: Optional[str] = None

    @field_validator("device", mode="before")
    @classmethod
    def normalize_requested_device(cls, value):
        return normalize_device(value) if value is not None else None


def _probe_voicelab_interpreter(rocm_python: str) -> dict:
    """Read Torch/device/dependency state from the interpreter that will run ML stages."""
    code = (
        "import importlib.util,json,torch; "
        "ok=torch.cuda.is_available(); "
        "print(json.dumps({'python':__import__('sys').version.split()[0],"
        "'torch':torch.__version__,'hip':getattr(torch.version,'hip',None),"
        "'gpu':torch.cuda.get_device_name(0) if ok else None,"
        "'vram':torch.cuda.mem_get_info() if ok else None,"
        "'deps':{n:importlib.util.find_spec(n) is not None for n in "
        "['speechbrain','librosa','peft','llama_cpp']}}))")
    try:
        result = subprocess.run([rocm_python, "-c", code], capture_output=True,
                                text=True, timeout=20, check=False,
                                env=_rocm_env(rocm_python))
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return json.loads(lines[-1]) if result.returncode == 0 and lines else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def _build_voicelab_preflight(request: VoiceLabRequest, cfg: dict) -> dict:
    """Build the canonical read-only start decision and sanitized UI report."""
    stages = [stage for stage in VOICELAB_STAGES if stage in request.stages]
    blockers, warnings = [], []
    def finding(target, code, message):
        target.append({"code": code, "message": message})

    raw = (request.zips_dir or cfg.get("zips_dir") or "").strip()
    zips_dir = _resolve_zips_dir(raw) if raw else ""
    if not raw:
        finding(blockers, "zips_unconfigured", "Configure an input dataset folder.")
    elif not os.path.isdir(zips_dir):
        finding(blockers, "zips_missing", "The input dataset folder does not exist.")
    narrator_count = zip_count = deduped_count = 0
    if os.path.isdir(zips_dir):
        for name in os.listdir(zips_dir):
            folder = os.path.join(zips_dir, name)
            if name.startswith("_") or not os.path.isdir(folder):
                continue
            narrator_count += 1
            zip_count += sum(item.lower().endswith(".zip") for item in os.listdir(folder))
        deduped = os.path.join(zips_dir, "_deduped")
        if os.path.isdir(deduped):
            deduped_count = sum(item.lower().endswith(".zip") for item in os.listdir(deduped))
        if "train" in stages and "dedup" not in stages and not deduped_count:
            finding(blockers, "dedup_missing", "Training requires deduplicated ZIP files or the dedup stage.")

    ml_stages = {"quality", "dedup", "train", "evaluate", "profile"}
    needs_rocm = bool(ml_stages.intersection(stages))
    rocm = cfg.get("rocm_python") or ""
    interpreter_ok = bool(rocm and os.path.isfile(rocm) and os.access(rocm, os.X_OK))
    probe = _probe_voicelab_interpreter(rocm) if needs_rocm and interpreter_ok else {}
    if needs_rocm and not interpreter_ok:
        finding(blockers, "interpreter_missing", "Configure an executable ROCm Python interpreter.")
    elif needs_rocm and not probe:
        finding(blockers, "interpreter_probe_failed", "The ROCm interpreter readiness probe failed.")
    deps = probe.get("deps", {})
    required_deps = set()
    if {"quality", "train", "evaluate"}.intersection(stages):
        required_deps.update(("librosa", "peft"))
    if {"dedup", "evaluate"}.intersection(stages):
        required_deps.add("speechbrain")
    if "profile" in stages:
        required_deps.add("llama_cpp")
    missing_deps = sorted(name for name in required_deps if not deps.get(name))
    if missing_deps:
        finding(blockers, "dependencies_missing", "Missing stage dependencies: " + ", ".join(missing_deps) + ".")

    profiler_model = (request.profiler_model or cfg.get("profiler_model") or "").strip()
    if "profile" in stages and profiler_model and not os.path.isfile(profiler_model):
        finding(blockers, "profiler_model_missing", "The configured profiler model does not exist.")
    for stage, filename in (("quality", "audit_voice_datasets.py"),
                            ("dedup", "voice_analysis.py"),
                            ("train", "batch_train_lora.py"),
                            ("evaluate", "evaluate_lora.py"),
                            ("profile", "voice_profiler.py"), ("name", "name_voices.py")):
        if stage in stages and not os.path.isfile(os.path.join(ROOT_DIR, filename)):
            finding(blockers, "stage_script_missing", f"The {stage} stage script is missing from this install.")
    if "profile" in stages and interpreter_ok and not any(
            item["code"] in ("dependencies_missing", "profiler_model_missing") for item in blockers):
        try:
            _run_profiler_preflight(
                _build_profiler_command(rocm, profiler_model, cfg.get("epub_dirs") or []),
                _rocm_env(rocm))
        except HTTPException as exc:
            finding(blockers, "profiler_not_ready", str(exc.detail))

    usage = shutil.disk_usage(DATA_DIR)
    free_gb = round(usage.free / 1024 ** 3, 1)
    if free_gb < 2:
        finding(blockers, "disk_critical", "Less than 2 GB of free disk remains.")
    elif free_gb < 10:
        finding(warnings, "disk_low", "Less than 10 GB of free disk remains.")
    vram = probe.get("vram") or []
    free_vram_gb = round(vram[0] / 1024 ** 3, 1) if len(vram) == 2 else None
    requested_device = request.device or "auto"
    if needs_rocm and requested_device != "cpu" and not probe.get("gpu"):
        finding(blockers, "gpu_unavailable", "The selected interpreter cannot see a GPU.")
    elif "train" in stages and requested_device != "cpu" and free_vram_gb is not None and free_vram_gb < 8:
        finding(blockers, "vram_low", "Training requires at least 8 GB of free VRAM.")
    if request.candidate_checkpoints and request.max_epochs == 1:
        finding(warnings, "candidate_duplicate", "A one-epoch candidate will match production and be discarded.")

    stable = {"stages": stages, "zips_dir": zips_dir, "narrators": narrator_count,
              "zips": zip_count, "deduped": deduped_count, "interpreter_ok": interpreter_ok,
              "torch": probe.get("torch"), "hip": probe.get("hip"), "gpu": probe.get("gpu"),
              "missing_deps": missing_deps, "blockers": [item["code"] for item in blockers],
              "settings": [requested_device, request.target_loss, request.max_epochs,
                           request.lora_r, request.candidate_checkpoints, request.name_apply,
                           request.name_overwrite]}
    preflight_id = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:20]
    return {"preflight_id": preflight_id, "ready": not blockers, "stages": stages,
            "blockers": blockers, "warnings": warnings,
            "dataset": {"narrator_count": narrator_count, "zip_count": zip_count,
                        "deduped_count": deduped_count},
            "runtime": {"interpreter_configured": interpreter_ok, "python": probe.get("python"),
                        "torch": probe.get("torch"), "hip": probe.get("hip"),
                        "device": requested_device, "gpu": probe.get("gpu"),
                        "free_vram_gb": free_vram_gb, "free_disk_gb": free_gb,
                        "dependencies": deps},
            "outputs": {"datasets": "LoRA datasets library", "models": "LoRA models library",
                        "manifest": "LoRA model manifest"},
            "_zips_dir": zips_dir, "_profiler_model": profiler_model}


def _persist_voicelab_run(run_id: Optional[str], updates: dict) -> None:
    """Best-effort durable summary updates must never stop the pipeline."""
    if not run_id:
        return
    try:
        update_run(RUN_HISTORY_DIR, run_id, updates)
    except Exception:
        logger.exception("Could not update Voice Lab run summary %s", run_id)


def _elapsed_seconds(iso: Optional[str]) -> Optional[float]:
    """Whole seconds since an ISO timestamp, or None when unparseable."""
    try:
        started = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    delta = datetime.datetime.now(datetime.timezone.utc) - started
    return round(max(0.0, delta.total_seconds()), 1)


def _summarize_history_run(record: dict) -> dict:
    """Compact, bounded view of one run for the health card — never the raw log."""
    failure = None
    for stage in record.get("stages") or []:
        if stage.get("failure"):
            failure = stage["failure"]  # keep the last (most recent) failing stage
    if failure is None and record.get("error"):
        failure = {"message": record["error"]}
    return {
        "id": record.get("id"),
        "status": record.get("status"),
        "finished_at": record.get("finished_at"),
        "next_action": record.get("next_action"),
        "failure": failure,
    }


def _build_voicelab_health(state: Optional[dict] = None, history_dir: Optional[str] = None,
                           models_dir: Optional[str] = None, manifest_path: Optional[str] = None,
                           root_dir: Optional[str] = None) -> dict:
    """Read-only Voice Lab health snapshot for the dashboard.

    Assembled from the live process state, persisted run summaries, checkpoint
    recovery journals, and runtime build identity. Never mutates process state
    or writes to disk. Recovery-required takes precedence over run status.
    """
    state = process_state.get("voicelab", {}) if state is None else state
    history_dir = RUN_HISTORY_DIR if history_dir is None else history_dir
    models_dir = LORA_MODELS_DIR if models_dir is None else models_dir
    manifest_path = LORA_MODELS_MANIFEST if manifest_path is None else manifest_path
    root_dir = ROOT_DIR if root_dir is None else root_dir

    try:
        runs = [r for r in list_runs(history_dir) if r.get("task") == "voicelab"]
    except Exception:
        logger.exception("Could not list Voice Lab run history")
        runs = []
    try:
        pending_recovery = list_adapters_needing_recovery(models_dir, manifest_path)
    except Exception:
        logger.exception("Could not scan for pending checkpoint recovery")
        pending_recovery = []

    running = bool(state.get("running"))
    paused = bool(state.get("paused"))
    run_id = state.get("run_id")
    active_record = next((r for r in runs if r.get("id") == run_id), None)

    active = None
    if running:
        idx = state.get("current_task_idx")
        tasks = state.get("tasks") or []
        stage_name = None
        stage_started = None
        if isinstance(idx, int) and 0 <= idx < len(tasks):
            stage_name = tasks[idx].get("name")
        stage_records = (active_record or {}).get("stages") or []
        if isinstance(idx, int) and 0 <= idx < len(stage_records):
            stage_started = stage_records[idx].get("started_at")
        elapsed = _elapsed_seconds(stage_started or (active_record or {}).get("started_at"))
        active = {
            "run_id": run_id,
            "stage": stage_name,
            "stage_index": idx if isinstance(idx, int) else None,
            "stage_count": len(tasks),
            "paused": paused,
            "elapsed_seconds": elapsed,
        }

    last_success = next((_summarize_history_run(r) for r in runs
                         if r.get("status") == "completed"), None)
    last_failure = next((_summarize_history_run(r) for r in runs
                         if r.get("status") in ("failed", "interrupted", "cancelled")), None)

    def _device(record):
        return ((record or {}).get("request") or {}).get("device")

    device = _device(active_record) or (_device(runs[0]) if runs else None)

    if pending_recovery:
        status = "recovery_required"
    elif running:
        status = "running"
    elif runs:
        newest = runs[0].get("status")
        status = "ok" if newest == "completed" else (newest or "idle")
    else:
        status = "idle"

    if pending_recovery:
        count = len(pending_recovery)
        next_action = (f"Recover {count} interrupted checkpoint operation"
                       f"{'s' if count != 1 else ''} in the LoRA models list "
                       "before starting a new run.")
    elif running:
        next_action = (active_record or {}).get("next_action") or "Wait for the current stage to finish."
    elif runs:
        next_action = runs[0].get("next_action") or "Review the most recent Voice Lab run."
    else:
        next_action = "No Voice Lab runs yet."

    return {
        "status": status,
        "running": running,
        "paused": paused,
        "active_run": active,
        "device": device,
        "last_success": last_success,
        "last_failure": last_failure,
        "pending_recovery": pending_recovery,
        "next_action": next_action,
        "build": get_runtime_info(root_dir),
        "run_count": len(runs),
    }


@router.get("/api/voicelab/health")
async def voicelab_health():
    return await asyncio.to_thread(_build_voicelab_health)


def _safe_config_summary() -> dict:
    """Non-sensitive LLM/config fields only — no base_url, api_key, or SSH host."""
    try:
        cfg = load_app_config(CONFIG_PATH)
    except Exception:
        logger.exception("Could not load app config for diagnostics")
        return {"available": False}
    llm = cfg.get("llm") or {}
    llm_mode = cfg.get("llm_mode", "local")
    return {
        "available": True,
        "llm_mode": llm_mode,
        "model_name": llm.get("model_name"),
        "is_remote_llm": is_remote_llm(llm_mode, llm.get("base_url", "")),
        "remote_ssh_configured": bool((cfg.get("llm_remote_ssh") or "").strip()),
    }


def _voicelab_log_identifiers() -> list:
    """List Voice Lab log file identifiers (name + size) — never their contents."""
    logs = []
    try:
        for name in sorted(os.listdir(API_LOG_DIR)):
            if "voicelab" not in name or not name.endswith(".log"):
                continue
            path = os.path.join(API_LOG_DIR, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            logs.append({"name": name, "size_bytes": size})
    except OSError:
        pass
    return logs


def _build_voicelab_diagnostics() -> dict:
    """Assemble a redacted, bounded Voice Lab diagnostics bundle (read-only)."""
    try:
        latest_run = next((r for r in list_runs(RUN_HISTORY_DIR)
                           if r.get("task") == "voicelab"), None)
    except Exception:
        logger.exception("Could not read latest Voice Lab run for diagnostics")
        latest_run = None
    sections = {
        "runtime": get_runtime_info(ROOT_DIR),
        "config": _safe_config_summary(),
        "voicelab_config": _load_voicelab_config(),
        "health": _build_voicelab_health(),
        "latest_run": latest_run,
        "logs": _voicelab_log_identifiers(),
    }
    return diagnostics_module.build_diagnostics(
        sections, home_dir=os.path.expanduser("~"))


@router.get("/api/voicelab/diagnostics")
async def voicelab_diagnostics():
    return await asyncio.to_thread(_build_voicelab_diagnostics)


@router.post("/api/voicelab/preflight")
async def voicelab_preflight(request: VoiceLabRequest):
    report = await asyncio.to_thread(_build_voicelab_preflight, request, _load_voicelab_config())
    return {key: value for key, value in report.items() if not key.startswith("_")}


@router.get("/api/voicelab/config")
async def voicelab_get_config():
    """Return the pipeline paths plus whether each resolves on this machine."""
    cfg = _load_voicelab_config()
    zips_dir_ok = False
    try:
        resolved_zips = _resolve_zips_dir(cfg["zips_dir"])
        zips_dir_ok = os.path.isdir(resolved_zips)
    except Exception as e:
        logger.warning(f"Failed to resolve voicelab zips_dir '{cfg.get('zips_dir')}': {e}")

    profiler_paths = get_profiler_paths(ROOT_DIR, DATA_DIR)
    effective_profiler_model = cfg["profiler_model"] or profiler_paths["model"]
    profiler_ready = False
    profiler_errors = []
    if os.path.isfile(cfg["rocm_python"]):
        try:
            await asyncio.to_thread(
                _run_profiler_preflight,
                _build_profiler_command(cfg["rocm_python"], effective_profiler_model,
                                        cfg["epub_dirs"]),
                _rocm_env(cfg["rocm_python"]))
            profiler_ready = True
        except HTTPException as e:
            profiler_errors.append(str(e.detail))
    return {
        "config": cfg,
        "checks": {
            "rocm_python": os.path.isfile(cfg["rocm_python"]),
            "batch_train_lora": os.path.isfile(os.path.join(ROOT_DIR, "batch_train_lora.py")),
            "voice_profiler": os.path.isfile(os.path.join(ROOT_DIR, "voice_profiler.py")),
            "voice_analysis": os.path.isfile(os.path.join(ROOT_DIR, "voice_analysis.py")),
            "name_voices": os.path.isfile(os.path.join(ROOT_DIR, "name_voices.py")),
            "profiler_model": os.path.isfile(effective_profiler_model),
            "profiler_environment": profiler_ready,
            "epub_dirs": all(os.path.isdir(path) for path in cfg["epub_dirs"]),
            "zips_dir": zips_dir_ok,
        },
        "profiler_errors": profiler_errors,
        "defaults": VOICELAB_DEFAULTS,
    }


@router.post("/api/voicelab/config")
async def voicelab_save_config(request: VoiceLabConfig):
    cfg = _load_voicelab_config()
    updates = {k: (v.strip() if isinstance(v, str) else v)
               for k, v in request.model_dump(exclude_none=True).items()}

    if updates.get("rocm_python"):
        path = updates["rocm_python"]
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise HTTPException(status_code=400,
                                detail=f"rocm_python must be an existing, executable file: {path}")
        _validate_voicelab_path(path, "rocm_python")
    if updates.get("profiler_model"):
        if not os.path.isfile(updates["profiler_model"]):
            raise HTTPException(status_code=400,
                                detail=f"profiler_model must be an existing file: {updates['profiler_model']}")
        _validate_voicelab_path(updates["profiler_model"], "profiler_model")
    if "epub_dirs" in updates:
        updates["epub_dirs"] = [path.strip() for path in updates["epub_dirs"] if path.strip()]

    cfg.update(updates)
    atomic_json_write(cfg, VOICELAB_CONFIG_PATH)
    return {"status": "saved", "config": cfg}


def _resolve_zips_dir(raw: str) -> str:
    """Resolve a (possibly relative) zips_dir the same way voicelab_start does.

    zips_dir is a machine-specific path (like rocm_python) and may be
    absolute; relative values are resolved against the runtime data root.
    """
    resolved = os.path.normpath(raw)
    if not os.path.isabs(resolved):
        resolved = os.path.abspath(os.path.join(DATA_DIR, resolved))
    else:
        resolved = os.path.abspath(resolved)
    return resolved


@router.get("/api/voicelab/inspect")
async def voicelab_inspect(zips_dir: Optional[str] = None):
    """Preview what a dedup input folder contains so the UI can show readiness."""
    cfg = _load_voicelab_config()
    root = (zips_dir or cfg["zips_dir"]).strip()
    if not root:
        raise HTTPException(status_code=400, detail="zips_dir is not configured. Set it in Voice Lab settings.")
    root = _resolve_zips_dir(root)
    # Don't enumerate the app's own upload/generated dirs (consistent with the
    # exec-path guard); an external operator-chosen dataset folder is expected.
    _validate_voicelab_path(root, "zips_dir")
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
    quality_summary = safe_load_json(os.path.join(root, "_quality", "summary.json"), default={})
    deduped_zips = (
        [f for f in os.listdir(deduped_dir) if f.lower().endswith(".zip")]
        if os.path.isdir(deduped_dir) else []
    )

    manifest = _load_manifest(LORA_MODELS_MANIFEST)
    trained = sum(1 for e in manifest if e.get("zip_source"))
    evaluated = sum(1 for e in manifest if (e.get("evaluation") or {}).get("status") in
                    ("pass", "warning"))
    unnamed = sum(1 for e in manifest
                  if e.get("zip_source") and e.get("dataset_id") and e.get("id") == e.get("dataset_id"))
    profiled = sum(1 for e in manifest if e.get("voice_profile"))

    return {
        "zips_dir": root,
        "narrator_count": len(narrators),
        "narrators": narrators,
        "deduped_exists": os.path.isdir(deduped_dir),
        "deduped_count": len(deduped_zips),
        "quality": {"zip_count": quality_summary.get("zip_count", 0),
                    "warning_clip_count": quality_summary.get("warning_clip_count", 0)},
        "manifest": {"trained": trained, "evaluated": evaluated,
                     "profiled": profiled, "unnamed": unnamed},
    }


def _voicelab_build_commands(req: VoiceLabRequest, cfg: dict, zips_dir: str):
    """Build the (stage_name, command, cwd, env) tuples for the requested stages.

    env is a scrubbed ROCm environment for the stages run under rocm_python, and
    None for the pure-stdlib `name` stage (which correctly runs under the web
    app's own interpreter/env)."""
    rocm = cfg["rocm_python"]
    deduped_dir = os.path.join(zips_dir, "_deduped")
    profiler_model = ((req.profiler_model or cfg["profiler_model"]).strip()
                      or get_profiler_paths(ROOT_DIR, DATA_DIR)["model"])
    rocm_env = _rocm_env(rocm)

    steps = []
    if "quality" in req.stages:
        cmd = [rocm, "-u", os.path.join(ROOT_DIR, "audit_voice_datasets.py"),
               "--zips2", zips_dir]
        steps.append(("quality", cmd, ROOT_DIR, rocm_env))
    if "dedup" in req.stages:
        cmd = [rocm, "-u", os.path.join(ROOT_DIR, "voice_analysis.py"),
               "--phase", "dedup", "--zips2", zips_dir]
        if req.device:
            cmd += ["--device", req.device]
        steps.append(("dedup", cmd, ROOT_DIR, rocm_env))
    if "train" in req.stages:
        cmd = [rocm, "-u", os.path.join(ROOT_DIR, "batch_train_lora.py"),
               "--zips_dir", deduped_dir,
               "--models_dir", LORA_MODELS_DIR,
               "--manifest", LORA_MODELS_MANIFEST,
               # batch_train_lora.py's own --datasets_dir/--train_script/--python
               # defaults are all hardcoded to one specific
               # alexandria-audiobook2.git checkout - pin them to *this* app
               # instance's own paths/train_lora.py/configured rocm_python
               # instead, so a worktree (or any other checkout) running this
               # code extracts/trains with its own paths rather than silently
               # falling back to a different checkout's (datasets_dir was
               # missed in an earlier pass that only caught train_script/python).
               "--datasets_dir", LORA_DATASETS_DIR,
               "--train_script", os.path.join(ROOT_DIR, "app", "train_lora.py"),
               "--python", rocm,
               "--target_loss", str(req.target_loss),
               "--max_epochs", str(req.max_epochs),
               "--lora_r", str(req.lora_r)]
        cmd += ["--candidate_checkpoints", str(req.candidate_checkpoints)]
        if req.device:
            cmd += ["--device", req.device]
        steps.append(("train", cmd, ROOT_DIR, rocm_env))
    if "evaluate" in req.stages:
        cmd = [rocm, "-u", os.path.join(ROOT_DIR, "evaluate_lora.py"),
               "--manifest", LORA_MODELS_MANIFEST,
               "--models-dir", LORA_MODELS_DIR,
               "--config", CONFIG_PATH]
        if req.device:
            cmd += ["--device", req.device]
        steps.append(("evaluate", cmd, ROOT_DIR, rocm_env))
    if "profile" in req.stages:
        cmd = _build_profiler_command(rocm, profiler_model, cfg["epub_dirs"])
        steps.append(("profile", cmd, ROOT_DIR, rocm_env))
    if "name" in req.stages:
        # Pure stdlib — safe to run under the web app's own interpreter/env
        cmd = [sys.executable, "-u", os.path.join(ROOT_DIR, "name_voices.py"),
               "--manifest", LORA_MODELS_MANIFEST, "--models-dir", LORA_MODELS_DIR]
        if req.name_apply:
            cmd.append("--apply")
        if req.name_overwrite:
            cmd.append("--overwrite")
        steps.append(("name", cmd, ROOT_DIR, None))
    return steps


def _revalidate_voicelab_runtime(zips_dir: str, rocm_python: str,
                                 profiler_model: str, stages: list[str]) -> Optional[HTTPException]:
    """Recheck mutable filesystem prerequisites immediately before subprocess launch."""
    error = _revalidate_voicelab_paths(
        (zips_dir, "zips_dir"),
        (rocm_python if any(stage in stages for stage in ("quality", "dedup", "train", "evaluate", "profile"))
         else None, "rocm_python"),
        (profiler_model if "profile" in stages else None, "profiler_model"),
    )
    if error:
        return error
    if any(stage in stages for stage in ("quality", "dedup", "train", "evaluate", "profile")) and not (
            os.path.isfile(rocm_python) and os.access(rocm_python, os.X_OK)):
        return HTTPException(status_code=400,
                             detail=f"ROCm interpreter not found or not executable: {rocm_python}")
    if "profile" in stages and not os.path.isfile(profiler_model):
        return HTTPException(status_code=400,
                             detail=f"profiler_model not found: {profiler_model}")
    if "dedup" in stages and not os.path.isdir(zips_dir):
        return HTTPException(status_code=400, detail=f"Input folder not found: {zips_dir}")
    if "train" in stages and "dedup" not in stages and not os.path.isdir(
            os.path.join(zips_dir, "_deduped")):
        return HTTPException(status_code=400,
                             detail=f"No _deduped folder in {zips_dir}; run dedup first.")
    return None


@router.post("/api/voicelab/start")
async def voicelab_start(request: VoiceLabRequest, background_tasks: BackgroundTasks):
    """Run the selected pipeline stages in sequence as one cancel/pausable job."""
    bad = [s for s in request.stages if s not in VOICELAB_STAGES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown stage(s): {', '.join(bad)}")
    # Keep canonical pipeline order regardless of how the request listed them
    request.stages = [s for s in VOICELAB_STAGES if s in request.stages]
    if not request.stages:
        raise HTTPException(status_code=400, detail="No stages selected.")

    cfg = _load_voicelab_config()
    preflight = await asyncio.to_thread(_build_voicelab_preflight, request, cfg)
    if not request.preflight_id:
        raise HTTPException(status_code=409, detail="Review the Voice Lab preflight before starting.")
    if request.preflight_id != preflight["preflight_id"]:
        raise HTTPException(status_code=409, detail="Voice Lab inputs changed; review a fresh preflight.")
    if preflight["blockers"]:
        raise HTTPException(status_code=400, detail="; ".join(
            finding["message"] for finding in preflight["blockers"]))
    zips_dir = preflight["_zips_dir"]
    _validate_voicelab_path(zips_dir, "zips_dir")
    if cfg.get("rocm_python"):
        _validate_voicelab_path(cfg["rocm_python"], "rocm_python")
    profiler_model = preflight["_profiler_model"]
    if profiler_model:
        _validate_voicelab_path(profiler_model, "profiler_model")

    steps = _voicelab_build_commands(request, cfg, zips_dir)
    effective_profiler_model = next(
        (command[command.index("--model") + 1]
         for stage, command, _cwd, _env in steps if stage == "profile"), "")
    check_global_gpu_lock("voicelab")

    def _run():
        state = process_state["voicelab"]
        run_id = state.get("run_id")
        _init_batch_state(state,
                          [f"Voice Lab: {len(steps)} stage(s) — {', '.join(s[0] for s in steps)}"],
                          [{"name": s[0], "status": "pending"} for s in steps])
        state["status"] = "running"
        state["process"] = None
        state["pid"] = None

        log_path = _init_task_log("voicelab", extra_header=f"# zips_dir={zips_dir}\n")
        sanitized_preflight = {key: value for key, value in preflight.items()
                               if not key.startswith("_")}
        request_summary = request.model_dump(exclude={"preflight_id", "zips_dir"})
        stage_records = [{"name": stage, "status": "pending", "started_at": None,
                          "finished_at": None, "duration_seconds": None,
                          "exit_status": None, "failure": None}
                         for stage, _cmd, _cwd, _env in steps]
        _persist_voicelab_run(run_id, {
            "request": request_summary, "preflight": sanitized_preflight,
            "build": get_runtime_info(ROOT_DIR), "stages": stage_records,
            "log": os.path.relpath(log_path, DATA_DIR) if log_path else None,
            "datasets": sanitized_preflight.get("dataset", {}),
            "adapters_before": len(_load_manifest(LORA_MODELS_MANIFEST)),
            "next_action": "Wait for the current stage to finish.",
        })

        failed = False
        for i, (stage, cmd, cwd, env) in enumerate(steps):
            if state["cancel"]:
                state["logs"].append("Pipeline cancelled.")
                break
            state["current_task_idx"] = i
            state["tasks"][i]["status"] = "running"
            state["logs"].append(f"--- [{i+1}/{len(steps)}] {stage} ---")
            stage_started = time.monotonic()
            stage_records[i] = {**stage_records[i], "status": "running",
                                "started_at": datetime.datetime.now(
                                    datetime.timezone.utc).isoformat()}
            _persist_voicelab_run(run_id, {"stages": stage_records,
                                           "next_action": f"Wait for {stage} to finish."})

            # Each earlier stage may run for hours. Recheck immediately before
            # this subprocess, not only when the background pipeline begins.
            error = _revalidate_voicelab_runtime(
                zips_dir, cfg["rocm_python"], effective_profiler_model, [stage])
            if error:
                state["logs"].append(f"[{stage}] aborted: {error.detail}")
                state["tasks"][i]["status"] = "failed"
                stage_records[i] = {**stage_records[i], "status": "failed",
                                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    "duration_seconds": round(time.monotonic() - stage_started, 3),
                                    "failure": {"type": "prerequisite_changed",
                                                "message": str(error.detail)}}
                _persist_voicelab_run(run_id, {"stages": stage_records,
                                               "next_action": "Correct the changed prerequisite and start a new run."})
                failed = True
                break

            try:
                rc, _ = _stream_subprocess_to_logs(cmd, cwd, state, log_prefix=f"[{stage}] ", log_file=log_path, env=env)
            except Exception as e:
                state["logs"].append(f"[{stage}] error launching: {e}")
                state["tasks"][i]["status"] = "failed"
                stage_records[i] = {**stage_records[i], "status": "failed",
                                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    "duration_seconds": round(time.monotonic() - stage_started, 3),
                                    "failure": {"type": "launch_error", "message": str(e)}}
                _persist_voicelab_run(run_id, {"stages": stage_records,
                                               "next_action": "Review the stage log and retry after correcting the launch error."})
                failed = True
                break

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                stage_records[i] = {**stage_records[i], "status": "cancelled",
                                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    "duration_seconds": round(time.monotonic() - stage_started, 3),
                                    "exit_status": rc, "failure": {"type": "cancelled"}}
                _persist_voicelab_run(run_id, {"stages": stage_records,
                                               "next_action": "Review partial outputs before starting another run."})
                break
            if rc == 0:
                state["tasks"][i]["status"] = "done"
                stage_records[i] = {**stage_records[i], "status": "completed",
                                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    "duration_seconds": round(time.monotonic() - stage_started, 3),
                                    "exit_status": 0}
                _persist_voicelab_run(run_id, {"stages": stage_records,
                                               "adapters_current": len(_load_manifest(LORA_MODELS_MANIFEST)),
                                               "next_action": "Continue to the next stage."})
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{stage}] failed (exit {rc}) — stopping pipeline.")
                stage_records[i] = {**stage_records[i], "status": "failed",
                                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    "duration_seconds": round(time.monotonic() - stage_started, 3),
                                    "exit_status": rc,
                                    "failure": {"type": "nonzero_exit", "exit_status": rc}}
                _persist_voicelab_run(run_id, {"stages": stage_records,
                                               "next_action": f"Review the {stage} log and retry from that stage."})
                failed = True
                break  # later stages depend on earlier ones; don't continue on failure

        state["process"] = None
        state["pid"] = None
        state["running"] = False
        if state.get("cancel"):
            state["status"] = "cancelled"
            _persist_voicelab_run(run_id, {
                "adapters_after": len(_load_manifest(LORA_MODELS_MANIFEST)),
                "next_action": "Review partial outputs before starting another run.",
            })
        elif failed:
            state["status"] = "failed"
            _persist_voicelab_run(run_id, {
                "adapters_after": len(_load_manifest(LORA_MODELS_MANIFEST)),
            })
        else:
            state["status"] = "done"
            state["logs"].append("Voice Lab pipeline finished.")
            _persist_voicelab_run(run_id, {
                "adapters_after": len(_load_manifest(LORA_MODELS_MANIFEST)),
                "next_action": "Review generated adapters and evaluation evidence.",
            })

    claim_gpu_task("voicelab")
    background_tasks.add_task(_run_claimed_background_task, "voicelab", _run)
    return {"status": "started", "stages": request.stages, "zips_dir": zips_dir}


@router.post("/api/voicelab/cancel")
async def voicelab_cancel():
    return _batch_cancel_helper("voicelab")


@router.post("/api/voicelab/pause")
async def voicelab_pause():
    return _pause_task("voicelab", "No Voice Lab pipeline is currently running.",
                        "Voice Lab is between stages, retry in a moment.",
                        "Voice Lab")


@router.post("/api/voicelab/resume")
async def voicelab_resume():
    return _resume_task("voicelab", "No Voice Lab pipeline is currently running.",
                         "Voice Lab")
