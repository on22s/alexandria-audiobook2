#!/usr/bin/env python3
"""Write resumable, warning-only quality reports for Voice Lab dataset ZIPs."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import zipfile

import numpy as np
import soundfile as sf

APP_DIR = Path(__file__).resolve().parent / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from utils import atomic_json_write
from voice_dataset_merge import get_file_fingerprint


REPORT_VERSION = 1
THRESHOLDS = {
    "duration_min_seconds": 1.0,
    "duration_max_seconds": 30.0,
    "rms_min": 0.015,
    "rms_max": 0.35,
    "silence_ratio_max": 0.50,
    "clipping_ratio_max": 0.001,
    "snr_estimate_min_db": 15.0,
    "sample_rate_min": 16000,
}


def get_clip_metrics(wav_bytes: bytes) -> tuple[dict, str]:
    audio, sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
    if not len(audio):
        raise ValueError("empty audio")
    mono = audio.mean(axis=1)
    absolute = np.abs(mono)
    frame_length = max(1, int(sample_rate * 0.02))
    usable = mono[:len(mono) - (len(mono) % frame_length)]
    frame_rms = (np.sqrt(np.mean(np.square(usable.reshape(-1, frame_length)), axis=1))
                 if len(usable) else np.array([0.0]))
    noise_floor = float(np.percentile(frame_rms, 10))
    signal_level = float(np.percentile(frame_rms, 90))
    snr_estimate = 20 * np.log10((signal_level + 1e-8) / (noise_floor + 1e-8))
    pcm_hash = hashlib.sha256(np.asarray(mono, dtype="<f4").tobytes()).hexdigest()
    return {
        "duration_seconds": len(mono) / sample_rate,
        "sample_rate": int(sample_rate),
        "channels": int(audio.shape[1]),
        "rms": float(np.sqrt(np.mean(np.square(mono)))),
        "peak": float(np.max(absolute)),
        "silence_ratio": float(np.mean(absolute < 0.005)),
        "clipping_ratio": float(np.mean(absolute >= 0.99)),
        "snr_estimate_db": float(snr_estimate),
    }, pcm_hash


def get_clip_warnings(metrics: dict) -> list[str]:
    warnings = []
    duration = metrics["duration_seconds"]
    if duration < THRESHOLDS["duration_min_seconds"]:
        warnings.append("too_short")
    if duration > THRESHOLDS["duration_max_seconds"]:
        warnings.append("too_long")
    if metrics["rms"] < THRESHOLDS["rms_min"]:
        warnings.append("low_level")
    if metrics["rms"] > THRESHOLDS["rms_max"]:
        warnings.append("high_level")
    if metrics["silence_ratio"] > THRESHOLDS["silence_ratio_max"]:
        warnings.append("excess_silence")
    if metrics["clipping_ratio"] > THRESHOLDS["clipping_ratio_max"]:
        warnings.append("clipping")
    if metrics["snr_estimate_db"] < THRESHOLDS["snr_estimate_min_db"]:
        warnings.append("low_snr")
    if metrics["sample_rate"] < THRESHOLDS["sample_rate_min"]:
        warnings.append("low_sample_rate")
    if metrics["channels"] > 1:
        warnings.append("multichannel")
    return warnings


def is_reusable_report(report: dict, fingerprint: dict) -> bool:
    return (report.get("version") == REPORT_VERSION
            and report.get("source_fingerprint") == fingerprint
            and report.get("thresholds") == THRESHOLDS
            and isinstance(report.get("clips"), list))


def audit_zip(path: Path, fingerprint: dict) -> dict:
    clips = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(name for name in archive.namelist() if name.lower().endswith(".wav"))
        for name in names:
            try:
                metrics, pcm_hash = get_clip_metrics(archive.read(name))
                clips.append({"path": name, "metrics": metrics, "pcm_sha256": pcm_hash,
                              "warnings": get_clip_warnings(metrics)})
            except Exception as error:
                clips.append({"path": name, "warnings": ["unreadable"], "error": str(error)})
    warning_counts = {}
    for clip in clips:
        for warning in clip["warnings"]:
            warning_counts[warning] = warning_counts.get(warning, 0) + 1
    return {
        "version": REPORT_VERSION, "warning_only": True,
        "source": str(path), "source_fingerprint": fingerprint,
        "thresholds": THRESHOLDS, "audited_at": time.time(),
        "clip_count": len(clips), "warning_clip_count": sum(bool(c["warnings"]) for c in clips),
        "warning_counts": warning_counts, "clips": clips,
    }


def build_summary(reports: list[dict]) -> dict:
    hashes = {}
    for report in reports:
        for clip in report["clips"]:
            digest = clip.get("pcm_sha256")
            if digest and clip["path"].startswith(("train/", "val/")):
                hashes.setdefault(digest, []).append({"zip": report["source"], "path": clip["path"]})
    duplicate_groups = [items for items in hashes.values() if len(items) > 1]
    return {
        "version": REPORT_VERSION, "warning_only": True, "thresholds": THRESHOLDS,
        "zip_count": len(reports), "clip_count": sum(r["clip_count"] for r in reports),
        "warning_clip_count": sum(r["warning_clip_count"] for r in reports),
        "exact_duplicate_groups": duplicate_groups, "reports": [r["source"] for r in reports],
        "generated_at": time.time(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zips2", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    output = args.output or args.zips2 / "_quality"
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    zips = sorted(path for narrator in args.zips2.iterdir()
                  if narrator.is_dir() and not narrator.name.startswith("_")
                  for path in narrator.iterdir() if path.suffix.lower() == ".zip")
    for index, path in enumerate(zips, 1):
        identity = hashlib.sha256(str(path.relative_to(args.zips2)).encode()).hexdigest()[:16]
        report_path = output / f"{identity}.json"
        fingerprint = get_file_fingerprint(path)
        existing = {}
        try:
            existing = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if not args.overwrite and is_reusable_report(existing, fingerprint):
            report = existing
            print(f"[{index}/{len(zips)}] SKIP {path.name}", flush=True)
        else:
            print(f"[{index}/{len(zips)}] AUDIT {path.name}", flush=True)
            report = audit_zip(path, fingerprint)
            atomic_json_write(report, str(report_path))
        reports.append(report)
    summary = build_summary(reports)
    atomic_json_write(summary, str(output / "summary.json"))
    print(f"Quality audit: {summary['clip_count']} clips, "
          f"{summary['warning_clip_count']} warning clips, "
          f"{len(summary['exact_duplicate_groups'])} duplicate groups", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
