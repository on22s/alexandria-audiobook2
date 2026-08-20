"""Build a held-out English eval set from a voice-library dataset.

WHY. Goal 2.9 records that English agrees with its reference far worse than
either CJK language - 0.29-0.34 f0 correlation against 0.72-0.74, about three
times the gross pitch error - and asks whether that is "a real weakness of that
arm or an artifact of that eval set". Re-running at n=150 on 2026-08-20 showed
the deficit is stable and the ARM difference is small, but it cannot answer the
question: every English number comes from LJSpeech, and more draws from the
same recordings cannot separate a weak arm from a hard reference set.

A SECOND ENGLISH REFERENCE IS ALREADY ON DISK. Every voice-library adapter
ships a dataset with a held-out val split - human audio and its text - plus the
reference clip the clone arm needs. Different speakers, different books,
different recording conditions. If English still scores 0.3 across eight of
them, the eval set is exonerated and the finding belongs to the arm.

Twenty val lines each is small, which is why this builds one set per adapter
and expects the caller to pool them rather than trusting any single one.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402


def held_out(dataset, book):
    """-> [{human_wav, text, id}] from the val split, paths relative to REPO."""
    meta = os.path.join(dataset, "val", "metadata.jsonl")
    if not os.path.exists(meta):
        raise SystemExit("no val split at %s - this adapter held nothing out "
                         "and cannot be an eval set" % meta)
    rows = []
    with open(meta, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            wav = os.path.join(dataset, entry["audio_filepath"])
            if not os.path.exists(wav):
                continue
            # EVERY KEY ljspeech_generate.py READS: id, book, text, human_wav
            # and seconds. The first build shipped three of those and 14 of 15
            # stages died on KeyError: 'book' after the chain had taken the GPU.
            # The row shape is a contract with the consumer, and a build that
            # cannot be consumed is not a build.
            rows.append({"id": os.path.splitext(
                             os.path.basename(entry["audio_filepath"]))[0],
                         "book": book,
                         "human_wav": os.path.relpath(wav, REPO),
                         "text": entry.get("text") or "",
                         "seconds": entry.get("duration")
                                    or entry.get("seconds") or 0.0})
    return [r for r in rows if r["text"].strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", required=True,
                        help="a voice-library dataset dir containing val/")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    dataset = os.path.abspath(args.dataset)
    book = os.path.basename(os.path.dirname(dataset))
    rows = held_out(dataset, book)
    if not rows:
        raise SystemExit("no usable val lines in %s" % dataset)

    ref = os.path.join(dataset, "ref.wav")
    ref_text_path = os.path.join(dataset, "ref_text.txt")
    if not (os.path.exists(ref) and os.path.exists(ref_text_path)):
        raise SystemExit("the clone arm needs ref.wav and ref_text.txt in %s"
                         % dataset)
    with open(ref_text_path, encoding="utf-8") as handle:
        ref_text = handle.read().strip()

    build = {
        "corpus": book,
        "licence": "user's own audiobook library; not redistributable",
        "target_rate": 24000,
        "ref_sample": os.path.relpath(ref, REPO),
        "ref_text": ref_text,
        "ref_source_id": "ref",
        # ONE AUDIOBOOK, SO ONE "BOOK". The corpus builds carry `test_books`
        # because their test split is drawn from named volumes; a library voice
        # has a single source. Emitting it anyway means every consumer sees the
        # same shape whichever builder produced the file - the shape mismatch
        # that crashed ljspeech_generate after it had rendered every clip.
        "test_books": [book],
        # `test` is the name ljspeech_generate.py reads. Same shape, so the
        # existing generator and prosody probe work unchanged.
        "test": rows,
        "provenance": provenance(__file__, args),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(build, handle, indent=1, ensure_ascii=False)
    print("%s: %d held-out lines -> %s"
          % (build["corpus"], len(rows), os.path.basename(args.out)))


if __name__ == "__main__":
    main()
