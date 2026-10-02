"""Versioned integrity contract for LoRA evaluation evidence."""

import hashlib
import json
import os


EVALUATION_EVIDENCE_VERSION = 2


def get_file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_evaluation_spec_sha256(probes, seed: int, thresholds: dict) -> str:
    spec = {"probes": list(probes), "seed": seed, "thresholds": thresholds}
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
                       expected_spec_sha256: str | None = None) -> str | None:
    if result.get("version") != EVALUATION_EVIDENCE_VERSION:
        return "unsupported evaluation evidence version"
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        return "evaluation integrity evidence is missing"
    if (expected_spec_sha256 is not None
            and evidence.get("evaluation_spec_sha256") != expected_spec_sha256):
        return "evaluation specification has changed"
    root = os.path.realpath(checkpoint_dir)
    for filename, expected_hash in (
            ("adapter_model.safetensors", evidence.get("checkpoint_sha256")),
            ("ref_sample.wav", evidence.get("reference_audio_sha256"))):
        path = get_checkpoint_evidence_path(root, filename)
        if not expected_hash or path is None:
            return f"evaluation evidence is missing or invalid {filename}"
        if get_file_sha256(path) != expected_hash:
            return f"evaluation evidence does not match {filename}"
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
        [(probe["id"], probe["text"]) for probe in probes], seed, thresholds)
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
