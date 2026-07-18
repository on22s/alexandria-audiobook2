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


def build_tts_clone_manifest(fixtures, root_dir, repetitions=1, targets=None,
                             max_new_tokens=2048):
    """Build immutable Base-model clone fixtures from repository audio."""
    normalized = []
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("at least one clone fixture is required")
    for index, fixture in enumerate(fixtures, 1):
        ref_path = os.path.abspath(os.path.join(root_dir, fixture.get("ref_audio") or ""))
        if not is_path_inside(ref_path, root_dir) or not os.path.isfile(ref_path):
            raise ValueError("clone reference audio must be a file inside the project")
        relative_ref = os.path.relpath(ref_path, root_dir)
        with open(ref_path, "rb") as ref_file:
            ref_digest = hashlib.sha256(ref_file.read()).hexdigest()
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
        adapter_path = os.path.abspath(os.path.join(
            root_dir, fixture.get("adapter_path") or ""))
        if not is_path_inside(adapter_path, root_dir) or not os.path.isdir(adapter_path):
            raise ValueError("LoRA adapter must be a directory inside the project")
        artifact_hashes = {}
        for filename in required_files:
            artifact_path = os.path.join(adapter_path, filename)
            if not os.path.isfile(artifact_path):
                raise ValueError(f"LoRA adapter is missing {filename}")
            with open(artifact_path, "rb") as artifact_file:
                artifact_hashes[filename] = hashlib.sha256(artifact_file.read()).hexdigest()
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
        dataset_path = os.path.abspath(os.path.join(root_dir, fixture.get("dataset_path") or ""))
        metadata_path = os.path.join(dataset_path, "metadata.jsonl")
        if not is_path_inside(dataset_path, root_dir) or not os.path.isfile(metadata_path):
            raise ValueError("LoRA training dataset must be inside the project")
        sample_count = fixture.get("sample_count", 8)
        if not isinstance(sample_count, int) or sample_count < 1:
            raise ValueError("LoRA training sample_count must be positive")
        with open(metadata_path, "rb") as metadata_file:
            metadata_raw = metadata_file.read()
        entries = [json.loads(line) for line in metadata_raw.decode("utf-8").splitlines()
                   if line.strip()][:sample_count]
        if len(entries) < sample_count:
            raise ValueError("LoRA training dataset has too few samples")
        audio_hashes = {}
        for entry in entries:
            relative_audio = entry.get("audio_filepath") or entry.get("audio")
            audio_path = os.path.abspath(os.path.join(dataset_path, relative_audio or ""))
            if not relative_audio or not is_path_inside(audio_path, dataset_path) or not os.path.isfile(audio_path):
                raise ValueError("LoRA training sample audio is missing or outside the dataset")
            with open(audio_path, "rb") as audio_file:
                audio_hashes[relative_audio] = hashlib.sha256(audio_file.read()).hexdigest()
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
        audio_path = os.path.abspath(os.path.join(root_dir, fixture.get("audio_path") or ""))
        if not is_path_inside(audio_path, root_dir) or not os.path.isfile(audio_path):
            raise ValueError("preparer audio must be a file inside the project")
        with open(audio_path, "rb") as audio_file:
            audio_sha256 = hashlib.sha256(audio_file.read()).hexdigest()
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
        dataset_path = os.path.abspath(os.path.join(root_dir, fixture.get("dataset_path") or ""))
        metadata_path = os.path.join(dataset_path, "metadata.jsonl")
        if not is_path_inside(dataset_path, root_dir) or not os.path.isfile(metadata_path):
            raise ValueError("dedup source dataset must be inside the project")
        samples_per_volume = fixture.get("samples_per_volume", 4)
        if not isinstance(samples_per_volume, int) or samples_per_volume < 1:
            raise ValueError("dedup samples_per_volume must be positive")
        with open(metadata_path, "rb") as metadata_file:
            metadata_raw = metadata_file.read()
        entries = [json.loads(line) for line in metadata_raw.decode("utf-8").splitlines()
                   if line.strip()][:samples_per_volume * 2]
        if len(entries) < samples_per_volume * 2:
            raise ValueError("dedup source dataset has too few samples")
        audio_hashes = {}
        for entry in entries:
            relative_path = entry.get("audio_filepath") or entry.get("audio")
            audio_path = os.path.abspath(os.path.join(dataset_path, relative_path or ""))
            if not relative_path or not is_path_inside(audio_path, dataset_path) or not os.path.isfile(audio_path):
                raise ValueError("dedup sample audio is missing or outside the dataset")
            with open(audio_path, "rb") as audio_file:
                audio_hashes[relative_path] = hashlib.sha256(audio_file.read()).hexdigest()
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
        zip_path = os.path.abspath(os.path.join(root_dir, fixture.get("zip_path") or ""))
        model_path = os.path.abspath(os.path.join(root_dir, fixture.get("model_path") or ""))
        if not is_path_inside(zip_path, root_dir) or not os.path.isfile(zip_path):
            raise ValueError("profiling zip must be a file inside the project")
        if not is_path_inside(model_path, root_dir) or not os.path.isfile(model_path):
            raise ValueError("profiling model must be a file inside the project")
        with open(zip_path, "rb") as source_file:
            zip_sha256 = hashlib.sha256(source_file.read()).hexdigest()
        with open(model_path, "rb") as model_file:
            model_sha256 = hashlib.sha256(model_file.read()).hexdigest()
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
