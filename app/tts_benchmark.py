"""Production TTS benchmark execution and deterministic WAV measurements."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import hashlib
import json
import os
import shutil
import threading
import time
import wave

import numpy as np

from lora_evidence import get_file_sha256
from adapter_checkpoint_transaction import ensure_adapter_generation_snapshot
from tts import TTSEngine
from gpu_stats import sample_gpu_utilization
from benchmark_validation import (get_benchmark_directory_path,
                                  get_adapter_artifact_path, get_benchmark_verified_file_path,
                                  get_benchmark_artifact_name, get_benchmark_output_path)


def _run_with_utilization_sampling(fn, poll_interval=1.0):
    """Run fn() while polling GPU utilization at most once per second,
    returning (fn's result, mean utilization percent or None if no samples
    landed).

    This is coarse telemetry, not proof that generation is compute-bound.
    Each sample can launch external driver probes; shorter requested intervals
    are clamped to limit measurement overhead. Longer intervals are retained.
    """
    samples = []
    stop = threading.Event()
    poll_interval = max(1.0, poll_interval)

    def _poll():
        while not stop.is_set():
            value = sample_gpu_utilization()
            if value is not None:
                samples.append(value)
            stop.wait(poll_interval)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    try:
        result = fn()
    finally:
        stop.set()
        poller.join(timeout=1)
    mean_utilization = round(sum(samples) / len(samples), 1) if samples else None
    return result, mean_utilization


def measure_wav(path, elapsed_seconds):
    """Return objective health and throughput measurements for a PCM WAV."""
    with wave.open(path, "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)
    if sample_width != 2:
        raise ValueError("TTS benchmark expects 16-bit PCM WAV output")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    duration = frame_count / sample_rate if sample_rate else 0.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
    silence_ratio = float(np.mean(np.abs(samples) < 0.001)) if samples.size else 1.0
    clipping_ratio = float(np.mean(np.abs(samples) >= 0.999)) if samples.size else 0.0
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "sample_rate": sample_rate, "channels": channels,
        "duration_seconds": round(duration, 3),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "audio_seconds_per_second": round(duration / elapsed_seconds, 4)
        if elapsed_seconds > 0 else 0.0,
        "peak": round(peak, 6), "rms": round(rms, 6),
        "silence_ratio": round(silence_ratio, 6),
        "clipping_ratio": round(clipping_ratio, 6),
    }


def run_custom_voice_case(engine, fixture, output_path, load_model=False):
    """Exercise TTSEngine's production CustomVoice call for one fixture."""
    voice_config = {fixture["speaker"]: {
        "voice": fixture["voice"], "seed": fixture["seed"],
        "default_style": fixture.get("instruct", "neutral"),
    }}
    load_started = time.monotonic()
    if load_model:
        engine._init_local_custom()
    load_seconds = time.monotonic() - load_started
    generation_started = time.monotonic()
    succeeded, mean_utilization = _run_with_utilization_sampling(lambda: engine.generate_custom_voice(
        fixture["text"], fixture.get("instruct", ""), fixture["speaker"],
        voice_config, output_path))
    generation_seconds = time.monotonic() - generation_started
    if not succeeded or not os.path.isfile(output_path):
        raise RuntimeError("CustomVoice generation did not produce a WAV")
    metrics = measure_wav(output_path, generation_seconds)
    metrics["model_load_seconds"] = round(load_seconds, 3)
    metrics["mean_gpu_utilization_pct"] = mean_utilization
    return metrics


def run_clone_voice_case(engine, fixture, output_path, root_dir, load_model=False):
    """Exercise Base-model prompt construction and cached clone generation."""
    ref_path = get_benchmark_verified_file_path(
        root_dir, fixture["ref_audio"], fixture["ref_audio_sha256"], "clone reference audio")
    voice_config = {fixture["speaker"]: {
        "type": "clone", "seed": fixture["seed"], "ref_audio": ref_path,
        "ref_text": fixture["ref_text"],
    }}
    load_started = time.monotonic()
    if load_model:
        engine._init_local_clone()
    load_seconds = time.monotonic() - load_started
    prompt_started = time.monotonic()
    engine._get_clone_prompt(fixture["speaker"], voice_config)
    prompt_seconds = time.monotonic() - prompt_started
    generation_started = time.monotonic()
    succeeded, mean_utilization = _run_with_utilization_sampling(lambda: engine.generate_clone_voice(
        fixture["text"], fixture["speaker"], voice_config, output_path))
    generation_seconds = time.monotonic() - generation_started
    if not succeeded or not os.path.isfile(output_path):
        raise RuntimeError("clone generation did not produce a WAV")
    metrics = measure_wav(output_path, generation_seconds)
    metrics.update({"model_load_seconds": round(load_seconds, 3),
                    "prompt_build_seconds": round(prompt_seconds, 3),
                    "mean_gpu_utilization_pct": mean_utilization})
    return metrics


