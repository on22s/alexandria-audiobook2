"""Can an audiobook's clips be matched back to the book's text?

WHY THIS DECIDES THE NEXT PIPELINE. Every training clip carries
`speaker: "UNKNOWN"`. Character identity is currently recovered by CLUSTERING
speaker embeddings, which is why a nine-voice cast production splits correctly
while one narrator performing twelve characters collapses into a single
tone-mixed dataset - and dataset tone mixture is the one variable that predicts
whether an adapter works (r=0.58, p=4.8e-08, `dataset_tone_spread.json`).

The app already annotates a book's text to per-line speakers. If a clip can be
located in the book, it inherits that line's speaker and the dataset becomes
one voice BY CONSTRUCTION rather than by clustering. 20 titles have both an
EPUB and audiobook clips. This asks, for one of them, whether the matching is
even possible - before anything is built on the assumption that it is.

WHAT WOULD MAKE THIS A LIE. A fuzzy matcher will always return SOMETHING. Two
guards make the answer falsifiable:

  - A DECOY BOOK. The same clips are matched against an unrelated novel. Real
    alignment must collapse there. A match rate that survives the decoy is
    measuring English, not this book.
  - MONOTONICITY. Clips are ordered in time by their audiobook offsets, and the
    book positions they match must ascend in the same order. A matcher landing
    on coincidental phrases produces a high match rate and NO correlation, and
    that is the failure this separates from success.

WHAT THE ANSWER TURNED OUT TO BE, AND WHY THE HEADLINE NUMBER IS CIRCULAR.
100% of clips locate, decoy 0.5%, order rho=1.000 - and then every located clip
proved word-for-word IDENTICAL to the book span it landed on. The clip text is
not a transcript at all; the original pipeline already force-aligned the
audiobook against the ebook and stored the book's own words. So a high match
rate confirms where the text came from rather than discovering an alignment,
and it must not be reported as the latter.

The finding underneath is better than the one this was built to test: **the
bridge does not need building.** Every clip already carries its exact book
text, so labelling a clip with the speaker its line was annotated with is a
lookup, not a fuzzy match. `exact_word_for_word` is therefore the number that
matters here, and `located` is only its precondition.

The decoy control still earns its place. Without it, "100% of clips match the
book" is equally consistent with a matcher that matches any English prose, and
0.5% on an unrelated novel is what makes the specificity a measurement.
"""
import argparse
import collections
import json
import os
import re
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))
from experiments.provenance import provenance  # noqa: E402

WORD = re.compile(r"[a-z0-9']+")


def words(text):
    """-> lowercase word list, with the preparer's pause markers dropped.

    Clip text carries runs of dots the preparer inserts for pauses; they are
    not in the book and would otherwise never match.
    """
    return WORD.findall(text.lower().replace(".", " "))


def book_words(epub_path):
    from routers.script import extract_epub_text
    return words(extract_epub_text(epub_path))


def gram_index(seq, k):
    """-> {k-gram: [positions]}. Positions are word offsets into the book."""
    idx = collections.defaultdict(list)
    for i in range(len(seq) - k + 1):
        idx[tuple(seq[i:i + k])].append(i)
    return idx


def locate(clip, index, k):
    """-> the book position this clip's text lands at, or None.

    A clip matches when one of its k-grams appears in the book. Ambiguous
    grams - ones occurring many times, which are usually stock phrases - are
    skipped rather than guessed at, so a match means a distinctive phrase was
    found and not that a common one was.
    """
    seq = words(clip)
    if len(seq) < k:
        return None
    for i in range(len(seq) - k + 1):
        hits = index.get(tuple(seq[i:i + k]))
        if hits and len(hits) <= 3:
            return hits[0] - i
    return None


