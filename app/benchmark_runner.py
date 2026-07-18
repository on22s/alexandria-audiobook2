"""Production-backed benchmark stage adapters."""

import hashlib
import os
import time
import json

from openai import OpenAI

from benchmark_core import load_resumable_benchmark_report, save_benchmark_report
from benchmark_fixtures import _hash_entries, get_normalized_source_chunks
from chunk_quality import validate_chunk_quality
from config_settings import load_app_config
from generate_script import LLMGenParams, process_chunk
from lmstudio_settings import get_lmstudio_status, get_remote_lmstudio_status
from utils import is_path_inside
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from review_script import check_text_loss, diff_entries, review_batch


def _load_text_fixture(fixture, uploads_dir):
    path = os.path.abspath(fixture.get("path") or "")
    if not path or not is_path_inside(path, uploads_dir) or not os.path.isfile(path):
        raise ValueError(f"fixture {fixture.get('id')} must be a file inside uploads")
    with open(path, "rb") as fixture_file:
        raw = fixture_file.read()
    digest = hashlib.sha256(raw).hexdigest()
    if fixture.get("chunk_number") is not None:
        if digest != fixture.get("source_sha256"):
            raise ValueError(f"fixture {fixture['id']} source hash changed")
        chunks = get_normalized_source_chunks(raw, fixture.get("chunk_size", 6000))
        chunk_number = fixture["chunk_number"]
        if not isinstance(chunk_number, int) or not 1 <= chunk_number <= len(chunks):
            raise ValueError(f"fixture {fixture['id']} chunk_number is out of range")
        text = chunks[chunk_number - 1]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != fixture["sha256"]:
            raise ValueError(f"fixture {fixture['id']} chunk hash changed")
        return text
    if digest != fixture["sha256"]:
        raise ValueError(f"fixture {fixture['id']} hash changed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"fixture {fixture['id']} is not UTF-8 text") from exc
    if not text.strip():
        raise ValueError(f"fixture {fixture['id']} is empty")
    return text


def _get_llm_benchmark_target(config, target):
    if target == "local":
        llm = config.get("llm_local") or config.get("llm") or {}
        status = get_lmstudio_status(llm.get("model_name"))
    elif target == "thunder":
        llm = config.get("llm_remote") or {}
        status = get_remote_lmstudio_status(
            (config.get("llm_remote_ssh") or "").strip(), llm.get("model_name"))
    else:
        raise ValueError(f"unsupported LLM benchmark target: {target}")
    if not llm.get("base_url") or not llm.get("model_name"):
        raise ValueError(f"{target} LLM endpoint is not configured")
    if not status.get("available") or not status.get("loaded"):
        raise ValueError(f"{target} LM Studio model is not ready")
    return llm, status


def run_script_generation_benchmark(manifest, environment, report_path, state,
                                    config_path, uploads_dir):
    """Run local script-generation cases and persist after every repetition."""
    if manifest["stage"] != "script_generation" or len(manifest["targets"]) != 1:
        raise ValueError("script-generation runs require exactly one target")
    target = manifest["targets"][0]
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, target)
    generation = config.get("generation") or {}
    prompts = config.get("prompts") or {}
    model_name = llm.get("model_name")
    params = LLMGenParams(
        system_prompt=prompts.get("system_prompt"),
        user_prompt_template=prompts.get("user_prompt"),
        max_tokens=generation.get("max_tokens", 4096),
        temperature=generation.get("temperature", 0.6),
        top_p=generation.get("top_p", 0.8), top_k=generation.get("top_k"),
        min_p=generation.get("min_p"),
        presence_penalty=generation.get("presence_penalty", 0.0),
        banned_tokens=generation.get("banned_tokens", []),
        context_length=status.get("context_length"))
    client = OpenAI(base_url=llm.get("base_url"), api_key=llm.get("api_key", "local"))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"])
                 for case in report.get("cases", [])}
    max_retries = manifest.get("settings", {}).get("max_retries", 0)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("script-generation max_retries must be a non-negative integer")
    for fixture_index, fixture in enumerate(manifest["fixtures"]):
        state["current_task_idx"] = fixture_index
        state["tasks"][fixture_index]["status"] = "running"
        text = _load_text_fixture(fixture, uploads_dir)
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            if state.get("cancel"):
                state["status"] = "cancelled"
                state["tasks"][fixture_index]["status"] = "cancelled"
                return report
            attempts = []
            started = time.monotonic()
            entries = process_chunk(
                client, model_name, text, fixture.get("chunk_number", 1),
                fixture.get("total_chunks", 1), params,
                previous_entries=fixture.get("previous_entries") or None,
                max_retries=max_retries,
                attempt_observer=attempts.append)
            quality = validate_chunk_quality(text, entries)
            case = {"fixture_id": fixture["id"], "repetition": repetition,
                    "status": "passed" if entries and quality["passed"] else "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "entry_count": len(entries), "attempts": attempts, "quality": quality}
            report["cases"].append(case)
            save_benchmark_report(report_path, report)
            state["logs"].append(
                f"{fixture['id']} repetition {repetition}: {case['status']}")
        state["tasks"][fixture_index]["status"] = "done"
    state["status"] = "complete"
    return report


def _load_review_fixture(fixture, scripts_dir):
    path = os.path.abspath(fixture.get("path") or "")
    if not is_path_inside(path, scripts_dir) or not os.path.isfile(path):
        raise ValueError(f"fixture {fixture.get('id')} must be a file inside scripts")
    with open(path, "rb") as source_file:
        raw = source_file.read()
    if hashlib.sha256(raw).hexdigest() != fixture.get("source_sha256"):
        raise ValueError(f"fixture {fixture['id']} source hash changed")
    try:
        all_entries = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"fixture {fixture['id']} is not valid UTF-8 JSON") from exc
    start = fixture.get("entry_start")
    count = fixture.get("entry_count")
    if not isinstance(start, int) or not isinstance(count, int) or start < 1 or count < 1:
        raise ValueError(f"fixture {fixture['id']} has invalid entry bounds")
    entries = all_entries[start - 1:start - 1 + count]
    if len(entries) != count or _hash_entries(entries) != fixture.get("sha256"):
        raise ValueError(f"fixture {fixture['id']} entry hash changed")
    return entries


