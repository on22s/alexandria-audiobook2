"""Run LLM-based benchmark stages entirely on the host where they execute.

Companion to tts_benchmark.py, JSON on standard input (--payload-stdin,
prints a RESULT= marker line) - but for script_generation/script_review/
persona_generation/nickname_detection instead of TTS. Reuses
benchmark_runner.py's per-case functions directly (no duplicated scoring
logic) so a case computed here and one computed in-process locally produce
identical report entries.

The whole point of running this ON the remote host (over SSH, via
benchmark_runner._run_llm_worker) rather than calling the remote LM Studio
endpoint from the local orchestrator is to keep the network out of the
timed path: payload["llm_config"]["base_url"] is the box's own localhost endpoint,
not the public forwarding URL - see
benchmark_runner._remote_llm_base_url.

Case-level exceptions are intentionally NOT caught here (unlike
tts_benchmark.py's execute_payload) - the local in-process loop this
mirrors doesn't catch them either, and a case failing hard should look the
same (a crashed run, not a silently "failed" case) whether it happened
locally or here.
"""

from benchmark_worker_protocol import emit_benchmark_worker_result
import argparse
import sys
import json
import signal

import benchmark_runner
from llm_provider import make_llm_client
from core import llm_timeout_seconds
from benchmark_execution import (BENCHMARK_STATE, BenchmarkCancelled,
                                 get_cancellable_benchmark_client)


def _repetitions_for(fixture, default_range):
    return fixture.get("repetition_numbers") or default_range


def execute_payload(stage, payload):
    """Run every pending fixture/repetition for `stage` and return the same
    case-dict list the local in-process loop would produce."""
    profile = payload.get("llm_config") or {
        "base_url": payload["base_url"], "api_key": payload.get("api_key", "local")}
    client = get_cancellable_benchmark_client(make_llm_client(profile, timeout=llm_timeout_seconds()))
    try:
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
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--payload-stdin", action="store_true", required=True)
    args = parser.parse_args()
    state = {'cancel':False}
    context = BENCHMARK_STATE.set(state)
    previous = signal.signal(signal.SIGTERM, lambda *_: state.update(cancel=True))
    try:
        emit_benchmark_worker_result(
            "LLM_BENCHMARK_RESULT=", lambda: execute_payload(args.stage, json.load(sys.stdin)))
    except BenchmarkCancelled:
        print('LLM_BENCHMARK_CANCELLED', flush=True)
    finally:
        signal.signal(signal.SIGTERM, previous)
        BENCHMARK_STATE.reset(context)


if __name__ == "__main__":
    main()
