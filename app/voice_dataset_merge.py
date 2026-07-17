"""Deterministic, provenance-preserving merges for confirmed voice datasets."""

import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

import numpy as np
import soundfile as sf


MERGE_VERSION = 1


def get_file_fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 2 * 1024 * 1024:
            handle.seek(stat.st_size - 1024 * 1024)
            digest.update(handle.read(1024 * 1024))
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "edge_sha256": digest.hexdigest()}


def get_pcm_hash(wav_bytes: bytes) -> str:
    audio, _sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
    return hashlib.sha256(np.asarray(audio, dtype="<f4").tobytes()).hexdigest()


def get_source_records(paths: list[Path]) -> list[dict]:
    return [{"path": str(path.resolve()), "fingerprint": get_file_fingerprint(path)}
            for path in sorted(paths, key=lambda item: str(item))]


def is_reusable_merge(destination: Path, sources: list[dict]) -> bool:
    if not destination.is_file() or not zipfile.is_zipfile(destination):
        return False
    try:
        with zipfile.ZipFile(destination) as archive:
            manifest = json.loads(archive.read("merge_manifest.json"))
        return manifest.get("version") == MERGE_VERSION and manifest.get("sources") == sources
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
        return False


def _load_metadata(archive: zipfile.ZipFile) -> list[dict]:
    try:
        lines = archive.read("metadata.jsonl").decode("utf-8").splitlines()
    except KeyError as error:
        raise ValueError("source archive has no metadata.jsonl") from error
    return [json.loads(line) for line in lines if line.strip()]


def merge_voice_datasets(paths: list[Path], destination: Path) -> dict:
    """Atomically merge confirmed same-speaker ZIPs, removing exact PCM duplicates."""
    if not paths:
        raise ValueError("at least one source archive is required")
    sources = get_source_records(paths)
    if is_reusable_merge(destination, sources):
        return {"status": "reused", "destination": str(destination)}

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    seen_pcm = {}
    merged_metadata = []
    provenance = []
    duplicate_count = 0
    reference = None
    reference_text = None
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as output:
            for source_index, source_path in enumerate(sorted(paths, key=lambda item: str(item))):
                with zipfile.ZipFile(source_path) as source:
                    if reference is None and "ref.wav" in source.namelist():
                        reference = source.read("ref.wav")
                    if reference_text is None and "ref_text.txt" in source.namelist():
                        reference_text = source.read("ref_text.txt")
                    for entry_index, entry in enumerate(_load_metadata(source)):
                        original_path = entry.get("audio_filepath", "")
                        if not original_path:
                            raise ValueError(f"metadata entry {entry_index} has no audio_filepath")
                        try:
                            wav_bytes = source.read(original_path)
                        except KeyError as error:
                            raise ValueError(f"metadata audio is missing: {original_path}") from error
                        pcm_hash = get_pcm_hash(wav_bytes)
                        if pcm_hash in seen_pcm:
                            duplicate_count += 1
                            provenance.append({"source_zip": str(source_path),
                                               "source_audio": original_path,
                                               "duplicate_of": seen_pcm[pcm_hash]})
                            continue
                        partition = "val" if original_path.startswith("val/") else "train"
                        output_path = f"{partition}/s{source_index:03d}_{entry_index:06d}.wav"
                        output.writestr(output_path, wav_bytes)
                        merged_entry = dict(entry)
                        merged_entry["audio_filepath"] = output_path
                        merged_metadata.append(merged_entry)
                        seen_pcm[pcm_hash] = output_path
                        provenance.append({"output_audio": output_path,
                                           "source_zip": str(source_path),
                                           "source_audio": original_path})
            if not merged_metadata:
                raise ValueError("source archives contain no unique audio entries")
            train = [entry for entry in merged_metadata if entry["audio_filepath"].startswith("train/")]
            val = [entry for entry in merged_metadata if entry["audio_filepath"].startswith("val/")]
            for name, entries in (("metadata.jsonl", merged_metadata),
                                  ("train/metadata.jsonl", train),
                                  ("val/metadata.jsonl", val)):
                if entries:
                    output.writestr(name, "".join(json.dumps(entry, ensure_ascii=False) + "\n"
                                                   for entry in entries))
            if reference is not None:
                output.writestr("ref.wav", reference)
            if reference_text is not None:
                output.writestr("ref_text.txt", reference_text)
            manifest = {"version": MERGE_VERSION, "sources": sources,
                        "unique_clip_count": len(merged_metadata),
                        "duplicate_clip_count": duplicate_count,
                        "provenance": provenance}
            output.writestr("merge_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"status": "merged", "destination": str(destination),
            "unique_clip_count": len(merged_metadata), "duplicate_clip_count": duplicate_count}
