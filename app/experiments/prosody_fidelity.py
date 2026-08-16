"""Measure the "sounds off" a non-speaker of the language cannot report.

WHY THIS EXISTS, AND WHY IT IS NOT THE SAME AS voice_quality. The listener on
this project speaks English. For Japanese and Chinese the most that can
honestly be reported is "it sounds off" - and the two languages are off in
different ways that neither jitter nor an embedding distance can see:

    JAPANESE has lexical PITCH ACCENT. 箸 and 橋 differ only in where the
    pitch falls. A synthetic voice with the wrong accent is not merely less
    pleasant, it says a different word - and CER cannot notice, because the
    characters it transcribes back are the same ones.

    CHINESE has TONE, carried almost entirely in f0. Wrong tone is wrong
    word, again invisible to a metric that compares text.

Both reduce to the same measurement: the f0 contour of the generated take
against a human reading the same line, aligned in time first. That is not a
metric this project invented - the Japanese accent-evaluation literature
scores exactly this (pitch correlation in semitones after DTW), and the
Chinese TTS evaluation literature reports F0 RMSE and Gross Pitch Error after
DTW as the standard prosody pair. Found by searching in Japanese and Chinese
rather than English, which is the only reason they turned up at all.

SEMITONES, NOT HERTZ. A 20 Hz error is a different amount of "wrong" on a
100 Hz voice than a 250 Hz one. Semitones make the measure comparable across
speakers, which is the whole point of using it on voices we cannot audition.

RHYTHM COMES FREE FROM THE ALIGNMENT. The DTW path's slope is the local
speaking-rate ratio; its variance is how unevenly the generated take is
paced. In the Japanese study that measure scored the HIGHEST of the three
(AUC 0.877), beating both embedding distance and pitch correlation - so it is
reported here rather than treated as a by-product.

NOT A REPLACEMENT FOR A LISTENER. It is what can be measured when no listener
of that language is available. Where one is available, the ratings win.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Standard in the prosody literature: a frame is a GROSS pitch error when it
# is off by more than 20%, which is roughly a musical third - audible as a
# wrong note rather than a slightly flat one.
GPE_TOLERANCE = 0.20


def semitones(hz, reference=100.0):
    import numpy as np
    return 12.0 * np.log2(np.asarray(hz, dtype="float64") / reference)


def compare(human_path, generated_path, sr=22050):
    """-> prosody agreement between a human take and a generated one."""
    import numpy as np
    from voice_compare_view import load_audio, f0_contour, alignment_path

    human, _ = load_audio(human_path, sr)
    generated, _ = load_audio(generated_path, sr)
    ht, hf = f0_contour(human, sr)
    gt, gf = f0_contour(generated, sr)
    if hf is None or gf is None:
        return {"error": "no f0 contour"}

    hx, gx = alignment_path(human_path, generated_path, sr)
    # Sample both contours on the shared DTW timeline, so frame i of each is
    # the same moment of the same sentence.
    hp = np.interp(hx, ht, hf, left=np.nan, right=np.nan)
    gp = np.interp(gx, gt, gf, left=np.nan, right=np.nan)
    both = ~np.isnan(hp) & ~np.isnan(gp)
    voiced = int(both.sum())
    result = {"voiced_frames": voiced,
              "human_seconds": round(float(len(human) / sr), 3),
              "generated_seconds": round(float(len(generated) / sr), 3)}
    if voiced >= 10:
        hs, gs = semitones(hp[both]), semitones(gp[both])
        # Centre both: we are asking whether the melody MOVES the same way,
        # not whether the voices sit at the same pitch. A correct reading an
        # octave down is right about accent and tone.
        hc, gc = hs - hs.mean(), gs - gs.mean()
        rmse = float(np.sqrt(np.mean((hc - gc) ** 2)))
        gross = float(np.mean(np.abs(hp[both] - gp[both]) / hp[both] > GPE_TOLERANCE))
        corr = (float(np.corrcoef(hc, gc)[0, 1])
                if hc.std() > 1e-9 and gc.std() > 1e-9 else None)
        result.update({
            "f0_correlation": None if corr is None else round(corr, 4),
            "f0_rmse_semitones": round(rmse, 4),
            "gross_pitch_error": round(gross, 4),
        })
    # Rhythm: how steadily the generated take keeps pace with the human one.
    if len(hx) > 3:
        dh, dg = np.diff(hx), np.diff(gx)
        keep = dh > 1e-9
        if keep.sum() > 3:
            slope = dg[keep] / dh[keep]
            result["tempo_irregularity"] = round(float(np.std(slope)), 4)
            result["tempo_ratio"] = round(float(np.median(slope)), 4)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--generated", required=True,
                    help="an *_generate.json artifact: rows of human/arm pairs")
    ap.add_argument("--arm", default=None, help="which arm (default: all)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "prosody_fidelity.json"))
    args = ap.parse_args()

    with open(args.generated, encoding="utf-8") as handle:
        doc = json.load(handle)
    rows = doc.get("rows") or []
    arms = [args.arm] if args.arm else list(doc.get("arms") or [])
    if not rows or not arms:
        sys.exit("artifact has no rows/arms")

    results = {}
    for arm in arms:
        scored = []
        for row in rows[:args.limit]:
            human = row.get("human_wav")
            generated = (row.get(arm) or {}).get("wav") if isinstance(
                row.get(arm), dict) else row.get(f"{arm}_wav")
            if not human or not generated:
                continue
            hp = os.path.join(REPO, human)
            gp = os.path.join(REPO, generated)
            if not (os.path.exists(hp) and os.path.exists(gp)):
                continue
            scored.append({"id": row.get("id"), **compare(hp, gp)})
        if not scored:
            continue
        import statistics
        def mean_of(key):
            vals = [s[key] for s in scored if isinstance(s.get(key), (int, float))]
            return round(statistics.mean(vals), 4) if vals else None
        results[arm] = {
            "n": len(scored),
            "f0_correlation_mean": mean_of("f0_correlation"),
            "f0_rmse_semitones_mean": mean_of("f0_rmse_semitones"),
            "gross_pitch_error_mean": mean_of("gross_pitch_error"),
            "tempo_irregularity_mean": mean_of("tempo_irregularity"),
            "rows": scored,
        }
        r = results[arm]
        print(f"  {arm:16} n={r['n']:3}  f0 corr {r['f0_correlation_mean']}  "
              f"rmse {r['f0_rmse_semitones_mean']} st  "
              f"GPE {r['gross_pitch_error_mean']}  "
              f"tempo irreg {r['tempo_irregularity_mean']}")
    if not results:
        sys.exit("no human/generated pair could be resolved from that artifact")

    document = {"source": os.path.relpath(args.generated, REPO),
                "language": doc.get("language"),
                "gpe_tolerance": GPE_TOLERANCE, "results": results}
    try:
        from experiments.provenance import provenance
        document["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        document["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    from utils import atomic_json_write
    atomic_json_write(document, args.out)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
