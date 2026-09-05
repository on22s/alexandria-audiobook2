"""Does a tighter-selected dataset produce better-SOUNDING speech, not just
closer-matching speech?

TWO DIFFERENT QUESTIONS, AND ONLY ONE HAS EVER BEEN ASKED HERE. ECAPA asks
"does this sound like the target speaker" - similarity. Goal 2.6 asks whether
the output is good speech by the measures the field actually uses, and it is
open and unmeasured. Those come apart: an adapter can imitate a narrator
closely and still produce breathy, noisy or clipped audio, and a listener
hears the second.

THE DATA ALREADY EXISTS AND IS ABOUT TO BE DELETED. Every identity-gate run
left its generated clips beside the human clip each was scored against -
1,293 wavs across four selection arms, paired by index. They were a by-product
nobody looked at. The 43GB working tree they sit in is regenerable but only by
retraining every arm, so this measures them first.

WHAT IS MEASURED
    DNSMOS on the GENERATED clip - ovrl, sig, bak. The field's no-reference
        measure, the one Emilia-Pipe filters on.
    duration_ratio - generated length over the human clip it was paired with.
        Goal 2.4's axis, on this corpus for the first time.
    the six signal statistics, so generated and training audio are described
        the same way.

WHAT THIS CANNOT SAY. DNSMOS is trained on human speech degraded by noise and
codecs, not on synthesis artefacts, so a high score is not proof a listener
would accept the voice - goal 7.1 needs ears and this is not a substitute for
them. It can say whether the arms DIFFER, which is what a paired comparison
across four selection levels is for.
"""
import argparse, glob, json, os, statistics, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.arm_audio_statistics import (  # noqa: E402
    clip_stats, dnsmos_stats, FEATURES, DNSMOS_FEATURES)
from experiments.provenance import provenance  # noqa: E402


def duration(path):
    import soundfile as sf
    try:
        info = sf.info(path)
        return float(info.duration)
    except Exception:
        return None


def arm_rows(arm_dir, dnsmos):
    """-> per-clip rows for one arm's generated audio, paired to its human clip."""
    gen = sorted(glob.glob(os.path.join(arm_dir, "adapter", "identity_check",
                                        "check_*.wav")))
    rows = []
    for g in gen:
        i = int(os.path.basename(g)[len("check_"):-4])
        # write_arm names val clips val_0000.wav in the order the gate reads
        # them, so index i of the generated set is index i of the val set.
        human = os.path.join(arm_dir, "val", f"val_{i:04d}.wav")
        st = clip_stats(g)
        if not st:
            continue
        if dnsmos:
            d = dnsmos_stats(g)
            if d:
                st.update(d)
        dg, dh = duration(g), duration(human) if os.path.exists(human) else None
        st["duration_ratio"] = (dg / dh) if (dg and dh) else None
        rows.append(st)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", required=True)
    ap.add_argument("--arms", nargs="+",
                    default=["control", "middle", "half", "tight"])
    ap.add_argument("--dnsmos", action="store_true")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    keys = FEATURES + (DNSMOS_FEATURES if args.dnsmos else []) + ["duration_ratio"]
    books = []
    for tag in sorted(os.listdir(args.work)):
        per = {}
        for arm in args.arms:
            d = os.path.join(args.work, tag, arm)
            if not os.path.isdir(d):
                continue
            rows = arm_rows(d, args.dnsmos)
            if not rows:
                continue
            per[arm] = {k: statistics.mean(r[k] for r in rows if r.get(k) is not None)
                        for k in keys
                        if any(r.get(k) is not None for r in rows)}
            per[arm]["clips"] = len(rows)
        if len(per) >= 2:
            books.append({"tag": tag, "arms": per})
            print(f"  {len(books):3} {tag[:50]}", flush=True)

    doc = {"note": "DNSMOS, signal statistics and duration ratio on the audio "
                   "each adapter GENERATED during its identity gate, paired by "
                   "index with the human clip it was scored against. Goal 2.6 "
                   "asks whether the output is good speech; ECAPA only ever "
                   "asked whether it resembles the target.",
           "arms": args.arms, "books": len(books), "per_book": books}
    from scipy import stats
    comp = {}
    for k in keys:
        for a, b in zip(args.arms, args.arms[1:]):
            pairs = [(bk["arms"][a][k], bk["arms"][b][k]) for bk in books
                     if a in bk["arms"] and b in bk["arms"]
                     and k in bk["arms"][a] and k in bk["arms"][b]]
            if len(pairs) < 3:
                continue
            d = [y - x for x, y in pairs]
            if not any(d):
                continue
            comp[f"{k}:{a}->{b}"] = {
                "n": len(d), "mean_delta": statistics.mean(d),
                "wins": sum(1 for x in d if x > 0),
                "p": float(stats.wilcoxon(d).pvalue)}
    doc["adjacent_comparisons"] = comp
    doc["provenance"] = provenance(__file__, vars(args))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(doc, open(args.out, "w", encoding="utf-8"), indent=1)

    print(f"\n{len(books)} books, arms {' -> '.join(args.arms)}\n")
    print(f"{'measure':22}{'step':20}{'delta':>11}{'wins':>9}{'p':>10}")
    for k, c in comp.items():
        feat, step = k.split(":")
        print(f"{feat:22}{step:20}{c['mean_delta']:+11.4f}"
              f"{c['wins']:5}/{c['n']:<3}{c['p']:10.4f}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
