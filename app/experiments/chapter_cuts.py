"""Where do chapter headings fall in each gold book's segmented entries, and
which attribution windows would a chapter-aware chunker change?

Issue #522 (section 10) asks the chunker to prefer chapter boundaries. The
product's attribution pass reads the segmented entries in fixed windows of
25, so a heading can land anywhere inside a window and the model sees the end
of one chapter and the start of the next as one passage. This writes, per
book, the entry indices where a window should be forced to start:

- `chapter`: entries whose text carries a heading. Heading styles are per
  book (there is no shared convention across the four gold books), so the
  regexes are listed here rather than guessed from `Chapter N`.
- `control`: the SAME number of cuts at seeded random non-heading entries.
  Any cut shifts the phase of every later window, and window composition
  moves scores on its own; the control separates "cut at a chapter" from
  "cut somewhere".

Feed the file to lora_serving_eval.py --window-cuts with --cut-arm chapter
or control; the fixed-stride run is the baseline.
"""
import argparse
import json
import os
import random
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

# Verified against the segmented entries on 2026-09-14: index18's "Chapter N:"
# lines survive segmentation only on the copyright page; its real chapter
# starts are absorbed into narration entries, so it contributes no cut.
HEADINGS = {
    "index18": r"^Chapter \d+:",
    "owarimonogatari3": r"^\[Owarimonogatari 3\] Mayoi Hell \d{3}",
    "mushoku16": r"^(■|-{5,})\s*$",
    "grimgar03": r"^Grimgar of Fantasy and Ash: Volume 3\n+\d{1,2}\. [A-Z]",
}


def heading_indices(seg, pattern):
    rx = re.compile(pattern, re.M)
    return [i for i, e in enumerate(seg) if i and rx.search(e.get("text") or "")]


def control_indices(n, k, exclude, seed):
    rng = random.Random(seed)
    pool = [i for i in range(1, n) if i not in exclude]
    return sorted(rng.sample(pool, min(k, len(pool))))


def main():
    from lora_serving_eval import load_book, make_windows, norm
    from experiments.provenance import provenance
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--books", nargs="+", default=list(HEADINGS))
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    doc = {"books": {}, "provenance": provenance(__file__, args)}
    for book in args.books:
        gold, src, seg, roster, want = load_book(book)
        heads = heading_indices(seg, HEADINGS[book])
        control = control_indices(len(seg), len(heads), set(heads), args.seed)
        gold_idx = [i for i, e in enumerate(seg) if norm(e.get("text")) in want]
        fixed = {i: tuple(w) for w in make_windows(len(seg), args.batch_size) for i in w}
        info = {"entries": len(seg), "gold_rows": len(gold_idx), "pattern": HEADINGS[book],
                "chapter": heads, "control": control}
        for arm in ("chapter", "control"):
            wins = {i: tuple(w) for w in make_windows(len(seg), args.batch_size, info[arm]) for i in w}
            info[f"{arm}_windows"] = len(set(wins.values()))
            info[f"{arm}_gold_rows_moved"] = sum(1 for i in gold_idx if wins[i] != fixed[i])
            info[f"{arm}_gold_rows_within_{args.batch_size}_after_cut"] = sum(
                1 for i in gold_idx if any(0 <= i - c < args.batch_size for c in info[arm]))
        info["fixed_windows"] = len(set(fixed.values()))
        doc["books"][book] = info
        print(f"{book}: {len(heads)} heading cuts; windows fixed {info['fixed_windows']} "
              f"chapter {info['chapter_windows']} control {info['control_windows']}; "
              f"gold rows within {args.batch_size} entries after a cut: "
              f"chapter {info[f'chapter_gold_rows_within_{args.batch_size}_after_cut']} "
              f"control {info[f'control_gold_rows_within_{args.batch_size}_after_cut']} of {len(gold_idx)}")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)


if __name__ == "__main__":
    main()
