"""Does the translator's-note marker test actually work? And where did it go?

THE OBSERVATION THAT PROMPTED THIS. `lexicon_attributed.json` reports
`resolved_by_back_matter: {}` - the fallback resolved ZERO books - and the term
verdicts came out `ja: 7775, unattributed: 1606` with **no Chinese verdicts at
all**. A corpus that contains "The Husky and His White Cat Shizun" and "Silent
Reading (Mo Du)" cannot honestly have zero Chinese terms, so something in the
attribution is not doing its job.

TWO CANDIDATE EXPLANATIONS, AND THEY NEED SEPARATING.

  1. REACH. `back_matter_language` is only called when the publisher is
     `unknown`. Seven Seas maps to `mixed`, not `unknown`, so its 627 books -
     the single largest source of Chinese titles in this library - never reach
     the marker test at all. If this is the whole story, the fix is one
     condition.

  2. THE MARKERS THEMSELVES. Maybe they fire too rarely, or the 20,000-character
     tail is too short, or the >=2-hits-and-double-the-other rule is too strict.
     If so, widening reach changes nothing and the markers need work.

These predict different fixes, so this measures both rather than assuming
either.

VALIDATION BEFORE APPLICATION. The marker test is run FIRST on books whose
publisher already names the tradition unambiguously - J-Novel Club and Yen On
are Japanese, full stop. On those books the answer is known, so the markers can
be scored: how often do they agree, how often do they stay silent, and - the
number that decides whether any of this is usable - how often do they say
Chinese about a book that is certainly Japanese. A test that is confidently
wrong 5% of the time cannot be pointed at the ambiguous books, and the honest
move is to find that out here rather than after 600 books have been mislabelled.

That is the same failure this file exists to correct: `dantian` was labelled
Japanese on 2 books out of 37 because a rule was trusted before it was scored.

TAIL LENGTH IS SWEPT, not assumed. A translator's note sits at the end, but
"the end" is a guess about EPUB structure - afterwords, glossaries and preview
chapters all sit between. Measuring hit rates at several tail sizes says
whether 20,000 characters was ever the right window.

NO BOOK TEXT IS STORED OR PRINTED. The output is marker counts and per-book
labels, which is a fact about vocabulary, not an extract.
"""
import argparse
import collections
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.lexicon_language_attribution import (  # noqa: E402
    LANGUAGE_MARKERS, PUBLISHER_LANGUAGE, book_metadata)

# Publishers whose catalogue is one tradition only. These are the control: the
# marker test is scored against them, never used to overrule them.
UNAMBIGUOUS = {name: lang for name, lang in PUBLISHER_LANGUAGE.items()
               if lang in ("ja", "zh")}


def count_markers(text):
    """-> {'ja': n, 'zh': n} for one lowercased chunk of text."""
    return {lang: sum(len(re.findall(pattern, text)) for pattern in patterns)
            for lang, patterns in LANGUAGE_MARKERS.items()}


