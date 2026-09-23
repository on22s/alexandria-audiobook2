#!/usr/bin/env python3
"""Benchmark profiler spectral features against a torch/ROCm prototype.

This is an evidence tool only.  Production Voice Lab extraction continues to
use ``voice_profiler.analyze_ref_wav`` until parity and end-to-end speedups are
demonstrated on representative clips.
"""

import argparse
import json
import os
import statistics
import time

import librosa
import numpy as np


FEATURE_TOLERANCES = {
    "mean_rms": {"rtol": 2e-4, "atol": 1e-7},
    "mean_centroid": {"rtol": 2e-4, "atol": 2e-2},
    "mean_rolloff": {"rtol": 2e-4, "atol": 2e-2},
    "flatness": {"rtol": 5e-4, "atol": 1e-7},
}


def get_librosa_spectral_features(y: np.ndarray, sr: int) -> dict[str, float]:
    """Return the production profiler's four directly replaceable features."""
    return {
        "mean_rms": float(np.mean(librosa.feature.rms(y=y)[0])),
        "mean_centroid": float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)[0])),
        "mean_rolloff": float(np.mean(librosa.feature.spectral_rolloff(
            y=y, sr=sr, roll_percent=0.85)[0])),
        "flatness": float(np.mean(librosa.feature.spectral_flatness(y=y)[0])),
    }


