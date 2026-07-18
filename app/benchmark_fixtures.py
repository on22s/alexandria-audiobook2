"""Deterministic benchmark fixtures derived from existing uploaded books."""

import hashlib
import os
import copy

from generate_script import fix_mojibake, split_into_chunks
from source_normalization import normalize_known_source_corruptions
from utils import is_path_inside


def get_normalized_source_chunks(raw, chunk_size):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("benchmark source is not UTF-8 text") from exc
    text = fix_mojibake(text)
    text, _ = normalize_known_source_corruptions(text)
    return split_into_chunks(text, max_size=chunk_size)


def build_script_generation_manifest(specs, uploads_dir, repetitions=1,
                                     targets=None, chunk_size=6000):
    """Build hashed chunk references without copying source text."""
    if not isinstance(specs, list) or not specs:
        raise ValueError("at least one source specification is required")
    if not isinstance(chunk_size, int) or chunk_size < 200:
        raise ValueError("chunk_size must be an integer of at least 200")
    fixtures = []
    for spec in specs:
        path = os.path.abspath(spec.get("path") or "")
        if not is_path_inside(path, uploads_dir) or not os.path.isfile(path):
            raise ValueError("benchmark source must be a file inside uploads")
        with open(path, "rb") as source_file:
            raw = source_file.read()
        source_sha256 = hashlib.sha256(raw).hexdigest()
        chunks = get_normalized_source_chunks(raw, chunk_size)
        for chunk_number in spec.get("chunk_numbers") or []:
            if not isinstance(chunk_number, int) or not 1 <= chunk_number <= len(chunks):
                raise ValueError(f"chunk_number out of range for {os.path.basename(path)}")
            chunk = chunks[chunk_number - 1]
            previous_entries = (spec.get("previous_entries_by_chunk") or {}).get(
                chunk_number, [])
            if not isinstance(previous_entries, list) or any(
                    not isinstance(entry, dict) for entry in previous_entries):
                raise ValueError("previous_entries_by_chunk values must be lists of entries")
            fixtures.append({
                "id": f"{os.path.splitext(os.path.basename(path))[0]}-chunk-{chunk_number}",
                "sha256": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                "path": path, "source_sha256": source_sha256,
                "chunk_number": chunk_number, "total_chunks": len(chunks),
                "chunk_size": chunk_size,
                "previous_entries": copy.deepcopy(previous_entries),
            })
    if not fixtures:
        raise ValueError("source specifications selected no chunks")
    return {"schema_version": 1, "stage": "script_generation",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": fixtures, "settings": {"max_retries": 0},
            "quality_thresholds": {"source_token_recall": 0.9,
                                   "ordered_trigram_recall": 0.9}}
