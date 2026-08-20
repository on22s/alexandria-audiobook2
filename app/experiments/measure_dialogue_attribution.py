"""How much spoken dialogue was left attributed to the narrator?

WHY A SCRIPT AND NOT A GREP. The first pass at this counted any entry
containing a quotation mark and reported "74% of dialogue attributed to
NARRATOR". That number is wrong in a way worth naming: narration routinely
contains quotes it should keep - `the meaning of the word "Incognito"`, `Ariel
was posing thoughtfully saying "Hmm"` - and counting those as misattributed
dialogue inflates the figure. An instrument that fails the cases you already
know the answer to is not measuring the thing you asked about (Rule 21).

WHAT IT COUNTS. An entry is a SPOKEN LINE when the quoted span covers almost
the whole entry (>=85% of its characters). In this corpus that separates
cleanly: of quoted entries, 89-97% sit above the threshold or well below it,
with little in between. Narration-with-a-quote lands low; a line of dialogue
lands at 1.00.

THE AMBIGUITY IT REFUSES TO HIDE. These books are first person, so a fully
quoted line can be the narrator's own thought rather than someone's speech.
This corpus writes some of those with curly quotes, so the report gives both
figures - every fully quoted entry, and only the straight-quoted ones - and
never averages them into a single headline. Which is right needs a reader who
knows the book; the point here is to stop guessing at the size of the problem.
"""
import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

# NO STRAIGHT SINGLE QUOTES. `'[^']{4,}'` looked harmless and matched
# APOSTROPHES: "Feel like a dad. My supposedly-absent paternal instincts" has
# two, four characters apart, so the line counted as quoted. That is how
# arc4_volume10wn - a book with no quotation marks at all - passed the
# measurability floor and got scored anyway. Curly ‘ ’ stay: they cannot be an
# apostrophe in this corpus without a matching partner.
# 『』 TOO. It is a real dialogue mark in Japanese - here it carries telepathy
# and quoted concepts - and 205 entries of mushoku18 use it, 155 of mushoku23.
# Leaving it out did not produce a wrong number so much as an incomplete one:
# the 67 lines under the split name were all 『』 speech, and the detector saw
# exactly one of them, which is how "the repair changes nothing" nearly became
# the finding instead of "the detector cannot see this book's other quote mark".
QUOTED = re.compile(r'"[^"]{2,}"|“[^”]{2,}”|「[^」]{2,}」|『[^』]{2,}』|‘[^’]{2,}’')
CURLY = re.compile(r'“[^”]{2,}”')
SPOKEN_COVERAGE = 0.85


def quote_coverage(text):
    """-> share of the entry's characters that sit inside quotation marks."""
    text = (text or "").strip()
    if not text:
        return 0.0
    return sum(len(s) for s in QUOTED.findall(text)) / len(text)


def is_spoken_line(text):
    """Is this entry a line of dialogue, rather than narration containing one?"""
    return quote_coverage(text) >= SPOKEN_COVERAGE


QUOTED_SHARE_FLOOR = 0.06
# Enough entries carrying `spoken` that the measure rests on the map rather
# than on punctuation. Retrofitting locates 89-96% on this corpus, and a book
# far below that has not really been mapped.
RECORDED_SHARE_FLOOR = 0.50


def spoken_field_count(entries):
    """How many entries carry the recorded answer rather than a guess."""
    return sum(1 for e in entries if "spoken" in e)


