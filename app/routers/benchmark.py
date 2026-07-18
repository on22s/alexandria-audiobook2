"""Benchmark preflight and state routes."""

import asyncio
import os
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from benchmark_core import (get_benchmark_preflight_id,
                            validate_benchmark_manifest)
from benchmark_environment import (collect_local_environment,
                                   collect_local_tts_environment,
                                   collect_thunder_environment,
                                   collect_thunder_tts_environment)
from benchmark_runner import (run_script_generation_benchmark,
                              run_lora_training_benchmark,
                              run_script_review_benchmark,
                              run_tts_generation_benchmark)
from config_settings import load_app_config
from core import (CONFIG_PATH, REPORTS_DIR, ROOT_DIR, SCRIPTS_DIR, UPLOADS_DIR,
                  _init_batch_state, _run_claimed_background_task, check_global_gpu_lock,
                  claim_gpu_task, process_state)


router = APIRouter()


class BenchmarkPreflightRequest(BaseModel):
    manifest: dict


class BenchmarkStartRequest(BenchmarkPreflightRequest):
    preflight_id: str


def _get_model_name(config, target):
    profile = config.get("llm_remote" if target == "thunder" else "llm_local") or {}
    model_name = profile.get("model_name") or (config.get("llm") or {}).get("model_name")
    if not model_name:
        raise ValueError(f"{target} model is not configured")
    return model_name


def _build_benchmark_preflight(request):
    manifest = validate_benchmark_manifest(request.manifest)
    check_global_gpu_lock("benchmark")
    config = load_app_config(CONFIG_PATH)
    environments = {}
    for target in manifest["targets"]:
        if manifest["stage"] in {"tts_generation", "voicelab_training"}:
            settings = manifest.get("settings") or {}
            if target == "local":
                environments[target] = collect_local_tts_environment(ROOT_DIR)
            else:
                environments[target] = collect_thunder_tts_environment(
                    ROOT_DIR, (config.get("llm_remote_ssh") or "").strip(),
                    settings.get("remote_root"), settings.get("remote_python"))
            continue
        model_name = _get_model_name(config, target)
        if target == "local":
            environments[target] = collect_local_environment(ROOT_DIR, model_name)
        else:
            ssh_alias = (config.get("llm_remote_ssh") or "").strip()
            environments[target] = collect_thunder_environment(
                ROOT_DIR, ssh_alias, model_name)
    return {"manifest": manifest, "environments": environments,
            "preflight_id": get_benchmark_preflight_id(manifest, environments),
            "benchmark_state": "ready"}


@router.post("/api/benchmark/preflight")
async def benchmark_preflight(request: BenchmarkPreflightRequest):
    try:
        return await asyncio.to_thread(_build_benchmark_preflight, request)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/benchmark/start")
async def benchmark_start(background_tasks: BackgroundTasks,
                          request: BenchmarkStartRequest):
    try:
        preflight = await asyncio.to_thread(_build_benchmark_preflight, request)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.preflight_id != preflight["preflight_id"]:
        raise HTTPException(status_code=409,
                            detail="Benchmark inputs or environment changed; review a fresh preflight.")
    manifest = preflight["manifest"]
    if manifest["stage"] not in {"script_generation", "script_review", "tts_generation", "voicelab_training"} or len(manifest["targets"]) != 1:
        raise HTTPException(status_code=400,
                            detail="Benchmark runs require a supported stage and exactly one target.")
    report_dir = os.path.join(REPORTS_DIR, "benchmarks")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"{preflight['preflight_id']}.json")
    state = process_state["benchmark"]
    claim_gpu_task("benchmark")
    _init_batch_state(state, ["Benchmark queued."],
                      [{"fixture_id": fixture["id"], "status": "pending"}
                       for fixture in manifest["fixtures"]])
    state.update({"status": "running", "report_path": report_path})

    def _run():
        environment = preflight["environments"][manifest["targets"][0]]
        if manifest["stage"] == "script_generation":
            run_script_generation_benchmark(
                manifest, environment, report_path, state, CONFIG_PATH, UPLOADS_DIR)
        elif manifest["stage"] == "script_review":
            run_script_review_benchmark(
                manifest, environment, report_path, state, CONFIG_PATH, SCRIPTS_DIR)
        elif manifest["stage"] == "tts_generation":
            run_tts_generation_benchmark(
                manifest, environment, report_path, state, CONFIG_PATH, ROOT_DIR)
        else:
            run_lora_training_benchmark(
                manifest, environment, report_path, state, CONFIG_PATH, ROOT_DIR)

    background_tasks.add_task(_run_claimed_background_task, "benchmark", _run)
    return {"status": "started", "report_path": report_path,
            "preflight_id": preflight["preflight_id"]}


@router.post("/api/benchmark/cancel")
async def benchmark_cancel():
    state = process_state["benchmark"]
    if not state["running"]:
        raise HTTPException(status_code=400, detail="No benchmark is currently running.")
    state["cancel"] = True
    state["logs"].append("Cancellation requested; waiting for the current model call.")
    return {"status": "cancel queued"}


@router.get("/api/benchmark/status")
async def benchmark_status():
    state = process_state["benchmark"]
    return {key: value for key, value in state.items()
            if key not in {"process", "processes"}}
