"""Are our characters acoustically different from each other in a rendered book?

Wu & Ling (TOALLIP 2025), "Enhanced Prosody Modeling and Character Voice
Controlling for Audiobook Speech Synthesis", test character voice control by
comparing the probability density of F0, energy and duration across their
character settings - not by a similarity score against a target. That question
has never been asked of our output, and it is the one a listener actually
answers: two characters whose distributions sit on top of each other sound like
one reader doing one voice, however well each matches its own reference.

Goal 3.3 asks that one character keeps one voice. This asks the complement:
that two characters do not share one.

WHAT THIS MEASURES AND WHAT IT DOES NOT. Overlapping F0, energy and rate
distributions mean two voices are not separated ON THOSE THREE FEATURES. They
are not a claim that a listener cannot tell them apart - timbre carries
identity that none of the three capture, which is why goal 6.5 exists and why
--ecapa-python is offered for the embedding view. A pair flagged here is a
candidate for listening to, not a defect established.
"""
import argparse
import collections
import json
import os
import sys
import wave

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402

MIN_CLIPS = 5


def clip_seconds(path):
    with wave.open(path, "rb") as handle:
        return handle.getnframes() / float(handle.getframerate() or 1)


def features(path):
    """-> {f0_median, f0_spread, rms, seconds} for one clip.

    f0 comes from reference_rebuild.measure, which is what every other
    reference and voice measurement in this repo already uses. A second
    pitch tracker here would be a second yardstick (Rule 15).
    """
    from experiments.reference_rebuild import measure
    out = dict(measure(path) or {})
    try:
        import numpy as np
        import soundfile as sf
        data, _ = sf.read(path)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        out["rms"] = float(np.sqrt(np.mean(np.square(data)))) if len(data) else None
    except Exception:                                   # noqa: BLE001
        out["rms"] = None
    try:
        out["seconds"] = clip_seconds(path)
    except Exception:                                   # noqa: BLE001
        out["seconds"] = None
    return out


def overlap(a, b, bins=24):
    """-> histogram intersection of two samples, 1.0 identical, 0.0 disjoint.

    Both samples are binned over their COMBINED range, so the number is a
    property of the pair rather than of whichever sample happened to be
    binned first.
    """
    values = [v for v in list(a) + list(b) if v is not None]
    if len(a) < 2 or len(b) < 2 or not values:
        return None
    lo, hi = min(values), max(values)
    if hi <= lo:
        return 1.0
    width = (hi - lo) / bins

    def density(sample):
        counts = [0] * bins
        for v in sample:
            if v is None:
                continue
            index = min(bins - 1, int((v - lo) / width))
            counts[index] += 1
        total = sum(counts) or 1
        return [c / total for c in counts]

    return sum(min(x, y) for x, y in zip(density(a), density(b)))


def semitones(a, b):
    """-> |pitch distance| between two medians, in semitones.

    Reported BESIDE the overlap because the two answer different questions and
    the overlap alone is easy to misread. NARRATOR and NATSUKI SUBARU overlap
    0.49 on F0 - which reads as "half the same voice" - while their medians sit
    130.9 Hz and 181.4 Hz apart, a 5.6-semitone gap nobody would miss. The
    overlap measures shared RANGE; this measures separation of centres. A pair
    is only a candidate for sounding alike when both say so.
    """
    if not a or not b or a <= 0 or b <= 0:
        return None
    import math
    return abs(12.0 * math.log(a / b, 2))


def median(values):
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else (clean[mid - 1] + clean[mid]) / 2.0


