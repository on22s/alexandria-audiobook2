"""Fetch one LibriQuote-test reader as two LJSpeech-shaped corpora.

LibriQuote (Michel, Epure, Cerisara; Findings of ACL 2026) pairs every
character quotation in its test split with the nearest narration utterance
by the same reader, and ships both at 16 kHz under `test_audios/`. That is
exactly the contrast Piits et al. (LREC 2022) trained on - character speech
against narration from one voice - so one reader becomes two corpora here,
`<out>/quotes` and `<out>/narration`, each written the way `hifitts_fetch.py`
writes a reader (wavs/<id>.wav, metadata.csv, corpus.json) so
`ljspeech_prepare.py` and everything after it run unchanged. Clip ids are
`<book>-<chapter>_<n>` so the split-by-source-work rule holds.

Licence: CC BY-NC 4.0 - evidence only, never a shipped adapter.
"""
import argparse
import collections
import io
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

HF_REPO = "gasmichel/LibriQuote"
API = f"https://huggingface.co/api/datasets/{HF_REPO}"
RESOLVE = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/"
LICENCE = "CC BY-NC 4.0 (LibriQuote); LibriVox recordings, Gutenberg texts"


def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
            last = exc
    raise last


def list_chapters(speaker):
    files = [s["rfilename"] for s in json.loads(fetch(API))["siblings"]]
    out = []
    for f in files:
        parts = f.split("/")
        if parts[0] == "benchmark" and len(parts) == 4 and parts[2] == speaker and f.endswith(".json"):
            out.append(f)
    return sorted(out)


def write_corpus(out, rows, speaker, kind):
    import soundfile as sf
    wavs = os.path.join(out, "wavs")
    os.makedirs(wavs, exist_ok=True)
    per_book, rates, seconds = collections.Counter(), set(), 0.0

    def field(text):
        return " ".join(text.split()).replace("|", "/")

    with open(os.path.join(out, "metadata.csv"), "w", encoding="utf-8") as fh:
        for row in rows:
            audio, rate = sf.read(io.BytesIO(row["audio"]), dtype="float32")
            rates.add(int(rate))
            sf.write(os.path.join(wavs, row["id"] + ".wav"), audio, int(rate))
            fh.write(f"{row['id']}|{field(row['text'])}|{field(row['text'])}\n")
            per_book[row["book"]] += 1
            seconds += len(audio) / rate
    if len(rates) != 1:
        sys.exit(f"mixed native sample rates {sorted(rates)}")
    doc = {"corpus": f"LibriQuote-test reader {speaker} ({kind})",
           "licence": LICENCE, "source": f"https://huggingface.co/datasets/{HF_REPO}",
           "sample_rate_native": rates.pop(), "rows_kept": len(rows),
           "seconds_kept": round(seconds, 1), "per_book": dict(sorted(per_book.items()))}
    from experiments.provenance import provenance
    doc["provenance"] = provenance(__file__, None)
    with open(os.path.join(out, "corpus.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"  {kind}: {len(rows)} clips, {seconds/60:.1f} min, books {dict(per_book)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--speaker", required=True, help="LibriQuote-test reader id, e.g. 4992")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-per-chapter", type=int, default=0)
    args = ap.parse_args()
    chapters = list_chapters(args.speaker)
    if not chapters:
        sys.exit(f"no benchmark chapters for reader {args.speaker}")
    print(f"reader {args.speaker}: {len(chapters)} chapters")
    quotes, narration, seen_narr = [], [], set()
    for path in chapters:
        book, chapter = path.split("/")[1], os.path.splitext(path.split("/")[3])[0]
        doc = json.loads(fetch(RESOLVE + path))
        qs = doc["quotations"][:args.max_per_chapter] if args.max_per_chapter else doc["quotations"]
        for q in qs:
            n = q["original_index"]
            cid = f"{book}-{chapter}_{n}"
            quotes.append({"id": cid, "book": book, "text": q["text"],
                           "audio": fetch(RESOLVE + "test_audios/" + q["audio_path"])})
            narr = q.get("narration") or {}
            npath = narr.get("audio_path")
            if npath and npath not in seen_narr and narr.get("text"):
                seen_narr.add(npath)
                narration.append({"id": cid, "book": book, "text": narr["text"],
                                  "audio": fetch(RESOLVE + "test_audios/" + npath)})
        print(f"  {path}: {len(qs)} quotes (running {len(quotes)} / narration {len(narration)})", flush=True)
    write_corpus(os.path.join(args.out, "quotes"), quotes, args.speaker, "quotes")
    write_corpus(os.path.join(args.out, "narration"), narration, args.speaker, "narration")


if __name__ == "__main__":
    main()
