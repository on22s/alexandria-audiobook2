#!/usr/bin/env python3
"""Fetch one Hi-Fi TTS reader as an LJSpeech-shaped corpus, for goal 2.9.

WHY A SECOND PUBLIC ENGLISH SET. Goal 2.9 asks whether English's poor
prosody agreement with its human reference is a weakness of the ARM or of
the EVAL SET, and every public English number in the document comes from
LJSpeech: one reader, non-fiction, 22.05 kHz. The eight-narrator run of
2026-08-20 (`second_english_eval_20260820.sh`) answered part of it on the
user's own audiobooks, but those are private and their transcripts came
from ASR. Hi-Fi TTS (Bakhturina et al., Interspeech 2021; OpenSLR 109) is
public, CC BY 4.0, human-transcribed LibriVox narration at 44.1 kHz, and its
file paths carry the book, so the split-by-source-work design of
`ljspeech_prepare.py` transfers unchanged.

WHAT IS WRITTEN, and why in this shape. `ljspeech_prepare.py`,
`ljspeech_build.py`, `ljspeech_generate.py` and `prosody_fidelity.py` are
reused as they are, so the output mimics an extracted LJSpeech tree:

    <out>/wavs/<id>.wav        native 44.1 kHz; build.py resamples both sides
    <out>/metadata.csv         id|text|text_normalized, no header
    <out>/corpus.json          name, licence, native rate, per-book counts,
                               shards read, provenance

    id = <book_slug>-<chapter>_<seq>   so that id.split("-")[0] is the book,
                                       which is what prepare and build assume

NEVER THE 41 GB TARBALL. The HF mirror (MikhailT/hifi-tts) is parquet
sharded by split with rows grouped by reader, so a reader can be pulled by
reading each shard's `speaker` column first (a few kilobytes over HTTP) and
fetching only the shards that hold the reader. Local disk had 24 GB free
when this was written.

    python experiments/hifitts_fetch.py --speaker 9017 \
        --out ../ab_test_runtime/corpora/hifitts/9017
"""
import argparse
import collections
import io
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

HF_REPO = "MikhailT/hifi-tts"
CITATION = ("Bakhturina, Lavrukhin, Ginsburg, Zhang. Hi-Fi Multi-Speaker "
            "English TTS Dataset. Interspeech 2021. OpenSLR 109.")
LICENCE = "CC BY 4.0 (LibriVox audio, Gutenberg text; " + CITATION + ")"
READERS = {"92": "Cori Samuel", "6097": "Phil Benson", "9017": "John Van Stan"}

# audio/9017_clean/14261/dartagnan03part3_62_dumas_0281.flac
#   -> slug dartagnan03part3, chapter 62, author dumas, seq 0281
# Non-greedy slug: it stops at the first `_<digits>_<letters>_<digits>` tail,
# which is the LibriVox naming convention for every path sampled.
_NAME = re.compile(r"^(?P<slug>.+?)_(?P<chapter>\d+)_(?P<author>[a-z]+)_(?P<seq>\d+)$")


def parse_file(path):
    """-> (speaker, clip_id, book) from a Hi-Fi TTS `file` value.

    Refuses a slug containing '-' because downstream derives the book with
    id.split('-')[0]; a dash would silently split one book into two.
    """
    parts = path.replace("\\", "/").split("/")
    if len(parts) < 4 or "_" not in parts[1]:
        raise ValueError(f"unexpected Hi-Fi TTS path: {path}")
    speaker = parts[1].split("_")[0]
    stem = os.path.splitext(parts[-1])[0]
    m = _NAME.match(stem)
    if not m:
        raise ValueError(f"unexpected Hi-Fi TTS clip name: {stem}")
    slug = m.group("slug")
    if "-" in slug:
        raise ValueError(f"book slug contains '-', which the id scheme uses "
                         f"as the book separator: {slug}")
    clip_id = f"{slug}-{m.group('chapter')}_{m.group('seq')}"
    return speaker, clip_id, slug


def usable(text, min_chars, max_chars):
    return min_chars <= len(text.strip()) <= max_chars


