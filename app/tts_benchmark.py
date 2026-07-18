"""Production TTS benchmark execution and deterministic WAV measurements."""

import argparse
import base64
import hashlib
import json
import os
import time
import wave

import numpy as np

from tts import TTSEngine


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
    succeeded = engine.generate_custom_voice(
        fixture["text"], fixture.get("instruct", ""), fixture["speaker"],
        voice_config, output_path)
    generation_seconds = time.monotonic() - generation_started
    if not succeeded or not os.path.isfile(output_path):
        raise RuntimeError("CustomVoice generation did not produce a WAV")
    metrics = measure_wav(output_path, generation_seconds)
    metrics["model_load_seconds"] = round(load_seconds, 3)
    return metrics


def run_clone_voice_case(engine, fixture, output_path, root_dir, load_model=False):
    """Exercise Base-model prompt construction and cached clone generation."""
    ref_path = os.path.abspath(os.path.join(root_dir, fixture["ref_audio"]))
    with open(ref_path, "rb") as ref_file:
        if hashlib.sha256(ref_file.read()).hexdigest() != fixture["ref_audio_sha256"]:
            raise ValueError("clone reference audio hash changed")
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
    succeeded = engine.generate_clone_voice(
        fixture["text"], fixture["speaker"], voice_config, output_path)
    generation_seconds = time.monotonic() - generation_started
    if not succeeded or not os.path.isfile(output_path):
        raise RuntimeError("clone generation did not produce a WAV")
    metrics = measure_wav(output_path, generation_seconds)
    metrics.update({"model_load_seconds": round(load_seconds, 3),
                    "prompt_build_seconds": round(prompt_seconds, 3)})
    return metrics


def execute_payload(payload, output_dir):
    """Run all cases with one engine so warm timings match production use."""
    config = {"tts": dict(payload.get("tts") or {})}
    config["tts"].update({"mode": "local", "compile_codec": False})
    engine = TTSEngine(config)
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cases = []
    first = True
    os.makedirs(output_dir, exist_ok=True)
    for fixture in payload["fixtures"]:
        repetitions = fixture.get("repetition_numbers") or range(
            1, payload["repetitions"] + 1)
        for repetition in repetitions:
            path = os.path.join(output_dir, f"{fixture['id']}-{repetition}.wav")
            try:
                if fixture.get("voice_type", "custom") == "clone":
                    metrics = run_clone_voice_case(
                        engine, fixture, path, root_dir, load_model=first)
                else:
                    metrics = run_custom_voice_case(
                        engine, fixture, path, load_model=first)
                status, error = "passed", None
            except Exception as exc:
                metrics, status, error = {}, "failed", str(exc)
            cases.append({"fixture_id": fixture["id"], "repetition": repetition,
                          "status": status, "metrics": metrics, "error": error})
            first = False
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    payload = json.loads(base64.b64decode(args.payload).decode("utf-8"))
    print("TTS_BENCHMARK_RESULT=" + json.dumps(execute_payload(payload, args.output_dir),
                                               separators=(",", ":")))


if __name__ == "__main__":
    main()
