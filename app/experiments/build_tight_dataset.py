"""Does CHOOSING tighter clips make a better voice? Build the two arms.

THE CLAIM UNDER TEST. Dataset tone mixture is the only variable that predicts
whether an adapter works - tightness against adapter ECAPA is r=0.58,
p=4.8e-08, n=74 (`dataset_tone_spread.json`), and it survives against vocal
tract length and f0, which ECAPA plays no part in. That is a CORRELATION over
datasets nobody chose. It does not say that choosing tighter clips produces a
better adapter, and the honest way to find out is to choose some and train.

THE CONTROL IS THE WHOLE DESIGN. Comparing a tight-selected set against the
shipped 200 would confound two things at once, because the shipped 200 are one
arbitrary volume and the tight set is drawn from the whole book. So both arms
are drawn from the SAME pool, at the SAME size, differing only in how the 200
were picked:

    control    200 clips drawn at random
    tight      the 200 clips nearest the pool centroid

and both are evaluated on ONE shared held-out set, reserved before either arm
is drawn so neither can have trained on it.

WHY THE POOL IS SAFE. A book's other volumes are only usable if they are the
same voice - Waking Gods has twelve narrators and its volumes are not. The
same-voice filter from `build_unseen_holdout` is imported rather than
reimplemented, so this cannot drift from the guard that was measured.

WHAT A NULL RESULT WOULD MEAN. If the tight arm does not win, tightness is a
symptom rather than a cause: loose datasets and bad adapters would share an
upstream reason (a difficult narrator, a noisy recording) without selection
being able to help. That is worth knowing for the cost of a few retrains, and
it is the outcome this is built to be able to show.
"""
import argparse
import json
import os
import random
import shutil
import sys
import zipfile

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.build_unseen_holdout import (  # noqa: E402
    read_metadata, key, volume_centroids, voice_similarity)
from experiments.provenance import provenance  # noqa: E402


def cached_clips(cache_path, stems):
    """-> {stem: (embeddings, clip filenames)} for the volumes we may use."""
    import pickle
    with open(cache_path, "rb") as fh:
        cache = pickle.load(fh)
    out = {}
    for k, v in cache.items():
        stem = os.path.basename(k)
        if stem in stems:
            out[stem] = (np.asarray(v[0], dtype=np.float64), list(v[1]))
    return out


def pool_from_book(trained_zip, source_dir, cache_path, min_similarity):
    """-> (pool, trained_stem, dropped) where pool is [(stem, clip, embedding)].

    Only volumes that are the same voice as the trained one contribute, and
    only clips the dedup run actually embedded, since an unembedded clip cannot
    be placed relative to the centroid.
    """
    trained = {key(r) for r, _ in read_metadata(trained_zip)}
    volumes = sorted(
        os.path.join(source_dir, n) for n in os.listdir(source_dir)
        if os.path.isfile(os.path.join(source_dir, n))
        and zipfile.is_zipfile(os.path.join(source_dir, n)))
    trained_stem = None
    for vol in volumes:
        if {key(r) for r, _ in read_metadata(vol)} & trained:
            trained_stem = os.path.splitext(os.path.basename(vol))[0]
            break
    if trained_stem is None:
        sys.exit(f"no source volume holds the clips of {trained_zip}")

    centroids = volume_centroids(cache_path)
    if trained_stem not in centroids:
        sys.exit(f"the trained volume {trained_stem} was never embedded")

    usable, dropped = [], []
    for vol in volumes:
        stem = os.path.splitext(os.path.basename(vol))[0]
        sim = voice_similarity(centroids, trained_stem, stem)
        if sim is None or sim < min_similarity:
            dropped.append((stem, sim))
            continue
        usable.append((stem, vol))

    cached = cached_clips(cache_path, {s for s, _ in usable})
    pool = []
    for stem, vol in usable:
        if stem not in cached:
            continue
        emb, names = cached[stem]
        emb = emb / np.clip(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9, None)
        for i, name in enumerate(names):
            if i < len(emb):
                pool.append((stem, vol, os.path.basename(str(name)), emb[i]))
    return pool, trained_stem, dropped


