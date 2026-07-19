"""Run LLM-based benchmark stages entirely on the host where they execute.

Companion to tts_benchmark.py, same CLI shape (--payload base64 JSON,
prints a RESULT= marker line) - but for script_generation/script_review/
persona_generation/nickname_detection instead of TTS. Reuses
benchmark_runner.py's per-case functions directly (no duplicated scoring
logic) so a case computed here and one computed in-process locally produce
identical report entries.

The whole point of running this ON the remote host (over SSH, via
benchmark_runner._run_llm_worker) rather than calling the remote LM Studio
endpoint from the local orchestrator is to keep the network out of the
timed path: payload["base_url"] must be the box's own localhost endpoint,
not the public forwarding URL - see
benchmark_runner._remote_llm_base_url.

Case-level exceptions are intentionally NOT caught here (unlike
tts_benchmark.py's execute_payload) - the local in-process loop this
mirrors doesn't catch them either, and a case failing hard should look the
same (a crashed run, not a silently "failed" case) whether it happened
locally or here.
"""

import argparse
import base64
import json

import benchmark_runner
from openai import OpenAI


def _repetitions_for(fixture, default_range):
    return fixture.get("repetition_numbers") or default_range


def execute_payload(stage, payload):
    """Run every pending fixture/repetition for `stage` and return the same
    case-dict list the local in-process loop would produce."""
    client = OpenAI(base_url=payload["base_url"], api_key=payload.get("api_key", "local"))
    model_name = payload["model_name"]
    default_range = range(1, (payload.get("repetitions") or 1) + 1)
    cases = []

    if stage == "script_generation":
        params = benchmark_runner.LLMGenParams(**payload["params"])
        for fixture in payload["fixtures"]:
            for repetition in _repetitions_for(fixture, default_range):
                cases.append(benchmark_runner._run_script_generation_case(
                    fixture, fixture["text"], repetition, client, model_name,
                    params, payload["max_retries"]))
    elif stage == "script_review":
        params = benchmark_runner.LLMGenParams(**payload["params"])
        for fixture in payload["fixtures"]:
            for repetition in _repetitions_for(fixture, default_range):
                cases.append(benchmark_runner._run_script_review_case(
                    fixture, fixture["original"], repetition, client, model_name,
                    params, payload["max_retries"], payload["word_ratio_min"],
                    payload["word_ratio_max"]))
    elif stage == "persona_generation":
        for fixture in payload["fixtures"]:
            for repetition in _repetitions_for(fixture, default_range):
                result = benchmark_runner._run_persona_case(
                    fixture, client, model_name, payload.get("context_length"))
                cases.append({"fixture_id": fixture["id"], "repetition": repetition,
                             **result})
    elif stage == "nickname_detection":
        for fixture in payload["fixtures"]:
            for repetition in _repetitions_for(fixture, default_range):
                cases.append(benchmark_runner._run_nickname_case(
                    fixture, repetition, client, model_name,
                    payload.get("context_length") or 4096,
                    payload.get("concurrency") or 1))
    else:
        raise ValueError(f"unsupported LLM benchmark stage: {stage}")

    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    payload = json.loads(base64.b64decode(args.payload).decode("utf-8"))
    print("LLM_BENCHMARK_RESULT=" + json.dumps(
        execute_payload(args.stage, payload), separators=(",", ":")))


if __name__ == "__main__":
    main()
