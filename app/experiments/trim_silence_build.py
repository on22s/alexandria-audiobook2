"""Trim leading and trailing silence from an eval set, so the correlation can
be tested as a cause.

WHY. The alignment diagnosis found error tracking silence across four Japanese
readers - silent_fraction correlating 0.51, and the reader ordering monotonic
in lead silence and pause count:

    gan          117 ms   lead 0.364  silent 0.240
    botchan      400 ms   lead 0.515  silent 0.339

That is a correlation over four readers, which is weak evidence and easy to
over-read: readers differ in many ways at once, and recording style could
drive both the silence and the boundary error without one causing the other.

THE TEST THAT SEPARATES THEM. Remove the silence and measure again. Same
clips, same reader, same model, same everything else - only the silence
changes. If alignment improves, the silence was doing the damage. If it does
not, the correlation was a property of the recordings rather than a mechanism,
and the 0.51 should stop being quoted as a lead.

WHAT IT DOES NOT TOUCH. Internal pauses are left alone. They are part of the
utterance, removing them would change what was said, and the goal is a
controlled comparison rather than the best possible score.

THE TRANSCRIPT IS UNCHANGED, so the CER arm of the same run stays comparable:
trimming edge silence removes no words.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def trim(path, destination, threshold_db=-40.0, keep_ms=25, frame=512):
    """Write `path` with edge silence removed. -> (kept_seconds, trimmed_seconds).

    A small margin is kept deliberately. Cutting exactly at the first sample
    above threshold clips the attack of the first consonant, which would be a
    different defect introduced while testing this one.
    """
    import numpy as np
    import soundfile as sf

    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    if not len(mono):
        return 0.0, 0.0
    frames = max(1, len(mono) // frame)
    energy = np.array([
        float(np.sqrt(np.mean(mono[i * frame:(i + 1) * frame] ** 2)) + 1e-12)
        for i in range(frames)])
    db = 20.0 * np.log10(energy / max(float(np.max(energy)), 1e-12))
    voiced = db > threshold_db
    if not voiced.any():
        sf.write(destination, audio, rate)
        return len(mono) / rate, 0.0
    first = int(np.argmax(voiced))
    last = int(len(voiced) - np.argmax(voiced[::-1]))
    margin = max(1, int((keep_ms / 1000.0) * rate / frame))
    start = max(0, (first - margin)) * frame
    end = min(len(mono), (last + margin) * frame)
    sf.write(destination, audio[start:end], rate)
    return (end - start) / rate, (len(mono) - (end - start)) / rate


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--threshold-db", type=float, default=-40.0)
    args = ap.parse_args()

    with open(args.build, encoding="utf-8") as handle:
        document = json.load(handle)
    wav_dir = os.path.join(args.out_dir, "wavs")
    os.makedirs(wav_dir, exist_ok=True)

    rows, removed_total, kept_total = [], 0.0, 0.0
    for row in document.get("test", []):
        source = row["human_wav"]
        source = source if os.path.isabs(source) else os.path.join(REPO, source)
        if not os.path.exists(source):
            continue
        destination = os.path.join(wav_dir, os.path.basename(source))
        kept, removed = trim(source, destination, args.threshold_db)
        kept_total += kept
        removed_total += removed
        rows.append({**row, "human_wav": os.path.relpath(destination, REPO),
                     "seconds": round(kept, 3)})
    if not rows:
        sys.exit("no clips resolved from that build")

    document["test"] = rows
    document["design"] = (document.get("design", "") +
                          "; edge silence trimmed at "
                          f"{args.threshold_db} dB, internal pauses untouched")
    out = os.path.join(args.out_dir, "build.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=1)
    print(f"trimmed {len(rows)} clips -> {out}")
    print(f"  audio kept {kept_total:.1f}s, silence removed {removed_total:.1f}s "
          f"({removed_total / max(kept_total + removed_total, 1e-9) * 100:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
