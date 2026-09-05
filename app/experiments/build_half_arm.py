"""Do 100 well-chosen clips beat 200 arbitrary ones?

WHAT IS CONFOUNDED TODAY. Every arm in the selection experiment holds exactly
200 clips, so size and selection have never been separated. The curve says
tighter is better (control 0.5226, middle 0.5467, tight 0.5776, and the second
half of the tightening carries 43 of 54 books at p=0.00002). It says nothing
about how much DATA that takes.

This arm holds the top 100 by tightness - half the clips, chosen twice as
strictly - and it makes two comparisons at once:

    half vs control   100 chosen against 200 arbitrary. If half wins or ties,
                      selection substitutes for data and dataset requirements
                      halve.
    half vs tight     100 against 200 at the same selection rule. This is the
                      cost of the missing 100 clips with tightness held as
                      constant as it can be, and it is the control that stops
                      a win being read as "less data is better".

WHY THE POOL MATTERS HERE. Achievable tightness is partly supply-limited -
pool size against tight-arm tightness is r=+0.265, p=0.048 over 56 books - so a
top-100 cut is strictly tighter than a top-200 cut from the same pool, and how
much tighter depends on the book. The realised tightness of every arm is
recorded rather than assumed.

Same pool, same seed, same held-out clips as the arms it is compared against.
"""
import argparse, json, os, random, sys
import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.build_tight_dataset import pool_from_book, texts_for, write_arm  # noqa: E402
from experiments.provenance import provenance  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trained-zip", required=True)
    ap.add_argument("--source-dir", required=True)
    ap.add_argument("--embeddings", default=os.path.join(
        REPO, "dedup_analysis", "embeddings_cache.pkl"))
    ap.add_argument("--out", required=True, help="the SAME directory the other arms use")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--full-n", type=int, default=200,
                    help="the size the other arms use; the draw must reproduce theirs")
    ap.add_argument("--held-out", type=int, default=20)
    ap.add_argument("--min-voice-similarity", type=float, default=0.85)
    ap.add_argument("--seed", type=int, default=20260905)
    args = ap.parse_args()

    existing = os.path.join(args.out, "arms.json")
    if not os.path.exists(existing):
        sys.exit(f"no prior build at {args.out}; this arm is only meaningful "
                 f"beside the arms it is compared against")
    prior = json.load(open(existing, encoding="utf-8"))
    if "half" in prior.get("arms", {}):
        print(f"SKIP {args.out} - half arm already built")
        return

    pool, trained_stem, _ = pool_from_book(
        args.trained_zip, args.source_dir, args.embeddings,
        args.min_voice_similarity)
    if len(pool) < args.full_n + args.held_out:
        sys.exit(f"pool holds {len(pool)} clips, need {args.full_n + args.held_out}")

    # Reproduce the original draw so the held-out clips are the same ones.
    rng = random.Random(args.seed)
    order = list(range(len(pool)))
    rng.shuffle(order)
    held_idx = order[:args.held_out]
    rest = order[args.held_out:]

    embs = np.array([pool[i][3] for i in rest])
    centroid = embs.mean(axis=0)
    centroid /= max(float(np.linalg.norm(centroid)), 1e-9)
    cos = {i: float(pool[i][3] @ centroid) for i in rest}
    ranked = sorted(rest, key=lambda i: -cos[i])
    half_idx = ranked[:args.n]

    rows = [pool[i] for i in half_idx]
    write_arm(os.path.join(args.out, "half"), rows,
              [pool[i] for i in sorted(held_idx)], texts_for(pool))
    # ONE DEFINITION OF TIGHTNESS ACROSS EVERY ARM. build_tight_dataset
    # measures cosine to the ARM's own centroid; measuring this one against the
    # POOL centroid put the two on different scales and made a top-100 cut look
    # LOOSER than the top-200 it is a subset of (0.9151 against 0.9176), which
    # is impossible. On the shared definition it is 0.9224 against 0.9176.
    he = np.array([pool[i][3] for i in half_idx])
    hc = he.mean(axis=0); hc /= max(float(np.linalg.norm(hc)), 1e-9)
    h = he @ hc
    prior.setdefault("arms", {})["half"] = {
        "clips": len(rows),
        "tightness": round(float(h.mean()), 4),
        "volumes": len({r[0] for r in rows}),
        "note": "top %d by tightness; half the clips of the other arms" % args.n,
    }
    prior["half_provenance"] = provenance(__file__, vars(args))
    with open(existing, "w", encoding="utf-8") as fh:
        json.dump(prior, fh, ensure_ascii=False, indent=1)
    a = prior["arms"]
    print(f"control {a['control']['tightness']} (200)   "
          f"tight {a['tight']['tightness']} (200)   "
          f"half {a['half']['tightness']} ({args.n})")


if __name__ == "__main__":
    main()
