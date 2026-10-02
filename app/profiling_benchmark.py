#!/usr/bin/env python3
"""Isolated production-backed worker for Voice Lab profiling benchmarks."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import json
import os
import sys
import time
from voicelab_settings import get_voice_lab_script_path
from lora_evidence import get_file_sha256



def validate_profiling_files(fixture, zip_path, model_path):
    """Verify actual worker paths against the immutable fixture identities."""
    for path, key in ((zip_path, "zip_sha256"), (model_path, "model_sha256")):
        if get_file_sha256(path) != fixture[key]:
            raise ValueError(f"profiling {key.removesuffix('_sha256')}_path hash changed at execution")

def execute_payload(payload):
    fixture = payload["fixture"]
    validate_profiling_files(fixture, payload["zip_path"], payload["model_path"])
    root_dir = payload["root_dir"]
    sys.path.insert(0, root_dir)
    sys.path.insert(0, os.path.dirname(get_voice_lab_script_path(root_dir, "voice_profiler.py")))
    from voice_profiler import (analyze_ref_wav, get_ref_text, get_ref_wav,
                                interpret_features, llm_describe,
                                parse_book_title, parse_narrator_name)
    from llama_cpp import Llama

    started = time.monotonic()
    model_started = time.monotonic()
    llm = Llama(model_path=payload["model_path"], n_ctx=2048,
                n_gpu_layers=-1, seed=fixture["seed"], verbose=False)
    model_seconds = time.monotonic() - model_started
    wav_bytes = get_ref_wav(payload["zip_path"])
    if wav_bytes is None:
        raise ValueError("profiling fixture has no ref.wav")
    acoustic_started = time.monotonic()
    features = analyze_ref_wav(wav_bytes)
    summary = interpret_features(features)
    acoustic_seconds = time.monotonic() - acoustic_started
    dataset_id = fixture["dataset_id"]
    llm_started = time.monotonic()
    description = llm_describe(
        llm, parse_narrator_name(dataset_id), summary,
        book_title=parse_book_title(dataset_id),
        ref_text=get_ref_text(payload["zip_path"]))
    llm_seconds = time.monotonic() - llm_started
    validate_profiling_files(fixture, payload["zip_path"], payload["model_path"])
    selected = {key: round(features[key], 6) for key in
                ("mean_f0", "std_f0", "mean_rms", "speaking_rate",
                 "mean_centroid", "smoothness", "flatness")}
    return {"status": "passed", "elapsed_seconds": round(time.monotonic() - started, 3),
            "phase_seconds": {"model_load": round(model_seconds, 3),
                              "acoustics": round(acoustic_seconds, 3),
                              "llm": round(llm_seconds, 3)},
            "voice_features": selected, "voice_profile": description,
            "profile_word_count": len(description.split())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    emit_benchmark_worker_result('PROFILING_BENCHMARK_RESULT=',
                                 lambda: execute_payload(get_decoded_worker_payload(args.payload)))


if __name__ == "__main__":
    main()
