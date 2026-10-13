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
    if not isinstance(stage, str) or stage not in STAGES:
        raise ValueError(f"unknown benchmark stage: {stage}")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("benchmark targets must be a non-empty list")
    if any(not isinstance(target, str) or target not in {"local", "thunder"}
           for target in targets):
        raise ValueError("benchmark targets may contain only local and thunder")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("benchmark fixtures must be a non-empty list")
    normalized_fixtures = []
    fixture_ids = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not fixture.get("id") or not fixture.get("sha256"):
            raise ValueError("each fixture requires id and sha256")
        fixture_id = fixture["id"]
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            raise ValueError("fixture id must be a non-empty string")
        if fixture_id in fixture_ids:
            raise ValueError(f"duplicate fixture id: {fixture_id}")
        fixture_ids.add(fixture_id)
        normalized_fixtures.append(copy.deepcopy(fixture))
    repetitions = manifest.get("repetitions", 1)
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
        raise ValueError("benchmark repetitions must be a positive integer")
    normalized = copy.deepcopy({key: value for key, value in manifest.items()
                                if key != "fixtures"})
    normalized["targets"] = list(dict.fromkeys(targets))
    normalized["fixtures"] = normalized_fixtures
    normalized["repetitions"] = repetitions
    settings = normalized.setdefault("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("benchmark settings must be an object")
    thresholds = normalized.setdefault("quality_thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("benchmark quality_thresholds must be an object")
    return normalized


def get_manifest_fingerprint(manifest):
    return _stable_hash(validate_benchmark_manifest(manifest))


def get_benchmark_llm_config(config, target):
    """Resolve the named benchmark target independently of the Setup toggle."""
    if target not in {"local", "thunder"}:
        raise ValueError(f"unsupported LLM benchmark target: {target}")
    from lmstudio_settings import get_active_llm_config
    selected = {**config, "llm_mode": "remote" if target == "thunder" else "local"}
    if target == "thunder":
        # Remote benchmarks have always required their explicit profile.
        selected["llm"] = {}
    return get_active_llm_config(selected)


def get_benchmark_execution_environment(manifest, environment, config):
    """Bind stage config to resume identity; publish only its digest."""
    stage = manifest["stage"]
    target = environment["target"]
    identity = {"version": 1, "stage": stage}
    if target == "thunder":
        identity["ssh_alias"] = config.get("llm_remote_ssh")
    if stage in {"script_generation", "script_review", "persona_generation", "nickname_detection"}:
        profile = get_benchmark_llm_config(config, target)
        identity["profile"] = {key: value for key, value in profile.items()
                               if key not in {"concurrency", "concurrency_for", "concurrency_environment"}}
        from llm_provider import resolve_api_key
        identity["resolved_api_key"] = resolve_api_key(profile.get("api_key"))
        # These two runners consume Setup sampling and prompt settings. The
        # persona/nickname runners use their production defaults instead.
        if stage in {"script_generation", "script_review"}:
            generation = config.get("generation") or {}
            identity["generation"] = {key: generation.get(key) for key in (
                "max_tokens", "temperature", "top_p", "top_k", "min_p",
                "presence_penalty", "banned_tokens")}
            prompt_keys = ("system_prompt", "user_prompt") if stage == "script_generation" else (
                "review_system_prompt", "review_user_prompt")
            prompts = config.get("prompts") or {}
            identity["prompts"] = {key: prompts.get(key) for key in prompt_keys}
            if stage == "script_review":
                from config_settings import get_review_labels_only
                if get_review_labels_only(config):
                    identity["prompts"]["review_labels_only"] = True
    if stage in {"tts_generation", "dataset_builder"}:
        identity["tts"] = config.get("tts") or {}
    result = copy.deepcopy(environment)
    result.setdefault("details", {})["execution_sha256"] = _stable_hash(identity)
    result["sha256"] = _stable_hash({"target": target, "details": result["details"]})
    return result


def get_benchmark_preflight_id(manifest, environments):
    normalized = validate_benchmark_manifest(manifest)
    if not isinstance(environments, dict):
        raise ValueError("benchmark environments must be an object")
    for target in normalized["targets"]:
        environment = environments.get(target)
        if not isinstance(environment, dict) or not environment.get("sha256"):
            raise ValueError(f"verified environment fingerprint is required for {target}")
    identities = {target: environment.get("sha256")
                  for target, environment in sorted(environments.items())}
    return _stable_hash({"manifest": _stable_hash(normalized),
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
            "manifest_sha256": _stable_hash(normalized),
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
    manifest_sha256 = get_manifest_fingerprint(manifest)
    if (report.get("manifest_sha256") != manifest_sha256
            or get_manifest_fingerprint(report.get("manifest")) != manifest_sha256):
        raise ValueError("benchmark manifest changed; refusing unsafe resume")
    stored_environment = report.get("environment")
    for evidence in (environment, stored_environment):
        if not isinstance(evidence, dict) or evidence.get("target") not in ("local", "thunder"):
            raise ValueError("benchmark environment is invalid; refusing unsafe resume")
        verified = build_environment_fingerprint(evidence["target"], evidence.get("details"))
        if evidence.get("sha256") != verified["sha256"]:
            raise ValueError("benchmark environment changed; refusing unsafe resume")
    if stored_environment["sha256"] != environment["sha256"]:
        raise ValueError("benchmark environment changed; refusing unsafe resume")
    return report
