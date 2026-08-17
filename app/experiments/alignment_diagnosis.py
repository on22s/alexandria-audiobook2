"""Why do some clips align badly? Goal 5.4's last open question.

WHAT IS ALREADY RULED OUT. Japanese alignment sits at 272 ms against a 150 ms
target while dataset-cut clips manage 39-86 ms. Three explanations have been
tested and eliminated:

    model capacity   base, large-v3 and the hybrid land within one point
    our cutting      the dataset's own 8 utterance ids, cut by us, gave an
                     identical 86 ms and 100% within tolerance
    over-segmentation 15 predicted segments against 8 expected did not hurt
                     alignment at all

What is left is WHICH CLIPS are in a set - per-reader error runs 117 ms on gan
to 400 ms on botchan. This asks what distinguishes them, using properties of
the audio rather than another guess.

WHAT IS MEASURED PER CLIP. Leading and trailing silence, duration, how much of
the clip is silent, and the number of internal pauses. A boundary probe is
being asked to find the edges of speech, so the amount of non-speech at the
edges is the first thing to suspect - and it is cheap to measure and easy to
act on if it turns out to matter.

CORRELATION, NOT CAUSE. A property that tracks the error is a lead, and the
report says so. With 50 clips this cannot separate two correlated properties
from each other, and it does not try.
"""
import argparse
import json
import math
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))


def clip_properties(path, silence_db=-40.0, frame=1024):
    """-> leading/trailing silence, silent fraction, internal pause count."""
    import numpy as np
    import soundfile as sf

    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    signal = audio.mean(axis=1)
    if not len(signal):
        return {}
    frames = max(1, len(signal) // frame)
    energy = np.array([
        float(np.sqrt(np.mean(signal[i * frame:(i + 1) * frame] ** 2)) + 1e-12)
        for i in range(frames)])
    db = 20.0 * np.log10(energy / max(float(np.max(energy)), 1e-12))
    voiced = db > silence_db
    if not voiced.any():
        return {"all_silent": True}
    first, last = int(np.argmax(voiced)), int(len(voiced) - np.argmax(voiced[::-1]))
    seconds_per_frame = frame / float(rate)
    # An internal pause is a run of silent frames long enough to be heard as
    # one - about a tenth of a second - between the first and last speech.
    min_run = max(1, int(0.10 / seconds_per_frame))
    pauses, run = 0, 0
    for flag in voiced[first:last]:
        if flag:
            if run >= min_run:
                pauses += 1
            run = 0
        else:
            run += 1
    return {
        "seconds": round(len(signal) / rate, 3),
        "lead_silence_s": round(first * seconds_per_frame, 3),
        "tail_silence_s": round((len(voiced) - last) * seconds_per_frame, 3),
        "silent_fraction": round(float((~voiced).mean()), 3),
        "internal_pauses": pauses,
    }


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    return round(num / den, 3) if den else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--builds", nargs="+", default=[
        os.path.join(REPO, "ab_test_runtime", "kokoro_ja_asr_eval", "build.json")])
    ap.add_argument("--per-reader", default=os.path.join(
        REPO, "ab_test_runtime", "experiments"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "alignment_diagnosis.json"))
    args = ap.parse_args()

    clips = []
    for build in args.builds:
        with open(build, encoding="utf-8") as handle:
            document = json.load(handle)
        for row in document.get("test", []):
            path = row["human_wav"]
            path = path if os.path.isabs(path) else os.path.join(REPO, path)
            if os.path.exists(path):
                clips.append({"id": row["id"], "book": row.get("book"),
                              **clip_properties(path)})
    if not clips:
        sys.exit("no clips resolved from those builds")

    # Per-reader alignment, from the arms already measured. A clip inherits
    # its reader's error because per-clip alignment is not stored - which is
    # itself worth recording as the limit of this analysis.
    by_reader = {}
    import glob
    for path in glob.glob(os.path.join(args.per_reader, "asr_ja_reader__*.json")):
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        arm = next(iter(doc.get("alignment", {}).values()), {})
        reader = os.path.basename(path)[len("asr_ja_reader__"):-len(".json")]
        if "median_error_s" in arm:
            by_reader[reader] = arm["median_error_s"]

    rows = [c for c in clips if c.get("book") in by_reader and "seconds" in c]
    for row in rows:
        row["reader_align_s"] = by_reader[row["book"]]

    properties = ("lead_silence_s", "tail_silence_s", "silent_fraction",
                  "internal_pauses", "seconds")
    correlations = {}
    for name in properties:
        xs = [r[name] for r in rows if isinstance(r.get(name), (int, float))]
        ys = [r["reader_align_s"] for r in rows
              if isinstance(r.get(name), (int, float))]
        correlations[name] = pearson(xs, ys)

    per_reader = {}
    for reader in sorted(by_reader):
        mine = [r for r in rows if r["book"] == reader]
        if not mine:
            continue
        per_reader[reader] = {
            "align_median_s": by_reader[reader],
            "clips": len(mine),
            **{name: round(statistics.mean(
                [r[name] for r in mine if isinstance(r.get(name), (int, float))]), 3)
               for name in properties},
        }

    document = {
        "clips": len(clips),
        "note": "clip properties against PER-READER alignment; per-clip "
                "alignment is not stored by asr_backends, so every clip of a "
                "reader carries that reader's error. Correlations are leads, "
                "not causes, and n=4 readers cannot separate correlated "
                "properties.",
        "correlation_with_alignment_error": correlations,
        "per_reader": per_reader,
        "rows": rows,
    }
    # Provenance, so a number can name the code that produced it. 58 of 95
    # artifact-writing scripts here omitted this; the gate artifacts goal 2.7
    # rests on are the cost - 87 files that cannot say what made them.
    try:
        from experiments.provenance import provenance
        document["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        document["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)

    print(f"{len(rows)} clips across {len(per_reader)} readers\n")
    print(f"{'reader':32}{'align':>9}{'lead':>8}{'tail':>8}{'silent':>8}{'pauses':>8}")
    for reader, stats in sorted(per_reader.items(), key=lambda kv: kv[1]["align_median_s"]):
        print(f"  {reader[:30]:32}{stats['align_median_s']*1000:>7.0f}ms"
              f"{stats['lead_silence_s']:>8.3f}{stats['tail_silence_s']:>8.3f}"
              f"{stats['silent_fraction']:>8.3f}{stats['internal_pauses']:>8.1f}")
    print("\ncorrelation with alignment error:")
    for name, value in correlations.items():
        print(f"   {name:20} {value}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
