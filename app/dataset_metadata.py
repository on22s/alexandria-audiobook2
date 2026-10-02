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
