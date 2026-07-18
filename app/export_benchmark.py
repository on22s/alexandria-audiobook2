#!/usr/bin/env python3
"""Isolated production-backed Audacity and M4B export benchmark worker."""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile

from project import ProjectManager


def execute_payload(payload):
    fixture = payload["fixture"]
    with tempfile.TemporaryDirectory(prefix="alexandria-export-") as root:
        voicelines = os.path.join(root, "voicelines")
        os.makedirs(voicelines)
        chunks = []
        for index, chunk in enumerate(fixture["chunks"]):
            source = os.path.join(payload["source_root"], chunk["audio_path"])
            target = os.path.join(voicelines, f"sample-{index}.wav")
            shutil.copy2(source, target)
            copied = dict(chunk)
            copied["audio_path"] = os.path.relpath(target, root)
            chunks.append(copied)
        with open(os.path.join(root, "chunks.json"), "w", encoding="utf-8") as output:
            json.dump(chunks, output)
        manager = ProjectManager(root)
        started = time.monotonic()
        if payload["stage"] == "audacity_export":
            success, message = manager.export_audacity()
            artifact = os.path.join(root, "audacity_export.zip")
        else:
            success, message = manager.merge_m4b(
                per_chunk_chapters=fixture["per_chunk_chapters"],
                metadata={"title": "Benchmark Book", "author": "Alexandria"})
            artifact = os.path.join(root, "audiobook.m4b")
        elapsed = time.monotonic() - started
        if not success or not os.path.isfile(artifact):
            raise RuntimeError(message)
        with open(artifact, "rb") as artifact_file:
            raw = artifact_file.read()
        result = {"status": "passed", "elapsed_seconds": round(elapsed, 3),
                  "artifact_bytes": len(raw),
                  "artifact_sha256": hashlib.sha256(raw).hexdigest()}
        if payload["stage"] == "audacity_export":
            with zipfile.ZipFile(artifact) as archive:
                names = sorted(archive.namelist())
                labels = archive.read("labels.txt").decode("utf-8").splitlines()
            result.update({"members": names, "label_count": len(labels)})
            if "project.lof" not in names or len(labels) != len(chunks):
                result["status"] = "failed"
        else:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration:stream=codec_name", "-of", "json", artifact],
                capture_output=True, text=True, timeout=30, check=False)
            if probe.returncode:
                raise RuntimeError(probe.stderr.strip() or "ffprobe failed")
            media = json.loads(probe.stdout)
            result["media"] = media
            codecs = [stream.get("codec_name") for stream in media.get("streams", [])]
            if "aac" not in codecs or float(media.get("format", {}).get("duration", 0)) <= 0:
                result["status"] = "failed"
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    payload = json.loads(base64.b64decode(args.payload).decode("utf-8"))
    print("EXPORT_BENCHMARK_RESULT=" + json.dumps(
        execute_payload(payload), separators=(",", ":")))


if __name__ == "__main__":
    main()
