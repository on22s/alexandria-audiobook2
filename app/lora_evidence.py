"""Versioned integrity contract for LoRA evaluation evidence."""

import hashlib
import json
import os
import re
from synthesis_settings import get_enabled_synthesis_window_settings

EVALUATION_EVIDENCE_VERSION = 4
GENERATION_FILES = ('adapter_config.json', 'adapter_model.safetensors',
                    'ref_sample.wav', 'training_meta.json')
SERVING_IDENTITY_POLICY_VERSION = 1
_SERVING_IDENTITY_DTYPES = frozenset({"bfloat16", "float32"})
_SERVING_IDENTITY_MERGE_MODES = frozenset({"merged", "unmerged"})


# The one source for model-setting defaults: config_settings.TTSConfig takes its
# field defaults from here, so Setup and the engine agree when config.json omits
# them (#1671). Kept here because this module must import without site packages.
TTS_MODEL_DEFAULTS = {"mode": "local", "device": "auto", "compile_codec": False,
                      "max_new_tokens": 2048, "language": "English"}


def get_tts_model_settings(config):
    """Read model inputs without constructing an engine or importing its dependencies."""
    tts = config.get("tts", {})
    settings = {name: tts.get(name, default) for name, default in TTS_MODEL_DEFAULTS.items()}
    windows = get_enabled_synthesis_window_settings(tts.get("synthesis_windows"))
    if windows is not None:
        settings["synthesis_windows"] = windows
    return settings


def validate_serving_identity(identity):
    """Return a validated copy of a serving-identity record, or None.

    Metadata-only: no Torch or model import. Unknown/incomplete records are
    never treated as matching anything, including themselves.
    """
    if not isinstance(identity, dict):
        return None
    if set(identity) != {"device", "dtype", "merge_mode", "policy_version"}:
        return None
    device, merge_mode, policy_version = (
        identity["device"], identity["merge_mode"], identity["policy_version"])
    if not isinstance(device, str) or re.fullmatch(r"cpu|mps|cuda(?::[0-9]+)?", device) is None:
        return None
    if not isinstance(identity["dtype"], str):
        return None
    dtype = identity["dtype"].removeprefix("torch.")
    if (dtype not in _SERVING_IDENTITY_DTYPES or not isinstance(merge_mode, str)
            or merge_mode not in _SERVING_IDENTITY_MERGE_MODES):
        return None
    if type(policy_version) is not int or policy_version != SERVING_IDENTITY_POLICY_VERSION:
        return None
    if (dtype == "bfloat16") != (merge_mode == "unmerged"):
        return None
    return {"device": device, "dtype": dtype, "merge_mode": merge_mode,
            "policy_version": SERVING_IDENTITY_POLICY_VERSION}


def get_engine_lora_serving_identity(engine, expected_synthesis_settings=None) -> dict | None:
    """Read an observed identity without loading or querying a runtime."""
    getter = getattr(engine, "get_lora_serving_identity", None)
    if not callable(getter):
        return None
    try:
        if (expected_synthesis_settings is not None
                and engine.get_model_settings() != expected_synthesis_settings):
            return None
        return validate_serving_identity(getter())
    except Exception:
        return None


def get_file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_evaluation_serving_identity_error(result, expected_identity):
    evidence = result.get("evidence")
    recorded = validate_serving_identity(evidence.get("serving_identity")) if isinstance(evidence, dict) else None
    expected = validate_serving_identity(expected_identity)
    if recorded is None or expected is None:
        return "evaluation serving identity is missing or unknown"
    if recorded != expected:
        return "evaluation serving identity has changed"
    return None


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
                               synthesis_settings=None, serving_identity=None) -> str:
    settings = get_tts_model_settings({}) if synthesis_settings is None else synthesis_settings
    spec = {"probes": list(probes), "seed": seed, "thresholds": thresholds,
            "synthesis_settings": settings}
    if serving_identity is not None:
        validated = validate_serving_identity(serving_identity)
        if validated is None:
            raise ValueError("evaluation serving identity is invalid")
        spec["serving_identity"] = validated
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
                       expected_synthesis_settings: dict | None = None,
                       expected_serving_identity=None,
                       require_current_serving: bool = False) -> str | None:
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
    recorded_identity = evidence.get("serving_identity") if "serving_identity" in evidence else None
    recorded_valid = validate_serving_identity(recorded_identity) if recorded_identity is not None else None
    if "serving_identity" in evidence and recorded_valid is None:
        return "evaluation serving identity is invalid"
    if require_current_serving or expected_serving_identity is not None:
        identity_error = get_evaluation_serving_identity_error(result, expected_serving_identity)
        if identity_error:
            return identity_error
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
        [(probe["id"], probe["text"]) for probe in probes], seed, thresholds, settings,
        serving_identity=recorded_valid)
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
