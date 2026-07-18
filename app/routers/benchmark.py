"""Benchmark preflight and state routes."""

import asyncio
import os
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from benchmark_core import (get_benchmark_preflight_id,
                            validate_benchmark_manifest)
from benchmark_environment import (collect_local_environment,
                                   collect_thunder_environment)
from benchmark_runner import run_script_generation_benchmark
from config_settings import load_app_config
from core import (CONFIG_PATH, REPORTS_DIR, ROOT_DIR, UPLOADS_DIR,
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
    if manifest["stage"] != "script_generation" or len(manifest["targets"]) != 1:
        raise HTTPException(status_code=400,
                            detail="Script-generation runs require exactly one target.")
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
        run_script_generation_benchmark(
            manifest, preflight["environments"][manifest["targets"][0]], report_path, state,
            CONFIG_PATH, UPLOADS_DIR)

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