def run_script_review_benchmark(manifest, environment, report_path, state,
                                config_path, scripts_dir):
    """Run production review batches and persist deterministic quality metrics."""
    if manifest["stage"] != "script_review" or len(manifest["targets"]) != 1:
        raise ValueError("script-review runs require exactly one target")
    config = load_app_config(config_path)
    llm, status = _get_llm_benchmark_target(config, manifest["targets"][0])
    generation = config.get("generation") or {}
    prompts = config.get("prompts") or {}
    params = LLMGenParams(
        prompts.get("review_system_prompt") or REVIEW_SYSTEM_PROMPT,
        prompts.get("review_user_prompt") or REVIEW_USER_PROMPT,
        generation.get("max_tokens", 4096), generation.get("temperature", 0.4),
        generation.get("top_p", 0.8), top_k=generation.get("top_k"),
        min_p=generation.get("min_p"),
        presence_penalty=generation.get("presence_penalty", 0.0),
        banned_tokens=generation.get("banned_tokens", []),
        context_length=status.get("context_length"))
    client = OpenAI(base_url=llm["base_url"], api_key=llm.get("api_key", "local"))
    report = load_resumable_benchmark_report(report_path, manifest, environment)
    completed = {(case["fixture_id"], case["repetition"]) for case in report["cases"]}
    max_retries = manifest.get("settings", {}).get("max_retries", 0)
    thresholds = manifest.get("quality_thresholds") or {}
    lower = thresholds.get("word_ratio_min", 0.95)
    upper = thresholds.get("word_ratio_max", 1.05)
    for fixture_index, fixture in enumerate(manifest["fixtures"]):
        state["current_task_idx"] = fixture_index
        state["tasks"][fixture_index]["status"] = "running"
        original = _load_review_fixture(fixture, scripts_dir)
        for repetition in range(1, manifest["repetitions"] + 1):
            if (fixture["id"], repetition) in completed:
                continue
            if state.get("cancel"):
                state["status"] = "cancelled"
                state["tasks"][fixture_index]["status"] = "cancelled"
                return report
            attempts = []
            started = time.monotonic()
            corrected = review_batch(
                client, llm["model_name"], original, 1, 1, params,
                previous_tail=fixture.get("previous_tail") or None,
                max_retries=max_retries, attempt_observer=attempts.append)
            corrected = corrected or []
            text_ok, _, _, ratio = check_text_loss(
                original, corrected, threshold=lower, upper_bound=upper)
            structural_ok = bool(corrected) and all(
                isinstance(entry, dict) and isinstance(entry.get("text"), str)
                and isinstance(entry.get("speaker"), str) for entry in corrected)
            case = {"fixture_id": fixture["id"], "repetition": repetition,
                    "status": "passed" if text_ok and structural_ok else "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "entry_count": len(corrected), "attempts": attempts,
                    "quality": {"passed": text_ok and structural_ok,
                                "word_ratio": round(ratio, 4),
                                "text_loss_passed": text_ok,
                                "structural_passed": structural_ok},
                    "changes": diff_entries(original, corrected)}
            report["cases"].append(case)
            save_benchmark_report(report_path, report)
            state["logs"].append(
                f"{fixture['id']} repetition {repetition}: {case['status']}")
        state["tasks"][fixture_index]["status"] = "done"
    state["status"] = "complete"
    return report
