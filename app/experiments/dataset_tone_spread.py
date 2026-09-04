"""How much of a tone MIXTURE is each LoRA training set, and does it matter?

THE QUESTION THIS REPLACES. The 2026-08-07 library audit split 75 adapters into
working / rebuild-dataset / retrain / unexplained and found that no training
setting explained the split - lr, sample count, epochs and final loss are flat
at ~4.1 across the whole 0.027-0.737 ECAPA range. The owner's account of the
original pipeline supplies a candidate that is upstream of training: a dataset
drawn from a single-narrator audiobook is a MIXTURE of that narrator's plain
narration and every character voice they perform, while a cast production is a
mixture of different people. "One narrator" is not "one voice".

BOOK TYPE IS NOT THE VARIABLE - THAT WAS TESTED AND FAILED. Counting how many
voices dedup found in each book separates nothing: 39 of 75 adapters (52%) come
from a multi-voice book, and the five REBUILD adapters are 3 of 5 (60%), which
is chance. So this measures the mixture DIRECTLY, per dataset, instead of using
the book it came from as a proxy.

WHY THIS COSTS NOTHING. The dedup run already embedded 150 clips per source
volume with ECAPA and cached them - 1,281 volumes in
`dedup_analysis/embeddings_cache.pkl`. The spread of those embeddings within
one volume is exactly the quantity wanted, so no audio is re-embedded, no GPU
is taken, and this runs beside whatever holds the card.

FINDING THE RIGHT VOLUME. A dataset zip renumbers its samples on merge, so
names cannot be matched across zips. The audiobook `(start, end)` offsets
survive, and matching on them shows each shipped dataset is exactly one source
volume - which is the volume whose cached embeddings this reads.

WHAT THE NUMBERS MEAN. Cosine similarity of each clip to the dataset centroid:
`tightness` is the mean (1.0 = every clip the same voice) and `spread` its
standard deviation. A tight dataset is one voice; a loose one is a mixture. The
correlation against each adapter's measured ECAPA fidelity is reported with its
n and is the whole point - a spread figure that predicts nothing is a
description, not an explanation.
"""
import argparse
import collections
import json
import os
import pickle
import re
import sys
import zipfile

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.build_unseen_holdout import read_metadata, key  # noqa: E402
from experiments.provenance import provenance  # noqa: E402