def clip_texts(volume_zip, limit):
    """-> [(start_offset, text)] from a source volume, in audiobook order."""
    rows, seen = [], set()
    with zipfile.ZipFile(volume_zip) as zf:
        for name in zf.namelist():
            if not name.endswith("metadata.jsonl"):
                continue
            for line in zf.read(name).decode("utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                key = (round(float(r["start"]), 2), round(float(r["end"]), 2))
                if key in seen:
                    continue
                seen.add(key)
                rows.append((float(r["start"]), r.get("text") or ""))
    rows.sort()
    return rows[:limit] if limit else rows


def spearman(xs, ys):
    if len(xs) < 3:
        return None, len(xs)
    from scipy import stats
    rho, p = stats.spearmanr(xs, ys)
    return {"rho": float(rho), "p": float(p)}, len(xs)


def run(epub, volumes, k, limit):
    book = book_words(epub)
    index = gram_index(book, k)
    out = []
    for vol in volumes:
        clips = clip_texts(vol, limit)
        found, exact = [], 0
        for t, txt in clips:
            pos = locate(txt, index, k)
            if pos is None:
                continue
            found.append((t, pos))
            seq = words(txt)
            # IS THE CLIP THE BOOK'S OWN WORDS? A located clip only proves a
            # distinctive phrase was shared. Comparing the whole span decides
            # whether the text was transcribed from audio or taken from the
            # book, and those imply completely different next steps.
            if book[pos:pos + len(seq)] == seq:
                exact += 1
        rho, n = spearman([t for t, _ in found], [p for _, p in found])
        out.append({
            "volume": os.path.basename(vol),
            "clips": len(clips),
            "located": len(found),
            "exact_word_for_word": exact,
            "rate": round(len(found) / len(clips), 4) if clips else None,
            "exact_rate": round(exact / len(clips), 4) if clips else None,
            "order_agreement": rho,
            "order_n": n,
        })
    return {"book_words": len(book), "k": k, "volumes": out}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--epub", required=True)
    ap.add_argument("--pairs", default=None,
                    help="JSON list of {epub, source_dir} to sweep after the "
                         "primary pair, for generality across titles")
    ap.add_argument("--decoy-epub", required=True,
                    help="an unrelated book; the match rate must collapse "
                         "here or the matcher is measuring English")
    ap.add_argument("--source-dir", required=True)
    ap.add_argument("--volumes", type=int, default=3)
    ap.add_argument("--k", type=int, default=6,
                    help="word n-gram length used to locate a clip")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments",
        "text_audio_alignment_pilot.json"))
    args = ap.parse_args()

    vols = sorted(
        os.path.join(args.source_dir, n)
        for n in os.listdir(args.source_dir)
        if os.path.isfile(os.path.join(args.source_dir, n))
        and zipfile.is_zipfile(os.path.join(args.source_dir, n)))[:args.volumes]
    if not vols:
        sys.exit(f"no source volumes under {args.source_dir}")

    real = run(args.epub, vols, args.k, args.limit)
    decoy = run(args.decoy_epub, vols, args.k, args.limit)

    sweep = []
    if args.pairs:
        for pair in json.load(open(args.pairs, encoding="utf-8")):
            pvols = sorted(
                os.path.join(pair["source_dir"], n)
                for n in os.listdir(pair["source_dir"])
                if os.path.isfile(os.path.join(pair["source_dir"], n))
                and zipfile.is_zipfile(os.path.join(pair["source_dir"], n)))[:1]
            if not pvols:
                sweep.append({"title": os.path.basename(pair["source_dir"]),
                              "why_unmeasured": "no source volumes"})
                continue
            try:
                res = run(pair["epub"], pvols, args.k, args.limit)
            except Exception as exc:                        # noqa: BLE001
                sweep.append({"title": os.path.basename(pair["source_dir"]),
                              "why_unmeasured": str(exc)[:120]})
                continue
            sweep.append({"title": os.path.basename(pair["source_dir"]),
                          "epub": os.path.basename(pair["epub"]),
                          **res["volumes"][0]})

    rates = [v["rate"] for v in real["volumes"] if v["rate"] is not None]
    drates = [v["rate"] for v in decoy["volumes"] if v["rate"] is not None]
    doc = {
        "note": "Can audiobook clips be located in the book's own text? If "
                "they can, a clip inherits the speaker its line was annotated "
                "with, and a per-character dataset stops depending on "
                "clustering audio.",
        "epub": os.path.basename(args.epub),
        "decoy_epub": os.path.basename(args.decoy_epub),
        "source_dir": os.path.basename(args.source_dir.rstrip("/")),
        "real": real,
        "decoy": decoy,
        "sweep": sweep,
        "mean_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "decoy_mean_rate": round(sum(drates) / len(drates), 4) if drates else None,
    }
    doc["provenance"] = provenance(__file__, vars(args))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)

    print(f"book words: {real['book_words']}   decoy: {decoy['book_words']}   "
          f"k={args.k}\n")
    for v, d in zip(real["volumes"], decoy["volumes"]):
        agree = v["order_agreement"]
        _ = v
        order = ("rho=%.3f p=%.1e" % (agree["rho"], agree["p"])
                 if agree else "not enough matches")
        print(f"  {v['volume'][-12:]:14} located {v['located']:4}/{v['clips']:<4} "
              f"exact {v['exact_word_for_word']:4} "
              f"decoy {(d['rate'] or 0)*100:4.1f}%   order {order}")
    print(f"\nmean match rate  {(doc['mean_rate'] or 0)*100:.1f}%"
          f"   decoy {(doc['decoy_mean_rate'] or 0)*100:.1f}%")
    if sweep:
        print("\nother titles (one volume each):")
        for row in sweep:
            if row.get("why_unmeasured"):
                print(f"   {row['title'][:44]:46} SKIP {row['why_unmeasured']}")
                continue
            print(f"   {row['title'][:44]:46} located "
                  f"{row['located']:4}/{row['clips']:<4} "
                  f"exact {row['exact_word_for_word']:4}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