def write_arm(out_dir, chosen, held_out, texts):
    """Lay out one arm as train_lora expects: train/ and val/ with metadata."""
    for split, rows in (("train", chosen), ("val", held_out)):
        d = os.path.join(out_dir, split)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "metadata.jsonl"), "w", encoding="utf-8") as fh:
            for i, (stem, vol, clip, _) in enumerate(rows):
                name = f"{split}_{i:04d}.wav"
                with zipfile.ZipFile(vol) as zf:
                    member = next((n for n in zf.namelist()
                                   if n.endswith("/" + clip) or n == clip), None)
                    if member is None:
                        continue
                    with open(os.path.join(d, name), "wb") as out:
                        out.write(zf.read(member))
                fh.write(json.dumps({
                    "audio_filepath": f"{split}/{name}",
                    "text": texts.get((stem, clip), ""),
                    "source_volume": stem,
                }, ensure_ascii=False) + "\n")
    # The reference sample every arm speaks with: the clip nearest the centroid
    # of what that arm actually trained on, so neither arm is handed the other's
    # idea of the voice.
    first = chosen[0]
    with zipfile.ZipFile(first[1]) as zf:
        member = next((n for n in zf.namelist()
                       if n.endswith("/" + first[2]) or n == first[2]), None)
        if member:
            with open(os.path.join(out_dir, "ref.wav"), "wb") as out:
                out.write(zf.read(member))
    with open(os.path.join(out_dir, "ref_text.txt"), "w", encoding="utf-8") as fh:
        fh.write(texts.get((first[0], first[2]), ""))


def texts_for(pool):
    """-> {(stem, clip): text} read once per volume rather than per clip."""
    out, seen = {}, {}
    for stem, vol, clip, _ in pool:
        if stem not in seen:
            seen[stem] = {os.path.basename(r["audio_filepath"]): r.get("text", "")
                          for r, _ in read_metadata(vol)}
        out[(stem, clip)] = seen[stem].get(clip, "")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trained-zip", required=True)
    ap.add_argument("--source-dir", required=True)
    ap.add_argument("--embeddings", default=os.path.join(
        REPO, "dedup_analysis", "embeddings_cache.pkl"))
    ap.add_argument("--out", required=True, help="gains control/ and tight/")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--held-out", type=int, default=20)
    ap.add_argument("--min-voice-similarity", type=float, default=0.85)
    ap.add_argument("--seed", type=int, default=20260905)
    args = ap.parse_args()

    pool, trained_stem, dropped = pool_from_book(
        args.trained_zip, args.source_dir, args.embeddings,
        args.min_voice_similarity)
    need = args.n + args.held_out
    if len(pool) < need:
        sys.exit(f"pool holds {len(pool)} embedded clips, need {need}")

    rng = random.Random(args.seed)
    order = list(range(len(pool)))
    rng.shuffle(order)
    # Reserved FIRST, so neither arm can be drawn from it.
    held_idx = set(order[:args.held_out])
    rest = [i for i in order[args.held_out:]]

    control_idx = rest[:args.n]
    embs = np.array([pool[i][3] for i in rest])
    centroid = embs.mean(axis=0)
    centroid /= max(float(np.linalg.norm(centroid)), 1e-9)
    ranked = sorted(rest, key=lambda i: -float(pool[i][3] @ centroid))
    tight_idx = ranked[:args.n]

    texts = texts_for(pool)
    held = [pool[i] for i in sorted(held_idx)]
    arms = {}
    for name, idx in (("control", control_idx), ("tight", tight_idx)):
        d = os.path.join(args.out, name)
        if os.path.exists(d):
            shutil.rmtree(d)
        rows = [pool[i] for i in idx]
        write_arm(d, rows, held, texts)
        cs = np.array([pool[i][3] for i in idx])
        c = cs.mean(axis=0); c /= max(float(np.linalg.norm(c)), 1e-9)
        arms[name] = {
            "clips": len(rows),
            "tightness": round(float((cs @ c).mean()), 4),
            "volumes": len({r[0] for r in rows}),
        }

    doc = {
        "note": "Two arms from one pool: 200 random against the 200 nearest "
                "the centroid, on a shared held-out set reserved first.",
        "trained_volume": trained_stem,
        "pool_clips": len(pool),
        "volumes_dropped_wrong_voice": [s for s, _ in dropped],
        "held_out": len(held),
        "seed": args.seed,
        "arms": arms,
        "overlap_between_arms": len(set(control_idx) & set(tight_idx)),
    }
    doc["provenance"] = provenance(__file__, vars(args))
    with open(os.path.join(args.out, "arms.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)

    print(f"pool {len(pool)} clips, {len(dropped)} volumes dropped as a "
          f"different voice")
    for name, a in arms.items():
        print(f"  {name:8} {a['clips']} clips from {a['volumes']} volumes, "
              f"tightness {a['tightness']}")
    print(f"  arms share {doc['overlap_between_arms']} clips; held out "
          f"{len(held)} that neither trained on")


if __name__ == "__main__":
    main()
