"""Versioned integrity contract for LoRA evaluation evidence."""

import hashlib
import json
import os
from synthesis_settings import get_enabled_synthesis_window_settings

EVALUATION_EVIDENCE_VERSION = 3
GENERATION_FILES = ('adapter_config.json', 'adapter_model.safetensors',
                    'ref_sample.wav', 'training_meta.json')


def get_tts_model_settings(config):
    """Read model inputs without constructing an engine or importing its dependencies."""
    tts = config.get("tts", {})
    settings = {"mode": tts.get("mode", "external"),
            "device": tts.get("device", "auto"),
            "compile_codec": tts.get("compile_codec", False),
            "max_new_tokens": tts.get("max_new_tokens", 2048),
            "language": tts.get("language", "English")}
    windows = get_enabled_synthesis_window_settings(tts.get("synthesis_windows"))
    if windows is not None:
        settings["synthesis_windows"] = windows
    return settings


def get_file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_adapter_generation_sha256_from_hashes(file_hashes):
    """Keep one content identity for the exact four serving files."""
    rows = [(name, file_hashes[name]) for name in GENERATION_FILES]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def get_adapter_generation_sha256(adapter_dir):
    """Identify the exact served config, weights, reference audio and metadata."""
    return get_adapter_generation_sha256_from_hashes({
        name: get_file_sha256(os.path.join(adapter_dir, name)) for name in GENERATION_FILES})


def get_adapter_generation_file_identity(adapter_dir):
    """Bound cached generation hashes to every native serving-file version."""
    identities = []
    for name in GENERATION_FILES:
        stat = os.stat(os.path.join(adapter_dir, name))
        identities.append((name, stat.st_dev, stat.st_ino, stat.st_size,
                           stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(identities)


def get_evaluation_spec_sha256(probes, seed: int, thresholds: dict,
                               synthesis_settings=None) -> str:
    settings = get_tts_model_settings({}) if synthesis_settings is None else synthesis_settings
    spec = {"probes": list(probes), "seed": seed, "thresholds": thresholds,
            "synthesis_settings": settings}
    encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_checkpoint_evidence_path(root, filename):
    """Return a regular evidence file contained in the resolved checkpoint."""
    if not isinstance(filename, str) or not filename:
        return None
    path = os.path.realpath(os.path.join(root, filename))
    try:
        if os.path.commonpath((root, path)) != root:
            return None
    except ValueError:
        return None
    return path if os.path.isfile(path) else None


def get_evidence_error(result: dict, checkpoint_dir: str,
                       expected_spec_sha256: str | None = None,
                       expected_synthesis_settings: dict | None = None) -> str | None:
    if result.get("version") != EVALUATION_EVIDENCE_VERSION:
        return "unsupported evaluation evidence version"
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        return "evaluation integrity evidence is missing"
    settings = result.get("synthesis_settings")
    if (not isinstance(settings, dict)
            or settings != get_tts_model_settings({"tts": settings})):
        return "evaluation synthesis settings are missing or invalid"
    if expected_synthesis_settings is not None and settings != expected_synthesis_settings:
        return "evaluation synthesis settings have changed"
    if (expected_spec_sha256 is not None
            and evidence.get("evaluation_spec_sha256") != expected_spec_sha256):
        return "evaluation specification has changed"
    root = os.path.realpath(checkpoint_dir)
    file_hashes = {}
    for filename in GENERATION_FILES:
        path = get_checkpoint_evidence_path(root, filename)
        if path is None:
            return f"evaluation evidence is missing or invalid {filename}"
        file_hashes[filename] = get_file_sha256(path)
    for filename, expected_hash in (
            ("adapter_model.safetensors", evidence.get("checkpoint_sha256")),
            ("ref_sample.wav", evidence.get("reference_audio_sha256"))):
        if not expected_hash or file_hashes[filename] != expected_hash:
            return f"evaluation evidence does not match {filename}"
    if evidence.get("adapter_generation_sha256") != get_adapter_generation_sha256_from_hashes(file_hashes):
        return "evaluation evidence does not match the serving generation"
    probes = result.get("probes")
    if not isinstance(probes, list) or not probes:
        return "evaluation probes are missing"
    thresholds = result.get("thresholds")
    if not isinstance(thresholds, dict):
        return "evaluation specification thresholds are missing"
    if any(not isinstance(probe, dict) for probe in probes):
        return "evaluation specification probes are invalid"
    seed = probes[0].get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        return "evaluation specification seed is invalid"
    for index, probe in enumerate(probes):
        probe_seed = probe.get("seed")
        if (not isinstance(probe.get("id"), str) or not probe["id"]
                or not isinstance(probe.get("text"), str) or not probe["text"]
                or isinstance(probe_seed, bool) or not isinstance(probe_seed, int)
                or probe_seed != seed + index):
            return "evaluation specification probes are invalid"
    observed_spec = get_evaluation_spec_sha256(
        [(probe["id"], probe["text"]) for probe in probes], seed, thresholds, settings)
    if evidence.get("evaluation_spec_sha256") != observed_spec:
        return "evaluation specification does not match recorded evidence"
    for probe in probes:
        filename = probe.get("audio_file", "")
        expected_hash = probe.get("audio_sha256")
        path = get_checkpoint_evidence_path(root, filename)
        if not expected_hash or path is None:
            return f"evaluation probe audio is invalid: {probe.get('id', 'unknown')}"
        if get_file_sha256(path) != expected_hash:
            return f"evaluation probe audio has changed: {probe.get('id', 'unknown')}"
    return None
