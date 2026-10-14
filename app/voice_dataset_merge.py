"""Deterministic, provenance-preserving merges for confirmed voice datasets."""

import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile

import numpy as np
import soundfile as sf

from dataset_metadata import get_training_reference_relative_path

MERGE_VERSION = 7


def get_source_fingerprint(handle) -> dict:
    """Identify opened bytes without changing the archive reader's position."""
    digest = hashlib.sha256()
    position = handle.tell()
    try:
        handle.seek(0)
        stat = os.fstat(handle.fileno())
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    finally:
        handle.seek(position)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sha256": digest.hexdigest()}


def get_file_fingerprint(path: Path) -> dict:
    with path.open("rb") as handle:
        return get_source_fingerprint(handle)


def get_pcm_hash(wav_bytes: bytes) -> str:
    try:
        audio, sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
    except (RuntimeError, OSError, ValueError) as exc:
        raise ValueError("dataset contains unsupported or malformed audio") from exc
    return get_decoded_pcm_hash(audio, sample_rate)


def get_decoded_pcm_hash(audio, sample_rate) -> str:
    """Identify decoded rate, frame/channel shape and interleaved samples."""
    # Hash the same interleaved little-endian samples without two PCM-sized
    # byte copies. Ensure C order for decoded buffers with unusual strides.
    pcm = np.ascontiguousarray(audio, dtype="<f4")
    digest = hashlib.sha256(f"{sample_rate}:{pcm.shape[0]}:{pcm.shape[1]}:".encode("ascii"))
    digest.update(memoryview(pcm))
    return digest.hexdigest()


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
    except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile):
        return False


def _load_metadata(archive: zipfile.ZipFile, name="metadata.jsonl") -> list[dict]:
    try:
        lines = archive.read(name).decode("utf-8").splitlines()
    except KeyError as error:
        raise ValueError("source archive has no metadata.jsonl") from error
    try:
        return [json.loads(line) for line in lines if line.strip()]
    except json.JSONDecodeError as error:
        raise ValueError("source archive metadata.jsonl is invalid JSON") from error


def _get_reference_pair(source, relative):
    """Pair the selected audio with evidence from its owning archive only."""
    entries = _load_metadata(source)
    training = (_load_metadata(source, "train/metadata.jsonl")
                if "train/metadata.jsonl" in source.namelist() else
                [entry for entry in entries if not entry["audio_filepath"].startswith("val/")])
    reference = source.read(relative)
    reference_hash = get_pcm_hash(reference)
    effective = (get_training_reference_relative_path(training, "ref.wav" in source.namelist())
                 if training else ("ref.wav" if "ref.wav" in source.namelist() else None))
    is_effective = effective == relative or (effective is not None and
                     get_pcm_hash(source.read(effective)) == reference_hash)
    if is_effective and "ref_text.txt" in source.namelist():
        text = source.read("ref_text.txt").decode("utf-8").strip()
        if text:
            return reference, text
    for entry in entries:
        if get_pcm_hash(source.read(entry["audio_filepath"])) == reference_hash:
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("selected reference has no matching transcript")
            return reference, text
    # Keep the trainer's documented legacy fallback within its own source.
    if is_effective and training and isinstance(training[0].get("text"), str) and training[0]["text"].strip():
        return reference, training[0]["text"]
    raise ValueError("selected reference has no source-owned transcript")


