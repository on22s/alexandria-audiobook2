"""Shared shape validation for uploaded and standalone training metadata."""
import json
import os


def get_dataset_metadata(path):
    """Read JSONL entries, refusing malformed rows with line-specific errors."""
    entries = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("row must be an object")
                    audio = entry.get("audio_filepath") or entry.get("audio", "")
                    if not isinstance(audio, str) or not audio.strip():
                        raise ValueError("audio filepath must be a nonempty string")
                    if not isinstance(entry.get("text"), str):
                        raise ValueError("text must be a string")
                    if entry.get("ref_audio") is not None and not isinstance(entry["ref_audio"], str):
                        raise ValueError("ref_audio must be a string when supplied")
                except ValueError as error:
                    raise ValueError(f"metadata.jsonl line {line_number}: {error}") from error
                entries.append(entry)
    except UnicodeDecodeError as error:
        raise ValueError("metadata.jsonl must be valid UTF-8") from error
    if not entries:
        raise ValueError("metadata.jsonl contains no training entries")
    return entries


def get_training_metadata(data_dir):
    """Read the training split when present; otherwise read root metadata."""
    split_path = os.path.join(data_dir, "train", "metadata.jsonl")
    used_split = os.path.exists(split_path)
    path = split_path if used_split else os.path.join(data_dir, "metadata.jsonl")
    return get_dataset_metadata(path), used_split


def require_dataset_wav(path):
    """Refuse non-WAV, empty, corrupt or nonfinite training audio."""
    if os.path.splitext(path)[1].lower() != ".wav":
        raise ValueError("training audio must be a WAV file")
    import numpy as np
    import soundfile as sf
    try:
        with sf.SoundFile(path) as audio:
            if audio.format not in {"WAV", "WAVEX", "RF64"} or audio.frames < 1:
                raise ValueError("training WAV must contain audio frames")
            frames = 0
            for block in audio.blocks(blocksize=4096, dtype="float32", always_2d=True):
                if not np.isfinite(block).all():
                    raise ValueError("training WAV contains nonfinite samples")
                frames += len(block)
            if frames != audio.frames:
                raise ValueError("training WAV could not be decoded completely")
    except (OSError, RuntimeError) as error:
        raise ValueError("training WAV is not decodable") from error


def get_training_reference_path(data_dir, entries):
    """Resolve the existing reference precedence without leaving the dataset."""
    from utils import is_path_inside
    relative = entries[0].get("ref_audio")
    if not relative:
        relative = "ref.wav" if os.path.exists(os.path.join(data_dir, "ref.wav")) else (
            entries[0].get("audio_filepath") or entries[0].get("audio", ""))
    path = os.path.realpath(os.path.join(data_dir, relative))
    if not is_path_inside(path, data_dir):
        raise ValueError(f"Reference audio escapes the dataset directory: {relative}")
    if not os.path.isfile(path):
        raise ValueError(f"Reference audio not found: {relative}")
    return path


def get_training_dataset_preflight(data_dir):
    """Validate metadata and native audio without allocating a model."""
    from audio_validation import validate_generated_audio, validate_finite_audio_values
    from utils import is_path_inside
    import soundfile as sf
    entries, used_split = get_training_metadata(data_dir)
    reference = get_training_reference_path(data_dir, entries)
    paths = {reference}
    for entry in entries:
        relative = entry.get("audio_filepath") or entry.get("audio")
        path = os.path.realpath(os.path.join(data_dir, relative))
        if not is_path_inside(path, data_dir):
            raise ValueError(f"Training audio escapes the dataset directory: {relative}")
        paths.add(path)
    for path in sorted(paths):
        validate_generated_audio(path, "training preflight")
        with sf.SoundFile(path) as audio:
            for block in audio.blocks(blocksize=65536, dtype="float32"):
                validate_finite_audio_values(block, "training preflight")
    return {"sample_count": len(entries), "used_split": used_split,
            "audio_count": len(paths), "reference": os.path.relpath(reference, data_dir)}


def get_training_reference_preview(text):
    import sys
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text[:60].encode(encoding, errors="backslashreplace").decode(encoding)


def get_training_reference_text(data_dir, ref_audio_path, samples):
    ref_text_file = os.path.join(data_dir, "ref_text.txt")
    ref_sample_text = ""
    if os.path.exists(ref_text_file):
        with open(ref_text_file, "r", encoding="utf-8") as f:
            ref_sample_text = f.read().strip()
        if ref_sample_text:
            print(f"[DATA] Using ref text from ref_text.txt: '{get_training_reference_preview(ref_sample_text)}...'", flush=True)
    if not ref_sample_text:
        from lora_evidence import get_file_sha256
        reference_hash = get_file_sha256(ref_audio_path)
        matched_sample = next((sample for sample in samples
                               if sample.get("audio_path")
                               and os.path.isfile(sample["audio_path"])
                               and get_file_sha256(sample["audio_path"]) == reference_hash), None)
        if matched_sample is not None:
            ref_sample_text = matched_sample["text"]
            print(f"[DATA] Using matching sample text as ref text: '{get_training_reference_preview(ref_sample_text)}...'", flush=True)
        else:
            # Legacy datasets: ref.wav is typically the first sample
            ref_sample_text = samples[0]["text"]
            print(f"[DATA] Using first sample text as ref text: '{get_training_reference_preview(ref_sample_text)}...'", flush=True)

    return ref_sample_text