def measurable(entries):
    """Does this book quote its dialogue at all?

    arc4_volume10wn does not: its dialogue entries carry no quotation marks
    ("Say... Petra, isn't this kinda close?" is attributed to SUBARU with no
    quotes anywhere), so a quote-based detector found 22 spoken lines in a
    6,173-entry book and cheerfully reported 59.1%. That number described the
    instrument, not the book.

    Refusing is the only honest output here. A metric that returns a plausible
    figure where it cannot see is the failure mode this project keeps paying
    for - the respelling scorer reported 38% rescue where the truth was 13%,
    and back_matter_language answered "no translator's note" 5,224 times
    because an exception handler returned a normal-looking answer.
    """
    if not entries:
        return False, "no entries"

    # THE RECORDED FACT OVERRIDES THE PUNCTUATION GATE. This refusal was
    # written when the only way to see dialogue was to look for quote marks,
    # and it is right for that case. It is wrong once `spoken` is present:
    # classify() already prefers the recorded answer, so a book whose entries
    # carry it can be measured no matter what its punctuation looks like.
    #
    # Leaving the gate ahead of that check refused 28 of 29 retrofitted books
    # on 2026-08-20 - every one of them carrying a source-derived map, and
    # every one reported as "does not mark dialogue with quotes" when the
    # source marks it 3,434 times. A guard built for the guess was still
    # blocking after the guess had been replaced.
    recorded = spoken_field_count(entries)
    if recorded / len(entries) >= RECORDED_SHARE_FLOOR:
        return True, None

    quoted = sum(1 for e in entries
                 if QUOTED.search(str(e.get("text", ""))))
    share = quoted / len(entries)
    if share < QUOTED_SHARE_FLOOR:
        return False, (f"only {share:.1%} of entries carry quotation marks; "
                       f"this book does not mark dialogue with quotes, so a "
                       f"quote-based measure cannot see it")
    return True, None


def classify(entries, narrator="NARRATOR"):
    spoken, narrator_spoken, curly_spoken, narrator_curly = 0, 0, 0, 0
    examples = []
    # PREFER THE RECORDED FACT. Scripts generated from 2026-08-18 carry
    # `spoken`, mapped from the source before the model rewrote anything.
    # Quote coverage is the fallback for everything written before that, and
    # it is a guess: it was wrong about apostrophes, about 『』, and about a
    # book whose quotes our own pipeline had stripped.
    for entry in entries:
        text = str(entry.get("text", ""))
        recorded = entry.get("spoken")
        if recorded is None:
            if not is_spoken_line(text):
                continue
        elif not recorded:
            continue
        spoken += 1
        is_narr = str(entry.get("speaker", "")).upper() == narrator
        curly = bool(CURLY.search(text))
        curly_spoken += curly
        if is_narr:
            narrator_spoken += 1
            narrator_curly += curly
            if len(examples) < 12:
                examples.append(text[:120])
    straight_spoken = spoken - curly_spoken
    straight_narrator = narrator_spoken - narrator_curly
    return {
        "spoken_lines": spoken,
        "left_with_narrator": narrator_spoken,
        "rate": round(narrator_spoken / spoken, 4) if spoken else None,
        "straight_quoted_only": {
            "spoken_lines": straight_spoken,
            "left_with_narrator": straight_narrator,
            "rate": round(straight_narrator / straight_spoken, 4) if straight_spoken else None,
            "why": "curly-quoted lines may be the first-person narrator's own "
                   "thought, which BELONGS to the narrator; this excludes them",
        },
        "examples_left_with_narrator": examples,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scripts", nargs="+", help="generated script JSON files")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "dialogue_attribution.json"))
    args = ap.parse_args()

    results = []
    for path in args.scripts:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        entries = doc if isinstance(doc, list) else doc.get("entries", [])
        ok, why = measurable(entries)
        row = {"book": os.path.basename(path).replace(".json", ""),
               "entries": len(entries), "measurable": ok}
        if not ok:
            row["not_measured"] = why
            results.append(row)
            print(f"{row['book']}: NOT MEASURED - {why}")
            continue
        row.update(classify(entries))
        results.append(row)
        rate = row["rate"]
        strict = row["straight_quoted_only"]["rate"]
        print(f"{row['book']}: {row['spoken_lines']} spoken lines, "
              f"{row['left_with_narrator']} left with NARRATOR "
              f"({rate:.1%})" if rate is not None else f"{row['book']}: no spoken lines")
        if strict is not None:
            print(f"    excluding curly-quoted (possible narrator thought): "
                  f"{strict:.1%}")

    doc = {"status": "complete", "candidates_considered": len(args.scripts),
           "spoken_line_threshold": SPOKEN_COVERAGE,
           "results": results, "provenance": provenance(__file__, args)}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
