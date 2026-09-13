#!/usr/bin/env python3
"""Re-split one voice-library dataset by TIME instead of by clip, for goal 2.9.

WHY. The eight private narrators of `second_english_eval_20260820` scored
~0.15 higher on f0 correlation than either public English set (LJSpeech and
Hi-Fi TTS 9017), which both hold out whole works. The private datasets hold
out 20 of 200 clips at random: `val/sample_1600` ends at 14684.9 s of the
audiobook and `train/sample_1601` starts at 14684.9 s - adjacent sentences of
one paragraph on opposite sides of the split. Same-paragraph material is
predicted to raise a prosody correlation, so the eight-narrator figure cannot
be read as "English prosody is fine on our own voices" until that is tested.

WHAT THIS BUILDS. Every clip carries `start`/`end` in the source recording.
Sort by start; the LAST `--val` clips become val; training takes the earliest
`--train` clips whose end falls at least `--gap` seconds before the first val
clip starts. Nothing from the gap is used on either side, so no val line has
a neighbour in training. The reference clip is drawn from TRAINING only, as
`ljspeech_build.py` does, and written with its text for the clone arm.

WHAT IT CANNOT TEST. The library zips cover ~30 minutes of a book (this one:
244.6-275.3 min), so the held-out block is at most that far from the training
block and still the same recording session. A drop toward the public sets
says adjacency was the leak; no drop says the remainder is session-level or
real, and only a different chapter could tell those apart.

    python experiments/library_time_split.py \
        --dataset ../ab_test_runtime/decontaminate/batch4/warm_baritone_30s_m_1/data \
        --out ../ab_test_runtime/time_split/warm_baritone_30s_m_1/data
"""
import argparse
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))


def load_rows(dataset):
    """Every clip the dataset has, train and val alike, with its source path."""
    rows = []
    for split in ("train", "val"):
        meta = os.path.join(dataset, split, "metadata.jsonl")
        if not os.path.exists(meta):
            continue
        with open(meta, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                r["_src"] = os.path.join(dataset, r["audio_filepath"])
                r["_name"] = os.path.basename(r["audio_filepath"])
                rows.append(r)
    missing = [r["_name"] for r in rows if r.get("start") is None or r.get("end") is None]
    if missing:
        sys.exit(f"{len(missing)} clips carry no start/end; cannot split by time")
    return sorted(rows, key=lambda r: r["start"])


def time_split(rows, val_n, train_n, gap_s):
    """-> (train, val, gap_dropped). Val is the latest `val_n` clips; train is
    the earliest `train_n` whose end precedes the val block by `gap_s`.
    Refuses rather than shrinking silently."""
    if len(rows) < val_n + train_n:
        sys.exit(f"only {len(rows)} clips for {train_n} + {val_n}")
    val = rows[-val_n:]
    cutoff = val[0]["start"] - gap_s
    eligible = [r for r in rows[:-val_n] if r["end"] <= cutoff]
    dropped = [r for r in rows[:-val_n] if r["end"] > cutoff]
    if len(eligible) < train_n:
        sys.exit(f"only {len(eligible)} clips end >= {gap_s}s before the val "
                 f"block; asked for {train_n}. Lower --train or --gap.")
    train = eligible[:train_n]
    last_train_end = max(r["end"] for r in train)
    assert last_train_end <= val[0]["start"] - gap_s
    return train, val, dropped


def write_split(rows, dest, split):
    os.makedirs(os.path.join(dest, split), exist_ok=True)
    out = []
    with open(os.path.join(dest, split, "metadata.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            dst = os.path.join(dest, split, r["_name"])
            shutil.copy2(r["_src"], dst)
            entry = {k: v for k, v in r.items() if not k.startswith("_")}
            entry["audio_filepath"] = f"{split}/{r['_name']}"
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out.append(entry)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", required=True, help="a library dataset dir with train/ and val/")
    ap.add_argument("--out", required=True)
    ap.add_argument("--val", type=int, default=20)
    ap.add_argument("--train", type=int, default=150)
    ap.add_argument("--gap", type=float, default=120.0,
                    help="seconds between the last training clip's end and the "
                         "first held-out clip's start")
    ap.add_argument("--ref-min-seconds", type=float, default=4.0)
    ap.add_argument("--ref-max-seconds", type=float, default=12.0)
    args = ap.parse_args()

    if os.path.exists(os.path.join(args.out, "train", "metadata.jsonl")):
        sys.exit(f"refusing to overwrite {args.out}")
    rows = load_rows(args.dataset)
    train, val, dropped = time_split(rows, args.val, args.train, args.gap)
    train_out = write_split(train, args.out, "train")
    val_out = write_split(val, args.out, "val")

    # Reference clip from TRAINING only, the median-length candidate.
    cands = [r for r in train if args.ref_min_seconds <= float(r.get("duration") or 0) <= args.ref_max_seconds]
    if not cands:
        sys.exit("no training clip in the reference length band")
    ref = cands[len(cands) // 2]
    shutil.copy2(ref["_src"], os.path.join(args.out, "ref.wav"))
    with open(os.path.join(args.out, "ref_text.txt"), "w", encoding="utf-8") as fh:
        fh.write(ref["text"])

    from experiments.provenance import provenance
    doc = {"experiment": "library_time_split",
           "source_dataset": os.path.relpath(args.dataset, REPO),
           "clips_available": len(rows),
           "source_span_s": [round(rows[0]["start"], 1), round(rows[-1]["end"], 1)],
           "train": {"n": len(train_out), "span_s": [round(train[0]["start"], 1), round(max(r["end"] for r in train), 1)]},
           "val": {"n": len(val_out), "span_s": [round(val[0]["start"], 1), round(val[-1]["end"], 1)]},
           "gap_s": round(val[0]["start"] - max(r["end"] for r in train), 1),
           "dropped_in_gap": len(dropped),
           "ref": ref["_name"],
           "provenance": provenance(__file__, args)}
    with open(os.path.join(args.out, "time_split.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"{len(rows)} clips over {doc['source_span_s'][0]/60:.1f}-{doc['source_span_s'][1]/60:.1f} min")
    print(f"  train {doc['train']['n']} clips, {doc['train']['span_s'][0]/60:.1f}-{doc['train']['span_s'][1]/60:.1f} min")
    print(f"  gap   {doc['gap_s']:.0f} s ({len(dropped)} clips unused)")
    print(f"  val   {doc['val']['n']} clips, {doc['val']['span_s'][0]/60:.1f}-{doc['val']['span_s'][1]/60:.1f} min")
    print(f"  ref   {ref['_name']}  -> {args.out}")


if __name__ == "__main__":
    main()
