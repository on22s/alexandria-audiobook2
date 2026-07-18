#!/usr/bin/env python3
"""Isolated production Dataset Builder batch benchmark worker."""

import argparse
import asyncio
import base64
import json
import os
import tempfile
import time

from routers import dataset_builder
from tts import TTSEngine
from tts_benchmark import measure_wav


def execute_payload(payload):
    fixture = payload["fixture"]
    engine = TTSEngine({"tts": {**(payload.get("tts") or {}),
                                "mode": "local", "compile_codec": False}})
    with tempfile.TemporaryDirectory(prefix="alexandria-dataset-builder-") as root:
        original_dir = dataset_builder.DATASET_BUILDER_DIR
        original_get_engine = dataset_builder.project_manager.get_engine
        state = dataset_builder.process_state["dataset_builder"]
        state.update({"running": False, "logs": [], "cancel": False})
        dataset_builder.DATASET_BUILDER_DIR = root
        dataset_builder.project_manager.get_engine = lambda: engine
        request = dataset_builder.DatasetBatchGenRequest(
            name="benchmark", description=fixture["description"],
            samples=[dataset_builder.LoraDatasetSample(**sample)
                     for sample in fixture["samples"]],
            global_seed=fixture["global_seed"], seeds=fixture["seeds"])
        started = time.monotonic()
        try:
            response = asyncio.run(dataset_builder.dataset_builder_generate_batch(request))
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                if not state["running"] and any(
                        str(line).startswith("[DONE]") for line in state["logs"]):
                    break
                time.sleep(0.05)
            else:
                state["cancel"] = True
                raise TimeoutError("Dataset Builder batch did not finish")
        finally:
            dataset_builder.DATASET_BUILDER_DIR = original_dir
            dataset_builder.project_manager.get_engine = original_get_engine
        elapsed = time.monotonic() - started
        project_dir = os.path.join(root, "benchmark")
        with open(os.path.join(project_dir, "state.json"), encoding="utf-8") as source:
            saved = json.load(source)
        outputs = []
        for index, sample in enumerate(saved.get("samples", [])):
            wav_path = os.path.join(project_dir, f"sample_{index:03d}.wav")
            metrics = measure_wav(wav_path, elapsed) if os.path.isfile(wav_path) else None
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
        return {"status": "passed" if passed else "failed",
                "elapsed_seconds": round(elapsed, 3), "outputs": outputs,
                "logs": list(state["logs"]), "completed": sum(
                    output["state"].get("status") == "done" for output in outputs)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    payload = json.loads(base64.b64decode(args.payload).decode("utf-8"))
    print("DATASET_BUILDER_BENCHMARK_RESULT=" + json.dumps(
        execute_payload(payload), separators=(",", ":")))


if __name__ == "__main__":
    main()
