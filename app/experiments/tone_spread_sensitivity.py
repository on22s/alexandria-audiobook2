"""Does r=0.58 survive the eight adapters whose score is now known to be wrong?

WHAT IS BEING TESTED. `dataset_tone_spread` correlated each dataset's tone
tightness against its adapter's ECAPA, r=0.584 over 74 adapters, and that
number is the reason the whole selection line was worth running. Its ECAPA axis
comes from `library_fidelity_seed_20260914_n20.json`, which scores every
adapter on its own dataset's val split - splits that predate the same-voice
guard. Re-gating the nine sub-0.45 adapters on voice-verified clips moved eight
of them, two across the pass mark and five DOWNWARD.

THIS IS A SENSITIVITY TEST, NOT A CORRECTION, and the distinction is the whole
honesty of it. Only 9 of 75 adapters have been re-gated - the ones already
below 0.45 - so the other 66 keep a score from the same possibly-unstable
procedure. Substituting the eight tells us whether r is FRAGILE to the points
known to be wrong. It cannot tell us what r would be if every adapter were
re-measured; only re-gating all 75 does that, and that is a GPU job.

Three readings, so the answer cannot hide in one:
    as_published   the r this project currently quotes
    substituted    the eight corrected values swapped in
    dropped        the nine excluded entirely, since a re-gate that refused
                   (silky_baritone_45s_m) has no corrected value at all
"""
import argparse, glob, json, os, statistics, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.provenance import provenance  # noqa: E402


def corr(xs, ys):
    from scipy import stats
    if len(xs) < 4:
        return {"n": len(xs), "r": None, "p": None}
    r, p = stats.pearsonr(xs, ys)
    rho, rp = stats.spearmanr(xs, ys)
    return {"n": len(xs), "r": float(r), "p": float(p),
            "rho": float(rho), "rho_p": float(rp)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--spread", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "dataset_tone_spread.json"))
    ap.add_argument("--rescored", default=os.path.join(
        REPO, "ab_test_runtime", "experiments"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "tone_spread_sensitivity.json"))
    args = ap.parse_args()

    spread = json.load(open(args.spread, encoding="utf-8"))
    rows = [r for r in spread["results"]
            if r.get("tightness") is not None
            and isinstance(r.get("ecapa"), (int, float))]

    fixed = {}
    for f in glob.glob(os.path.join(args.rescored, "rescore_vf__*.json")):
        name = os.path.basename(f)[len("rescore_vf__"):-5]
        fixed[name] = json.load(open(f, encoding="utf-8"))["median_ecapa"]
    # An adapter that was re-gated but REFUSED has no corrected value, so it
    # cannot be substituted - only dropped. Naming it here keeps the two
    # readings honest about different populations.
    refused = ["silky_baritone_45s_m"]

    out = {
        "note": "Sensitivity of dataset_tone_spread's r to the eight adapters "
                "re-gated on voice-verified clips. NOT a correction: only 9 of "
                "75 adapters were re-gated, so the other 66 keep a score from "
                "the same procedure.",
        "adapters_rescored": len(fixed), "refused": refused,
    }

    t = [r["tightness"] for r in rows]
    e = [r["ecapa"] for r in rows]
    out["as_published"] = corr(t, e)

    e2 = [fixed.get(r["adapter"], r["ecapa"]) for r in rows]
    out["substituted"] = corr(t, e2)
    out["substituted_count"] = sum(1 for r in rows if r["adapter"] in fixed)

    keep = [r for r in rows if r["adapter"] not in fixed
            and r["adapter"] not in refused]
    out["dropped"] = corr([r["tightness"] for r in keep],
                          [r["ecapa"] for r in keep])

    moved = [(r["adapter"], r["ecapa"], fixed[r["adapter"]])
             for r in rows if r["adapter"] in fixed]
    out["moved"] = [{"adapter": a, "was": w, "now": n, "delta": n - w}
                    for a, w, n in sorted(moved, key=lambda x: x[1])]
    out["provenance"] = provenance(__file__, vars(args))
    json.dump(out, open(args.out, "w", encoding="utf-8"), indent=1)

    print(f"adapters in the correlation : {len(rows)}")
    print(f"scores substituted          : {out['substituted_count']}\n")
    for k in ("as_published", "substituted", "dropped"):
        c = out[k]
        if c["r"] is None:
            print(f"  {k:14} too few points"); continue
        print(f"  {k:14} r = {c['r']:+.3f}  p = {c['p']:.2e}  n = {c['n']}"
              f"   (rho {c['rho']:+.3f})")
    print("\nadapters whose score moved:")
    for m in out["moved"]:
        print(f"   {m['adapter']:34} {m['was']:.3f} -> {m['now']:.3f}  {m['delta']:+.3f}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
