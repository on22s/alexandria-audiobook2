"""Shared read-only validation for benchmark execution inputs."""

import os
from utils import is_path_inside
from lora_evidence import get_file_sha256


def get_benchmark_file_path(root, name):
    """Resolve an existing fixture file without allowing it outside its root."""
    if not isinstance(name, str) or not name:
        raise ValueError("benchmark file is outside the project or missing")
    path = os.path.realpath(os.path.join(root, name))
    if not is_path_inside(path, root) or not os.path.isfile(path):
        raise ValueError("benchmark file is outside the project or missing")
    return path


def get_benchmark_directory_path(root, name):
    """Resolve an existing fixture directory within its invocation root."""
    if not isinstance(name, str) or not name:
        raise ValueError("benchmark directory is outside the project or missing")
    path = os.path.realpath(os.path.join(root, name))
    if not is_path_inside(path, root) or not os.path.isdir(path):
        raise ValueError("benchmark directory is outside the project or missing")
    return path


def get_benchmark_verified_file_path(root, name, expected_sha256, description):
    """Resolve a contained fixture file and verify its declared byte identity."""
    path = get_benchmark_file_path(root, name)
    if get_file_sha256(path) != expected_sha256:
        raise ValueError(f"{description} hash changed")
    return path


def get_benchmark_training_audio_path(root, relative):
    """Apply the existing training member-name policy and shared containment."""
    if (not isinstance(relative, str) or not relative or os.path.isabs(relative)
            or os.path.normpath(relative).startswith('..')):
        raise ValueError(f"unsafe training audio path: {relative!r}")
    try:
        return get_benchmark_file_path(root, relative)
    except ValueError as exc:
        raise ValueError(f"unsafe training audio path: {relative!r}") from exc


def get_benchmark_archive_audio_path(root, name):
    """Keep portable dedup ZIP member names unchanged and contained."""
    if (not isinstance(name, str) or not name
            or any(char in name for char in ("\\", ":", "\x00"))
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError(f"unsafe dedup audio member name: {name!r}")
    return get_benchmark_file_path(root, name)


def get_benchmark_artifact_name(name, description="benchmark artifact"):
    """Require one literal path component using the existing TTS name policy."""
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(char in name for char in ("/", "\\", ":", "\x00"))):
        raise ValueError(f"{description} must be a literal filename or directory name")
    return name


def get_benchmark_output_path(root, name):
    """Resolve a named output without following an existing destination symlink."""
    name = get_benchmark_artifact_name(name)
    root = os.path.realpath(root)
    path = os.path.join(root, name)
    if os.path.islink(path) or not is_path_inside(path, root):
        raise ValueError("benchmark output path is a symlink or outside the output root")
    return path


def get_adapter_artifact_path(adapter_path, filename):
    """Resolve a fixture artifact without escaping its local or staged adapter."""
    if (not isinstance(filename, str) or not filename or
            os.path.isabs(filename) or "\\" in filename or
            ".." in filename.split("/")):
        raise ValueError("LoRA adapter artifact path is outside the adapter or invalid")
    artifact_path = os.path.realpath(os.path.join(adapter_path, filename))
    if not is_path_inside(artifact_path, adapter_path) or not os.path.isfile(artifact_path):
        raise ValueError(f"LoRA adapter artifact is outside the adapter or missing: {filename}")
    return artifact_path


def get_lora_training_sample_count(value):
    """Return the existing positive-integer training calibration count."""
    if not isinstance(value, int) or value < 1:
        raise ValueError("LoRA training sample_count must be positive")
    return value


def get_lora_training_entries(entries, sample_count):
    """Select the complete admitted workload before staging or launching training."""
    count = get_lora_training_sample_count(sample_count)
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("LoRA training metadata entries must be objects")
    if len(entries) < count:
        raise ValueError("LoRA training dataset has too few samples")
    return entries[:count]


def validate_persona_speakers(speakers):
    """Require at least one speaker before persona discovery or scoring."""
    if (not isinstance(speakers, list) or not speakers
            or any(not isinstance(speaker, str) or not speaker.strip()
                   for speaker in speakers)):
        raise ValueError("persona speakers must be non-empty strings")
