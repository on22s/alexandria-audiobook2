"""Run a hash-verified calibration through production train_lora.py."""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def execute_fixture(fixture, python_executable, train_script, output_root):
    source_dir = os.path.abspath(os.path.join(fixture["root_dir"],
                                              fixture["dataset_path"]))
    metadata_path = os.path.join(source_dir, "metadata.jsonl")
    if _hash_file(metadata_path) != fixture["metadata_sha256"]:
        raise ValueError("training metadata hash changed")
    with open(metadata_path, encoding="utf-8") as metadata_file:
        entries = [json.loads(line) for line in metadata_file if line.strip()]
    entries = entries[:fixture["sample_count"]]
    for relative_path, expected in fixture["audio_sha256"].items():
        if _hash_file(os.path.join(source_dir, relative_path)) != expected:
            raise ValueError(f"training audio hash changed: {relative_path}")
    os.makedirs(output_root, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="alexandria-lora-dataset-") as dataset_dir:
        for entry in entries:
            relative_path = entry.get("audio_filepath") or entry.get("audio")
            destination = os.path.join(dataset_dir, relative_path)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(os.path.join(source_dir, relative_path), destination)
        with open(os.path.join(dataset_dir, "metadata.jsonl"), "w", encoding="utf-8") as output:
            for entry in entries:
                output.write(json.dumps(entry, ensure_ascii=False) + "\n")
        output_dir = os.path.join(output_root, fixture["id"])
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
    with open(os.path.join(output_dir, "training_meta.json"), encoding="utf-8") as meta_file:
        meta = json.load(meta_file)
    adapter_path = os.path.join(output_dir, "adapter_model.safetensors")
    if not os.path.isfile(adapter_path) or _hash_file(adapter_path) != meta.get("checkpoint_sha256"):
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
    payload = json.loads(base64.b64decode(args.payload).decode("utf-8"))
    try:
        metrics = execute_fixture(payload["fixture"], payload["python"],
                                  payload["train_script"], payload["output_root"])
        result = {"status": "passed", "metrics": metrics, "error": None}
    except Exception as exc:
        result = {"status": "failed", "metrics": {}, "error": str(exc)}
    print("LORA_TRAINING_BENCHMARK_RESULT=" + json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
