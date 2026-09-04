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

WHY MULTI-VOICE BOOKS ARE REFUSED. The volumes are only safe to draw from when
the whole audiobook is one voice. Dracula [Audible Edition] is a nine-voice cast
production and dedup split it into nine datasets, so a random volume there is
probably a DIFFERENT narrator - which would score both adapters against the
wrong person and look like an ordinary result. The builder counts how many
deduped datasets the book produced and refuses above one unless the caller
passes --allow-multi-voice, which nothing should do until the pool is filtered
by speaker.

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
import random
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
    """-> how many distinct voices dedup found in this audiobook.

    Datasets are named <book>_char<N>_vol<NN>.zip, so the count of siblings
    sharing a book prefix is the number of voices the clustering separated.
    """
    ded = os.path.dirname(os.path.abspath(trained_zip))
    stem = re.sub(r"_char\d+_vol\d+\.zip$", "",
                  os.path.basename(trained_zip))
    if stem == os.path.basename(trained_zip):
        return 1
    return sum(1 for n in os.listdir(ded)
               if n.endswith(".zip")
               and re.sub(r"_char\d+_vol\d+\.zip$", "", n) == stem)


def build(trained_zip, source_dir, out_dir, lines, seed,
          allow_multi_voice=False):
    voices = voices_in_book(trained_zip)
    if voices > 1 and not allow_multi_voice:
        sys.exit(
            f"{os.path.basename(trained_zip)} comes from a book dedup split "
            f"into {voices} voices, so an unseen volume is probably a "
            f"different narrator. Filter the pool by speaker first; "
            f"--allow-multi-voice overrides this and should not be used to "
            f"produce evidence.")
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
    for vol in volumes:
        rows = read_metadata(vol)
        keys = {key(r) for r, _ in rows}
        if keys & trained:
            contributing.append((os.path.basename(vol), len(keys & trained)))
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
    picked = pool[:lines]
    if not picked:
        sys.exit("no unseen clips remain after excluding contributing volumes")

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
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            written.append(entry)

    doc = {
        "trained_zip": os.path.basename(trained_zip),
        "source_dir": os.path.basename(source_dir.rstrip("/")),
        "source_volumes": len(volumes),
        "volumes_excluded_as_training_data": contributing,
        "voices_in_book": voices,
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
    ap.add_argument("--allow-multi-voice", action="store_true",
                    help="draw from a book holding more than one voice; the "
                         "sample will mix narrators and is not evidence")
    args = ap.parse_args()

    doc = build(args.trained_zip, args.source_dir, args.out, args.lines,
                args.seed, args.allow_multi_voice)
    print(f"source volumes            : {doc['source_volumes']}")
    print(f"excluded as training data : "
          f"{[v for v, _ in doc['volumes_excluded_as_training_data']]}")
    print(f"unseen clip pool          : {doc['unseen_pool']}")
    print(f"written to {args.out}/val : {doc['written']}")


if __name__ == "__main__":
    main()
