"""Recheck existing TTS WAVs on CPU, without generating new speech.

Usage: CUDA_VISIBLE_DEVICES="" OMP_NUM_THREADS=4 python verify_tts_audio.py
       --app /path/to/app --home /path/to/pulled/tnr-0 --out result.json
"""
import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.app))
    import numpy as np
    import soundfile as sf
    import torch
    from experiments.asr_backends import run_transformers_whisper, word_error_rate
    from tts_vram_benchmark import make_chunks
    from voice_reference import _speaker_similarities

    assert not torch.cuda.is_available(), "This verification must run on CPU"
    torch.set_num_threads(4)
    # Known positive/negative controls for the scoring instrument.
    assert word_error_rate("one two", "one two") == 0
    assert word_error_rate("one two", "") == 1
    assert word_error_rate("one two", "three four") == 1
    assert word_error_rate("one", "one two three") == 2
    random.seed(42)
    texts = [c["text"] for c in make_chunks(32)]
    directories = {
        "w1_seed42": args.home / "tts_batch_quality_wavs/w1",
        "w4_seed42": args.home / "tts_batch_quality_wavs/w4",
        "w16_seed42": args.home / "tts_batch_quality_wavs/w16",
        "w1_seed43": args.home / "tts_batch_quality_wavs_ctl_43/w1",
        "w1_seed42_rerun": args.home / "tts_batch_quality_wavs_ctl_42/w1",
    }
    original = json.loads((args.home / "tts_batch_quality_tnr0_20260929.json").read_text())
    saved = {}
    for tag in directories:
        if tag.startswith("w1_seed43"):
            doc = json.loads((args.home / "tts_ctl_w1_seed43_20260929.json").read_text())
        elif tag.endswith("_rerun"):
            doc = json.loads((args.home / "tts_ctl_w1_seed42_20260929.json").read_text())
        else:
            doc = original
        worker = 4 if tag.startswith("w4") else 16 if tag.startswith("w16") else 1
        saved[tag] = {r["line"]: r for r in doc["per_clip"] if r["workers"] == worker}
    rows, cache = [], {}
    started = time.monotonic()
    for tag, directory in directories.items():
        assert {p.name for p in directory.glob("*.wav")} == {
            "temp_batch_%d.wav" % i for i in range(32)}, directory
        for index, text in enumerate(texts):
            path = directory / ("temp_batch_%d.wav" % index)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            audio, rate = sf.read(path, dtype="float32", always_2d=True)
            assert len(audio) and np.isfinite(audio).all(), path
            assert np.max(np.abs(audio)) > 0, path
            assert audio.shape[1] == 1 and rate == 24000, path
            duration = len(audio) / rate
            assert round(duration, 2) == saved[tag][index]["seconds"], path
            t0 = time.monotonic()
            reused = digest in cache
            if not reused:
                hyp, _ = run_transformers_whisper(
                    str(path), model_id="openai/whisper-base.en")
                cache[digest] = hyp
            hyp = cache[digest]
            score = word_error_rate(text, hyp)
            rows.append({"arm": tag, "line": index, "reference": text,
                         "transcript": hyp, "wer": score, "saved_wer": saved[tag][index]["wer"],
                         "saved_prefix_matches": hyp[:120] == saved[tag][index]["hyp"],
                         "wav_sha256": digest, "seconds": duration,
                         "peak_abs": float(np.max(np.abs(audio))),
                         "clip_fraction": float(np.mean(np.abs(audio) >= .9999)),
                         "asr_seconds": time.monotonic() - t0, "reused_identical_audio": reused})
            print("ASR %d/160 %s line=%d WER=%.3f saved=%.3f %.2fs" %
                  (len(rows), tag, index, score, saved[tag][index]["wer"],
                   time.monotonic() - t0), flush=True)
            args.out.write_text(json.dumps({"status": "running", "stage": "asr",
                "completed": len(rows), "requested": 160, "rows": rows}, indent=2) + "\n")
    pairs, keys = [], []
    for tag, directory in directories.items():
        if tag == "w1_seed42":
            continue
        for index in range(32):
            pairs.append((str(directories["w1_seed42"] / ("temp_batch_%d.wav" % index)),
                          str(directory / ("temp_batch_%d.wav" % index))))
            keys.append((tag, index))
    print("ECAPA 128 pairs on CPU", flush=True)
    similarities = _speaker_similarities(pairs)
    assert similarities and len(similarities) == 128
    assert all(s is not None and np.isfinite(s) for s in similarities)
    ecapa = {}
    for tag in directories:
        if tag == "w1_seed42":
            continue
        values = [value for (arm, _), value in zip(keys, similarities) if arm == tag]
        ecapa[tag] = {"n": len(values), "median": statistics.median(values), "min": min(values)}
    assert min(ecapa["w1_seed42_rerun"]["median"], ecapa["w1_seed42_rerun"]["min"]) > .999
    summary = {}
    for tag in directories:
        arm_rows = [r for r in rows if r["arm"] == tag]
        summary[tag] = {"n": len(arm_rows),
            "mean_wer": statistics.mean(r["wer"] for r in arm_rows),
            "median_wer": statistics.median(r["wer"] for r in arm_rows),
            "lines_wer_over_0_2": sum(r["wer"] > .2 for r in arm_rows),
            "saved_wer_matches_rounded": sum(round(r["wer"],3) == r["saved_wer"] for r in arm_rows),
            "saved_transcript_prefix_matches": sum(r["saved_prefix_matches"] for r in arm_rows)}
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (
        Path(__file__), args.app / "tts_vram_benchmark.py",
        args.app / "experiments/asr_backends.py", args.app / "experiments/_ecapa_batch.py")}
    args.out.write_text(json.dumps({"status": "complete", "completed": 160,
        "requested": 160, "unique_audio_asr": len(cache),
        "elapsed_seconds": time.monotonic() - started, "device": "cpu",
        "asr_model": "openai/whisper-base.en", "summary": summary,
        "ecapa": ecapa, "ecapa_pairs": [{"arm":tag, "line":i, "cosine":value}
                                     for (tag,i),value in zip(keys,similarities)],
        "provenance": {"script": str(Path(__file__).resolve()), "source_sha256": source_hashes},
        "rows": rows}, indent=2) + "\n")
    print("COMPLETE", json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
