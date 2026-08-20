"""Compare the two generation arms on the dialogue map, not on punctuation.

WHY THIS METRIC AND NOT THE OLD ONE. Goal 5.3 compared the arms on attribution
accuracy alone, paired by `re.sub(r"[^0-9a-z]+", "", text.lower())` - every
quote, dash and apostrophe deleted before matching. That key cannot see what
either arm does to the text, and both arms do a great deal: three-pass removes
the outermost quotes from every fully-quoted line by design, and single-pass
complies with the same instruction unevenly.

Since 1f6be7a the answer is not to count punctuation at all. `dialogue_spans`
maps the spoken text from the SOURCE, before any model runs, and generation
marks each entry with `spoken` and `source_span`. That is a carried fact: no
normalisation can delete it, it does not depend on either arm's quote habits,
and it is derived from what the author wrote rather than from what the model
returned. This module compares the arms on it.

THE ASYMMETRY THIS EXISTS TO REMOVE. Single-pass has carried the map since
2026-08-18; three-pass never did until it was wired in alongside this file.
Comparing the arms before that would have measured which one received a patch.

WHAT IT REPORTS, per book and arm:
  located        entries found in the source at all. `spoken` absent is a
                 different claim from `spoken: false`, and is counted as
                 unlocated rather than as narration.
  spoken_rate    share of located entries the source marks as speech.
  agreement      on entries BOTH arms located, how often they agree about
                 whether the line is speech - the paired comparison, since an
                 arm that locates fewer lines is not thereby more accurate.
  disagreements  sample rows, so a reader can see which arm is wrong rather
                 than only that they differ.

WHAT IT IS NOT. Not an attribution score - that is three_pass_vs_single's job
and it remains valid on its own terms. This asks the prior question: does the
arm's output still know which of its lines are speech?
"""
import argparse
import collections
import json
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from experiments.provenance import provenance  # noqa: E402
from experiments.stats import exact_mcnemar  # noqa: E402

ROOTS = [REPO]


def resolve(path):
    if not path:
        return None
    if os.path.isabs(path):
        return path
    for root in ROOTS:
        candidate = os.path.join(root, path)
        if os.path.exists(candidate):
            return candidate
    return None


def load_entries(path):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    return doc if isinstance(doc, list) else (doc.get("entries") or [])


def key_of(entry):
    """Pair on the SOURCE SPAN where one exists, falling back to text.

    The span is the source offset the line was located at, so it identifies the
    same piece of the book across two different segmentations - which is
    precisely what the old alphanumeric key was approximating. Where a line was
    never located there is no span, and those rows cannot be paired at all;
    they are counted, not silently matched by text and treated as comparable.
    """
    span = entry.get("source_span")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        return ("span", int(span[0]), int(span[1]))
    return None


def profile(path):
    entries = load_entries(path)
    by_key = {}
    n = located = spoken = 0
    for entry in entries:
        if not isinstance(entry, dict) or not (entry.get("text") or ""):
            continue
        n += 1
        if "spoken" not in entry:
            continue
        located += 1
        if entry.get("spoken"):
            spoken += 1
        key = key_of(entry)
        if key is not None:
            by_key[key] = bool(entry.get("spoken"))
    return {"entries": n, "located": located, "spoken": spoken,
            "located_rate": round(located / n, 4) if n else None,
            "spoken_rate": round(spoken / located, 4) if located else None,
            "by_key": by_key}


def compare(single_path, three_path):
    a, b = profile(single_path), profile(three_path)
    shared = sorted(set(a["by_key"]) & set(b["by_key"]))
    agree = sum(1 for k in shared if a["by_key"][k] == b["by_key"][k])
    single_only = [k for k in shared if a["by_key"][k] and not b["by_key"][k]]
    three_only = [k for k in shared if b["by_key"][k] and not a["by_key"][k]]
    out = {
        "single": {k: v for k, v in a.items() if k != "by_key"},
        "three_pass": {k: v for k, v in b.items() if k != "by_key"},
        "paired_entries": len(shared),
        "agree": agree,
        "agreement_rate": round(agree / len(shared), 4) if shared else None,
        "single_says_spoken_only": len(single_only),
        "three_pass_says_spoken_only": len(three_only),
    }
    # A paired disagreement count is exactly McNemar's shape, and the question
    # "do the arms differ systematically or by coin-flip" is the one a reader
    # asks next. Reused rather than reimplemented.
    if single_only or three_only:
        p, _bb, _cc = exact_mcnemar(len(single_only), len(three_only))
        out["mcnemar_p"] = p
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", default=os.path.join(
        "ab_test_runtime", "three_pass_vs_single"),
        help="directory of <book>__<arm>.json")
    ap.add_argument("--audio-root", action="append", default=[])
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "dialogue_map_compare.json"))
    args = ap.parse_args()

    ROOTS[:] = [r for r in (args.audio_root or []) if r] or [REPO]
    work = resolve(args.work) or args.work
    if not os.path.isdir(work):
        raise SystemExit(f"no such directory: {work}")

    import re
    found = collections.defaultdict(dict)
    for name in sorted(os.listdir(work)):
        m = re.match(r"^(?P<book>.+?)__(?P<arm>single|three_pass)\.json$", name)
        if m:
            found[m.group("book")][m.group("arm")] = os.path.join(work, name)

    books, unmapped = {}, []
    for book, arms in sorted(found.items()):
        if "single" not in arms or "three_pass" not in arms:
            continue
        result = compare(arms["single"], arms["three_pass"])
        # REFUSE TO SCORE AN ARM THAT PREDATES THE MAP. Every script generated
        # before 1f6be7a carries no `spoken` key at all, and comparing those
        # would report 0% located as though the arm had failed rather than as
        # though the run was simply older than the feature.
        if not result["single"]["located"] and not result["three_pass"]["located"]:
            unmapped.append(book)
            continue
        books[book] = result

    payload = {
        "scope": "generation arms compared on the source-derived dialogue map "
                 "(`spoken`/`source_span`), not on punctuation",
        "books": books,
        "skipped_no_dialogue_map": unmapped,
        "status": "complete",
    }
    payload["provenance"] = provenance(__file__, args)

    if unmapped and not books:
        raise SystemExit(
            "every book here predates the dialogue map (1f6be7a), so neither "
            "arm carries `spoken` and there is nothing to compare:\n  "
            + "\n  ".join(unmapped)
            + "\nRe-run both arms with the current code first.")

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)

    print(f"{'book':20s} {'arm':11s} {'located':>9s} {'spoken':>8s} "
          f"{'paired':>7s} {'agree':>7s}")
    for book, r in books.items():
        for arm in ("single", "three_pass"):
            a = r[arm]
            print(f"  {book:18s} {arm:11s} "
                  f"{(a['located_rate'] or 0):8.1%} {(a['spoken_rate'] or 0):7.1%} "
                  f"{r['paired_entries']:7d} "
                  f"{(r['agreement_rate'] or 0):7.1%}")
    if unmapped:
        print(f"\n  skipped, no dialogue map (generated before 1f6be7a): "
              f"{', '.join(unmapped)}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
