"""Deterministic benchmark fixtures derived from existing uploaded books."""

import hashlib
import os
import copy
import json

from generate_script import fix_mojibake, split_into_chunks
from source_normalization import normalize_known_source_corruptions
from lora_evidence import get_file_sha256
from benchmark_validation import (get_lora_training_sample_count, get_lora_training_entries, validate_persona_speakers,
                                  get_benchmark_file_path, get_benchmark_directory_path,
                                  get_benchmark_training_audio_path, get_benchmark_archive_audio_path,
                                  get_adapter_artifact_path)
from dedup_benchmark import get_dedup_selected_entries


def _load_jsonl_entries(raw: bytes, label: str) -> list[dict]:
    try:
        lines = raw.decode("utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} metadata must be valid UTF-8 JSONL") from exc
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"{label} metadata entries must be objects")
    return entries


def get_normalized_source_chunks(raw, chunk_size):
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("benchmark source is not UTF-8 text") from exc
    text = fix_mojibake(text)
    text, _ = normalize_known_source_corruptions(text)
    return split_into_chunks(text, max_size=chunk_size)


def get_script_fixture_source_id(path, root_dir):
    relative = os.path.relpath(os.path.realpath(path), os.path.realpath(root_dir)).replace(os.sep, "/")
    identity = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    return f"{os.path.splitext(os.path.basename(path))[0]}-{identity}"


def build_script_generation_manifest(specs, uploads_dir, repetitions=1,
                                     targets=None, chunk_size=6000):
    """Build hashed chunk references without copying source text."""
    if not isinstance(specs, list) or not specs:
        raise ValueError("at least one source specification is required")
    if not isinstance(chunk_size, int) or chunk_size < 200:
        raise ValueError("chunk_size must be an integer of at least 200")
    fixtures = []
    for spec in specs:
        try:
            path = get_benchmark_file_path(uploads_dir, os.path.abspath(spec.get("path") or ""))
        except ValueError as exc:
            raise ValueError("benchmark source must be a file inside uploads") from exc
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
                "id": f"{get_script_fixture_source_id(path, uploads_dir)}-chunk-{chunk_number}",
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
        try:
            path = get_benchmark_file_path(scripts_dir, os.path.abspath(spec.get("path") or ""))
        except ValueError as exc:
            raise ValueError("review source must be a file inside scripts") from exc
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
                "id": f"{get_script_fixture_source_id(path, scripts_dir)}-entries-{start}-{start + len(selected) - 1}",
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


