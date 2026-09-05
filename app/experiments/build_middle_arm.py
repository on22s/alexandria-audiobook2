"""A third point on the curve: is the gain a threshold, or does more help more?

WHAT THE TWO-ARM RESULT LEFT OPEN. Selecting the 200 clips nearest a pool's
centroid beat 200 drawn at random on 34 of 52 books, mean +0.056, Wilcoxon
p=0.0007. So selection works. But how MUCH tighter the selected arm was does
not predict how much it won by - r=-0.011, p=0.94 across the same 52 books -
and two different mechanisms produce that:

    threshold   getting off the worst clips is the entire gain, and tightening
                further adds nothing. Predicts middle ~= tight > control.
    monotonic   more tightness is more better, and the flat dose-response is
                just noise at n=52. Predicts tight > middle > control.

They are indistinguishable with two arms because control and tight are the
extremes. A third arm at intermediate tightness separates them.

HOW THE MIDDLE IS PLACED. Not "the middle of the ranking" - that would sit a
hair above the random arm, since the tight arm is the top 2.6% of a pool of
thousands and the distribution is dense in the middle. The target is the
MIDPOINT IN TIGHTNESS between the two existing arms, and the 200 clips whose
cosine to the centroid lands nearest that value. So the three arms are roughly
evenly spaced on the axis the hypothesis is about, which is what makes a
curve readable.

IT REBUILDS THE SAME POOL AND THE SAME HELD-OUT SET. Given the same seed the
draw is reproducible, so the middle arm is scored on exactly the clips the
other two were, and never on anything any arm trained on. Existing arm
directories are left untouched: their adapters are already trained.
"""
import argparse
import json
import os
import random
import sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.build_tight_dataset import (  # noqa: E402
    pool_from_book, texts_for, write_arm)
from experiments.provenance import provenance  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trained-zip", required=True)
    ap.add_argument("--source-dir", required=True)
    ap.add_argument("--embeddings", default=os.path.join(
        REPO, "dedup_analysis", "embeddings_cache.pkl"))
    ap.add_argument("--out", required=True,
                    help="the SAME directory the two-arm build used")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--held-out", type=int, default=20)
    ap.add_argument("--min-voice-similarity", type=float, default=0.85)
    ap.add_argument("--seed", type=int, default=20260905)
    args = ap.parse_args()

    existing = os.path.join(args.out, "arms.json")
    if not os.path.exists(existing):
        sys.exit(f"no two-arm build at {args.out}; the middle arm is only "
                 f"meaningful beside the control and tight arms it is placed "
                 f"between")
    prior = json.load(open(existing, encoding="utf-8"))
    if "middle" in prior.get("arms", {}):
        print(f"SKIP {args.out} - middle arm already built")
        return

    pool, trained_stem, dropped = pool_from_book(
        args.trained_zip, args.source_dir, args.embeddings,
        args.min_voice_similarity)
    need = args.n + args.held_out
    if len(pool) < need:
        sys.exit(f"pool holds {len(pool)} embedded clips, need {need}")

    # Reproduce the original draw exactly: same seed, same shuffle, so the
    # held-out clips are the ones the other two arms were scored on.
    rng = random.Random(args.seed)
    order = list(range(len(pool)))
    rng.shuffle(order)
    held_idx = order[:args.held_out]
    rest = order[args.held_out:]

    embs = np.array([pool[i][3] for i in rest])
    centroid = embs.mean(axis=0)
    centroid /= max(float(np.linalg.norm(centroid)), 1e-9)
    cos = {i: float(pool[i][3] @ centroid) for i in rest}

    control_idx = rest[:args.n]
    ranked = sorted(rest, key=lambda i: -cos[i])
    tight_idx = ranked[:args.n]

    c_mean = float(np.mean([cos[i] for i in control_idx]))
    t_mean = float(np.mean([cos[i] for i in tight_idx]))
    target = (c_mean + t_mean) / 2.0
    # The n clips whose tightness sits nearest the midpoint between the two
    # existing arms - evenly spaced on the axis the hypothesis is about.
    middle_idx = sorted(rest, key=lambda i: abs(cos[i] - target))[:args.n]

    held = [pool[i] for i in sorted(held_idx)]
    texts = texts_for(pool)
    rows = [pool[i] for i in middle_idx]
    write_arm(os.path.join(args.out, "middle"), rows, held, texts)

    m_cos = np.array([cos[i] for i in middle_idx])
    prior.setdefault("arms", {})["middle"] = {
        "clips": len(rows),
        "tightness": round(float(m_cos.mean()), 4),
        "volumes": len({r[0] for r in rows}),
        "target_midpoint": round(target, 4),
        "control_mean_cos": round(c_mean, 4),
        "tight_mean_cos": round(t_mean, 4),
    }
    prior["overlap_middle_tight"] = len(set(middle_idx) & set(tight_idx))
    prior["overlap_middle_control"] = len(set(middle_idx) & set(control_idx))
    prior["middle_provenance"] = provenance(__file__, vars(args))
    with open(existing, "w", encoding="utf-8") as fh:
        json.dump(prior, fh, ensure_ascii=False, indent=1)

    a = prior["arms"]
    print(f"control {a['control']['tightness']}  "
          f"middle {a['middle']['tightness']}  "
          f"tight {a['tight']['tightness']}   "
          f"(target {target:.4f})")
    print(f"  middle shares {prior['overlap_middle_tight']} clips with tight, "
          f"{prior['overlap_middle_control']} with control")


if __name__ == "__main__":
    main()
