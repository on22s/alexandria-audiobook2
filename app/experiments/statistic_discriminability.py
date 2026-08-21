"""Which acoustic statistics actually separate one character from another?

U-Style (TASLP 2024) justifies a normalisation choice with a claim about what
speech statistics carry: "all normalization operations only calculate the mean,
not the variance, as the mean can reflect global para-linguistic information
variations. In contrast, the variance is mainly related to the linguistic
content."

If that holds, a MEAN-like statistic (f0 median, vocal-tract length) should
separate speakers and a VARIANCE-like one (f0 spread) should not. It is a
falsifiable claim about our own measurements, and it decides something
practical: `character_distinctiveness.py` averages the F0, energy and length
overlaps with equal weight, which is only right if all three discriminate.

The test is the standard one - between-speaker variance over within-speaker
variance, the F-ratio of a one-way analysis of variance. A statistic whose
values scatter as much inside one character as between characters cannot tell
them apart, whatever its units.

WHAT THIS IS NOT. A high ratio means the statistic separates THESE speakers in
THIS rendering. It is not evidence that a listener uses that cue, and with a
handful of characters the ratios are estimates, not constants - which is why
the artifact records the speaker count and the per-character clip counts beside
every number.
"""
import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.character_distinctiveness import features, load_manifest  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

# Named so the claim under test is legible in the output.
STATISTICS = (("f0_median", "mean-like"), ("vtl_cm", "mean-like"),
              ("rms", "mean-like"), ("seconds", "mean-like"),
              ("f0_spread", "variance-like"))


def mean(values):
    return sum(values) / len(values) if values else None


def f_ratio(groups):
    """-> between-group variance / within-group variance, or None.

    Groups is {name: [values]}. This is the one-way ANOVA F statistic without
    its degrees-of-freedom scaling: the comparison here is BETWEEN statistics
    measured on the same groups, so the common factor cancels and leaving it
    out keeps the number readable.
    """
    usable = {k: [v for v in vals if v is not None] for k, vals in groups.items()}
    usable = {k: v for k, v in usable.items() if len(v) >= 2}
    if len(usable) < 2:
        return None
    everything = [v for vals in usable.values() for v in vals]
    grand = mean(everything)
    between = sum(len(v) * (mean(v) - grand) ** 2 for v in usable.values())
    within = sum((x - mean(v)) ** 2 for v in usable.values() for x in v)
    if within <= 0:
        return None
    return (between / max(1, len(usable) - 1)) / (within / max(1, len(everything) - len(usable)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chunks", default=os.path.join(REPO, "chunks.json"))
    ap.add_argument("--audio-dir", required=True)
    ap.add_argument("--min-clips", type=int, default=5)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    by_speaker, unmatched = load_manifest(args.chunks, args.audio_dir)
    per_clip = {}
    for speaker, paths in sorted(by_speaker.items()):
        if len(paths) < args.min_clips:
            continue
        per_clip[speaker] = [features(p) for p in paths]
    if len(per_clip) < 2:
        raise SystemExit("need at least two characters above --min-clips")

    rows = []
    for name, kind in STATISTICS:
        groups = {s: [f.get(name) for f in fs] for s, fs in per_clip.items()}
        ratio = f_ratio(groups)
        measured = {s: len([v for v in vals if v is not None])
                    for s, vals in groups.items()}
        rows.append({"statistic": name, "kind": kind,
                     "f_ratio": None if ratio is None else round(ratio, 3),
                     "clips_measured": measured})
    rows.sort(key=lambda r: -(r["f_ratio"] or -1))

    ranked = [r for r in rows if r["f_ratio"] is not None]
    means = [r["f_ratio"] for r in ranked if r["kind"] == "mean-like"]
    variances = [r["f_ratio"] for r in ranked if r["kind"] == "variance-like"]
    verdict = "not decidable: one of the two kinds has no measurable statistic"
    if means and variances:
        verdict = ("consistent with U-Style: every variance-like statistic "
                   "ranks below the best mean-like one"
                   if max(variances) < max(means)
                   else "against U-Style: a variance-like statistic separates "
                        "these speakers as well as the best mean-like one")

    doc = {"status": "complete", "provenance": provenance(__file__, args),
           "scope": "between-speaker over within-speaker variance for each "
                    "acoustic statistic, on one rendered book. Separation of "
                    "these speakers in this rendering, not evidence about what "
                    "a listener uses",
           "characters": len(per_clip),
           "clips_unmatched_to_a_chunk": unmatched,
           "claim_under_test": "U-Style (TASLP 2024): the mean of a speech "
                               "statistic carries speaker identity, the "
                               "variance carries linguistic content",
           "statistics": rows, "verdict": verdict}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%d characters\n" % len(per_clip))
    print("%-12s %-15s %9s" % ("statistic", "kind", "F ratio"))
    for row in rows:
        print("%-12s %-15s %9s" % (
            row["statistic"], row["kind"],
            "n/a" if row["f_ratio"] is None else "%.3f" % row["f_ratio"]))
    print("\n%s" % verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