def write_rows(rows, out, min_chars, max_chars, decode=None,
               max_per_book=0, per_book=None, have=None):
    """Write the reader's rows LJSpeech-style. -> (kept rows, per-book counts).

    `rows` are dicts with speaker, file, text, text_normalized and audio
    (bytes). `decode` turns bytes into (samples, rate) and exists so a test
    can supply silence instead of flac. `max_per_book` stops writing a book
    once it has that many usable clips - reader 9017 alone is ~19,000 usable
    clips (11 GB of WAV) and the pipeline draws 200 + 150 - with `per_book`
    carrying the running counts across shards.
    """
    import soundfile as sf
    if decode is None:
        def decode(data):
            return sf.read(io.BytesIO(data), dtype="float32")
    wavs = os.path.join(out, "wavs")
    os.makedirs(wavs, exist_ok=True)
    kept, rate_seen = [], set()
    per_book = collections.Counter() if per_book is None else per_book
    # Written by hand, not csv.writer: LJSpeech's reader is QUOTE_NONE with
    # no escape character, and dialogue is full of double quotes that a
    # csv.writer would backslash-escape into the text.
    def field(text):
        return " ".join(text.split()).replace("|", "/")

    with open(os.path.join(out, "metadata.csv"), "a", encoding="utf-8") as fh:
        for row in rows:
            normalized = (row.get("text_normalized") or "").strip()
            if not usable(normalized, min_chars, max_chars):
                continue
            _, clip_id, book = parse_file(row["file"])
            if have is not None and clip_id in have:
                continue                        # resumed: already written
            if max_per_book and per_book[book] >= max_per_book:
                continue
            audio, rate = decode(row["audio"])
            rate_seen.add(int(rate))
            path = os.path.join(wavs, clip_id + ".wav")
            if not os.path.exists(path):
                sf.write(path, audio, int(rate))
            raw = field(row.get("text") or normalized)
            fh.write(f"{clip_id}|{raw}|{field(normalized)}\n")
            kept.append({"id": clip_id, "book": book, "seconds":
                         round(len(audio) / float(rate), 3)})
            per_book[book] += 1
            if have is not None:
                have.add(clip_id)
    return kept, per_book, rate_seen


def shard_names(api):
    files = api.list_repo_files(HF_REPO, repo_type="dataset")
    names = sorted(f for f in files
                   if f.startswith("data/train.clean-") and f.endswith(".parquet"))
    if not names:
        sys.exit(f"no train.clean shards listed for {HF_REPO}")
    return names