def run_lora_voice_case(engine, fixture, output_path, root_dir, load_model=False):
    """Exercise adapter loading, prompt construction, and production LoRA generation."""
    adapter_path = get_benchmark_directory_path(root_dir, fixture["adapter_path"])
    for filename in fixture["adapter_artifact_sha256"]:
        get_adapter_artifact_path(adapter_path, filename)
    with ensure_adapter_generation_snapshot(adapter_path) as (snapshot, generation):
        for filename, expected in fixture["adapter_artifact_sha256"].items():
            artifact_path = get_adapter_artifact_path(snapshot, filename)
            if get_file_sha256(artifact_path) != expected:
                raise ValueError(f"LoRA adapter artifact hash changed: {filename}")
        voice_data = {"type": "lora", "adapter_path": adapter_path,
                      "seed": fixture["seed"], "adapter_generation_sha256": generation}
        load_started = time.monotonic()
        model = engine._init_local_lora(snapshot, generation_sha256=generation, source_adapter_path=adapter_path)
        load_seconds = time.monotonic() - load_started
        with open(os.path.join(snapshot, "training_meta.json"), "r",
                  encoding="utf-8") as meta_file:
            ref_text = json.load(meta_file).get("ref_sample_text", "")
        prompt_started = time.monotonic()
        engine._ensure_lora_prompt(snapshot, model, ref_text, generation_sha256=generation, source_adapter_path=adapter_path)
        prompt_seconds = time.monotonic() - prompt_started
    import torch
    torch.manual_seed(fixture["seed"])
    generation_started = time.monotonic()
    succeeded, mean_utilization = _run_with_utilization_sampling(lambda: engine.generate_lora_voice(
        fixture["text"], fixture.get("instruct", ""), voice_data, output_path))
    generation_seconds = time.monotonic() - generation_started
    if not succeeded or not os.path.isfile(output_path):
        raise RuntimeError("LoRA generation did not produce a WAV")
    metrics = measure_wav(output_path, generation_seconds)
    metrics["model_and_adapter_load_seconds"] = round(load_seconds, 3)
    metrics["prompt_build_seconds"] = round(prompt_seconds, 3)
    metrics["mean_gpu_utilization_pct"] = mean_utilization
    return metrics


def run_design_voice_case(engine, fixture, output_path, load_model=False):
    """Exercise the production VoiceDesign preview call with a fixed seed."""
    load_started = time.monotonic()
    if load_model:
        engine._init_local_design()
    load_seconds = time.monotonic() - load_started
    generation_started = time.monotonic()
    (preview_path, _), mean_utilization = _run_with_utilization_sampling(
        lambda: engine.generate_voice_design(
            description=fixture["description"], sample_text=fixture["text"],
            seed=fixture["seed"]))
    generation_seconds = time.monotonic() - generation_started
    if not os.path.isfile(preview_path):
        raise RuntimeError("VoiceDesign generation did not produce a WAV")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    shutil.move(preview_path, output_path)
    metrics = measure_wav(output_path, generation_seconds)
    metrics["model_load_seconds"] = round(load_seconds, 3)
    metrics["mean_gpu_utilization_pct"] = mean_utilization
    return metrics


def execute_payload(payload, output_dir, asset_root=None):
    """Run all cases with one engine so warm timings match production use."""
    config = {"tts": dict(payload.get("tts") or {})}
    config["tts"].update({"mode": "local", "compile_codec": False})
    engine = TTSEngine(config)
    root_dir = asset_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cases = []
    warmed_voice_types = set()
    os.makedirs(output_dir, exist_ok=True)
    for fixture in payload["fixtures"]:
        voice_type = fixture.get("voice_type", "custom")
        if voice_type not in ("design", "lora", "clone"):
            voice_type = "custom"
        repetitions = fixture.get("repetition_numbers") or range(
            1, payload["repetitions"] + 1)
        for repetition in repetitions:
            load_model = voice_type not in warmed_voice_types
            try:
                fixture_id = get_benchmark_artifact_name(fixture['id'], "TTS fixture ID")
                if type(repetition) is not int or repetition < 1:
                    raise ValueError("TTS fixture ID or repetition is invalid for an output filename")
                path = get_benchmark_output_path(output_dir, f"{fixture_id}-{repetition}.wav")
                if voice_type == "design":
                    metrics = run_design_voice_case(
                        engine, fixture, path, load_model=load_model)
                elif voice_type == "lora":
                    metrics = run_lora_voice_case(
                        engine, fixture, path, root_dir, load_model=load_model)
                elif voice_type == "clone":
                    metrics = run_clone_voice_case(
                        engine, fixture, path, root_dir, load_model=load_model)
                else:
                    metrics = run_custom_voice_case(
                        engine, fixture, path, load_model=load_model)
                warmed_voice_types.add(voice_type)
                status, error = "passed", None
            except Exception as exc:
                metrics, status, error = {}, "failed", str(exc)
            cases.append({"fixture_id": fixture["id"], "repetition": repetition,
                          "status": status, "metrics": metrics, "error": error})
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--asset-root", help="Invocation root for privately staged benchmark assets")
    args = parser.parse_args()
    emit_benchmark_worker_result('TTS_BENCHMARK_RESULT=',
                                 lambda: execute_payload(get_decoded_worker_payload(args.payload), args.output_dir, asset_root=args.asset_root))


if __name__ == "__main__":
    main()
