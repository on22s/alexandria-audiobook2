"""What ratio do goals 2.5 and 2.6 report when NOTHING has been synthesised?

THE QUESTION. 2.6 reads OPEN on one cell: the Chinese clone arm preserves
vocal tract length at 1.0607x against a 0.95-1.05x band. Doubling the sample
moved it by 0.003, so it is not noise in the usual sense, and the goal
concluded it is "a statement about the method". 2.5 reads OPEN the same way on
one cell: English cloning at 0.81x f0 median against 0.95-1.05x.

Both conclusions assume the BAND IS ACHIEVABLE. Nobody has checked. Every
number in those goals is a ratio of two medians - median over generated clips
divided by median over the human clips of the same speaker - and that
statistic has a spread of its own that has never been measured. If two disjoint
halves of one human's OWN recordings already differ by 6%, then 1.0607x is
what the instrument returns for a perfect match and the band is wrong, not the
model ([[Rule 21]]: validate the instrument before trusting the readings).

WHAT THIS DOES. For each language it takes the human side only - the same
`human_wav` column the goals measure against - splits those clips into two
disjoint random halves, and computes exactly the ratio the goal computes,
median(half A) / median(half B). Same speaker, same session, same recording
chain, nothing synthesised anywhere. It repeats that over many random splits
and reports the distribution.

HOW TO READ IT. The `outside_band` column is the finding. If a measure's
human-vs-human ratio falls outside the goal's own band on a material fraction
of splits, that band cannot be met by any arm, including a perfect one, and
the goal needs a corrected target rather than a product decision. If it almost
never does, the instrument is sound and the failing cell is real.

WHAT THIS NULL IS NOT, stated because it decides how far the result carries.
The goal pairs BY LINE - generated line X against the human's line X - so its
two sides differ only in synthesis. The two halves here differ in CONTENT as
well, because a speaker has only one recording of each line, and they are half
the size of the goal's samples. Both facts push this null's spread WIDER than
the goal's. So the asymmetry is:

  * a measure that stays inside its band here is decisively fine - the band
    survives a harder test than the goal applies;
  * a measure that falls outside is SUSPECT, not convicted, and earns a
    content-matched follow-up rather than a conclusion.

This is deliberately NOT a fix and not an arm. It measures the ruler.
"""
import argparse
import json
import os
import random
import statistics
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from voice_compare_view import (pitch_stats, vocal_tract_length,  # noqa: E402
                                voice_quality)
from experiments.manifest import read_inputs  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

# The same three the failing cells live on, plus the two that are MET - a null
# test that only covered the failures could not show that the ruler works
# where the goals say it does, which is half of what makes the result readable.
MEASURES = ("f0_median", "f0_spread", "jitter_local", "shimmer_local", "vtl_cm")

# The goals' own bands, copied here so the report says PASS/FAIL in the same
# terms GOALS.md does rather than leaving the reader to apply them by hand.
BANDS = {"f0_median": (0.95, 1.05), "f0_spread": (0.90, 1.15),
         "jitter_local": (0.85, 1.15), "shimmer_local": (0.85, 1.15),
         "vtl_cm": (0.95, 1.05)}

LANGUAGES = {"en": "ljspeech_generate.json",
             "ja": "kokoro_generate.json",
             "zh": "aishell3_generate.json"}