def get_torch_spectral_features(y: np.ndarray, sr: int, device: str) -> dict[str, float]:
    """Compute matching features with shared torch frames and one shared STFT."""
    import torch
    import torch.nn.functional as F

    waveform = torch.as_tensor(y, dtype=torch.float32, device=device)
    n_fft = 2048
    hop_length = 512

    rms_frames = F.pad(waveform, (n_fft // 2, n_fft // 2)).unfold(0, n_fft, hop_length)
    rms = torch.sqrt(torch.mean(rms_frames.square(), dim=1))

    window = torch.hann_window(n_fft, periodic=True, device=device)
    magnitude = torch.stft(
        waveform, n_fft=n_fft, hop_length=hop_length, window=window,
        center=True, pad_mode="constant", return_complex=True,
    ).abs()
    frequencies = torch.linspace(0, sr / 2, magnitude.shape[0], device=device).unsqueeze(1)
    magnitude_sum = magnitude.sum(dim=0).clamp_min(torch.finfo(magnitude.dtype).tiny)
    centroid = (frequencies * magnitude).sum(dim=0) / magnitude_sum

    cumulative = magnitude.cumsum(dim=0)
    threshold = 0.85 * cumulative[-1]
    rolloff_indices = torch.argmax((cumulative >= threshold).to(torch.int8), dim=0)
    rolloff = frequencies.squeeze(1)[rolloff_indices]

    power_spectrum = magnitude.square().clamp_min(1e-10)
    flatness = torch.exp(torch.mean(torch.log(power_spectrum), dim=0)) / torch.mean(
        power_spectrum, dim=0).clamp_min(torch.finfo(magnitude.dtype).tiny)

    return {
        "mean_rms": float(rms.mean().item()),
        "mean_centroid": float(centroid.mean().item()),
        "mean_rolloff": float(rolloff.mean().item()),
        "flatness": float(flatness.mean().item()),
    }


def compare_features(reference: dict[str, float], candidate: dict[str, float]) -> dict:
    comparisons = {}
    for name, tolerance in FEATURE_TOLERANCES.items():
        absolute_error = abs(candidate[name] - reference[name])
        allowed_error = tolerance["atol"] + tolerance["rtol"] * abs(reference[name])
        comparisons[name] = {
            "reference": reference[name],
            "candidate": candidate[name],
            "absolute_error": absolute_error,
            "allowed_error": allowed_error,
            "passed": absolute_error <= allowed_error,
        }
    return comparisons


def _synchronize(device: str) -> None:
    if device.startswith("cuda"):
        import torch
        torch.cuda.synchronize()


def _median_runtime(function, repeats: int, device: str) -> float:
    samples = []
    for _ in range(repeats):
        _synchronize(device)
        started = time.perf_counter()
        function()
        _synchronize(device)
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def get_librosa_operation_times(y: np.ndarray, sr: int, repeats: int) -> dict[str, float]:
    """Time each acoustic operation used by production ``analyze_ref_wav``."""
    operations = {
        "yin": lambda: librosa.yin(y, fmin=50, fmax=400, sr=sr),
        "rms": lambda: librosa.feature.rms(y=y),
        "centroid": lambda: librosa.feature.spectral_centroid(y=y, sr=sr),
        "rolloff": lambda: librosa.feature.spectral_rolloff(
            y=y, sr=sr, roll_percent=0.85),
        "harmonic": lambda: librosa.effects.harmonic(y, margin=2.0),
        "flatness": lambda: librosa.feature.spectral_flatness(y=y),
        "onset": lambda: librosa.onset.onset_detect(y=y, sr=sr, units="time"),
    }
    for operation in operations.values():
        operation()
    return {
        name: _median_runtime(operation, repeats, "cpu")
        for name, operation in operations.items()
    }


def benchmark_clip(path: str, device: str, repeats: int) -> dict:
    y, sr = librosa.load(path, sr=22050, mono=True)
    reference = get_librosa_spectral_features(y, sr)
    candidate = get_torch_spectral_features(y, sr, device)
    comparisons = compare_features(reference, candidate)

    # Warm both paths before timing so import/kernel initialization is excluded.
    get_librosa_spectral_features(y, sr)
    get_torch_spectral_features(y, sr, device)
    librosa_seconds = _median_runtime(
        lambda: get_librosa_spectral_features(y, sr), repeats, "cpu")
    torch_seconds = _median_runtime(
        lambda: get_torch_spectral_features(y, sr, device), repeats, device)
    operation_seconds = get_librosa_operation_times(y, sr, repeats)
    replaceable_seconds = sum(operation_seconds[name]
                              for name in ("rms", "centroid", "rolloff", "flatness"))
    operation_total = sum(operation_seconds.values())
    return {
        "path": os.path.abspath(path),
        "duration_seconds": len(y) / sr,
        "parity_passed": all(item["passed"] for item in comparisons.values()),
        "features": comparisons,
        "median_seconds": {"librosa": librosa_seconds, "torch": torch_seconds},
        "speedup": librosa_seconds / torch_seconds if torch_seconds else None,
        "librosa_operation_seconds": operation_seconds,
        "estimated_replaceable_fraction": (
            replaceable_seconds / operation_total if operation_total else 0.0),
    }


def main() -> int:
    import torch

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clips", nargs="+", help="WAV/FLAC clips to benchmark")
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    results = [benchmark_clip(path, args.device, args.repeats) for path in args.clips]
    report = {
        "device": args.device,
        "device_name": (torch.cuda.get_device_name(0)
                        if args.device.startswith("cuda") and torch.cuda.is_available()
                        else "CPU"),
        "scope": "profiler acoustic operations; speedup covers spectral subset only and excludes load and LLM",
        "clips": results,
        "parity_passed": all(result["parity_passed"] for result in results),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Device: {report['device_name']} ({args.device})")
        print(f"Scope: {report['scope']}")
        for result in results:
            status = "PASS" if result["parity_passed"] else "FAIL"
            print(f"{status} {result['path']} — {result['speedup']:.2f}x spectral speedup")
            print("  estimated replaceable share of acoustic operations: "
                  f"{result['estimated_replaceable_fraction']:.1%}")
            for name, comparison in result["features"].items():
                mark = "ok" if comparison["passed"] else "drift"
                print(f"  {name}: {mark}, abs error {comparison['absolute_error']:.6g}")
    return 0 if report["parity_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