def load_manifest(chunks_path, audio_dir):
    """-> {speaker: [wav path, ...]} using each chunk's uid as the file name."""
    with open(chunks_path, encoding="utf-8") as handle:
        chunks = json.load(handle)
    by_uid = {c.get("uid"): c for c in chunks if c.get("uid")}
    out, unmatched = collections.defaultdict(list), 0
    for name in sorted(os.listdir(audio_dir)):
        if not name.endswith(".wav"):
            continue
        chunk = by_uid.get(os.path.splitext(name)[0])
        if chunk is None:
            unmatched += 1
            continue
        speaker = (chunk.get("speaker") or "").strip() or "?"
        out[speaker].append(os.path.join(audio_dir, name))
    return out, unmatched


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chunks", default=os.path.join(REPO, "chunks.json"))
    ap.add_argument("--audio-dir", required=True)
    ap.add_argument("--min-clips", type=int, default=MIN_CLIPS,
                    help="characters with fewer clips are reported but not "
                         "compared; a distribution over three clips is noise")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    by_speaker, unmatched = load_manifest(args.chunks, args.audio_dir)
    if not by_speaker:
        raise SystemExit("no clip matched a chunk uid; wrong --chunks or --audio-dir?")

    measured, skipped = {}, {}
    for speaker, paths in sorted(by_speaker.items()):
        if len(paths) < args.min_clips:
            skipped[speaker] = len(paths)
            continue
        rows = [features(p) for p in paths]
        measured[speaker] = {
            "clips": len(rows),
            "f0_median": median([r.get("f0_median") for r in rows]),
            "f0_spread": median([r.get("f0_spread") for r in rows]),
            "rms": median([r.get("rms") for r in rows]),
            "seconds": median([r.get("seconds") for r in rows]),
            "_f0": [r.get("f0_median") for r in rows],
            "_rms": [r.get("rms") for r in rows],
            "_sec": [r.get("seconds") for r in rows],
        }

    names = sorted(measured)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            row = {"a": a, "b": b}
            for key, field in (("f0", "_f0"), ("rms", "_rms"), ("seconds", "_sec")):
                row["%s_overlap" % key] = overlap(measured[a][field],
                                                  measured[b][field])
            row["f0_semitones_apart"] = None if semitones(
                measured[a]["f0_median"], measured[b]["f0_median"]) is None else round(
                semitones(measured[a]["f0_median"], measured[b]["f0_median"]), 2)
            # SECONDS IS EXCLUDED FROM THE MEAN, and measured rather than
            # judged: statistic_discriminability.py puts clip length at an
            # F-ratio of 2.24 between these characters against 43.63 for f0
            # median and 22.43 for energy - it barely separates them, and
            # averaging it in changed which pair ranked most-overlapping.
            # NARRATOR vs Subaru read 0.355 with it and 0.200 without, on a
            # pair 12.2 semitones apart. It is still reported per feature; it
            # is just not allowed to dilute the summary.
            values = [row[k] for k in ("f0_overlap", "rms_overlap")
                      if row[k] is not None]
            row["mean_overlap"] = round(sum(values) / len(values), 4) if values else None
            pairs.append(row)
    pairs.sort(key=lambda r: -(r["mean_overlap"] or 0))

    for stats in measured.values():
        for key in ("_f0", "_rms", "_sec"):
            stats.pop(key, None)

    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "mean_overlap_features": ["f0_overlap", "rms_overlap"],
        "scope": "per-character F0, energy and clip-length distributions in one "
                 "rendered book, and how far each pair overlaps. Overlap means "
                 "not separated ON THESE FEATURES; it is not a claim about what "
                 "a listener can hear. f0_semitones_apart is reported beside "
                 "every overlap because the two are different questions",
        "audio_dir": args.audio_dir,
        "clips_unmatched_to_a_chunk": unmatched,
        "characters": measured,
        "characters_below_min_clips": skipped,
        "pairs": pairs,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%-24s %6s %10s %10s %8s" % ("character", "clips", "f0", "rms", "sec"))
    for name in names:
        s = measured[name]
        print("%-24s %6d %10s %10s %8s" % (
            name[:24], s["clips"],
            "n/a" if s["f0_median"] is None else "%.1f" % s["f0_median"],
            "n/a" if s["rms"] is None else "%.4f" % s["rms"],
            "n/a" if s["seconds"] is None else "%.2f" % s["seconds"]))
    if skipped:
        print("below --min-clips %d: %s" % (args.min_clips, skipped))
    print("\npairs, most-overlapping first:")
    print("  %-20s %-20s %6s %7s %9s" % ("a", "b", "mean", "f0 ovl", "semitones"))
    for row in pairs:
        print("  %-20s %-20s %6.3f %7.3f %9s" % (
            row["a"][:20], row["b"][:20], row["mean_overlap"] or 0,
            row["f0_overlap"] or 0,
            "n/a" if row["f0_semitones_apart"] is None
            else "%.2f" % row["f0_semitones_apart"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