def build_tts_clone_manifest(fixtures, root_dir, repetitions=1, targets=None,
                             max_new_tokens=2048):
    """Build immutable Base-model clone fixtures from repository audio."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one clone fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        ref_path = get_benchmark_file_path(root_dir, fixture.get("ref_audio") or "")
        relative_ref = os.path.relpath(ref_path, root_dir)
        ref_digest = get_file_sha256(ref_path)
        selected = {"voice_type": "clone", "text": fixture.get("text"),
                    "speaker": fixture.get("speaker", "CLONE"),
                    "seed": fixture.get("seed", 0), "ref_audio": relative_ref,
                    "ref_audio_sha256": ref_digest,
                    "ref_text": fixture.get("ref_text")}
        if any(not isinstance(selected[key], str) or not selected[key].strip()
               for key in ("text", "speaker", "ref_text")):
            raise ValueError("clone text, speaker, and ref_text must be non-empty")
        if not isinstance(selected["seed"], int) or selected["seed"] < 0:
            raise ValueError("clone seed must be a non-negative integer")
        selected.update({"id": fixture.get("id") or f"clone-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "tts_generation",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {"max_new_tokens": max_new_tokens},
            "quality_thresholds": {"min_duration_seconds": 0.1,
                                   "max_silence_ratio": 0.98,
                                   "max_clipping_ratio": 0.01}}


def build_tts_lora_manifest(fixtures, root_dir, repetitions=1, targets=None,
                            max_new_tokens=2048):
    """Build immutable LoRA fixtures including every required adapter artifact."""
    required_files = ("adapter_config.json", "adapter_model.safetensors",
                      "ref_sample.wav", "training_meta.json")
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one LoRA fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        adapter_path = get_benchmark_directory_path(root_dir, fixture.get("adapter_path") or ".")
        artifact_hashes = {}
        for filename in required_files:
            artifact_path = get_adapter_artifact_path(adapter_path, filename)
            artifact_hashes[filename] = get_file_sha256(artifact_path)
        selected = {"voice_type": "lora", "text": fixture.get("text"),
                    "instruct": fixture.get("instruct", "neutral"),
                    "speaker": fixture.get("speaker", "LORA"),
                    "seed": fixture.get("seed", 0),
                    "adapter_path": os.path.relpath(adapter_path, root_dir),
                    "adapter_artifact_sha256": artifact_hashes}
        if any(not isinstance(selected[key], str) or not selected[key].strip()
               for key in ("text", "instruct", "speaker")):
            raise ValueError("LoRA text, instruct, and speaker must be non-empty")
        if not isinstance(selected["seed"], int) or selected["seed"] < 0:
            raise ValueError("LoRA seed must be a non-negative integer")
        selected.update({"id": fixture.get("id") or f"lora-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "tts_generation",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {"max_new_tokens": max_new_tokens},
            "quality_thresholds": {"min_duration_seconds": 0.1,
                                   "max_silence_ratio": 0.98,
                                   "max_clipping_ratio": 0.01}}


def build_tts_design_manifest(fixtures, repetitions=1, targets=None,
                              max_new_tokens=2048):
    """Build deterministic VoiceDesign preview fixtures."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one VoiceDesign fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        selected = {"voice_type": "design", "text": fixture.get("text"),
                    "description": fixture.get("description"),
                    "seed": fixture.get("seed", 0)}
        if any(not isinstance(selected[key], str) or not selected[key].strip()
               for key in ("text", "description")):
            raise ValueError("VoiceDesign text and description must be non-empty")
        if not isinstance(selected["seed"], int) or selected["seed"] < 0:
            raise ValueError("VoiceDesign seed must be a non-negative integer")
        selected.update({"id": fixture.get("id") or f"design-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "tts_generation",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {"max_new_tokens": max_new_tokens},
            "quality_thresholds": {"min_duration_seconds": 0.1,
                                   "max_silence_ratio": 0.98,
                                   "max_clipping_ratio": 0.01}}


def build_lora_training_manifest(fixtures, root_dir, repetitions=1, targets=None):
    """Build immutable calibration fixtures from production training datasets."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one LoRA training fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        dataset_path = get_benchmark_directory_path(root_dir, fixture.get("dataset_path") or ".")
        metadata_path = get_benchmark_file_path(dataset_path, "metadata.jsonl")
        sample_count = get_lora_training_sample_count(fixture.get("sample_count", 8))
        with open(metadata_path, "rb") as metadata_file:
            metadata_raw = metadata_file.read()
        entries = get_lora_training_entries(_load_jsonl_entries(metadata_raw, "LoRA training"), sample_count)
        audio_hashes = {}
        for entry in entries:
            relative_audio = entry.get("audio_filepath") or entry.get("audio")
            audio_path = get_benchmark_training_audio_path(dataset_path, relative_audio)
            audio_hashes[relative_audio] = get_file_sha256(audio_path)
        selected = {"dataset_path": os.path.relpath(dataset_path, root_dir),
                    "metadata_sha256": hashlib.sha256(metadata_raw).hexdigest(),
                    "sample_count": sample_count, "audio_sha256": audio_hashes,
                    "epochs": fixture.get("epochs", 1), "seed": fixture.get("seed", 42),
                    "lr": fixture.get("lr", 1e-6), "lora_r": fixture.get("lora_r", 8),
                    "lora_alpha": fixture.get("lora_alpha", 16),
                    "grad_accum": fixture.get("grad_accum", 1),
                    "language": fixture.get("language", "english")}
        if selected["epochs"] < 1 or selected["lora_r"] < 1 or selected["lora_alpha"] < 1 or selected["grad_accum"] < 1:
            raise ValueError("LoRA training hyperparameters must be positive")
        selected.update({"id": fixture.get("id") or f"lora-training-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "voicelab_training",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}


def build_voicelab_preparer_manifest(fixtures, root_dir, repetitions=1,
                                     targets=None):
    """Build immutable ASR calibration fixtures for the Voice Lab preparer."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one preparer fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        audio_path = get_benchmark_file_path(root_dir, fixture.get("audio_path") or "")
        audio_sha256 = get_file_sha256(audio_path)
        selected = {"audio_path": os.path.relpath(audio_path, root_dir),
                    "audio_sha256": audio_sha256,
                    "limit": fixture.get("limit", 1),
                    "language": fixture.get("language", "en"),
                    "model_revision": "f6b48018ad95afcf85637f433dc0fc4f4672ce34"}
        if not isinstance(selected["limit"], int) or selected["limit"] < 1:
            raise ValueError("preparer limit must be positive")
        if not isinstance(selected["language"], str) or not selected["language"].strip():
            raise ValueError("preparer language must be non-empty")
        selected.update({"id": fixture.get("id") or f"preparer-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "voicelab_preparer",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}


def build_voicelab_dedup_manifest(fixtures, root_dir, repetitions=1,
                                  targets=None):
    """Build immutable two-volume ECAPA dedup calibration fixtures."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one dedup fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        dataset_path = get_benchmark_directory_path(root_dir, fixture.get("dataset_path") or ".")
        metadata_path = get_benchmark_file_path(dataset_path, "metadata.jsonl")
        samples_per_volume = fixture.get("samples_per_volume", 4)
        with open(metadata_path, "rb") as metadata_file:
            metadata_raw = metadata_file.read()
        entries = get_dedup_selected_entries(
            _load_jsonl_entries(metadata_raw, "dedup"), samples_per_volume)
        audio_hashes = {}
        for entry in entries:
            relative_path = entry.get("audio_filepath") or entry.get("audio")
            audio_path = get_benchmark_archive_audio_path(dataset_path, relative_path)
            audio_hashes[relative_path] = get_file_sha256(audio_path)
        selected = {"dataset_path": os.path.relpath(dataset_path, root_dir),
                    "metadata_sha256": hashlib.sha256(metadata_raw).hexdigest(),
                    "samples_per_volume": samples_per_volume,
                    "audio_sha256": audio_hashes,
                    "model_id": "speechbrain/spkrec-ecapa-voxceleb",
                    "seed": fixture.get("seed", 42)}
        selected.update({"id": fixture.get("id") or f"dedup-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "voicelab_dedup",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}


def build_voicelab_profiling_manifest(fixtures, root_dir, repetitions=1,
                                      targets=None):
    """Build immutable acoustic + GGUF Voice Lab profiling fixtures."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one profiling fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        zip_path = get_benchmark_file_path(root_dir, fixture.get("zip_path") or "")
        model_path = get_benchmark_file_path(root_dir, fixture.get("model_path") or "")
        zip_sha256 = get_file_sha256(zip_path)
        model_sha256 = get_file_sha256(model_path)
        selected = {"zip_path": os.path.relpath(zip_path, root_dir),
                    "zip_sha256": zip_sha256,
                    "model_path": os.path.relpath(model_path, root_dir),
                    "model_sha256": model_sha256,
                    "dataset_id": fixture.get("dataset_id", "narrator_benchmark_voice_book_char1_vol01"),
                    "seed": fixture.get("seed", 42)}
        selected.update({"id": fixture.get("id") or f"profiling-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "voicelab_profiling",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}


def build_voicelab_naming_manifest(fixtures, repetitions=1, targets=None):
    """Build self-contained deterministic Voice Lab naming fixtures."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one naming fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        entries = copy.deepcopy(fixture.get("entries"))
        if not isinstance(entries, list) or not entries or any(
                not isinstance(entry, dict) or not entry.get("id")
                or not entry.get("dataset_id") or not entry.get("voice_profile")
                for entry in entries):
            raise ValueError("naming entries require id, dataset_id, and voice_profile")
        selected = {"entries": entries}
        selected.update({"id": fixture.get("id") or f"naming-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "voicelab_naming",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}


def build_persona_generation_manifest(fixtures, repetitions=1, targets=None):
    """Build self-contained advanced-persona discovery fixtures."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one persona fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        entries = copy.deepcopy(fixture.get("entries"))
        if not isinstance(entries, list) or not entries or any(
                not isinstance(entry, dict) or not isinstance(entry.get("speaker"), str)
                or not isinstance(entry.get("text"), str) for entry in entries):
            raise ValueError("persona entries require speaker and text strings")
        speakers = fixture.get("speakers") or list(dict.fromkeys(
            entry["speaker"] for entry in entries
            if entry["speaker"].upper() not in {"NARRATOR", "NARRATION", "NARRATIVE"}))
        validate_persona_speakers(speakers)
        selected = {"entries": entries, "speakers": speakers,
                    "batch_size": fixture.get("batch_size", 40)}
        if not isinstance(selected["batch_size"], int) or selected["batch_size"] < 1:
            raise ValueError("persona batch_size must be positive")
        selected.update({"id": fixture.get("id") or f"persona-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "persona_generation",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {"max_retries": 0}}


def build_nickname_detection_manifest(fixtures, repetitions=1, targets=None):
    """Build self-contained alias-evidence fixtures with expected mappings."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one nickname fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        entries = copy.deepcopy(fixture.get("entries"))
        expected = copy.deepcopy(fixture.get("expected_aliases"))
        if not isinstance(entries, list) or len(entries) < 2 or any(
                not isinstance(entry, dict) or not isinstance(entry.get("speaker"), str)
                or not isinstance(entry.get("text"), str) for entry in entries):
            raise ValueError("nickname entries require speaker and text strings")
        if not isinstance(expected, dict) or not expected or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in expected.items()):
            raise ValueError("expected_aliases must be a non-empty string mapping")
        selected = {"entries": entries, "expected_aliases": expected,
                    "existing_aliases": copy.deepcopy(fixture.get("existing_aliases") or {})}
        selected.update({"id": fixture.get("id") or f"nicknames-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "nickname_detection",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}


def build_export_manifest(stage, fixtures, root_dir, repetitions=1, targets=None):
    """Build immutable Audacity or M4B export fixtures from existing WAVs."""
    if stage not in {"audacity_export", "m4b_export"}:
        raise ValueError("export stage must be audacity_export or m4b_export")
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one export fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        chunks = copy.deepcopy(fixture.get("chunks"))
        if not isinstance(chunks, list) or not chunks:
            raise ValueError("export chunks must be a non-empty list")
        audio_sha256 = {}
        for chunk in chunks:
            relative_path = chunk.get("audio_path") if isinstance(chunk, dict) else None
            path = get_benchmark_file_path(root_dir, relative_path or "")
            audio_sha256[relative_path] = get_file_sha256(path)
        selected = {"chunks": chunks, "audio_sha256": audio_sha256,
                    "per_chunk_chapters": fixture.get("per_chunk_chapters", True)}
        selected.update({"id": fixture.get("id") or f"{stage}-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": stage,
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}


def build_dataset_builder_manifest(fixtures, repetitions=1, targets=None):
    """Build self-contained VoiceDesign batch-orchestration fixtures."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one Dataset Builder fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        samples = copy.deepcopy(fixture.get("samples"))
        if not isinstance(samples, list) or not samples or any(
                not isinstance(sample, dict)
                or not isinstance(sample.get("text"), str)
                or not isinstance(sample.get("emotion", ""), str)
                for sample in samples):
            raise ValueError("Dataset Builder samples require text and emotion strings")
        selected = {"description": fixture.get("description", "Benchmark voice"),
                    "samples": samples,
                    "global_seed": fixture.get("global_seed", 42),
                    "seeds": copy.deepcopy(fixture.get("seeds"))}
        if selected["seeds"] is not None and (not isinstance(selected["seeds"], list)
                or len(selected["seeds"]) != len(samples)
                or any(not isinstance(seed, int) or isinstance(seed, bool)
                       for seed in selected["seeds"])):
            raise ValueError("Dataset Builder seeds must match the sample count")
        selected.update({"id": fixture.get("id") or f"dataset-builder-{index}",
                         "sha256": _hash_entries(selected)})
        normalized.append(selected)
    return {"schema_version": 1, "stage": "dataset_builder",
            "targets": targets or ["local"], "repetitions": repetitions,
            "fixtures": normalized, "settings": {}}
