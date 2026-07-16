#!/usr/bin/env python3
"""Execute a planned real-book corpus against the configured LM Studio model."""

import argparse
import json
import os
import time
from collections import defaultdict

from openai import OpenAI

from chunk_quality import validate_chunk_quality
from config_settings import load_app_config
from core import CONFIG_PATH
from generate_script import LLMGenParams, process_chunk
from lmstudio_settings import ensure_ideal_settings
from utils import atomic_json_write


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, (len(ordered) * percentile + 99) // 100 - 1)
    return round(ordered[index], 3)


def summarize_cases(cases):
    """Return aggregate latency, retry, token, quality, and category results."""
    latencies = [case["elapsed_seconds"] for case in cases]
    attempts = [attempt for case in cases for attempt in case.get("attempts", [])]
    categories = defaultdict(lambda: {"total": 0, "passed": 0})
    for case in cases:
        bucket = categories[case["category"]]
        bucket["total"] += 1
        bucket["passed"] += case["status"] == "passed"
    return {
        "total": len(cases),
        "passed": sum(case["status"] == "passed" for case in cases),
        "failed": sum(case["status"] != "passed" for case in cases),
        "retry_cases": sum(len(case.get("attempts", [])) > 1 for case in cases),
        "attempts": len(attempts),
        "prompt_tokens": sum(attempt.get("prompt_tokens") or 0 for attempt in attempts),
        "completion_tokens": sum(attempt.get("completion_tokens") or 0 for attempt in attempts),
        "latency_seconds": {"total": round(sum(latencies), 3),
                            "p50": _percentile(latencies, 50),
                            "p95": _percentile(latencies, 95)},
        "by_category": dict(sorted(categories.items())),
    }


def run_manifest(manifest, output_path, limit=None):
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    config = load_app_config(CONFIG_PATH)
    llm = config.get("llm", {})
    generation = config.get("generation") or {}
    prompts = config.get("prompts") or {}
    base_url = llm.get("base_url", "http://localhost:1234/v1")
    model = llm.get("model_name", "local-model")
    _, status, heal_message = ensure_ideal_settings(
        config.get("llm_mode", "local"), base_url, model,
        ssh_alias=config.get("llm_remote_ssh"))
    client = OpenAI(base_url=base_url, api_key=llm.get("api_key", "local"))
    params = LLMGenParams(
        system_prompt=prompts.get("system_prompt"),
        user_prompt_template=prompts.get("user_prompt"),
        max_tokens=generation.get("max_tokens", 4096),
        temperature=generation.get("temperature", 0.6),
        top_p=generation.get("top_p", 0.8),
        top_k=generation.get("top_k"),
        min_p=generation.get("min_p"),
        presence_penalty=generation.get("presence_penalty", 0.0),
        banned_tokens=generation.get("banned_tokens", []),
        context_length=status.get("context_length"),
    )
    cases = [(book, passage) for book in manifest.get("books", [])
             for passage in book.get("passages", [])]
    if limit is not None:
        cases = cases[:limit]
    report = {"schema_version": 1, "model": model, "base_url": base_url,
              "lmstudio_status": status, "settings_message": heal_message, "cases": []}
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    for index, (book, passage) in enumerate(cases, 1):
        attempts = []
        started = time.monotonic()
        entries = process_chunk(
            client, model, passage["text"], index, len(cases), params,
            max_retries=2, attempt_observer=attempts.append)
        quality = validate_chunk_quality(passage["text"], entries)
        report["cases"].append({
            "book": book["name"], "category": passage["category"],
            "passage_sha256": passage["sha256"],
            "status": "passed" if entries and quality["passed"] else "failed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "attempts": attempts, "quality": quality,
            "entry_count": len(entries),
        })
        report["summary"] = summarize_cases(report["cases"])
        atomic_json_write(report, output_path)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    with open(args.manifest, encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    report = run_manifest(manifest, args.output, limit=args.limit)
    summary = report["summary"]
    print(f"Completed {summary['total']} case(s): {summary['passed']} passed, "
          f"{summary['retry_cases']} retried")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
