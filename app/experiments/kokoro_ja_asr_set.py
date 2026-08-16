"""Cut a Japanese ASR evaluation set from the LibriVox audio already on disk.

WHY THIS EXISTS. Goal 5.4's Japanese arm clears both target conditions
(CER 7.67% vs 20%, alignment median 39 ms vs 150 ms) but only on 10 clips,
where English and Chinese were measured on 50. Confirming it was blocked on
corpus rather than compute: the same-speaker build's novel has 6,294
transcript rows and 34 downloaded clips, so no re-slicing of it reaches 50.

But 5.4 measures TRANSCRIPTION AND ALIGNMENT, not voice identity. It does not
need the same-speaker design that build inherits from the voice goals - and
`corpora/kokoro/librivox` already holds 405 MB of audio for four other
Japanese novels whose transcripts carry sample-accurate offsets. So the set
can be cut locally, with no network.

Several readers rather than one is not a compromise here. An ASR result from a
single voice is the narrower claim; boundaries and characters that hold across
four readers is the broader one.

Cutting is `kokoro_fetch.cut` rather than a second ffmpeg invocation, and the
transcripts come from `kokoro_fetch.load_metadata`, so the offset convention
(column 5, sample offsets at 22050) has one implementation.
"""
import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kokoro_fetch import ROOT, NATIVE_RATE, cut, load_metadata

LIBRIVOX = os.path.join(ROOT, "librivox")
# Match the clips the existing Japanese arm was measured on, so the two
# results compare directly: 22050 Hz mono FLAC. asr_backends refuses to
# resample silently and skips any clip whose rate differs from the first.
RATE = 22050


def available_novels():
    """Novels whose LibriVox MP3s are already downloaded."""
    if not os.path.isdir(LIBRIVOX):
        return []
    return sorted(name for name in os.listdir(LIBRIVOX)
                  if os.path.isdir(os.path.join(LIBRIVOX, name))
                  and os.path.exists(os.path.join(ROOT, f"{name}.metadata.txt")))


def eligible_rows(novel, min_chars, max_chars):
    """Rows whose text fits the window AND whose source MP3 is on disk."""
    rows = []
    for row in load_metadata(novel):
        if not min_chars <= len(row["text"]) <= max_chars:
            continue
        source = os.path.join(LIBRIVOX, novel, row["mp3"])
        if not os.path.exists(source):
            continue
        row["source_mp3"] = source
        rows.append(row)
    return rows


