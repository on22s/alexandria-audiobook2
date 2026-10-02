#!/usr/bin/env python3
"""Isolated production-backed Audacity and M4B export benchmark worker."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile

from project import ProjectManager
from benchmark_validation import get_benchmark_file_path, get_benchmark_output_path
from lora_evidence import get_file_sha256


def execute_payload(payload):
    fixture = payload["fixture"]
    with tempfile.TemporaryDirectory(prefix="alexandria-export-") as root:
        voicelines = get_benchmark_output_path(root, "voicelines")
        os.makedirs(voicelines)
        chunks = []
        for index, chunk in enumerate(fixture["chunks"]):
            expected = fixture["audio_sha256"].get(chunk["audio_path"])
            if not expected:
                raise ValueError(f"export audio is unverified: {chunk['audio_path']}")
            source = get_benchmark_file_path(payload["source_root"], chunk["audio_path"])
            target = get_benchmark_output_path(voicelines, f"sample-{index}.wav")
            shutil.copy2(source, target)
            if get_file_sha256(target) != expected:
                raise ValueError(f"export audio hash changed: {chunk['audio_path']}")
            copied = dict(chunk)
            copied["audio_path"] = os.path.relpath(target, root)
            chunks.append(copied)
        with open(get_benchmark_output_path(root, "chunks.json"), "w", encoding="utf-8") as output:
            json.dump(chunks, output)
        manager = ProjectManager(root)
        started = time.monotonic()
        if payload["stage"] == "audacity_export":
            success, message = manager.export_audacity()
            artifact = get_benchmark_output_path(root, "audacity_export.zip")
        else:
            success, message = manager.merge_m4b(
                per_chunk_chapters=fixture["per_chunk_chapters"],
                metadata={"title": "Benchmark Book", "author": "Alexandria"})
            artifact = get_benchmark_output_path(root, "audiobook.m4b")
        elapsed = time.monotonic() - started
        if not success or not os.path.isfile(artifact):
            raise RuntimeError(message)
        digest = hashlib.sha256()
        artifact_bytes = 0
        with open(artifact, "rb") as artifact_file:
            for block in iter(lambda: artifact_file.read(1024 * 1024), b""):
                digest.update(block)
                artifact_bytes += len(block)
        result = {"status": "passed", "elapsed_seconds": round(elapsed, 3),
                  "artifact_bytes": artifact_bytes,
                  "artifact_sha256": digest.hexdigest()}
        if payload["stage"] == "audacity_export":
            with zipfile.ZipFile(artifact) as archive:
                names = sorted(archive.namelist())
                labels = archive.read("labels.txt").decode("utf-8").splitlines()
            result.update({"members": names, "label_count": len(labels)})
            if "project.lof" not in names or len(labels) != len(chunks):
                result["status"] = "failed"
        else:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_chapters", "-show_entries",
                 "format=duration:stream=codec_name:chapter=start_time,end_time,tags",
                 "-of", "json", artifact],
                capture_output=True, text=True, timeout=30, check=False)
            if probe.returncode:
                raise RuntimeError(probe.stderr.strip() or "ffprobe failed")
            media = json.loads(probe.stdout)
            result["media"] = media
            codecs = [stream.get("codec_name") for stream in media.get("streams", [])]
            chapters = media.get("chapters", [])
            expected_chapters = len(chunks) if fixture["per_chunk_chapters"] else None
            chapters_valid = bool(chapters) and all(
                float(chapter.get("end_time", 0)) > float(chapter.get("start_time", 0))
                and (chapter.get("tags") or {}).get("title") for chapter in chapters)
            if ("aac" not in codecs
                    or float(media.get("format", {}).get("duration", 0)) <= 0
                    or not chapters_valid
                    or (expected_chapters is not None and len(chapters) != expected_chapters)):
                result["status"] = "failed"
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    emit_benchmark_worker_result('EXPORT_BENCHMARK_RESULT=',
                                 lambda: execute_payload(get_decoded_worker_payload(args.payload)))


if __name__ == "__main__":
    main()
