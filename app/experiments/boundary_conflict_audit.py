"""Flag preparer chunk boundaries where the ASR and the source text disagree.

Boeffard et al. (LREC 2012) split an audiobook on pauses, recognised each
piece, aligned the recognition to the book by word-level Levenshtein, and
raised an alert whenever the alignment at a segment's FIRST or LAST word was
an insertion or a deletion - the cases where a cut cannot be trusted. On 11
hours of Proust that fired on 8.3% of segments, and a manual check found a
real split error behind 8% of the alerts. The alert rate is a quality number
for the preparer that nobody has measured here.

Input: the preparer's `metadata.jsonl` (chunk `start`/`end` seconds and the
source `text` it assigned) and `dataset_temp/asr_segments.json` (Wav2Vec2
`word_segments` with `start`/`end`/`word`). For each chunk, the ASR words
inside its time span are aligned to its text; a chunk is ALERTED when its
first or last text word is not matched by an equal ASR word (after
normalisation), i.e. the boundary word was inserted, deleted or substituted.

`--zip` is the same rule on a finished preparer dataset (the zips the
library is trained from): no word timings survive in those, so each clip is
transcribed on its own with whisper.cpp and its first/last text word checked
against the clip's own transcript. A cut that dropped or split a boundary
word shows up the same way.
"""
import argparse
import difflib
import io
import json
import os
import re
import sys
import tempfile
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

_WORD = re.compile(r"[\w']+", re.UNICODE)


def normalise(text):
    return [w.lower().replace("*", "") for w in _WORD.findall(text.replace("*", ""))]


FUZZY = 0.6


def _close(a, b):
    """A boundary word the ASR misspelled is not a cut error: Hero of Ages
    (2026-09-14) alerted on fatren/fattron, druffel/druffle, terris/terrorists
    and hemalurgy/himmelergy - half its 24% - with exact matching."""
    return difflib.SequenceMatcher(a=a, b=b).ratio() >= FUZZY


def boundary_status(text_words, asr_words):
    """-> ("ok" | "first" | "last" | "both" | "empty") for one chunk.

    difflib's opcodes over the two word lists: the first text word is safe
    if an 'equal' block starts at text index 0, or the block there is a
    'replace' whose first ASR word is a close spelling of it; the last word
    likewise at the end.
    """
    if not text_words or not asr_words:
        return "empty"
    ops = difflib.SequenceMatcher(a=text_words, b=asr_words, autojunk=False).get_opcodes()
    first_ok = any((tag == "equal" and i1 == 0) or
                   (tag == "replace" and i1 == 0 and _close(text_words[0], asr_words[j1]))
                   for tag, i1, i2, j1, j2 in ops)
    last_ok = any((tag == "equal" and i2 == len(text_words)) or
                  (tag == "replace" and i2 == len(text_words) and _close(text_words[-1], asr_words[j2 - 1]))
                  for tag, i1, i2, j1, j2 in ops)
    if first_ok and last_ok:
        return "ok"
    if not first_ok and not last_ok:
        return "both"
    return "first" if not first_ok else "last"


def audit(chunks, word_segments, slack=0.15):
    """-> per-chunk statuses. `slack` widens the time window so a boundary word
    the ASR timed a few frames outside the cut still counts as inside."""
    words = sorted(word_segments, key=lambda w: w["start"])
    out = []
    for c in chunks:
        inside = [w["word"] for w in words
                  if w["start"] >= c["start"] - slack and w["end"] <= c["end"] + slack]
        status = boundary_status(normalise(c["text"]), normalise(" ".join(inside)))
        out.append({"audio_filepath": c.get("audio_filepath"), "start": c["start"], "end": c["end"],
                    "status": status, "asr_words": len(inside)})
    return out


def audit_zip(path, whisper_model, whisper_bin, language="en"):
    """-> per-clip statuses for a preparer dataset zip (root metadata.jsonl)."""
    import soundfile as sf
    from asr_backends import run_whisper_cpp
    z = zipfile.ZipFile(path)
    rows_in = [json.loads(l) for l in z.read("metadata.jsonl").decode("utf-8").splitlines() if l.strip()]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, c in enumerate(rows_in):
            audio, sr = sf.read(io.BytesIO(z.read(c["audio_filepath"])), dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            wav = os.path.join(tmp, "clip.wav")
            sf.write(wav, audio, sr)
            hyp, _ = run_whisper_cpp(wav, whisper_model, whisper_bin, language=language)
            asr_words = normalise(hyp or "")
            status = boundary_status(normalise(c["text"]), asr_words)
            out.append({"audio_filepath": c["audio_filepath"], "start": c.get("start"), "end": c.get("end"),
                        "status": status, "asr_words": len(asr_words)})
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(rows_in)}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--metadata", help="preparer metadata.jsonl")
    ap.add_argument("--asr", help="dataset_temp/asr_segments.json")
    ap.add_argument("--zip", help="instead: a finished preparer dataset zip, transcribed clip by clip")
    ap.add_argument("--whisper-cpp-bin", default=os.path.join(REPO, "whisper.cpp", "build", "bin", "whisper-cli"))
    ap.add_argument("--whisper-cpp-model", default=os.path.join(REPO, "whisper.cpp", "models", "ggml-base.en.bin"))
    ap.add_argument("--language", default="en")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if bool(args.zip) == bool(args.metadata and args.asr):
        ap.error("give --zip, or --metadata with --asr")
    if args.zip:
        rows = audit_zip(args.zip, args.whisper_cpp_model, args.whisper_cpp_bin, args.language)
    else:
        chunks = [json.loads(l) for l in open(args.metadata, encoding="utf-8") if l.strip()]
        segs = json.load(open(args.asr, encoding="utf-8"))["word_segments"]
        rows = audit(chunks, segs)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    alerted = sum(v for k, v in counts.items() if k != "ok")
    from experiments.provenance import provenance
    doc = {"source": os.path.abspath(args.zip or args.metadata), "chunks": len(rows), "alerted": alerted, "alert_rate": round(alerted / max(1, len(rows)), 4),
           "by_status": counts, "rows": rows, "provenance": provenance(__file__, args)}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"{len(rows)} chunks, {alerted} alerted ({doc['alert_rate']*100:.1f}%): {counts}")


if __name__ == "__main__":
    main()