def spread(rows, limit):
    """Round-robin across (novel, mp3) so the set is not one chapter.

    Same reasoning as kokoro_fetch.spread, which exists because an earlier
    LJSpeech split drew 165 of 200 clips from a single book. Deterministic:
    sorted buckets, sorted within.
    """
    grouped = collections.OrderedDict()
    for row in sorted(rows, key=lambda r: (r["book"], r["mp3"], r["id"])):
        grouped.setdefault((row["book"], row["mp3"]), []).append(row)

    # INTERLEAVE THE BOOKS, don't just walk the buckets in order. Ordered by
    # (book, mp3), one pass visits every bucket of the first book before the
    # second, so a limit smaller than the bucket count silently drops whole
    # readers off the end - the first run of this cut it to 50 and got 11/24/15
    # from three books and ZERO from kusamakura, which is the same
    # one-source-dominates flaw kokoro_fetch.spread exists to prevent, just one
    # level up.
    per_book = collections.OrderedDict()
    for key, queue in grouped.items():
        per_book.setdefault(key[0], []).append(queue)
    buckets = collections.OrderedDict()
    for position in range(max(len(v) for v in per_book.values())):
        for book, queues in per_book.items():
            if position < len(queues):
                buckets[(book, position)] = queues[position]

    picked = []
    while len(picked) < limit:
        progressed = False
        for queue in buckets.values():
            if queue:
                picked.append(queue.pop(0))
                progressed = True
                if len(picked) == limit:
                    return picked
        if not progressed:
            break
    return picked


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--clips", type=int, default=50)
    ap.add_argument("--min-chars", type=int, default=18)
    ap.add_argument("--max-chars", type=int, default=70)
    ap.add_argument("--out-dir", default=os.path.join(
        REPO, "ab_test_runtime", "kokoro_ja_asr_eval"))
    ap.add_argument("--split-by-reader", action="store_true",
                    help="also write one build per reader, so a pooled result "
                         "can be decomposed without re-cutting anything")
    args = ap.parse_args()

    novels = available_novels()
    if not novels:
        sys.exit(f"no downloaded novels under {LIBRIVOX}")
    print(f"novels with audio on disk: {', '.join(novels)}")

    pool = []
    for novel in novels:
        rows = eligible_rows(novel, args.min_chars, args.max_chars)
        print(f"  {novel[:44]:46} {len(rows):5} eligible")
        pool.extend(rows)
    if len(pool) < args.clips:
        sys.exit(f"only {len(pool)} eligible rows, {args.clips} requested")

    picked = spread(pool, args.clips)
    if len(picked) < args.clips:
        sys.exit(f"spread yielded {len(picked)}, {args.clips} requested")

    wav_dir = os.path.join(args.out_dir, "wavs")
    os.makedirs(wav_dir, exist_ok=True)
    test, failures = [], []
    for row in picked:
        destination = os.path.join(wav_dir, f"{row['id']}.flac")
        if not os.path.exists(destination):
            ok, error = cut(row["source_mp3"], row["start"], row["end"],
                            destination, RATE)
            if not ok:
                failures.append(f"{row['id']}: {error}")
                continue
        seconds = (row["end"] - row["start"]) / float(NATIVE_RATE)
        test.append({"id": row["id"], "book": row["book"], "text": row["text"],
                     "seconds": seconds,
                     "human_wav": os.path.relpath(destination, REPO)})

    # A silently short or empty clip would score as a transcription failure and
    # be read as the backend's fault, so refuse the whole set instead.
    import soundfile as sf
    for entry in test:
        info = sf.info(os.path.join(REPO, entry["human_wav"]))
        length = info.frames / float(info.samplerate)
        if info.samplerate != RATE or not 0.4 <= length <= 30.0:
            failures.append(f"{entry['id']}: {length:.2f}s at {info.samplerate}Hz")
    if failures:
        sys.exit("refusing to write a build with bad clips:\n  "
                 + "\n  ".join(failures[:10]))

    document = {
        "corpus": "Kokoro Speech Dataset v1.3 / LibriVox",
        "language": "ja",
        "design": "multi-reader Japanese ASR set cut from locally held "
                  "LibriVox audio; transcription and alignment only, so a "
                  "single speaker is not required",
        "readers": sorted({entry["book"] for entry in test}),
        "clip_rate": RATE,
        "test": test,
    }
    build = os.path.join(args.out_dir, "build.json")
    with open(build, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=1)
    by_book = collections.Counter(entry["book"] for entry in test)
    print(f"\nwrote {build}: {len(test)} clips at {RATE} Hz")
    for book, count in sorted(by_book.items()):
        print(f"  {book[:44]:46} {count:3}")

    if args.split_by_reader:
        # A POOLED NUMBER CANNOT SAY WHICH READER IT CAME FROM. The 50-clip run
        # scored CER 28.6% where the single-reader n=10 arm scored 7.7%, and
        # aggregates alone cannot separate "several readers are harder" from
        # "one recording is dragging the mean". Same clips, same rate, just
        # regrouped - so a per-reader run adds no new material and stays
        # comparable with the pooled one.
        split_dir = os.path.join(args.out_dir, "by_reader")
        os.makedirs(split_dir, exist_ok=True)
        print("\nper-reader builds:")
        for book in sorted(by_book):
            rows = [entry for entry in test if entry["book"] == book]
            path = os.path.join(split_dir, f"{book}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({**document, "design": document["design"]
                           + f"; single reader subset ({book})",
                           "readers": [book], "test": rows},
                          handle, ensure_ascii=False, indent=1)
            print(f"  {book[:44]:46} {len(rows):3} -> {os.path.basename(path)}")


if __name__ == "__main__":
    main()
