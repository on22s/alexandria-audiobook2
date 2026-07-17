#!/usr/bin/env python3
"""Generate fixed LoRA probes and write resumable, warning-only evaluations."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

import librosa
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config_settings import load_app_config
from tts import TTSEngine
from utils import atomic_json_write


EVALUATION_VERSION = 1
PROBES = (
    ("narration", "The ancient library stood quietly beneath a sky filled with stars."),
    ("dialogue", "I understand the danger, but we cannot abandon them now."),
)
THRESHOLDS = {
    "speaker_similarity_min": 0.55,
    "silence_ratio_max": 0.35,
    "clipping_ratio_max": 0.001,
}


def get_audio_metrics(path: str) -> dict[str, float]:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if not len(audio):
        raise ValueError("generated audio is empty")
    absolute = np.abs(audio)
    return {
        "duration_seconds": len(audio) / sample_rate,
        "rms": float(np.sqrt(np.mean(np.square(audio)))),
        "silence_ratio": float(np.mean(absolute < 0.005)),
        "clipping_ratio": float(np.mean(absolute >= 0.99)),
    }


def get_warnings(metrics: dict) -> list[str]:
    warnings = []
    if metrics["speaker_similarity"] < THRESHOLDS["speaker_similarity_min"]:
        warnings.append("low_speaker_similarity")
    if metrics["silence_ratio"] > THRESHOLDS["silence_ratio_max"]:
        warnings.append("excess_silence")
    if metrics["clipping_ratio"] > THRESHOLDS["clipping_ratio_max"]:
        warnings.append("clipping")
    return warnings


def load_embedding_audio(path: str):
    import torch
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)
    peak = np.max(np.abs(audio)) if len(audio) else 0.0
    if peak:
        audio = audio / peak
    return torch.from_numpy(np.asarray(audio, dtype=np.float32)).unsqueeze(0)


def get_speaker_similarity(model, reference_path: str, probe_path: str,
                           device: str) -> float:
    import torch
    with torch.no_grad():
        reference = model.encode_batch(load_embedding_audio(reference_path).to(device)).flatten()
        probe = model.encode_batch(load_embedding_audio(probe_path).to(device)).flatten()
        return float(torch.nn.functional.cosine_similarity(reference, probe, dim=0).cpu())


def is_complete_evaluation(result: dict, adapter_dir: str) -> bool:
    return (
        result.get("version") == EVALUATION_VERSION
        and [item.get("id") for item in result.get("probes", [])]
        == [probe_id for probe_id, _text in PROBES]
        and all(os.path.isfile(os.path.join(adapter_dir, item.get("audio_file", "")))
                for item in result.get("probes", []))
    )


def evaluate_adapter(entry: dict, models_dir: str, engine: TTSEngine,
                     embedding_model, device: str) -> dict:
    adapter_id = entry["id"]
    adapter_dir = os.path.join(models_dir, adapter_id)
    reference_path = os.path.join(adapter_dir, "ref_sample.wav")
    if not os.path.isfile(reference_path):
        raise FileNotFoundError(f"missing ref_sample.wav for {adapter_id}")

    probe_results = []
    for probe_id, text in PROBES:
        filename = f"evaluation_{probe_id}.wav"
        output_path = os.path.join(adapter_dir, filename)
        generated = engine.generate_voice(
            text=text, instruct_text="", speaker="_evaluation_",
            voice_config={"_evaluation_": {
                "type": "lora", "adapter_id": adapter_id, "adapter_path": adapter_dir,
            }},
            output_path=output_path,
        )
        if generated is False or not os.path.isfile(output_path):
            raise RuntimeError(f"probe generation failed: {probe_id}")
        metrics = get_audio_metrics(output_path)
        metrics["speaker_similarity"] = get_speaker_similarity(
            embedding_model, reference_path, output_path, device)
        warnings = get_warnings(metrics)
        probe_results.append({
            "id": probe_id, "text": text, "audio_file": filename,
            "metrics": metrics, "warnings": warnings,
        })

    warnings = sorted({warning for result in probe_results for warning in result["warnings"]})
    return {
        "version": EVALUATION_VERSION,
        "status": "warning" if warnings else "pass",
        "warning_only": True,
        "warnings": warnings,
        "thresholds": THRESHOLDS,
        "evaluated_at": time.time(),
        "probes": probe_results,
    }


def main() -> int:
    import torch
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), default=None)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    with open(args.manifest, encoding="utf-8") as handle:
        manifest = json.load(handle)
    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    from speechbrain.inference.speaker import EncoderClassifier
    embedding_model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=os.path.join(args.models_dir, ".evaluation_ecapa"),
        run_opts={"device": device},
    )
    embedding_model.eval()
    engine = TTSEngine(load_app_config(args.config))

    failures = 0
    for entry in manifest:
        adapter_id = entry.get("id", "")
        adapter_dir = os.path.join(args.models_dir, adapter_id)
        result_path = os.path.join(adapter_dir, "evaluation.json")
        if not adapter_id or not os.path.isdir(adapter_dir):
            continue
        existing = {}
        try:
            with open(result_path, encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, json.JSONDecodeError):
            pass
        if not args.overwrite and is_complete_evaluation(existing, adapter_dir):
            print(f"SKIP {adapter_id} — evaluation complete", flush=True)
            entry["evaluation"] = {key: existing.get(key) for key in
                                   ("version", "status", "warnings", "evaluated_at")}
            continue
        print(f"EVALUATE {adapter_id}", flush=True)
        try:
            result = evaluate_adapter(entry, args.models_dir, engine, embedding_model, device)
            atomic_json_write(result, result_path)
            entry["evaluation"] = {key: result[key] for key in
                                   ("version", "status", "warnings", "evaluated_at")}
            print(f"  {result['status'].upper()} — {', '.join(result['warnings']) or 'clean'}",
                  flush=True)
        except Exception as error:
            failures += 1
            entry["evaluation"] = {"version": EVALUATION_VERSION, "status": "failed",
                                   "warnings": [str(error)], "evaluated_at": time.time()}
            print(f"  FAILED — {error}", flush=True)
        atomic_json_write(manifest, args.manifest)
    atomic_json_write(manifest, args.manifest)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
