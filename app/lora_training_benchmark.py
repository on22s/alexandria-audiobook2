"""Run a hash-verified calibration through production train_lora.py."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time

from benchmark_validation import (get_lora_training_sample_count, get_benchmark_directory_path,
                                  get_benchmark_training_audio_path, get_benchmark_verified_file_path,
                                  get_benchmark_artifact_name, get_benchmark_output_path)
from lora_evidence import get_file_sha256


def _safe_file_path(root, relative):
    return get_benchmark_training_audio_path(root, relative)


def execute_fixture(fixture, python_executable, train_script, output_root):
    sample_count = get_lora_training_sample_count(fixture["sample_count"])
    fixture_id = get_benchmark_artifact_name(fixture["id"], "training fixture id")
    get_benchmark_output_path(output_root, fixture_id)
    source_dir = get_benchmark_directory_path(fixture["root_dir"], fixture["dataset_path"])
    metadata_path = get_benchmark_verified_file_path(
        source_dir, "metadata.jsonl", fixture["metadata_sha256"], "training metadata")
    with open(metadata_path, encoding="utf-8") as metadata_file:
        entries = [json.loads(line) for line in metadata_file if line.strip()]
    entries = entries[:sample_count]
    paths = [entry.get("audio_filepath") or entry.get("audio") for entry in entries]
    for relative_path in [*fixture["audio_sha256"], *paths]:
        _safe_file_path(source_dir, relative_path)
    if any(path not in fixture["audio_sha256"] for path in paths):
        raise ValueError("training metadata references unverified audio")
    for relative_path, expected in fixture["audio_sha256"].items():
        if get_file_sha256(_safe_file_path(source_dir, relative_path)) != expected:
            raise ValueError(f"training audio hash changed: {relative_path}")
    os.makedirs(output_root, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="alexandria-lora-dataset-") as dataset_dir:
        for entry in entries:
            relative_path = entry.get("audio_filepath") or entry.get("audio")
            destination = os.path.join(dataset_dir, relative_path)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(_safe_file_path(source_dir, relative_path), destination)
        with open(os.path.join(dataset_dir, "metadata.jsonl"), "w", encoding="utf-8") as output:
            for entry in entries:
                output.write(json.dumps(entry, ensure_ascii=False) + "\n")
        output_dir = get_benchmark_output_path(output_root, fixture_id)
        shutil.rmtree(output_dir, ignore_errors=True)
        command = [python_executable, "-u", train_script, "--data_dir", dataset_dir,
                   "--output_dir", output_dir, "--epochs", str(fixture["epochs"]),
                   "--lr", str(fixture["lr"]), "--batch_size", "1",
                   "--lora_r", str(fixture["lora_r"]), "--lora_alpha", str(fixture["lora_alpha"]),
                   "--gradient_accumulation_steps", str(fixture["grad_accum"]),
                   "--language", fixture["language"], "--seed", str(fixture["seed"])]
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, text=True, timeout=7200, check=False)
        elapsed = time.monotonic() - started
        if result.returncode:
            raise RuntimeError((result.stdout + "\n" + result.stderr)[-4000:])
    output_dir = get_benchmark_output_path(output_root, fixture_id)
    with open(get_benchmark_output_path(output_dir, "training_meta.json"), encoding="utf-8") as meta_file:
        meta = json.load(meta_file)
    adapter_path = get_benchmark_output_path(output_dir, "adapter_model.safetensors")
    if not os.path.isfile(adapter_path) or get_file_sha256(adapter_path) != meta.get("checkpoint_sha256"):
        raise ValueError("trained adapter checkpoint hash does not match metadata")
    return {"elapsed_seconds": round(elapsed, 3),
            "setup_seconds": round(elapsed - meta["training_time_seconds"], 3),
            "training_seconds": meta["training_time_seconds"],
            "samples_per_second": round(meta["num_samples"] * meta["epochs"] / meta["training_time_seconds"], 4),
            "num_samples": meta["num_samples"], "epochs": meta["epochs"],
            "final_loss": meta["final_loss"], "best_loss": meta["best_loss"],
            "oom_skips": meta["oom_skips"], "checkpoint_sha256": meta["checkpoint_sha256"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    def execute():
        payload = get_decoded_worker_payload(args.payload)
        return execute_fixture(payload["fixture"], payload["python"], payload["train_script"], payload["output_root"])
    emit_benchmark_worker_result('LORA_TRAINING_BENCHMARK_RESULT=',
                                 execute, metrics_only=True)


if __name__ == "__main__":
    main()
