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
"""
import argparse
import difflib
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

_WORD = re.compile(r"[\w']+", re.UNICODE)


def normalise(text):
    return [w.lower().replace("*", "") for w in _WORD.findall(text.replace("*", ""))]


def boundary_status(text_words, asr_words):
    """-> ("ok" | "first" | "last" | "both" | "empty") for one chunk.

    difflib's opcodes over the two word lists: the first text word is safe
    only if an 'equal' block starts at text index 0, the last only if an
    'equal' block ends at the last text index.
    """
    if not text_words or not asr_words:
        return "empty"
    ops = difflib.SequenceMatcher(a=text_words, b=asr_words, autojunk=False).get_opcodes()
    first_ok = any(tag == "equal" and i1 == 0 for tag, i1, i2, _, _ in ops)
    last_ok = any(tag == "equal" and i2 == len(text_words) for tag, i1, i2, _, _ in ops)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--metadata", required=True, help="preparer metadata.jsonl")
    ap.add_argument("--asr", required=True, help="dataset_temp/asr_segments.json")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    chunks = [json.loads(l) for l in open(args.metadata, encoding="utf-8") if l.strip()]
    segs = json.load(open(args.asr, encoding="utf-8"))["word_segments"]
    rows = audit(chunks, segs)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    alerted = sum(v for k, v in counts.items() if k != "ok")
    from experiments.provenance import provenance
    doc = {"chunks": len(rows), "alerted": alerted, "alert_rate": round(alerted / max(1, len(rows)), 4),
           "by_status": counts, "rows": rows, "provenance": provenance(__file__, args)}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"{len(rows)} chunks, {alerted} alerted ({doc['alert_rate']*100:.1f}%): {counts}")


if __name__ == "__main__":
    main()
