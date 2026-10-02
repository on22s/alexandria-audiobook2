#!/usr/bin/env python3
"""Disposable production-backed worker for Voice Lab naming benchmarks."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from benchmark_validation import get_benchmark_artifact_name, get_benchmark_output_path


def execute_payload(payload):
    fixture = payload["fixture"]
    for entry in fixture["entries"]:
        get_benchmark_artifact_name(entry.get("id"), "naming benchmark adapter ID")
    with tempfile.TemporaryDirectory(prefix="alexandria-naming-") as temp_dir:
        models_dir = get_benchmark_output_path(temp_dir, "models")
        os.makedirs(models_dir)
        manifest_path = get_benchmark_output_path(models_dir, "manifest.json")
        for entry in fixture["entries"]:
            os.makedirs(get_benchmark_output_path(models_dir, entry["id"]))
        with open(manifest_path, "w", encoding="utf-8") as manifest_file:
            json.dump(fixture["entries"], manifest_file, ensure_ascii=False)
        started = time.monotonic()
        result = subprocess.run(
            [payload["python"], payload["script"], "--manifest", manifest_path,
             "--models-dir", models_dir, "--apply"],
            capture_output=True, text=True, timeout=60, check=False)
        elapsed = time.monotonic() - started
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        models_dir = get_benchmark_output_path(temp_dir, "models")
        manifest_path = get_benchmark_output_path(models_dir, "manifest.json")
        with open(manifest_path, "rb") as manifest_file:
            raw = manifest_file.read()
        named = json.loads(raw.decode("utf-8"))
        adapter_dirs = sorted(name for name in os.listdir(models_dir)
                              if os.path.isdir(get_benchmark_output_path(models_dir, name)))
        ids = [entry["id"] for entry in named]
        passed = (sorted(ids) == adapter_dirs and os.path.isfile(manifest_path + ".bak")
                  and all(entry.get("name") == entry["id"] for entry in named))
        return {"status": "passed" if passed else "failed",
                "elapsed_seconds": round(elapsed, 6), "named_ids": ids,
                "adapter_dirs": adapter_dirs,
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "backup_created": os.path.isfile(manifest_path + ".bak")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    emit_benchmark_worker_result('NAMING_BENCHMARK_RESULT=',
                                 lambda: execute_payload(get_decoded_worker_payload(args.payload)))


if __name__ == "__main__":
    main()
