"""Build an evaluation set from clips NEITHER adapter in a contamination pair ever saw.

THE BLOCKER THIS REMOVES. Goal 2.7 has six adapters that passed their identity
gate and were refused only for not beating their shipped score - a score
measured on clips the shipped adapter trained on. GOALS.md recorded that
settling it "needs clips neither adapter ever saw" and that "no such data
exists today". That was wrong, and cheaply checkable.

Every dataset zip is a 200-clip slice of an audiobook that was segmented into
dozens of 200-clip volumes. Matching the trained clips back by their absolute
(start, end) offsets shows the whole shipped library was built from ONE volume
per narrator: `narrator_ralph_lister_gardens_of_the_moon_char1_vol01` is all
200 clips of source volume 15, and 51 further volumes of the same narrator have
never been used by anything. This script assembles held-out sets from them.

WHY THE WHOLE SOURCE VOLUME IS EXCLUDED, not just the matched clips. If any
clip of a volume was trained on, this drops the entire volume. The adapter saw
that volume's 200 clips, and a neighbouring clip from the same passage is close
enough - same scene, same emotional register, sometimes a sentence continued -
that calling it unseen would overstate the case. Losing 200 clips out of 10,400
costs nothing.

HOW A DIFFERENT NARRATOR IS KEPT OUT, AND WHY COUNTING CLUSTERS DID NOT DO IT.
An unseen volume is only usable if it is the same VOICE, not merely the same
book. The first version of this guard counted how many datasets dedup produced
for the book and refused above one. That number tracks how many CONTIGUOUS
per-actor blocks the chunking happened to produce, not how many people are in
the recording, and the two come apart exactly where it matters:

    book                       within-vol  between-vol    truth
    Gardens of the Moon           0.824       0.965       1 narrator
    Dracula [Audible Edition]     0.700       0.558       9-voice cast, caught
    Waking Gods ("Various")       0.528       0.619       cast, PASSED as 1 voice
    86-- (two named narrators)    0.728       0.745       ambiguous, PASSED

Dracula was caught only because it is epistolary - long contiguous stretches
are one actor, so volumes cluster. A cast whose actors alternate puts all of
them in every volume, the volumes then resemble each other, and cluster
counting waves it through. Waking Gods did exactly that, and 16 of its 17
volumes turned out to sit at 0.326-0.859 similarity to the trained one.

So the guard now asks the question directly, per pair: how similar is this
candidate volume to the volume the adapter trained on? Volumes below
--min-voice-similarity are dropped from the pool and the figure is recorded for
every one, so a reader can re-judge the threshold rather than trust it.

THE THRESHOLD IS CALIBRATED, NOT CHOSEN. Across the six books measured, the
clean ones put every sibling volume at 0.919-0.992 against the trained volume,
while the two contaminated ones reach down to 0.326 and 0.793. 0.85 sits in the
empty band between, the same way the identity gate's 0.45 does. It is a
6-book calibration and should be revisited when a seventh disagrees.

WHAT THIS IS NOT. Character identity is not established here. The clips carry
speaker "UNKNOWN" and the dedup heatmap for Gardens of the Moon puts all 52
volumes at 0.59-0.75 with no cluster structure - one narrator performing every
character. So a held-out sample is drawn from the SAME mixture of narration and
character voices as the training slice, which is what makes it the right
comparison, but it is a narrator-level holdout and must not be described as a
per-character one.
"""
import argparse
import json
import os
import pickle
import random
import shutil
import re
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402