# THE AUDIO IS NOT IN THE WORKTREE. Manifests are committed; the wav files
# they point at are not, and should not be - they are gigabytes of generated
# and corpus audio. Development happens in a worktree ([[Rule 24]]), so a probe
# that resolves clip paths against its own checkout finds nothing and drops
# every clip. Ask git for the main checkout and fall back to it, the same way
# ready.sh borrows the venv from there.
def _main_checkout():
    import subprocess
    try:
        out = subprocess.run(["git", "-C", REPO, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            return line.split(" ", 1)[1].strip()
    return None


AUDIO_ROOTS = [REPO]


def resolve(path):
    """First root that actually holds the file; None if none does."""
    if not path:
        return None
    if os.path.isabs(path):
        return path
    for root in AUDIO_ROOTS:
        candidate = os.path.join(root, path)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(AUDIO_ROOTS[0], path)


def measure(path):
    """Every measure for one clip, or None for any that would not compute."""
    out = {}
    full = resolve(path)
    if not full or not os.path.exists(full):
        return out
    # Same three calls, same order, as pitch_quality_probe.measure - the goal's
    # numbers come from those functions and a null test computed any other way
    # would not be a null test of the same instrument ([[Rule 15]]).
    out.update(pitch_stats(full) or {})
    out.update(voice_quality(full) or {})
    vtl = vocal_tract_length(full)
    if vtl is not None:
        out["vtl_cm"] = round(float(vtl), 4)
    return out


def collect(manifest_path, limit):
    """-> ([{measure: value} per human clip], dropped_count).

    A clip survives only if EVERY measure computed, matching
    pitch_quality_probe's rule. Partial clips would change which subset each
    measure is taken over and make the five columns incomparable.
    """
    with open(manifest_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("results") or payload.get("rows") or payload
    kept, dropped = [], 0
    for row in rows:
        if limit and len(kept) >= limit:
            break
        m = measure(row.get("human_wav"))
        if any(m.get(k) is None for k in MEASURES):
            dropped += 1
            continue
        kept.append(m)
    return kept, dropped


def split_ratios(clips, measure_name, trials, rng):
    """Ratio of medians over many disjoint random half-splits."""
    values = [c[measure_name] for c in clips]
    ratios = []
    for _ in range(trials):
        idx = list(range(len(values)))
        rng.shuffle(idx)
        half = len(idx) // 2
        a = [values[i] for i in idx[:half]]
        b = [values[i] for i in idx[half:half * 2]]
        mb = statistics.median(b)
        if mb:
            ratios.append(statistics.median(a) / mb)
    return ratios


def summarise(ratios, band):
    ratios = sorted(ratios)
    n = len(ratios)
    if not n:
        return {"trials": 0}
    lo, hi = band
    outside = sum(1 for r in ratios if r < lo or r > hi)
    return {
        "trials": n,
        "median": round(statistics.median(ratios), 4),
        "p5": round(ratios[int(0.05 * n)], 4),
        "p95": round(ratios[min(int(0.95 * n), n - 1)], 4),
        "worst_low": round(ratios[0], 4),
        "worst_high": round(ratios[-1], 4),
        "band": list(band),
        "outside_band": outside,
        "outside_band_pct": round(100.0 * outside / n, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=150,
                    help="human clips per language (0 = all available)")
    ap.add_argument("--trials", type=int, default=500,
                    help="random half-splits per measure")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--manifest", action="append", default=[],
                    metavar="LANG=FILE", help="override one language's manifest")
    ap.add_argument("--audio-root", action="append", default=[],
                    help="where to look for the manifests' wav files. "
                         "Defaults to this checkout, then the main checkout, "
                         "because the audio is untracked and lives there.")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "instrument_null_test.json"))
    args = ap.parse_args()

    AUDIO_ROOTS[:] = [r for r in (args.audio_root or []) if r] or [REPO]
    main_checkout = _main_checkout()
    if main_checkout and main_checkout not in AUDIO_ROOTS:
        AUDIO_ROOTS.append(main_checkout)
    print(f"audio roots: {AUDIO_ROOTS}")

    languages = dict(LANGUAGES)
    for override in args.manifest:
        lang, _, name = override.partition("=")
        if lang not in languages:
            raise SystemExit(f"unknown language {lang!r}")
        languages[lang] = name

    rng = random.Random(args.seed)
    result = {
        "scope": "human-vs-human null test: the ratio goals 2.5 and 2.6 "
                 "report when both sides are the same speaker's own "
                 "recordings and nothing was synthesised",
        "seed": args.seed, "trials_per_measure": args.trials,
        "clips_requested": args.limit, "languages": {},
    }
    read = []
    for lang, name in sorted(languages.items()):
        path = resolve(os.path.join("ab_test_runtime", "experiments", name))
        if not path or not os.path.exists(path):
            result["languages"][lang] = {"error": f"no manifest at {name}"}
            continue
        read.append(path)
        clips, dropped = collect(path, args.limit)
        print(f"{lang}: {len(clips)} human clips measured, {dropped} dropped",
              flush=True)
        # Refuse rather than report an empty language. A manifest whose paths
        # do not resolve drops every clip and would otherwise print a tidy
        # "0 trials" that reads like a measurement.
        if not clips and dropped:
            raise SystemExit(f"{lang}: every one of {dropped} clips was "
                             f"dropped - check the paths in {name}")
        entry = {"n_clips": len(clips), "dropped": dropped, "measures": {}}
        for m in MEASURES:
            entry["measures"][m] = summarise(
                split_ratios(clips, m, args.trials, rng), BANDS[m])
        result["languages"][lang] = entry

    result["read_inputs"] = read_inputs(read, REPO)
    result["status"] = "complete"
    result["provenance"] = provenance(__file__, args)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1, ensure_ascii=False)
    print(f"\nwrote {args.out}")

    for lang, entry in sorted(result["languages"].items()):
        if "measures" not in entry:
            continue
        print(f"\n{lang}  (n={entry['n_clips']} human clips, "
              f"same speaker, nothing synthesised)")
        print(f"  {'measure':14s} {'median':>8s} {'p5':>8s} {'p95':>8s} "
              f"{'band':>13s} {'outside':>9s}")
        for m in MEASURES:
            s = entry["measures"][m]
            if not s.get("trials"):
                continue
            band = f"{s['band'][0]}-{s['band'][1]}"
            print(f"  {m:14s} {s['median']:8.4f} {s['p5']:8.4f} "
                  f"{s['p95']:8.4f} {band:>13s} {s['outside_band_pct']:8.1f}%")


if __name__ == "__main__":
    main()
