import logging
import os
import sys
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from core import (
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
from utils import atomic_json_write


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


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

VOICELAB_STAGES = ("dedup", "train", "profile", "name")


def _rocm_env(rocm_python: str) -> dict:
    """Environment for the ROCm pipeline stages.

    The web app runs inside its own venv (app/env), so os.environ has VIRTUAL_ENV
    and a PATH that front-loads app/env/bin — which has no torch/librosa. The
    dedup/train/profile stages run under a *different* interpreter (rocm_python);
    without scrubbing, any bare `python`/`pip`/tool those scripts shell out to
    would resolve back into app/env and import the wrong (torch-less) packages.
    Drop VIRTUAL_ENV and front-load the ROCm interpreter's own bin dir instead."""
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    bin_dir = os.path.dirname(os.path.abspath(rocm_python))
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


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
    if updates.get("pipeline_repo"):
        if not os.path.isdir(updates["pipeline_repo"]):
            raise HTTPException(status_code=400,
                                detail=f"pipeline_repo must be an existing directory: {updates['pipeline_repo']}")
        _validate_voicelab_path(updates["pipeline_repo"], "pipeline_repo")
    if updates.get("profiler_model"):
        if not os.path.isfile(updates["profiler_model"]):
            raise HTTPException(status_code=400,
                                detail=f"profiler_model must be an existing file: {updates['profiler_model']}")
        _validate_voicelab_path(updates["profiler_model"], "profiler_model")

    cfg.update(updates)
    atomic_json_write(cfg, VOICELAB_CONFIG_PATH)
    return {"status": "saved", "config": cfg}


def _resolve_zips_dir(raw: str) -> str:
    """Resolve a (possibly relative) zips_dir the same way voicelab_start does.

    zips_dir is a machine-specific path (like rocm_python/pipeline_repo) and may be
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
    """Build the (stage_name, command, cwd, env) tuples for the requested stages.

    env is a scrubbed ROCm environment for the stages run under rocm_python, and
    None for the pure-stdlib `name` stage (which correctly runs under the web
    app's own interpreter/env)."""
    rocm = cfg["rocm_python"]
    repo = cfg["pipeline_repo"]
    deduped_dir = os.path.join(zips_dir, "_deduped")
    profiler_model = (req.profiler_model or cfg["profiler_model"]).strip()
    rocm_env = _rocm_env(rocm)

    steps = []
    if "dedup" in req.stages:
        cmd = [rocm, "-u", os.path.join(ROOT_DIR, "voice_analysis.py"),
               "--phase", "dedup", "--zips2", zips_dir]
        if req.device:
            cmd += ["--device", req.device]
        steps.append(("dedup", cmd, ROOT_DIR, rocm_env))
    if "train" in req.stages:
        cmd = [rocm, "-u", os.path.join(repo, "batch_train_lora.py"),
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
        steps.append(("train", cmd, repo, rocm_env))
    if "profile" in req.stages:
        cmd = [rocm, "-u", os.path.join(repo, "voice_profiler.py"),
               "--manifest", LORA_MODELS_MANIFEST]
        if profiler_model:
            cmd += ["--model", profiler_model]
        steps.append(("profile", cmd, repo, rocm_env))
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
    needs_rocm = any(s in request.stages for s in ("dedup", "train", "profile"))
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
    for s, fname, base in (("train", "batch_train_lora.py", cfg["pipeline_repo"]),
                           ("profile", "voice_profiler.py", cfg["pipeline_repo"])):
        if s in request.stages:
            if not base:
                raise HTTPException(status_code=400,
                                    detail="pipeline_repo is not configured. Set it in Voice Lab settings.")
            if not os.path.isfile(os.path.join(base, fname)):
                raise HTTPException(status_code=400,
                                    detail=f"{fname} not found in {base}. Check the pipeline repo path in Voice Lab settings.")
            _validate_voicelab_path(base, "pipeline_repo")

    steps = _voicelab_build_commands(request, cfg, zips_dir)

    def _run():
        state = process_state["voicelab"]
        _init_batch_state(state,
                          [f"Voice Lab: {len(steps)} stage(s) — {', '.join(s[0] for s in steps)}"],
                          [{"name": s[0], "status": "pending"} for s in steps])
        state["status"] = "running"
        state["process"] = None
        state["pid"] = None

        # Re-validate immediately before exec, not just synchronously above -
        # background_tasks.add_task defers this whole closure until after the
        # HTTP response is sent, leaving a window where a path that passed the
        # checks above (e.g. a symlink) could be repointed before the
        # subprocess below actually starts.
        e = _revalidate_voicelab_paths(
            (cfg["rocm_python"] if needs_rocm else None, "rocm_python"),
            (profiler_model if ("profile" in request.stages and profiler_model) else None, "profiler_model"),
            (cfg["pipeline_repo"] if ("train" in request.stages or "profile" in request.stages) else None, "pipeline_repo"),
        )
        if e:
            state["status"] = "failed"
            state["running"] = False
            state["logs"].append(f"Aborted: {e.detail}")
            return

        log_path = _init_task_log("voicelab", extra_header=f"# zips_dir={zips_dir}\n")

        failed = False
        for i, (stage, cmd, cwd, env) in enumerate(steps):
            if state["cancel"]:
                state["logs"].append("Pipeline cancelled.")
                break
            state["current_task_idx"] = i
            state["tasks"][i]["status"] = "running"
            state["logs"].append(f"--- [{i+1}/{len(steps)}] {stage} ---")

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
