#!/usr/bin/env python3
"""Isolated production Dataset Builder batch benchmark worker."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import asyncio
import json
import os
import tempfile
import time

from routers import dataset_builder
from tts import TTSEngine
from tts_benchmark import measure_wav
from benchmark_validation import get_benchmark_output_path


def execute_payload(payload):
    fixture = payload["fixture"]
    request = dataset_builder.DatasetBatchGenRequest(
        name="benchmark", description=fixture["description"],
        samples=[dataset_builder.LoraDatasetSample(**sample)
                 for sample in fixture["samples"]],
        global_seed=fixture["global_seed"], seeds=fixture["seeds"])
    engine = TTSEngine({"tts": {**(payload.get("tts") or {}),
                                "mode": "local", "compile_codec": False}})
    with tempfile.TemporaryDirectory(prefix="alexandria-dataset-builder-") as root:
        original_dir = dataset_builder.DATASET_BUILDER_DIR
        original_get_engine = dataset_builder.project_manager.get_engine
        state = dataset_builder.process_state["dataset_builder"]
        state.update({"running": False, "logs": [], "cancel": False})
        dataset_builder.DATASET_BUILDER_DIR = root
        dataset_builder.project_manager.get_engine = lambda: engine
        original_start_worker = dataset_builder.start_claimed_task_thread
        workers = []

        def start_worker(*args, **kwargs):
            worker = original_start_worker(*args, **kwargs)
            workers.append(worker)
            return worker

        dataset_builder.start_claimed_task_thread = start_worker
        started = time.monotonic()
        try:
            response = asyncio.run(dataset_builder.dataset_builder_generate_batch(request))
            deadline = started + 1800
            if not workers:
                raise RuntimeError("Dataset Builder did not start an owned worker")
            for worker in workers:
                worker.join(max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    raise TimeoutError("Dataset Builder batch did not finish")
        finally:
            # Cancellation cannot interrupt an active model call. This isolated
            # worker keeps its routing and temporary tree until the owned thread
            # exits; the existing 3600s subprocess supervisor bounds a stuck call.
            if any(worker.is_alive() for worker in workers):
                state["cancel"] = True
            for worker in workers:
                worker.join()
            dataset_builder.DATASET_BUILDER_DIR = original_dir
            dataset_builder.project_manager.get_engine = original_get_engine
            dataset_builder.start_claimed_task_thread = original_start_worker
        elapsed = time.monotonic() - started
        project_dir = get_benchmark_output_path(root, "benchmark")
        with open(get_benchmark_output_path(project_dir, "state.json"), encoding="utf-8") as source:
            saved = json.load(source)
        outputs = []
        for index, sample in enumerate(saved.get("samples", [])):
            wav_path = get_benchmark_output_path(project_dir, f"sample_{index:03d}.wav")
            metrics = measure_wav(wav_path, 0) if os.path.isfile(wav_path) else None
            if metrics is not None:
                # No per-sample timer exists: keep health metrics, not fabricated
                # sample latency or throughput derived from whole-batch time.
                metrics.pop("elapsed_seconds")
                metrics.pop("audio_seconds_per_second")
            outputs.append({"index": index, "state": sample, "metrics": metrics})
        expected_descriptions = [
            f"{fixture['description']}, {sample.get('emotion', '').strip()}"
            if sample.get("emotion", "").strip() else fixture["description"]
            for sample in fixture["samples"]]
        passed = (response.get("total") == len(fixture["samples"])
                  and len(outputs) == len(fixture["samples"])
                  and all(output["state"].get("status") == "done"
                          and output["metrics"]
                          and output["state"].get("description") == expected_descriptions[index]
                          for index, output in enumerate(outputs)))
        audio_duration = sum(output["metrics"]["duration_seconds"]
                             for output in outputs if output["metrics"])
        return {"status": "passed" if passed else "failed",
                "elapsed_seconds": round(elapsed, 3),
                "audio_duration_seconds": round(audio_duration, 3),
                "audio_seconds_per_second": round(audio_duration / elapsed, 4)
                if elapsed > 0 else 0.0,
                "outputs": outputs,
                "logs": list(state["logs"]), "completed": sum(
                    output["state"].get("status") == "done" for output in outputs)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    emit_benchmark_worker_result('DATASET_BUILDER_BENCHMARK_RESULT=',
                                 lambda: execute_payload(get_decoded_worker_payload(args.payload)))


if __name__ == "__main__":
    main()