def _with_backoff(fn, what, log=print, tries=6):
    """Retry `fn` on HTTP 429 with exponential backoff; anything else raises."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:                          # noqa: BLE001
            if "429" not in str(exc) or attempt == tries - 1:
                raise
            wait = 30 * (2 ** attempt)
            log(f"    {what}: rate limited (429); waiting {wait}s")
            time.sleep(wait)


def shard_has_reader(fs, name, speaker):
    """-> row count and the reader's row count, reading only `speaker`.

    A few range requests per shard, which is what makes skipping the other
    readers' shards nearly free.
    """
    import pyarrow.parquet as pq
    with fs.open(f"hf://datasets/{HF_REPO}/{name}", "rb") as fh:
        speakers = pq.ParquetFile(fh).read(columns=["speaker"]).column("speaker").to_pylist()
    return len(speakers), sum(1 for s in speakers if str(s) == speaker)


def iter_reader_rows(fs, names, speaker, log=print, scratch=None):
    """Yield (shard, rows) for shards holding `speaker`, cheapest first.

    Probes each shard's `speaker` column to decide whether to fetch it; rows
    are grouped by reader, so once the reader has been seen and a shard
    without them follows, the rest are not read. A shard that has the reader
    is downloaded ONCE as a whole file (`hf_hub_download`) and deleted after
    use: reading 0.5 GB through fsspec range requests drew HTTP 429 from the
    CDN on 2026-09-13 a hundred clips in.
    """
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download
    import tempfile
    scratch = scratch or tempfile.mkdtemp(prefix="hifitts_")
    seen = False
    for name in names:
        total, hit = _with_backoff(lambda: shard_has_reader(fs, name, speaker),
                                   f"probe {name}", log)
        if not hit:
            if seen:
                log(f"  {name}: no more {speaker} rows; stopping")
                return
            log(f"  {name}: {total} rows, none of reader {speaker}")
            continue
        seen = True
        log(f"  {name}: {hit} of {total} rows are reader {speaker}; downloading")
        local = _with_backoff(lambda: hf_hub_download(
            HF_REPO, name, repo_type="dataset", local_dir=scratch),
            f"download {name}", log)
        # Row groups one at a time: a whole shard's audio column is several
        # hundred MB and the first version was killed for memory pressure.
        try:
            pf = pq.ParquetFile(local)
            for batch in pf.iter_batches(batch_size=256, columns=[
                    "speaker", "file", "text", "text_normalized", "audio"]):
                rows = []
                for rec in batch.to_pylist():
                    if str(rec["speaker"]) != speaker:
                        continue
                    audio = rec["audio"]
                    rec["audio"] = audio["bytes"] if isinstance(audio, dict) else audio
                    rec["speaker"] = str(rec["speaker"])
                    rows.append(rec)
                if rows:
                    yield name, rows
        finally:
            try:
                os.remove(local)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--speaker", default="9017",
                    help="Hi-Fi TTS reader id (clean subset: 92, 6097, 9017)")
    ap.add_argument("--out", default=None,
                    help="default ab_test_runtime/corpora/hifitts/<speaker>")
    ap.add_argument("--min-chars", type=int, default=60,
                    help="mirror ljspeech_prepare: clause fragments carry too "
                         "little prosody to compare, so they are not written")
    ap.add_argument("--max-chars", type=int, default=220)
    ap.add_argument("--max-rows", type=int, default=0,
                    help="stop after this many usable rows once at least "
                         "--min-books books are represented (0 = the whole "
                         "reader)")
    ap.add_argument("--min-books", type=int, default=4)
    ap.add_argument("--max-per-book", type=int, default=400,
                    help="usable clips kept per source work, in corpus "
                         "order; 0 = all")
    ap.add_argument("--resume", action="store_true",
                    help="continue into an existing <out>: clips already in "
                         "metadata.csv are skipped and counted")
    args = ap.parse_args()
    out = args.out or os.path.join(REPO, "ab_test_runtime", "corpora",
                                   "hifitts", args.speaker)
    os.makedirs(out, exist_ok=True)
    meta = os.path.join(out, "metadata.csv")
    have, per_book = set(), collections.Counter()
    if os.path.exists(meta):
        if not args.resume:
            sys.exit(f"refusing to append to an existing {meta}; pass --resume "
                     f"to continue it or remove it first")
        with open(meta, encoding="utf-8") as fh:
            for line in fh:
                clip_id = line.split("|", 1)[0].strip()
                if clip_id:
                    have.add(clip_id)
                    per_book[clip_id.split("-")[0]] += 1
        print(f"resuming: {len(have)} clips already written across "
              f"{len(per_book)} books")

    from huggingface_hub import HfApi, HfFileSystem
    api, fs = HfApi(), HfFileSystem()
    names = shard_names(api)
    print(f"{len(names)} train.clean shards; looking for reader {args.speaker} "
          f"({READERS.get(args.speaker, '?')})")

    t0 = time.time()
    kept_all, rates, shards = [], set(), []
    for name, rows in iter_reader_rows(fs, names, args.speaker,
                                       scratch=os.path.join(out, "_shards")):
        kept, _, rate_seen = write_rows(rows, out, args.min_chars,
                                        args.max_chars,
                                        max_per_book=args.max_per_book,
                                        per_book=per_book, have=have)
        kept_all += kept
        rates |= rate_seen
        if name not in shards:
            shards.append(name)
            print(f"    {name.split('-')[1]}: total {len(have)} clips across "
                  f"{len(per_book)} books")
        if args.max_rows and len(have) >= args.max_rows \
                and len(per_book) >= args.min_books:
            print(f"  reached --max-rows {args.max_rows} with "
                  f"{len(per_book)} books; stopping")
            break
    try:
        os.rmdir(os.path.join(out, "_shards"))
    except OSError:
        pass
    if not have:
        sys.exit(f"no usable rows for reader {args.speaker}")
    if not rates and have:
        # A resume that found nothing new still has to name the rate.
        import soundfile as sf
        first = sorted(have)[0]
        rates = {sf.info(os.path.join(out, "wavs", first + ".wav")).samplerate}
    if len(rates) != 1:
        sys.exit(f"mixed native sample rates {sorted(rates)}; refusing to "
                 f"write a corpus.json that names one")

    doc = {"corpus": f"Hi-Fi TTS reader {args.speaker} "
                     f"({READERS.get(args.speaker, 'unknown reader')})",
           "licence": LICENCE,
           "source": f"https://huggingface.co/datasets/{HF_REPO}",
           "sample_rate_native": rates.pop(),
           "shards_read": shards,
           "rows_kept": len(have),
           "seconds_kept_this_run": round(sum(r["seconds"] for r in kept_all), 1),
           "resumed": bool(args.resume),
           "per_book": dict(sorted(per_book.items())),
           "selection": {"min_chars": args.min_chars,
                         "max_chars": args.max_chars,
                         "max_rows": args.max_rows,
                         "max_per_book": args.max_per_book},
           "elapsed_s": round(time.time() - t0, 1)}
    from experiments.provenance import provenance
    doc["provenance"] = provenance(__file__, args)
    with open(os.path.join(out, "corpus.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"\n  {len(have)} clips, {len(per_book)} books -> {out}")
    for book, n in sorted(per_book.items(), key=lambda kv: -kv[1]):
        print(f"    {book:36} {n}")


if __name__ == "__main__":
    main()
