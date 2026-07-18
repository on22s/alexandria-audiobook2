"""Shared contracts for reproducible local-versus-remote benchmarks."""

import copy
import hashlib
import json
import os

from utils import atomic_json_write, safe_load_json


MANIFEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1

STAGES = {
    "script_generation": {"gpu": True, "inputs": ["source_text"], "outputs": ["script"]},
    "script_review": {"gpu": True, "inputs": ["script"], "outputs": ["reviewed_script"]},
    "persona_generation": {"gpu": True, "inputs": ["script"], "outputs": ["personas"]},
    "nickname_detection": {"gpu": True, "inputs": ["script"], "outputs": ["aliases"]},
    "tts_generation": {"gpu": True, "inputs": ["script", "voice_config"], "outputs": ["audio"]},
    "voicelab_preparer": {"gpu": True, "inputs": ["audiobook"], "outputs": ["dataset"]},
    "voicelab_dedup": {"gpu": True, "inputs": ["dataset"], "outputs": ["deduplicated_dataset"]},
    "voicelab_training": {"gpu": True, "inputs": ["dataset"], "outputs": ["adapter"]},
    "voicelab_profiling": {"gpu": True, "inputs": ["adapter"], "outputs": ["profile"]},
    "voicelab_naming": {"gpu": False, "inputs": ["profile"], "outputs": ["named_adapter"]},
    "dataset_builder": {"gpu": True, "inputs": ["source_audio"], "outputs": ["dataset"]},
    "audacity_export": {"gpu": False, "inputs": ["audio"], "outputs": ["audacity_project"]},
    "m4b_export": {"gpu": False, "inputs": ["audio"], "outputs": ["m4b"]},
}


def get_stage_registry():
    """Return a copy so callers cannot mutate the canonical registry."""
    return copy.deepcopy(STAGES)


def _stable_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_benchmark_manifest(manifest):
    """Return a normalized copy of a supported benchmark manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("benchmark manifest must be an object")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported benchmark manifest schema_version; expected {MANIFEST_SCHEMA_VERSION}")
    stage = manifest.get("stage")
    if stage not in STAGES:
        raise ValueError(f"unknown benchmark stage: {stage}")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("benchmark targets must be a non-empty list")
    if any(target not in {"local", "thunder"} for target in targets):
        raise ValueError("benchmark targets may contain only local and thunder")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("benchmark fixtures must be a non-empty list")
    normalized_fixtures = []
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not fixture.get("id") or not fixture.get("sha256"):
            raise ValueError("each fixture requires id and sha256")
        normalized_fixtures.append(copy.deepcopy(fixture))
    repetitions = manifest.get("repetitions", 1)
    if not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("benchmark repetitions must be a positive integer")
    normalized = copy.deepcopy(manifest)
    normalized["targets"] = list(dict.fromkeys(targets))
    normalized["fixtures"] = normalized_fixtures
    normalized["repetitions"] = repetitions
    normalized.setdefault("settings", {})
    normalized.setdefault("quality_thresholds", {})
    return normalized


def get_manifest_fingerprint(manifest):
    return _stable_hash(validate_benchmark_manifest(manifest))


def get_benchmark_preflight_id(manifest, environments):
    identities = {target: environment.get("sha256")
                  for target, environment in sorted(environments.items())}
    return _stable_hash({"manifest": get_manifest_fingerprint(manifest),
                         "environments": identities})


def build_environment_fingerprint(target, observations):
    """Build a comparable fingerprint from adapter-verified observations."""
    if target not in {"local", "thunder"}:
        raise ValueError("environment target must be local or thunder")
    if not isinstance(observations, dict):
        raise ValueError("environment observations must be an object")
    required = ("hostname", "gpu_name", "backend", "python_version", "git_commit")
    missing = [field for field in required if not observations.get(field)]
    if missing:
        raise ValueError(f"environment observations missing: {', '.join(missing)}")
    details = copy.deepcopy(observations)
    return {"target": target, "details": details,
            "sha256": _stable_hash({"target": target, "details": details})}


def build_benchmark_report(manifest, environment):
    normalized = validate_benchmark_manifest(manifest)
    if not isinstance(environment, dict) or not environment.get("sha256"):
        raise ValueError("verified environment fingerprint is required")
    return {"schema_version": RESULT_SCHEMA_VERSION,
            "manifest": normalized,
            "manifest_sha256": get_manifest_fingerprint(normalized),
            "environment": copy.deepcopy(environment), "cases": []}


def save_benchmark_report(path, report):
    atomic_json_write(report, path)


def load_resumable_benchmark_report(path, manifest, environment):
    """Load only a report from the exact same manifest and environment."""
    if not os.path.exists(path):
        return build_benchmark_report(manifest, environment)
    report = safe_load_json(path, default=None)
    if not isinstance(report, dict):
        raise ValueError("benchmark report is unreadable")
    if report.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ValueError("benchmark report schema does not match")
    if report.get("manifest_sha256") != get_manifest_fingerprint(manifest):
        raise ValueError("benchmark manifest changed; refusing unsafe resume")
    if report.get("environment", {}).get("sha256") != environment.get("sha256"):
        raise ValueError("benchmark environment changed; refusing unsafe resume")
    return report
