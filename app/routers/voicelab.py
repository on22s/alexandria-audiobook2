import asyncio
import logging
import json
import os
import subprocess
import sys
from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from pydantic import field_validator

from core import (
    CONFIG_PATH,
    DATA_DIR,
    LORA_DATASETS_DIR,
    LORA_MODELS_DIR,
    LORA_MODELS_MANIFEST,
    ROOT_DIR,
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

    @field_validator("device", mode="before")
    @classmethod
    def normalize_requested_device(cls, value):
        return normalize_device(value) if value is not None else None


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
    _validate_voicelab_path(zips_dir, "zips_dir")

    # Validate prerequisites up front with actionable errors
    needs_rocm = any(s in request.stages for s in ("quality", "dedup", "train", "evaluate", "profile"))
    if needs_rocm:
        if not cfg["rocm_python"]:
            raise HTTPException(status_code=400,
                                detail="rocm_python is not configured. Set it in Voice Lab settings.")
        if not (os.path.isfile(cfg["rocm_python"]) and os.access(cfg["rocm_python"], os.X_OK)):
            raise HTTPException(status_code=400,
                                detail=f"ROCm interpreter not found or not executable: {cfg['rocm_python']}. Set it in Voice Lab settings.")
        _validate_voicelab_path(cfg["rocm_python"], "rocm_python")
    profiler_model = (request.profiler_model or cfg["profiler_model"] or "").strip()
    if "profile" in request.stages and profiler_model:
        if not os.path.isfile(profiler_model):
            raise HTTPException(status_code=400,
                                detail=f"profiler_model not found: {profiler_model}. Set it in Voice Lab settings.")
        _validate_voicelab_path(profiler_model, "profiler_model")
    if "dedup" in request.stages and not os.path.isdir(zips_dir):
        raise HTTPException(status_code=400, detail=f"Input folder not found: {zips_dir}")
    if "train" in request.stages and not os.path.isdir(os.path.join(zips_dir, "_deduped")) and "dedup" not in request.stages:
        raise HTTPException(status_code=400,
                            detail=f"No _deduped folder in {zips_dir}; run the dedup stage first.")
    # These ship with this repo, so a miss means a broken install, not
    # misconfiguration - there is no path for the user to correct.
    for s, fname in (("quality", "audit_voice_datasets.py"),
                     ("train", "batch_train_lora.py"),
                     ("evaluate", "evaluate_lora.py"),
                     ("profile", "voice_profiler.py")):
        if s in request.stages and not os.path.isfile(os.path.join(ROOT_DIR, fname)):
            raise HTTPException(status_code=400,
                                detail=f"{fname} is missing from this install.")

    steps = _voicelab_build_commands(request, cfg, zips_dir)
    effective_profiler_model = next(
        (command[command.index("--model") + 1]
         for stage, command, _cwd, _env in steps if stage == "profile"), "")
    if "profile" in request.stages:
        profile_cmd = next(command for stage, command, _cwd, _env in steps if stage == "profile")
        await asyncio.to_thread(
            _run_profiler_preflight, profile_cmd, _rocm_env(cfg["rocm_python"]))

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
        for i, (stage, cmd, cwd, env) in enumerate(steps):
            if state["cancel"]:
                state["logs"].append("Pipeline cancelled.")
                break
            state["current_task_idx"] = i
            state["tasks"][i]["status"] = "running"
            state["logs"].append(f"--- [{i+1}/{len(steps)}] {stage} ---")

            # Each earlier stage may run for hours. Recheck immediately before
            # this subprocess, not only when the background pipeline begins.
            error = _revalidate_voicelab_runtime(
                zips_dir, cfg["rocm_python"], effective_profiler_model, [stage])
            if error:
                state["logs"].append(f"[{stage}] aborted: {error.detail}")
                state["tasks"][i]["status"] = "failed"
                failed = True
                break

            try:
                rc, _ = _stream_subprocess_to_logs(cmd, cwd, state, log_prefix=f"[{stage}] ", log_file=log_path, env=env)
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