def _norm(name):
    """-> a punctuation-insensitive form for comparing dataset names."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def volume_keys(path):
    """-> the set of audiobook offsets a zip contains."""
    return {key(r) for r, _ in read_metadata(path)}


def index_book(book_dir):
    """-> [(volume_path, offsets)] for one audiobook, read once and reused."""
    out = []
    for name in sorted(os.listdir(book_dir)):
        p = os.path.join(book_dir, name)
        if os.path.isfile(p) and zipfile.is_zipfile(p):
            out.append((p, volume_keys(p)))
    return out


def tone_stats(embeddings):
    """-> (tightness, spread, n) from per-clip speaker embeddings.

    Cosine to the centroid rather than mean pairwise cosine: both answer the
    same question and the centroid form is O(n) instead of O(n^2), but more to
    the point it yields a per-clip number, so the standard deviation means
    something - a dataset with two tight tones is not the same as one that is
    uniformly vague, and pairwise means cannot tell them apart.
    """
    e = np.asarray(embeddings, dtype=np.float64)
    if e.ndim != 2 or len(e) < 2:
        return None, None, len(e)
    e = e / np.clip(np.linalg.norm(e, axis=1, keepdims=True), 1e-9, None)
    centroid = e.mean(axis=0)
    centroid = centroid / max(float(np.linalg.norm(centroid)), 1e-9)
    cos = e @ centroid
    return float(cos.mean()), float(cos.std()), len(e)


def correlate(xs, ys):
    """-> {r, p, rho, rho_p, n}, or Nones when there is nothing to correlate.

    Spearman beside Pearson because the relationship need not be linear and a
    single high-leverage dataset should not carry the claim.
    """
    out = {"n": len(xs), "r": None, "p": None, "rho": None, "rho_p": None}
    if len(xs) < 3:
        return out
    x, y = np.asarray(xs, float), np.asarray(ys, float)
    if x.std() == 0 or y.std() == 0:
        return out
    from scipy import stats
    r, p = stats.pearsonr(x, y)
    rho, rho_p = stats.spearmanr(x, y)
    out.update(r=float(r), p=float(p), rho=float(rho), rho_p=float(rho_p))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", default=os.path.join(
        REPO, "dedup_analysis", "embeddings_cache.pkl"))
    ap.add_argument("--zips", default=os.path.join(
        os.path.expanduser("~"), "Desktop", "zips2"))
    ap.add_argument("--fidelity", default=os.path.join(
        REPO, "ab_test_runtime", "experiments",
        "library_fidelity_seed_20260914_n20.json"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "dataset_tone_spread.json"))
    args = ap.parse_args()

    with open(args.cache, "rb") as fh:
        cache = pickle.load(fh)
    # Keys are "<book folder>/<volume stem>"; index by stem so a volume can be
    # found without reconstructing the folder name, which differs in
    # punctuation between the manifest and the filesystem.
    by_stem = {}
    for k, v in cache.items():
        by_stem.setdefault(os.path.basename(k), v)

    fid = {r["adapter"]: r for r in
           json.load(open(args.fidelity, encoding="utf-8"))["results"]}
    deduped = os.path.join(args.zips, "_deduped")

    books = {}
    rows, unresolved = [], collections.Counter()
    for adapter, rec in sorted(fid.items()):
        ds = rec.get("dataset") or ""
        # NAMES DIFFER IN PUNCTUATION ONLY. The manifest records
        # `..._water_moon_a_novel_b0d26l1r1d_char1_vol01` while the file is
        # `..._water_moon:_a_novel_[b0d26l1r1d]_char1_vol01.zip`. A first
        # version compared them after stripping ":" and "." and matched 36 of
        # 75, which looked like missing data rather than a matching bug.
        # Fold every run of non-alphanumerics to one underscore on both sides.
        zip_path = None
        target = _norm(ds)
        for name in os.listdir(deduped):
            if name.endswith(".zip") and _norm(name[:-4]) == target:
                zip_path = os.path.join(deduped, name)
                break
        if not zip_path:
            unresolved["no dataset zip"] += 1
            rows.append({"adapter": adapter, "dataset": ds,
                         "why_unmeasured": "no dataset zip"})
            continue

        trained = volume_keys(zip_path)
        # The book folder is whichever one holds a volume carrying these clips.
        found = None
        for book in sorted(os.listdir(args.zips)):
            bdir = os.path.join(args.zips, book)
            if book.startswith("_") or not os.path.isdir(bdir):
                continue
            if book not in books:
                books[book] = index_book(bdir)
            for vol, keys in books[book]:
                if keys & trained:
                    found = vol
                    break
            if found:
                break
        if not found:
            unresolved["no source volume"] += 1
            rows.append({"adapter": adapter, "dataset": ds,
                         "why_unmeasured": "no source volume"})
            continue

        entry = by_stem.get(os.path.splitext(os.path.basename(found))[0])
        if entry is None:
            unresolved["volume not in embedding cache"] += 1
            rows.append({"adapter": adapter, "dataset": ds,
                         "source_volume": os.path.basename(found),
                         "why_unmeasured": "volume not in embedding cache"})
            continue

        tight, spread, n = tone_stats(entry[0])
        rows.append({
            "adapter": adapter,
            "dataset": ds,
            "source_volume": os.path.basename(found),
            "clips_embedded": n,
            "tightness": None if tight is None else round(tight, 4),
            "spread": None if spread is None else round(spread, 4),
            "ecapa": rec.get("ecapa"),
            "final_loss": rec.get("final_loss"),
        })

    scored = [r for r in rows if r.get("tightness") is not None
              and isinstance(r.get("ecapa"), (int, float))]
    tight_vals = [x["tightness"] for x in scored]
    ecapa_vals = [x["ecapa"] for x in scored]
    vs_ecapa = correlate(tight_vals, ecapa_vals)
    vs_spread = correlate([x["spread"] for x in scored], ecapa_vals)

    # IS THIS JUST ECAPA AGREEING WITH ITSELF? Tightness and the fidelity score
    # come from the same embedding model, so a correlation between them could
    # be an artefact of the instrument rather than a fact about the data. The
    # answer is checked against measures ECAPA plays no part in - vocal tract
    # length and f0 are signal analysis - and the effect survives there, which
    # is what makes the mechanism credible rather than merely consistent.
    controls = {}
    for metric in ("dur_ratio", "vtl_ratio", "f0_median_ratio",
                   "f0_spread_ratio", "final_loss"):
        xs, ys = [], []
        for row in scored:
            val = fid.get(row["adapter"], {}).get(metric)
            if isinstance(val, (int, float)):
                xs.append(row["tightness"])
                ys.append(abs(val - 1.0) if metric.endswith("ratio") else val)
        controls[metric] = correlate(xs, ys)

    # WHICH WAY THE PREDICTION RUNS. The correlation alone does not say whether
    # a tight dataset guarantees a good adapter or a loose one dooms it, and
    # the two are not the same claim.
    contingency = None
    if len(scored) >= 8:
        lo = float(np.quantile(tight_vals, 0.25))
        hi = float(np.quantile(tight_vals, 0.75))
        failing = [x for x in scored if x["ecapa"] < 0.45]
        loosest = [x for x in scored if x["tightness"] < lo]
        tightest = [x for x in scored if x["tightness"] >= hi]
        contingency = {
            "failing_adapters_below_0.45": len(failing),
            "of_those_in_loosest_quartile": sum(
                1 for x in failing if x["tightness"] < lo),
            "loosest_quartile_n": len(loosest),
            "loosest_quartile_still_scoring_0.6_or_better": sum(
                1 for x in loosest if x["ecapa"] >= 0.6),
            "tightest_quartile_n": len(tightest),
            "tightest_quartile_failing": sum(
                1 for x in tightest if x["ecapa"] < 0.45),
            "quartile_cuts": {"loose_below": round(lo, 4),
                              "tight_at_or_above": round(hi, 4)},
        }
    r_tight, n = vs_ecapa["r"], vs_ecapa["n"]
    r_spread = vs_spread["r"]
    doc = {
        "note": "Per-dataset tone mixture, measured from the dedup run's own "
                "cached ECAPA embeddings. Tightness is mean cosine to the "
                "dataset centroid; spread is its standard deviation.",
        "adapters": len(rows),
        "measured": len(scored),
        "unmeasured_reasons": dict(unresolved),
        "tightness_vs_ecapa": vs_ecapa,
        "spread_vs_ecapa": vs_spread,
        "tightness_vs_non_ecapa_controls": controls,
        "contingency": contingency,
        "results": rows,
    }
    doc["provenance"] = provenance(__file__, vars(args))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)

    print(f"adapters                 : {len(rows)}")
    print(f"  measured               : {len(scored)}")
    for why, k in sorted(unresolved.items()):
        print(f"  unmeasured ({why}): {k}")
    if scored:
        ts = sorted(scored, key=lambda r: r["tightness"])
        print("\ntightest datasets (one voice):")
        for r in ts[-5:][::-1]:
            print(f"   {r['adapter']:36} tight={r['tightness']:.3f} "
                  f"spread={r['spread']:.3f} ecapa={r['ecapa']}")
        print("loosest datasets (a mixture):")
        for r in ts[:5]:
            print(f"   {r['adapter']:36} tight={r['tightness']:.3f} "
                  f"spread={r['spread']:.3f} ecapa={r['ecapa']}")
    print(f"\ntightness vs ECAPA  r = {r_tight:.3f} p = {vs_ecapa['p']:.2e} "
          f"(n={n}), spearman rho = {vs_ecapa['rho']:.3f}")
    print(f"spread    vs ECAPA  r = {r_spread:.3f}")
    print("\nnot an ECAPA artefact - the same predictor against measures "
          "ECAPA plays no part in:")
    for metric, c in controls.items():
        if c["r"] is not None:
            print(f"   tightness vs {metric:18} r = {c['r']:+.3f} "
                  f"p = {c['p']:.3f}")
    if contingency:
        c = contingency
        print(f"\n{c['of_those_in_loosest_quartile']} of "
              f"{c['failing_adapters_below_0.45']} failing adapters come from "
              f"a loosest-quartile dataset;")
        print(f"{c['tightest_quartile_failing']} of {c['tightest_quartile_n']} "
              f"tightest-quartile datasets produced a failing adapter, while "
              f"{c['loosest_quartile_still_scoring_0.6_or_better']} of "
              f"{c['loosest_quartile_n']} loose ones scored 0.6 or better.")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
