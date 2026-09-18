"""A quote-mark segmenter, and how far it gets on PDNC (issue #588).

Pass 1 of the three-pass pipeline asks an LLM which stretches of a chunk are
speech. For a book that marks its dialogue with quote marks that is a
question punctuation already answers, and every Pass-1 call is a place an
error can start. This module is the deterministic alternative, and
`measure_on_pdnc` scores it against the 28 PDNC novels, whose gold labels
every quotation span by hand.

`segment(text)` -> [(start, end)] mark-exclusive spans:

- a straight `"` alternates open / close; `“ ”` pair by shape;
- `‘` opens only after whitespace or punctuation and `’` closes only when
  not followed by a letter, so apostrophes (`don’t`, `Tom’s`) stay
  narration;
- a paragraph break inside an open quote closes the span, matching the
  convention that a multi-paragraph speech re-opens its mark on each
  paragraph and PDNC's one-sub-span-per-paragraph gold.

Scoring: a gold span is FOUND when one predicted span covers >= 95% of its
characters; a predicted span is TRUE when >= 50% of it lies inside a gold
span. PDNC deliberately excludes quoted thoughts, letters and headings, so
its precision here understates the segmenter (those spans are quoted text
the pipeline can still hand to Pass 2); recall is the number that decides.
"""
import argparse
import ast
import bisect
import csv
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

PARAGRAPH = "\n\n"


def segment(text):
    spans, open_at, kind, i, n = [], None, None, 0, len(text)
    while i < n:
        c = text[i]
        if open_at is not None:
            if text.startswith(PARAGRAPH, i):
                spans.append((open_at, i))
                open_at = None
                i += 2
                continue
            closes = ((c == '"' and kind == '"') or (c == "”" and kind == "“")
                      or (c == "’" and kind == "‘" and not (i + 1 < n and text[i + 1].isalpha())))
            if closes:
                spans.append((open_at, i))
                open_at = None
        elif c in '"“' or (c == "‘" and (i == 0 or not text[i - 1].isalnum())):
            open_at, kind = i + 1, c
        i += 1
    return spans


def _covered(spans, starts, s, e, fraction):
    """Does one span in `spans` overlap [s, e) by >= fraction of [s, e)?"""
    j = bisect.bisect_right(starts, s) - 1
    for k in range(max(0, j - 1), min(len(spans), j + 2)):
        other_s, other_e = spans[k]
        overlap = max(0, min(other_e, e) - max(other_s, s))
        if overlap >= fraction * (e - s):
            return True
    return False


def score_novel(text, gold_spans, predicted):
    gs = sorted(gold_spans)
    ps = sorted(predicted)
    gstarts, pstarts = [g[0] for g in gs], [p[0] for p in ps]
    found = sum(_covered(ps, pstarts, s, e, 0.95) for s, e in gs)
    true = sum(_covered(gs, gstarts, a, b, 0.5) for a, b in ps)
    return {"gold": len(gs), "predicted": len(ps), "found": found, "true": true,
            "recall": found / len(gs) if gs else None,
            "precision": true / len(ps) if ps else None,
            "marks": {m: text.count(m) for m in '"“‘'}}


def measure_on_pdnc(folder):
    out = {}
    for novel in sorted(os.listdir(folder)):
        quote_path = os.path.join(folder, novel, "quotation_info.csv")
        text_path = os.path.join(folder, novel, "novel_text.txt")
        if not (os.path.exists(quote_path) and os.path.exists(text_path)):
            continue
        text = open(text_path, encoding="utf-8").read()
        gold = [tuple(span) for q in csv.DictReader(open(quote_path, encoding="utf-8"))
                for span in ast.literal_eval(q["quoteByteSpans"] or "[]")]
        out[novel] = score_novel(text, gold, segment(text))
    return out


def main():
    from experiments.provenance import provenance
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--folder", required=True, help="PDNC data directory (one folder per novel)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    novels = measure_on_pdnc(args.folder)
    gold = sum(r["gold"] for r in novels.values())
    pred = sum(r["predicted"] for r in novels.values())
    pooled = {"gold": gold, "predicted": pred,
              "recall": sum(r["found"] for r in novels.values()) / gold,
              "precision": sum(r["true"] for r in novels.values()) / pred}
    for name, r in novels.items():
        print("%-28s gold %5d pred %5d recall %5.1f precision %5.1f" % (
            name, r["gold"], r["predicted"], 100 * r["recall"], 100 * r["precision"]))
    print("pooled recall %.2f precision %.2f over %d novels" % (
        100 * pooled["recall"], 100 * pooled["precision"], len(novels)))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"experiment": "quote_segmenter_pdnc", "novels": novels, "pooled": pooled,
                   "scoring": "gold FOUND when one predicted span covers >=95% of it; "
                              "predicted TRUE when >=50% of it lies in a gold span",
                   "provenance": provenance(__file__, args)}, fh, indent=1)


if __name__ == "__main__":
    main()