def merge_voice_datasets(paths: list[Path], destination: Path) -> dict:
    """Atomically merge confirmed same-speaker ZIPs, removing exact PCM duplicates."""
    if not paths:
        raise ValueError("at least one source archive is required")
    sources = get_source_records(paths)
    if is_reusable_merge(destination, sources):
        return {"status": "reused", "destination": str(destination)}

    destination.parent.mkdir(parents=True, exist_ok=True)
    # A unique sibling temp file prevents concurrent merges to the same
    # destination from truncating one another's in-progress archive.
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
        delete=False)
    temporary = Path(temporary_handle.name)
    temporary_handle.close()
    seen_pcm = {}
    seen_transcripts = {}
    merged_metadata = []
    provenance = []
    duplicate_count = 0
    reference_paths = {}
    root_reference = None
    first_training_reference = None
    reference_sources = contextlib.ExitStack()
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as output:
            for source_index, source_path in enumerate(sorted(paths, key=lambda item: str(item))):
                source = zipfile.ZipFile(source_path)
                try:
                    if get_source_fingerprint(source.fp) != sources[source_index]["fingerprint"]:
                        raise ValueError("source archive changed before copying samples")
                    if root_reference is None and "ref.wav" in source.namelist():
                        root_reference = (source, "ref.wav")
                        reference_sources.callback(source.close)
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
                            if entry.get("text") != seen_transcripts[pcm_hash]:
                                raise ValueError(
                                    f"Conflicting transcripts for duplicate audio: {source_path}:{original_path} "
                                    f"duplicates {seen_pcm[pcm_hash]}")
                            duplicate_count += 1
                            provenance.append({"source_zip": str(source_path),
                                               "source_audio": original_path,
                                               "duplicate_of": seen_pcm[pcm_hash]})
                            continue
                        partition = "val" if original_path.startswith("val/") else "train"
                        if partition == "train" and first_training_reference is None:
                            first_training_reference = (source, entry.get("ref_audio") or original_path)
                            reference_sources.callback(source.close)
                        output_path = f"{partition}/s{source_index:03d}_{entry_index:06d}.wav"
                        output.writestr(output_path, wav_bytes)
                        merged_entry = dict(entry)
                        merged_entry["audio_filepath"] = output_path
                        ref_audio = entry.get("ref_audio")
                        if ref_audio:
                            if ref_audio == original_path:
                                merged_entry["ref_audio"] = output_path
                            else:
                                key = (source_index, ref_audio)
                                if key not in reference_paths:
                                    try:
                                        ref_bytes = source.read(ref_audio)
                                    except KeyError as error:
                                        raise ValueError(f"metadata reference audio is missing: {ref_audio}") from error
                                    get_pcm_hash(ref_bytes)
                                    reference_paths[key] = f"references/s{source_index:03d}_{len(reference_paths):06d}.wav"
                                    output.writestr(reference_paths[key], ref_bytes)
                                merged_entry["ref_audio"] = reference_paths[key]
                        merged_metadata.append(merged_entry)
                        seen_pcm[pcm_hash] = output_path
                        seen_transcripts[pcm_hash] = entry.get("text")
                        provenance.append({"output_audio": output_path,
                                           "source_zip": str(source_path),
                                           "source_audio": original_path})
                finally:
                    # Only these two possible owners stay open through selection.
                    # Their original ZIP member index and FD survive a path replacement.
                    if not any(reference is not None and reference[0] is source
                               for reference in (root_reference, first_training_reference)):
                        source.close()
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
            selected_reference = root_reference
            if train and get_training_reference_relative_path(train, root_reference is not None) != "ref.wav":
                selected_reference = first_training_reference
            if selected_reference is not None:
                try:
                    reference, reference_text = _get_reference_pair(*selected_reference)
                except zipfile.BadZipFile as error:
                    raise ValueError("source archive changed or contains malformed reference members") from error
                if root_reference is not None:
                    output.writestr("ref.wav", reference)
                output.writestr("ref_text.txt", reference_text)
            manifest = {"version": MERGE_VERSION, "sources": sources,
                        "unique_clip_count": len(merged_metadata),
                        "duplicate_clip_count": duplicate_count,
                        "provenance": provenance}
            output.writestr("merge_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        reference_sources.close()
        if get_source_records(paths) != sources:
            raise ValueError("source archive changed during merge; destination was not replaced")
        os.replace(temporary, destination)
    finally:
        reference_sources.close()
        if temporary.exists():
            temporary.unlink()
    return {"status": "merged", "destination": str(destination),
            "unique_clip_count": len(merged_metadata), "duplicate_clip_count": duplicate_count}
