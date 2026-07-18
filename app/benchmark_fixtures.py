"""Deterministic benchmark fixtures derived from existing uploaded books."""

import hashlib
import os
import copy
import json

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


def _hash_entries(entries):
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_script_review_manifest(specs, scripts_dir, repetitions=1,
                                 targets=None, batch_size=25):
    """Build immutable entry-slice references from saved annotated scripts."""
    if not isinstance(specs, list) or not specs:
        raise ValueError("at least one review source specification is required")
    if not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("review batch_size must be a positive integer")
    fixtures = []
    for spec in specs:
        path = os.path.abspath(spec.get("path") or "")
        if not is_path_inside(path, scripts_dir) or not os.path.isfile(path):
            raise ValueError("review source must be a file inside scripts")
        with open(path, "rb") as source_file:
            raw = source_file.read()
        try:
            entries = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("review source must be valid UTF-8 JSON") from exc
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("review source must contain a list of entries")
        for start in spec.get("entry_starts") or []:
            if not isinstance(start, int) or start < 1 or start > len(entries):
                raise ValueError(f"entry_start out of range for {os.path.basename(path)}")
            selected = entries[start - 1:start - 1 + batch_size]
            fixtures.append({
                "id": f"{os.path.splitext(os.path.basename(path))[0]}-entries-{start}-{start + len(selected) - 1}",
                "sha256": _hash_entries(selected), "path": path,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "entry_start": start, "entry_count": len(selected),
                "previous_tail": copy.deepcopy(entries[max(0, start - 3):start - 1]),
            })
    if not fixtures:
        raise ValueError("review specifications selected no entry batches")
    return {"schema_version": 1, "stage": "script_review",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": fixtures, "settings": {"max_retries": 0},
            "quality_thresholds": {"word_ratio_min": 0.95,
                                   "word_ratio_max": 1.05}}


def build_tts_generation_manifest(fixtures, repetitions=1, targets=None,
                                  max_new_tokens=2048):
    """Build self-contained CustomVoice fixtures usable on either machine."""
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one TTS fixture is required")
    normalized = []
    for index, fixture in enumerate(fixtures, 1):
        if not isinstance(fixture, dict):
            raise ValueError("TTS fixtures must be objects")
        selected = {"text": fixture.get("text"),
                    "instruct": fixture.get("instruct", "neutral"),
                    "speaker": fixture.get("speaker", "NARRATOR"),
                    "voice": fixture.get("voice", "Ryan"),
                    "seed": fixture.get("seed", 0)}
        if not isinstance(selected["text"], str) or not selected["text"].strip():
            raise ValueError("TTS fixture text must be non-empty")
        if any(not isinstance(selected[key], str) or not selected[key].strip()
               for key in ("instruct", "speaker", "voice")):
            raise ValueError("TTS fixture instruct, speaker, and voice must be non-empty")
        if not isinstance(selected["seed"], int) or selected["seed"] < 0:
            raise ValueError("TTS fixture seed must be a non-negative integer")
        digest = _hash_entries(selected)
        selected.update({"id": fixture.get("id") or f"tts-{index}",
                         "sha256": digest})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "tts_generation",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized,
            "settings": {"max_new_tokens": max_new_tokens},
            "quality_thresholds": {"min_duration_seconds": 0.1,
                                   "max_silence_ratio": 0.98,
                                   "max_clipping_ratio": 0.01}}
