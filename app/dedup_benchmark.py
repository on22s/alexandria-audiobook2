"""Hash-verified production Voice Lab dedup benchmark worker."""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import zipfile


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_dataset_content(path):
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        metadata = archive.read("metadata.jsonl")
        entries = [json.loads(line) for line in metadata.decode("utf-8").splitlines()
                   if line.strip()]
        digest.update(metadata)
        for entry in entries:
            relative_path = entry["audio_filepath"]
            digest.update(relative_path.encode("utf-8"))
            digest.update(archive.read(relative_path))
    return digest.hexdigest(), len(entries)


def execute_fixture(fixture, python_executable, analysis_script):
    source_dir = os.path.abspath(os.path.join(fixture["root_dir"], fixture["dataset_path"]))
    metadata_path = os.path.join(source_dir, "metadata.jsonl")
    if _hash_file(metadata_path) != fixture["metadata_sha256"]:
        raise ValueError("dedup metadata hash changed")
    with open(metadata_path, encoding="utf-8") as metadata_file:
        entries = [json.loads(line) for line in metadata_file if line.strip()]
    entries = entries[:fixture["samples_per_volume"] * 2]
    for relative_path, expected in fixture["audio_sha256"].items():
        if _hash_file(os.path.join(source_dir, relative_path)) != expected:
            raise ValueError(f"dedup audio hash changed: {relative_path}")
    with tempfile.TemporaryDirectory(prefix="alexandria-dedup-benchmark-") as scratch:
        narrator_dir = os.path.join(scratch, "zips", "benchmark_narrator")
        os.makedirs(narrator_dir)
        size = fixture["samples_per_volume"]
        for volume_index, volume_entries in enumerate((entries[:size], entries[size:]), 1):
            zip_path = os.path.join(narrator_dir, f"volume_{volume_index:02d}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for entry in volume_entries:
                    relative_path = entry.get("audio_filepath") or entry.get("audio")
                    archive.write(os.path.join(source_dir, relative_path), relative_path)
                metadata = "".join(json.dumps(entry, ensure_ascii=False) + "\n"
                                   for entry in volume_entries)
                archive.writestr("metadata.jsonl", metadata)
        output_dir = os.path.join(scratch, "dedup_output")
        env = dict(os.environ, PYTHONHASHSEED=str(fixture["seed"]))
        command = [python_executable, "-u", analysis_script, "--phase", "dedup",
                   "--device", "cuda", "--zips2", os.path.join(scratch, "zips"),
                   "--dedup-out", output_dir, "--seed", str(fixture["seed"])]
        started = time.monotonic()
        result = subprocess.run(command, cwd=scratch, env=env, capture_output=True,
                                text=True, timeout=3600, check=False)
        elapsed = time.monotonic() - started
        if result.returncode:
            raise RuntimeError((result.stdout + "\n" + result.stderr)[-4000:])
        with open(os.path.join(output_dir, "dedup_clusters.json"), encoding="utf-8") as report_file:
            report = json.load(report_file)
        narrator = report["narrators"]["benchmark_narrator"]
        deduped_dir = os.path.join(scratch, "zips", "_deduped")
        output_zips = []
        for name in os.listdir(deduped_dir):
            path = os.path.join(deduped_dir, name)
            if name.endswith(".zip"):
                content_hash, sample_count = _hash_dataset_content(path)
                output_zips.append({"name": name, "archive_sha256": _hash_file(path),
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
    payload = json.loads(base64.b64decode(args.payload).decode("utf-8"))
    try:
        metrics = execute_fixture(payload["fixture"], payload["python"],
                                  payload["analysis_script"])
        result = {"status": "passed", "metrics": metrics, "error": None}
    except Exception as exc:
        result = {"status": "failed", "metrics": {}, "error": str(exc)}
    print("DEDUP_BENCHMARK_RESULT=" + json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
