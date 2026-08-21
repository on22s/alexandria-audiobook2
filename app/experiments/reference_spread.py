"""Build several references for one speaker, spanning a range of typicality.

METTS (TASLP 2024) perturbs the FORMANTS of its reference signal to strip
speaker timbre, on the stated grounds that formants are determined by the vocal
tract and "represent their vocal identity", and reports that doing so beat both
SALN and speaker-adversarial training on speaker cosine similarity. We want the
opposite of their perturbation - we are trying to KEEP timbre - but the claim
underneath is testable here and has never been tested: does a reference whose
formants sit closer to the speaker's own centre produce a more similar clone?

#367 moved goals 2.5 and 2.6 by replacing one reference with a better one, but
it changed length and typicality together and said so. This changes typicality
alone - as far as it can be isolated. Every arm is a concatenation from the
same speaker inside the same 10-15s band, differing in how far it sits from
that speaker's median f0 and vocal-tract length. THE BAND IS NOT A CONSTANT
DURATION: the measured spread on LJSpeech runs 10.5s to 14.3s, so a result
here bounds the typicality effect rather than isolating it perfectly, and a
correlation that tracks duration instead must be ruled out by reading the
per-arm table, which records both.

`reference_rebuild.py` already knows how to measure a speaker's centre, build
banded candidate concatenations, and score their distance from it. Those are
imported, not reimplemented - the arms and #367's single reference must be
chosen by the same yardstick or the comparison is between two yardsticks.

    arm 0   nearest the speaker's centre  (this is what #367 picked)
    arm K-1 the farthest candidate measured
    between, even quantiles of the sorted distance list

A flat result is as useful as a sloped one: it would mean reference typicality
is not a lever on 2.1, and we can stop treating reference choice as important.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402
from experiments.reference_rebuild import (BAND, ROOTS, TARGET_SECONDS,  # noqa: E402
                                           _main_checkout, candidates,
                                           corpus_medians, distance, measure,
                                           resolve, write_concat)


def pick_spread(scored, arms):
    """-> `arms` candidates at even quantiles of the sorted distance list.

    Always includes the nearest and the farthest: the question is whether
    similarity tracks distance, and an interior-only sample cannot answer it.
    Duplicate picks are dropped rather than repeated, so a corpus with few
    usable candidates yields fewer arms and says so instead of running the
    same reference twice under two names.
    """
    if arms < 2:
        return scored[:1]
    last = len(scored) - 1
    seen, out = set(), []
    for i in range(arms):
        index = round(i * last / (arms - 1))
        if index in seen:
            continue
        seen.add(index)
        out.append(scored[index])
    return out


def score_candidates(build, target, corpus_limit, keep, work_dir):
    """-> (speaker centre, candidates sorted nearest-first)."""
    meta = resolve(build.get("metadata"))
    train_dir = resolve(build.get("train_dir"))
    if not meta or not train_dir:
        raise SystemExit("build needs metadata + train_dir")
    entries = [json.loads(line) for line in open(meta, encoding="utf-8")
               if line.strip()]
    centre = corpus_medians(
        [os.path.join(train_dir, e["audio_filepath"]) for e in entries],
        corpus_limit)
    if not centre:
        raise SystemExit("could not measure the speaker's corpus")
    runs = candidates(entries, train_dir, target)
    if not runs:
        raise SystemExit("no consecutive run lands inside %s-%ss" % BAND)

    scored = []
    for start, paths, texts, seconds in runs[:keep]:
        tmp = os.path.join(work_dir, "cand%d.wav" % start)
        try:
            write_concat(paths, tmp)
            m = measure(tmp)
            d = distance(m, centre)
        except Exception as exc:                        # noqa: BLE001
            print("  candidate %d unusable: %s" % (start, exc))
            continue
        if d is None:
            continue
        scored.append({"start": start, "paths": paths, "texts": texts,
                       "seconds": seconds, "measures": m, "distance": d,
                       "tmp": tmp})
    scored.sort(key=lambda s: s["distance"])
    return centre, scored


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--arms", type=int, default=4)
    ap.add_argument("--target-seconds", type=float, default=TARGET_SECONDS)
    ap.add_argument("--corpus-clips", type=int, default=60)
    ap.add_argument("--candidates", type=int, default=40)
    ap.add_argument("--audio-root", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    ROOTS[:] = [r for r in (args.audio_root or []) if r] or [REPO]
    main_checkout = _main_checkout()
    if main_checkout and main_checkout not in ROOTS:
        ROOTS.append(main_checkout)

    os.makedirs(args.out_dir, exist_ok=True)
    path = resolve(args.build) or args.build
    with open(path, encoding="utf-8") as handle:
        build = json.load(handle)

    centre, scored = score_candidates(build, args.target_seconds,
                                      args.corpus_clips, args.candidates,
                                      args.out_dir)
    if not scored:
        raise SystemExit("no candidate could be measured")
    chosen = pick_spread(scored, args.arms)

    arms = []
    for index, candidate in enumerate(chosen):
        wav = os.path.join(args.out_dir, "ref_spread%d.wav" % index)
        os.replace(candidate["tmp"], wav)
        new = dict(build)
        new["ref_sample"] = os.path.relpath(wav, ROOTS[-1])
        new["ref_text"] = " ".join(t.strip() for t in candidate["texts"]
                                   if t.strip())
        new["ref_source_id"] = "spread%d@%dx%d" % (
            index, candidate["start"], len(candidate["paths"]))
        new["ref_seconds"] = round(candidate["seconds"], 3)
        new["reference_spread"] = {
            "arm": index,
            "from_build": os.path.relpath(path, ROOTS[-1]),
            "corpus_centre": {k: round(v, 4) for k, v in centre.items()},
            "measures": {k: round(v, 4) for k, v in candidate["measures"].items()
                         if isinstance(v, (int, float))},
            "distance_from_centre": round(candidate["distance"], 5),
            "candidates_measured": len(scored),
            "note": "duration band is held constant across arms; only "
                    "typicality varies; the band is 10-15s, not one length",
        }
        new["provenance"] = provenance(__file__, args)
        build_path = os.path.join(args.out_dir, "build_spread%d.json" % index)
        with open(build_path, "w", encoding="utf-8") as handle:
            json.dump(new, handle, indent=1, ensure_ascii=False)
        arms.append({"arm": index, "build": build_path, "ref_sample": wav,
                     "distance": round(candidate["distance"], 5),
                     "seconds": round(candidate["seconds"], 3),
                     "measures": new["reference_spread"]["measures"]})

    for candidate in scored:
        if os.path.exists(candidate["tmp"]):
            os.remove(candidate["tmp"])

    doc = {"status": "complete", "provenance": provenance(__file__, args),
           "scope": "references for one speaker spanning distance from that "
                    "speaker's own f0/vocal-tract centre; duration kept "
                    "inside one band, not held equal",
           "corpus_centre": {k: round(v, 4) for k, v in centre.items()},
           "candidates_measured": len(scored),
           "requested_arms": args.arms, "arms": arms}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)
    print("centre %s | %d candidates measured | %d arms"
          % (doc["corpus_centre"], len(scored), len(arms)))
    for arm in arms:
        print("  arm %d  distance %.5f  %.1fs  %s"
              % (arm["arm"], arm["distance"], arm["seconds"], arm["measures"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