def _rows(zf, name):
    out = []
    for line in zf.read(name).decode("utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def read_metadata(path):
    """-> [(row, member_name)] for every metadata.jsonl in a dataset zip."""
    rows = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith("metadata.jsonl"):
                rows += [(r, name) for r in _rows(zf, name)]
    return rows


def key(row):
    """A clip's identity in the source audiobook, independent of file naming.

    Sample numbers are reassigned when a dataset is merged, so they cannot
    match across zips. The (start, end) offsets are the audiobook's own
    timeline and survive every repackaging.
    """
    return (round(float(row["start"]), 2), round(float(row["end"]), 2))


def voices_in_book(trained_zip):
    """-> how many datasets dedup produced for this audiobook.

    RECORDED, NOT USED AS A GATE. It counts contiguous per-actor blocks rather
    than people - see the module docstring for the two books it waves through -
    so it is kept as context on the artifact and nothing is refused on it.
    """
    ded = os.path.dirname(os.path.abspath(trained_zip))
    stem = re.sub(r"_char\d+_vol\d+\.zip$", "",
                  os.path.basename(trained_zip))
    if stem == os.path.basename(trained_zip):
        return 1
    return sum(1 for n in os.listdir(ded)
               if n.endswith(".zip")
               and re.sub(r"_char\d+_vol\d+\.zip$", "", n) == stem)


def volume_centroids(cache_path):
    """-> {volume stem: unit centroid} from the dedup run's cached embeddings.

    Keyed by stem alone because the cache spells book folders the way the
    filesystem does, while manifests spell them with different punctuation.
    """
    import numpy as np
    with open(cache_path, "rb") as fh:
        cache = pickle.load(fh)
    out = {}
    for k, v in cache.items():
        e = np.asarray(v[0], dtype=np.float64)
        if e.ndim != 2 or not len(e):
            continue
        e = e / np.clip(np.linalg.norm(e, axis=1, keepdims=True), 1e-9, None)
        c = e.mean(axis=0)
        n = float(np.linalg.norm(c))
        if n > 0:
            out.setdefault(os.path.basename(k), c / n)
    return out


def voice_similarity(centroids, trained_stem, candidate_stem):
    """-> cosine between two volumes' voices, or None if either is unknown.

    None is not zero. A volume the dedup run never embedded cannot be judged,
    and the caller must refuse it rather than treat 'unknown' as 'different'
    or as 'same'.
    """
    a, b = centroids.get(trained_stem), centroids.get(candidate_stem)
    if a is None or b is None:
        return None
    return float(a @ b)


def verify_clip_voices(candidates, trained_zip, work, threshold, anchors,
                       python_bin):
    """-> [(candidate, mean cosine to the trained voice)] for clips that pass.

    WHY A CLIP-LEVEL CHECK EXISTS AT ALL. The volume-level guard above keeps a
    VOLUME whose centroid resembles the trained one, but a volume of a
    multi-voice book contains every character in it, and the deduped character
    zips exist for vol01 ONLY - all 75 of them - so no later volume carries a
    character label to filter on. Measured over 68 holdouts and 1,360 clips:
    10.7% of held-out clips are a different person, 28 of 68 holdouts carry at
    least one, and char2-or-higher adapters average 22.3% against 5.9% for
    char1. `crisp_mezzo_30s_f` was 7 of 20, which is how one adapter came to
    read 0.132 on this holdout and 0.552 on another.

    WHY ECAPA AND NOT THE CACHED EMBEDDINGS. Four features built from the dedup
    cache were scored against these 1,360 ECAPA-labelled clips and every one of
    them is near chance - centroid AUC 0.599, nearest-trained-clip 0.562, top-5
    0.583, top-20 0.606. The cache cannot tell two characters of one book
    apart, so a cheap filter built on it would have looked principled and
    filtered nothing. It is not used.

    THE THRESHOLD IS THE TROUGH OF A BIMODAL DISTRIBUTION, not a round number:
    those 1,360 clips form one mode at 0.00-0.20 and another peaking at 0.75,
    with the sparsest band at 0.25-0.30. An earlier cut at 0.45 sat on the
    rising edge of the REAL mode and called 62 good clips foreign.

    NO SILENT FALLBACK. Without the speechbrain interpreter this returns None
    and the caller refuses. A verifier that waves clips through when it cannot
    run is the shape that turns a configuration error into a corpus of
    believable non-results.
    """
    if not candidates:
        return []
    if not python_bin or not os.path.exists(python_bin):
        return None
    os.makedirs(work, exist_ok=True)
    refs = []
    with zipfile.ZipFile(trained_zip) as zf:
        wavs = sorted(n for n in zf.namelist() if n.endswith(".wav"))
        for i, member in enumerate(wavs[:anchors]):
            dest = os.path.join(work, "anchor_%02d.wav" % i)
            with open(dest, "wb") as fh:
                fh.write(zf.read(member))
            refs.append(dest)
    if not refs:
        return None
    paths = [c[0] for c in candidates]
    pairs = [(r, pth) for pth in paths for r in refs]
    cos, err = _ecapa(pairs, python_bin)
    if cos is None:
        return None
    kept = []
    for i, cand in enumerate(candidates):
        window = [x for x in cos[i * len(refs):(i + 1) * len(refs)]
                  if x is not None]
        if not window:
            continue
        score = sum(window) / len(window)
        if score >= threshold:
            kept.append((cand, score))
    return kept


def _ecapa(pairs, python_bin):
    """Thin seam over library_voice_fidelity.ecapa_pairs, so tests can stub it."""
    from library_voice_fidelity import ecapa_pairs
    cos, err = ecapa_pairs(pairs, python_bin)
    if not cos or all(x is None for x in cos):
        return None, err
    return cos, err


def build(trained_zip, source_dir, out_dir, lines, seed,
          embeddings=None, min_voice_similarity=0.85,
          min_clip_voice=0.30, clip_anchors=2, oversample=3,
          min_lines=12,
          sibling_python=None, verify_clips=True):
    voices = voices_in_book(trained_zip)
    centroids = volume_centroids(embeddings) if embeddings else {}
    if not centroids:
        sys.exit("no cached volume embeddings, so no candidate volume can be "
                 "shown to be the same voice as the trained one. Pass "
                 "--embeddings; refusing rather than guessing.")
    trained = {key(r) for r, _ in read_metadata(trained_zip)}
    if not trained:
        sys.exit(f"no clips found in {trained_zip}")

    volumes = sorted(
        os.path.join(source_dir, n) for n in os.listdir(source_dir)
        if os.path.isfile(os.path.join(source_dir, n))
        and zipfile.is_zipfile(os.path.join(source_dir, n)))
    if not volumes:
        sys.exit(f"no source volumes under {source_dir}")

    contributing, pool = [], []
    seen_keys = set()
    similarities, wrong_voice, unjudged = {}, [], []
    trained_stem = None
    for vol in volumes:
        rows = read_metadata(vol)
        keys = {key(r) for r, _ in rows}
        if keys & trained:
            contributing.append((os.path.basename(vol), len(keys & trained)))
            trained_stem = os.path.splitext(os.path.basename(vol))[0]
            continue
    if trained_stem is None:
        sys.exit(f"no source volume under {source_dir} contains the clips in "
                 f"{os.path.basename(trained_zip)}")
    if trained_stem not in centroids:
        sys.exit(f"the trained volume {trained_stem} was never embedded, so "
                 f"no candidate can be compared against it")

    for vol in volumes:
        stem = os.path.splitext(os.path.basename(vol))[0]
        if stem == trained_stem:
            continue
        rows = read_metadata(vol)
        sim = voice_similarity(centroids, trained_stem, stem)
        similarities[os.path.basename(vol)] = sim
        if sim is None:
            unjudged.append(os.path.basename(vol))
            continue
        if sim < min_voice_similarity:
            wrong_voice.append(os.path.basename(vol))
            continue
        # A zip carries metadata.jsonl at the root AND inside train/ and val/,
        # so every clip is listed more than once. Deduplicating on the
        # audiobook offset keeps one entry per clip; without it the same clip
        # can be drawn twice and counted as two independent measurements.
        for r, member in rows:
            k = key(r)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            pool.append((vol, member, r))

    matched = sum(n for _, n in contributing)
    if matched < len(trained):
        # Fail loud: an unmatched trained clip means some volume holding it was
        # not seen here, and its clips would be sitting in the "unseen" pool.
        sys.exit(f"only {matched} of {len(trained)} trained clips were traced "
                 f"to a source volume; refusing to build a holdout that may "
                 f"contain training data")

    rng = random.Random(seed)
    rng.shuffle(pool)
    # THE SHUFFLE IS LOAD-BEARING and is pinned by a test. Downstream, the
    # identity gate reads the FIRST `--lines` entries of the file this writes,
    # so a written order that reflected volume or offset would hand it a
    # systematically skewed sample rather than a random one.
    picked_scores, rejected_wrong_voice, unjudged_clips = [], 0, 0
    candidates = []
    if not verify_clips:
        picked = pool[:lines]
    else:
        # Oversample, verify, keep the first `lines` survivors. Verifying only
        # `lines` candidates would silently return a short holdout whenever a
        # book is contaminated, which is exactly the case this exists for.
        work = os.path.join(out_dir, "_voice_check")
        candidates = pool[:lines * max(1, oversample)]
        staged = []
        for i, (vol, member, row) in enumerate(candidates):
            wav_member = os.path.join(os.path.dirname(member),
                                      os.path.basename(row["audio_filepath"]))
            wav_member = wav_member.replace(os.sep, "/")
            try:
                with zipfile.ZipFile(vol) as zf:
                    try:
                        data = zf.read(wav_member)
                    except KeyError:
                        cand = [n for n in zf.namelist()
                                if n.endswith(os.path.basename(row["audio_filepath"]))]
                        if not cand:
                            continue
                        data = zf.read(cand[0])
            except (KeyError, OSError):
                continue
            os.makedirs(work, exist_ok=True)
            path = os.path.join(work, "cand_%03d.wav" % i)
            with open(path, "wb") as fh:
                fh.write(data)
            staged.append((path, (vol, member, row)))
        kept = verify_clip_voices(staged, trained_zip, work, min_clip_voice,
                                  clip_anchors, sibling_python)
        if kept is None:
            sys.exit("clip-level voice check could not run (no speechbrain "
                     "interpreter, or no anchor clips in the trained zip). "
                     "Refusing to write a holdout that may hold another "
                     "character - pass --sibling-python, or --no-verify-clips "
                     "to build one deliberately unverified.")
        picked = []
        for (path, entry), score in kept:
            if len(picked) >= lines:
                break
            picked.append(entry)
            picked_scores.append(round(float(score), 4))
        rejected_wrong_voice = len(staged) - len(kept)
        unjudged_clips = len(candidates) - len(staged)
        shutil.rmtree(work, ignore_errors=True)
    if not picked:
        # NAME THE CAUSE. These are two different failures and the fix differs:
        # an empty pool means the volume guard excluded everything, while an
        # empty result from a non-empty pool means every candidate was judged
        # to be another character. A single message sent readers to the wrong
        # one.
        if pool and verify_clips:
            sys.exit(f"every one of {len(candidates)} candidate clips scored "
                     f"below --min-clip-voice {min_clip_voice}: none of them "
                     f"is the trained voice. Refusing to write an empty "
                     f"holdout.")
        sys.exit("no unseen clips remain after excluding contributing volumes")

    # A SHORT HOLDOUT IS NOT A SMALL PROBLEM, IT IS A DIFFERENT MEASUREMENT.
    # Empty was refused from the start; short was not, and on the first real
    # run `velvety_mezzo_30s_f_gothic` rejected 54 of 60 candidates - that book
    # is about 90% another voice - and quietly wrote SIX clips. The gate then
    # scored it without complaint and its median came from 6 lines rather than
    # the 12 every other adapter used, which is not comparable to them and was
    # visible nowhere in the output. Refusing here is the same rule the empty
    # case already follows; the only reason it was not applied is that nobody
    # had yet seen a book contaminated badly enough to produce it.
    if verify_clips and len(picked) < lines:
        shortfall = lines - len(picked)
        if len(picked) < min_lines:
            sys.exit(
                f"only {len(picked)} of {lines} clips survived the voice "
                f"check ({rejected_wrong_voice} of {len(candidates)} "
                f"candidates were another character). A holdout this short is "
                f"not comparable to a full one - its median comes from fewer "
                f"draws. Raise --oversample to verify more candidates, or "
                f"lower --min-lines to accept it deliberately.")
        print(f"WARNING: {len(picked)} of {lines} clips, {shortfall} short - "
              f"{rejected_wrong_voice} of {len(candidates)} candidates were "
              f"another character", file=sys.stderr)

    val = os.path.join(out_dir, "val")
    os.makedirs(val, exist_ok=True)
    meta_path = os.path.join(val, "metadata.jsonl")
    written = []
    with open(meta_path, "w", encoding="utf-8") as fh:
        for i, (vol, member, row) in enumerate(picked):
            wav_member = os.path.join(os.path.dirname(member),
                                      os.path.basename(row["audio_filepath"]))
            wav_member = wav_member.replace(os.sep, "/")
            with zipfile.ZipFile(vol) as zf:
                try:
                    data = zf.read(wav_member)
                except KeyError:
                    cand = [n for n in zf.namelist()
                            if n.endswith(os.path.basename(row["audio_filepath"]))]
                    if not cand:
                        continue
                    data = zf.read(cand[0])
            name = f"unseen_{i:03d}.wav"
            with open(os.path.join(val, name), "wb") as out:
                out.write(data)
            entry = {"audio_filepath": f"val/{name}",
                     "text": row.get("text") or "",
                     "duration": row.get("duration"),
                     "start": row["start"], "end": row["end"],
                     "source_volume": os.path.basename(vol)}
            # None, not a number, when the clip was never verified: a reader
            # must be able to tell an unchecked holdout from a checked one.
            entry["voice_similarity"] = (picked_scores[i]
                                         if i < len(picked_scores) else None)
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            written.append(entry)

    doc = {
        "trained_zip": os.path.basename(trained_zip),
        "source_dir": os.path.basename(source_dir.rstrip("/")),
        "source_volumes": len(volumes),
        "volumes_excluded_as_training_data": contributing,
        "voices_in_book_recorded_not_gated": voices,
        "min_voice_similarity": min_voice_similarity,
        "volume_similarity_to_trained": similarities,
        "clips_written_short_by": (lines - len(picked)) if verify_clips else 0,
        "min_lines": min_lines if verify_clips else None,
        "clips_verified": verify_clips,
        "min_clip_voice": min_clip_voice if verify_clips else None,
        "clips_rejected_wrong_voice": rejected_wrong_voice,
        "clips_unreadable": unjudged_clips,
        "volumes_dropped_wrong_voice": sorted(wrong_voice),
        "volumes_dropped_unjudged": sorted(unjudged),
        "trained_clips": len(trained),
        "unseen_pool": len(pool),
        "requested": lines,
        "written": len(written),
        "seed": seed,
        "clips": written,
    }
    doc["provenance"] = provenance(__file__, vars())
    with open(os.path.join(out_dir, "holdout.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trained-zip", required=True,
                    help="the deduped dataset zip the adapter was trained on")
    ap.add_argument("--source-dir", required=True,
                    help="folder of that audiobook's source volume archives")
    ap.add_argument("--out", required=True,
                    help="directory to write; gains a val/ split")
    ap.add_argument("--lines", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260904)
    ap.add_argument("--embeddings", default=os.path.join(
        REPO, "dedup_analysis", "embeddings_cache.pkl"),
        help="the dedup run's cached per-clip embeddings, used to check that "
             "a candidate volume is the same voice as the trained one")
    ap.add_argument("--min-clip-voice", type=float, default=0.30,
                    help="per-CLIP cosine to the trained voice below which a "
                         "held-out clip is a different person. 0.30 is the "
                         "trough between the two modes of 1,360 labelled "
                         "clips; 0.45 sits on the real mode's rising edge")
    ap.add_argument("--min-lines", type=int, default=12,
                    help="refuse rather than write a holdout shorter than "
                         "this. 12 is what the identity gate reads, so below "
                         "it the median rests on fewer draws than every other "
                         "adapter's and is not comparable")
    ap.add_argument("--clip-anchors", type=int, default=2,
                    help="reference clips drawn from the trained zip; more "
                         "than one so a single odd clip cannot set the bar")
    ap.add_argument("--oversample", type=int, default=3,
                    help="candidates verified per line wanted, so a "
                         "contaminated book still yields a full holdout")
    ap.add_argument("--sibling-python", default=os.environ.get(
                        "ALEXANDRIA_SIBLING_PYTHON"),
                    help="interpreter with speechbrain; without it the build "
                         "REFUSES rather than skipping verification")
    ap.add_argument("--no-verify-clips", action="store_true",
                    help="write a deliberately unverified holdout. Recorded "
                         "in the artifact as clips_verified=false")
    ap.add_argument("--min-voice-similarity", type=float, default=0.85,
                    help="drop candidate volumes below this cosine to the "
                         "trained volume (calibrated on six books: clean ones "
                         "sit at 0.919-0.992, contaminated reach 0.326)")
    args = ap.parse_args()

    doc = build(args.trained_zip, args.source_dir, args.out, args.lines,
                args.seed, args.embeddings, args.min_voice_similarity,
                min_clip_voice=args.min_clip_voice,
                clip_anchors=args.clip_anchors,
                min_lines=args.min_lines,
                oversample=args.oversample,
                sibling_python=args.sibling_python,
                verify_clips=not args.no_verify_clips)
    print(f"source volumes            : {doc['source_volumes']}")
    print(f"excluded as training data : "
          f"{[v for v, _ in doc['volumes_excluded_as_training_data']]}")
    print(f"dropped, wrong voice      : "
          f"{len(doc['volumes_dropped_wrong_voice'])} volumes "
          f"(below {doc['min_voice_similarity']} similarity)")
    if doc["volumes_dropped_unjudged"]:
        print(f"dropped, never embedded   : "
              f"{len(doc['volumes_dropped_unjudged'])} volumes")
    print(f"unseen clip pool          : {doc['unseen_pool']}")
    print(f"written to {args.out}/val : {doc['written']}")


if __name__ == "__main__":
    main()
