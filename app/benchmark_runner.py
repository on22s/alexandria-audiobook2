"""Production-backed benchmark stage adapters."""

import hashlib
import os
import time

from openai import OpenAI

from benchmark_core import load_resumable_benchmark_report, save_benchmark_report
from benchmark_fixtures import get_normalized_source_chunks
from chunk_quality import validate_chunk_quality
from config_settings import load_app_config
from generate_script import LLMGenParams, process_chunk
from lmstudio_settings import get_lmstudio_status
from utils import is_path_inside


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


def run_script_generation_benchmark(manifest, environment, report_path, state,
                                    config_path, uploads_dir):
    """Run local script-generation cases and persist after every repetition."""
    if manifest["stage"] != "script_generation" or manifest["targets"] != ["local"]:
        raise ValueError("initial script-generation adapter supports local-only manifests")
    config = load_app_config(config_path)
    llm = config.get("llm_local") or config.get("llm") or {}
    generation = config.get("generation") or {}
    prompts = config.get("prompts") or {}
    model_name = llm.get("model_name")
    status = get_lmstudio_status(model_name)
    if not status.get("available") or not status.get("loaded"):
        raise ValueError("local LM Studio model is not ready")
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