def verdict_from_counts(hits, minimum=2, ratio=2.0):
    """The same rule `back_matter_language` applies, factored out so the sweep
    can vary it instead of re-implementing it slightly differently."""
    ja, zh = hits.get("ja", 0), hits.get("zh", 0)
    if ja >= minimum and ja > zh * ratio:
        return "ja"
    if zh >= minimum and zh > ja * ratio:
        return "zh"
    return "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkpoint", default=os.path.join(
        REPO, "ab_test_runtime", "lexicon_scan", "checkpoint.json"))
    ap.add_argument("--tails", type=int, nargs="+", default=[20000, 60000, 200000],
                    help="tail sizes in characters to sweep")
    ap.add_argument("--limit", type=int, default=0, help="0 = every book")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_backmatter_probe.json"))
    args = ap.parse_args()

    from routers.script import extract_epub_text

    with open(args.checkpoint, encoding="utf-8") as handle:
        done = json.load(handle).get("done", {})
    books = [path for path, record in done.items() if "terms" in record]
    if args.limit:
        books = books[:args.limit]
    if not books:
        sys.exit("no books contributed terms; nothing to probe")

    largest = max(args.tails)
    rows, unreadable = [], 0
    for index, path in enumerate(books, 1):
        publisher = book_metadata(path)
        declared = publisher.get("language_of", "unknown")
        try:
            text = extract_epub_text(path)[-largest:].lower()
        except Exception:                                   # noqa: BLE001
            unreadable += 1
            continue
        row = {"publisher": publisher.get("publisher") or "(none)",
               "declared": declared, "by_tail": {}}
        for tail in sorted(args.tails):
            hits = count_markers(text[-tail:])
            row["by_tail"][str(tail)] = {"hits": hits,
                                         "verdict": verdict_from_counts(hits)}
        rows.append(row)
        if index % 500 == 0:
            print(f"  {index}/{len(books)} books", flush=True)

    # --- 1. Does the marker test agree with publishers that are not in doubt?
    validation = {}
    for tail in sorted(args.tails):
        control = [r for r in rows if r["declared"] in ("ja", "zh")]
        agree = sum(1 for r in control
                    if r["by_tail"][str(tail)]["verdict"] == r["declared"])
        silent = sum(1 for r in control
                     if r["by_tail"][str(tail)]["verdict"] == "unknown")
        wrong = [r for r in control
                 if r["by_tail"][str(tail)]["verdict"] not in ("unknown", r["declared"])]
        spoke = len(control) - silent
        validation[str(tail)] = {
            "control_books": len(control),
            "spoke": spoke,
            "silent": silent,
            "agreed": agree,
            "contradicted_publisher": len(wrong),
            # The number that decides usability: when it does answer, how often
            # is that answer right?
            "precision_when_it_speaks": round(agree / spoke, 4) if spoke else None,
            "coverage": round(spoke / len(control), 4) if control else None,
            "contradicted_examples": [
                {"publisher": r["publisher"], "hits": r["by_tail"][str(tail)]["hits"]}
                for r in wrong[:8]],
        }

    # --- 2. What would it resolve among the books the publisher cannot settle?
    application = {}
    for tail in sorted(args.tails):
        ambiguous = [r for r in rows if r["declared"] in ("mixed", "unknown")]
        verdicts = collections.Counter(
            r["by_tail"][str(tail)]["verdict"] for r in ambiguous)
        by_publisher = collections.defaultdict(collections.Counter)
        for r in ambiguous:
            by_publisher[r["publisher"]][r["by_tail"][str(tail)]["verdict"]] += 1
        application[str(tail)] = {
            "ambiguous_books": len(ambiguous),
            "verdicts": dict(verdicts),
            "resolved": len(ambiguous) - verdicts.get("unknown", 0),
            "by_publisher": {name: dict(counts) for name, counts
                             in sorted(by_publisher.items(),
                                       key=lambda kv: -sum(kv[1].values()))[:10]},
        }

    document = {
        "books_probed": len(rows),
        "unreadable": unreadable,
        "tails_swept": sorted(args.tails),
        "note": "marker counts only; no book text is stored. The control is "
                "books whose publisher names one tradition unambiguously, so "
                "the marker test is SCORED there before being applied to the "
                "books the publisher cannot settle.",
        "validation_against_unambiguous_publishers": validation,
        "application_to_ambiguous_books": application,
        "publisher_counts": dict(collections.Counter(
            r["publisher"] for r in rows).most_common()),
    }
    # Provenance, so a number can name the code that produced it. 58 of 95
    # artifact-writing scripts here omitted this; the gate artifacts goal 2.7
    # rests on are the cost - 87 files that cannot say what made them.
    try:
        from experiments.provenance import provenance
        document["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        document["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)

    print(f"\nprobed {len(rows)} books ({unreadable} unreadable)\n")
    print("VALIDATION - marker test against publishers that are not in doubt")
    print(f"{'tail':>8}{'control':>9}{'spoke':>7}{'right':>7}{'wrong':>7}{'precision':>11}{'coverage':>10}")
    for tail in sorted(args.tails):
        v = validation[str(tail)]
        print(f"{tail:>8}{v['control_books']:>9}{v['spoke']:>7}{v['agreed']:>7}"
              f"{v['contradicted_publisher']:>7}"
              f"{(v['precision_when_it_speaks'] if v['precision_when_it_speaks'] is not None else 0):>11.3f}"
              f"{(v['coverage'] if v['coverage'] is not None else 0):>10.3f}")
    print("\nAPPLICATION - books the publisher cannot settle")
    print(f"{'tail':>8}{'books':>8}{'resolved':>10}  verdicts")
    for tail in sorted(args.tails):
        a = application[str(tail)]
        print(f"{tail:>8}{a['ambiguous_books']:>8}{a['resolved']:>10}  {a['verdicts']}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
