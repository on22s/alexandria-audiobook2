"""Prune a voice dataset the way Chalamandaris et al. (LREC 2014) pruned
audiobooks before training a TTS voice, and write the pruned copy.

Two rules, applied independently so the report says which one fired:

- PROSODIC: per clip, (F0 mean, F0 std) via pyin; drop the `--discard`
  fraction farthest from the dataset centroid by Mahalanobis distance. The
  paper's reading is that these are the character voices and prominent
  passages a narrator performs, which pull a general-purpose voice around.
- SEGMENTAL: per clip, whisper.cpp transcribes the audio and the word error
  rate against the dataset's own text is the stand-in for the paper's HMM
  alignment score; drop clips above `--max-wer`. A high WER means the text
  and the audio disagree (a misaligned cut, a skipped line, a reading error),
  which is the kind of clip that teaches an adapter the wrong thing.

The paper found prosodic pruning alone made no significant difference and the
two together did (p < 0.001 on paragraphs), so both are on by default.

Input is a dataset zip as the library stores them (train/ and val/ with
metadata.jsonl, `audio_filepath` + `text` per row); output is the same layout
under --out with the dropped rows removed and `prune_report.json` beside it.
"""
import argparse
import collections
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from asr_backends import run_whisper_cpp, word_error_rate  # noqa: E402
from experiments.provenance import provenance  # noqa: E402


def mahalanobis_flags(feats, discard_fraction):
    """-> ids of the `discard_fraction` clips farthest from the (f0_mean, f0_std) centroid.

    Clips without a pitch estimate are never flagged here - silence is the
    segmental rule's business, and a NaN would poison the covariance.
    """
    usable = [f for f in feats if np.isfinite(f["f0_mean"]) and np.isfinite(f["f0_std"])]
    n_drop = int(round(len(usable) * discard_fraction))
    if n_drop <= 0 or len(usable) < 3:
        return set()
    X = np.array([[f["f0_mean"], f["f0_std"]] for f in usable], dtype=float)
    cov = np.cov(X.T) + np.eye(2) * 1e-6
    d = np.sqrt(np.einsum("ij,jk,ik->i", X - X.mean(0), np.linalg.inv(cov), X - X.mean(0)))
    order = np.argsort(-d)
    return {usable[i]["id"] for i in order[:n_drop]}


def segmental_flags(rows, max_wer):
    """-> ids whose ASR hypothesis disagrees with their text beyond max_wer."""
    out = set()
    for r in rows:
        hyp = (r.get("hypothesis") or "").strip()
        if not hyp or word_error_rate(r["text"], hyp) > max_wer:
            out.add(r["id"])
    return out


def clip_f0(audio, sr):
    import librosa
    f0, voiced, _ = librosa.pyin(audio, fmin=60, fmax=500, sr=sr, frame_length=1024)
    f0v = f0[voiced & np.isfinite(f0)]
    if not len(f0v):
        return float("nan"), float("nan")
    return float(np.mean(f0v)), float(np.std(f0v))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--zip", required=True, help="dataset zip (train/ + val/ metadata.jsonl)")
    ap.add_argument("--out", required=True, help="directory for the pruned dataset")
    ap.add_argument("--discard", type=float, default=0.10, help="prosodic: fraction to drop (paper: 10%%)")
    ap.add_argument("--max-wer", type=float, default=0.35)
    ap.add_argument("--no-segmental", action="store_true")
    ap.add_argument("--whisper-cpp-bin", default=os.path.join(REPO, "whisper.cpp", "build", "bin", "whisper-cli"))
    ap.add_argument("--whisper-cpp-model", default=os.path.join(REPO, "whisper.cpp", "models", "ggml-base.en.bin"))
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    import soundfile as sf
    z = zipfile.ZipFile(args.zip)
    rows = []
    for split in ("train", "val"):
        for line in z.read(f"{split}/metadata.jsonl").decode("utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                r["split"] = split
                r["id"] = r["audio_filepath"]
                rows.append(r)
    print(f"{len(rows)} clips in {os.path.basename(args.zip)}", flush=True)

    feats, hyps = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for i, r in enumerate(rows):
            audio, sr = sf.read(io.BytesIO(z.read(r["audio_filepath"])), dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            m, s = clip_f0(audio, sr)
            feats.append({"id": r["id"], "f0_mean": m, "f0_std": s})
            if not args.no_segmental:
                wav = os.path.join(tmp, "clip.wav")
                sf.write(wav, audio, sr)
                text, _ = run_whisper_cpp(wav, args.whisper_cpp_model, args.whisper_cpp_bin, language=args.language)
                hyps[r["id"]] = text
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(rows)}", flush=True)
    for r in rows:
        r["hypothesis"] = hyps.get(r["id"], "")
    prosodic = mahalanobis_flags(feats, args.discard)
    segmental = set() if args.no_segmental else segmental_flags(rows, args.max_wer)
    dropped = prosodic | segmental

    os.makedirs(args.out, exist_ok=True)
    kept = collections.Counter()
    # The library zips carry a ROOT metadata.jsonl (all rows) beside the split
    # ones; batch_train_lora's extractor flattens train/ up to the root when
    # the root file is missing, which breaks every audio_filepath in it.
    with open(os.path.join(args.out, "metadata.jsonl"), "w", encoding="utf-8") as root:
        for r in rows:
            if r["id"] not in dropped:
                root.write(json.dumps({k: v for k, v in r.items()
                                       if k not in ("split", "id", "hypothesis")}, ensure_ascii=False) + "\n")
    for split in ("train", "val"):
        os.makedirs(os.path.join(args.out, split), exist_ok=True)
        with open(os.path.join(args.out, split, "metadata.jsonl"), "w", encoding="utf-8") as fh:
            for r in rows:
                if r["split"] != split or r["id"] in dropped:
                    continue
                with open(os.path.join(args.out, r["audio_filepath"]), "wb") as w:
                    w.write(z.read(r["audio_filepath"]))
                clean = {k: v for k, v in r.items() if k not in ("split", "id", "hypothesis")}
                fh.write(json.dumps(clean, ensure_ascii=False) + "\n")
                kept[split] += 1
    fmap = {f["id"]: f for f in feats}
    report = {"zip": os.path.abspath(args.zip), "clips": len(rows), "kept": dict(kept),
              "dropped_prosodic": sorted(prosodic), "dropped_segmental": sorted(segmental),
              "dropped_both": sorted(prosodic & segmental), "discard": args.discard, "max_wer": args.max_wer,
              "per_clip": [{"id": r["id"], "split": r["split"], "f0_mean": fmap[r["id"]]["f0_mean"],
                            "f0_std": fmap[r["id"]]["f0_std"],
                            "wer": (word_error_rate(r["text"], r["hypothesis"]) if r["hypothesis"] else None),
                            "dropped": r["id"] in dropped} for r in rows],
              "provenance": provenance(__file__, args)}
    with open(os.path.join(args.out, "prune_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print(f"dropped {len(dropped)} of {len(rows)}: prosodic {len(prosodic)}, segmental {len(segmental)}, "
          f"both {len(prosodic & segmental)}; kept {dict(kept)} -> {args.out}")


if __name__ == "__main__":
    main()
