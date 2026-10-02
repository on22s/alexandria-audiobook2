"""Hash-verified production Voice Lab dedup benchmark worker."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
import zipfile
from benchmark_validation import (get_benchmark_file_path, get_benchmark_directory_path,
                                  get_benchmark_archive_audio_path, get_benchmark_output_path)
from lora_evidence import get_file_sha256


def get_dedup_dataset_path(root, name):
    try:
        return get_benchmark_directory_path(root, name)
    except ValueError as exc:
        raise ValueError("dedup dataset is outside the fixture root or missing") from exc


def get_dedup_audio_path(root, name):
    """Return a contained source with an unchanged portable ZIP member name."""
    return get_benchmark_archive_audio_path(root, name)


def _hash_dataset_content(path):
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        metadata = archive.read("metadata.jsonl")
        entries = [json.loads(line) for line in metadata.decode("utf-8").splitlines()
                   if line.strip()]
        digest.update(metadata)
        for entry in entries:
            relative_path = entry.get("audio_filepath") or entry.get("audio")
            digest.update(relative_path.encode("utf-8"))
            digest.update(archive.read(relative_path))
    return digest.hexdigest(), len(entries)



def get_dedup_selected_entries(entries, samples_per_volume):
    """Select exactly two full volumes; invalid counts cannot alter slicing."""
    if type(samples_per_volume) is not int or samples_per_volume < 1:
        raise ValueError("dedup samples_per_volume must be a positive integer")
    if not isinstance(entries, list) or len(entries) < samples_per_volume * 2:
        raise ValueError("dedup source dataset has too few samples for two volumes")
    selected = entries[:samples_per_volume * 2]
    if any(not isinstance(entry, dict) for entry in selected):
        raise ValueError("dedup metadata entries must be objects")
    return selected


def validate_dedup_audio_hash_coverage(entries, audio_sha256):
    """Every selected clip needs a declared hash before any processing."""
    if not isinstance(audio_sha256, dict):
        raise ValueError("dedup audio hashes must be a mapping")
    for entry in entries:
        path = entry.get("audio_filepath") or entry.get("audio")
        if not isinstance(path, str) or not path:
            raise ValueError("dedup selected audio path is missing")
        if path not in audio_sha256:
            raise ValueError(f"dedup selected audio hash is missing: {path}")

def execute_fixture(fixture, python_executable, analysis_script):
    source_dir = get_dedup_dataset_path(fixture["root_dir"], fixture["dataset_path"])
    metadata_path = get_benchmark_file_path(source_dir, "metadata.jsonl")
    with open(metadata_path, "rb") as metadata_file:
        metadata_raw = metadata_file.read()
    if hashlib.sha256(metadata_raw).hexdigest() != fixture["metadata_sha256"]:
        raise ValueError("dedup metadata hash changed")
    entries = get_dedup_selected_entries(
        [json.loads(line) for line in metadata_raw.decode("utf-8").splitlines() if line.strip()],
        fixture["samples_per_volume"])
    validate_dedup_audio_hash_coverage(entries, fixture["audio_sha256"])
    audio_paths = {name: get_dedup_audio_path(source_dir, name)
                   for name in fixture["audio_sha256"]}
    for relative_path, expected in fixture["audio_sha256"].items():
        if get_file_sha256(audio_paths[relative_path]) != expected:
            raise ValueError(f"dedup audio hash changed: {relative_path}")
    with tempfile.TemporaryDirectory(prefix="alexandria-dedup-benchmark-") as scratch:
        zips_dir = get_benchmark_output_path(scratch, "zips")
        narrator_dir = get_benchmark_output_path(zips_dir, "benchmark_narrator")
        os.makedirs(narrator_dir)
        size = fixture["samples_per_volume"]
        for volume_index, volume_entries in enumerate((entries[:size], entries[size:]), 1):
            zip_path = get_benchmark_output_path(narrator_dir, f"volume_{volume_index:02d}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for entry in volume_entries:
                    relative_path = entry.get("audio_filepath") or entry.get("audio")
                    archive.write(audio_paths[relative_path], relative_path)
                metadata = "".join(json.dumps(entry, ensure_ascii=False) + "\n"
                                   for entry in volume_entries)
                archive.writestr("metadata.jsonl", metadata)
        output_dir = get_benchmark_output_path(scratch, "dedup_output")
        env = dict(os.environ, PYTHONHASHSEED=str(fixture["seed"]))
        command = [python_executable, "-u", analysis_script, "--phase", "dedup",
                   "--device", "cuda", "--zips2", zips_dir,
                   "--dedup-out", output_dir, "--seed", str(fixture["seed"])]
        started = time.monotonic()
        result = subprocess.run(command, cwd=scratch, env=env, capture_output=True,
                                text=True, timeout=3600, check=False)
        elapsed = time.monotonic() - started
        if result.returncode:
            raise RuntimeError((result.stdout + "\n" + result.stderr)[-4000:])
        output_dir = get_benchmark_output_path(scratch, "dedup_output")
        zips_dir = get_benchmark_output_path(scratch, "zips")
        with open(get_benchmark_output_path(output_dir, "dedup_clusters.json"), encoding="utf-8") as report_file:
            report = json.load(report_file)
        narrator = report["narrators"]["benchmark_narrator"]
        deduped_dir = get_benchmark_output_path(zips_dir, "_deduped")
        output_zips = []
        for name in os.listdir(deduped_dir):
            path = get_benchmark_output_path(deduped_dir, name)
            if name.endswith(".zip"):
                content_hash, sample_count = _hash_dataset_content(path)
                output_zips.append({"name": name, "archive_sha256": get_file_sha256(path),
                                    "content_sha256": content_hash,
                                    "sample_count": sample_count})
    similarity = narrator["similarity_matrix"][0][1]
    return {"elapsed_seconds": round(elapsed, 3), "similarity": round(similarity, 6),
            "clusters": narrator["clusters"], "output_zip_count": len(output_zips),
            "output_zips": output_zips}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    def execute():
        payload = get_decoded_worker_payload(args.payload)
        return execute_fixture(payload["fixture"], payload["python"], payload["analysis_script"])
    emit_benchmark_worker_result('DEDUP_BENCHMARK_RESULT=',
                                 execute, metrics_only=True)


if __name__ == "__main__":
    main()
