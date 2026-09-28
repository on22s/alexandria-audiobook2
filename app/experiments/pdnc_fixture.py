"""Turn PDNC novels into fixtures, so generalisation stops needing a person.

Every result in this investigation rests on 772 rows across four light novels,
all translated, all contemporary. Whether the adapter's +5.4 is a property of
the adapter or of that corpus has been the largest open risk, and I repeatedly
described testing it as blocked on someone hand-labelling a fifth book. That
was wrong: the Project Dialogism Novel Corpus already carries speaker-attributed
quotations for 28 public-domain novels.

WHAT PDNC GIVES THAT OUR OWN GOLD DOES NOT.

  quoteType     Explicit / Implicit / Anaphoric per quotation. Pride and
                Prejudice is 26% explicit and 50% IMPLICIT, so this is not the
                soft benchmark classic prose sounds like - and it allows
                stratifying results by how much attribution cue exists, which
                our own fixtures never labelled.
  Aliases       curated per character, replacing alias groups we assembled by
                hand and repeatedly got wrong.
  Category      major / intermediate / minor, replacing the 5%-of-lines
                frequency proxy used for "lead character".

WHAT IT DOES NOT TEST. PDNC annotates quotations in raw novel text, so this
measures ATTRIBUTION only and bypasses segmentation entirely. It says nothing
about the misfiling problem, and a book where every quotation is already
delimited is an easier world than the segmenter's output.

LICENCE. The novels are public domain; the repository declares no licence for
the ANNOTATIONS. Fine for internal evaluation, unresolved for anything shipped.
"""
import argparse, ast, collections, csv, json, os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SPECIAL = {"UNKNOWN", "UNNAMED", "NOT_DIALOGUE"}

# PDNC MARKS NON-CHARACTERS WITH A LEADING UNDERSCORE, and this builder did
# not know it. `_group`, `_unknowable` and `_narr` are pseudo-speakers in
# character_info.csv, present in 21 of the corpus's 28 novels but absent from
# PrideAndPrejudice and TheSignOfTheFour - which is why the first fixtures
# looked clean and the fault stayed hidden until the Austen books were built.
#
# Two distinct harms, and only the second changes a score:
#   roster  the model was offered `_GROUP` and `_UNKNOWABLE` as candidates in
#           some books and not others, so a cross-book comparison was partly a
#           comparison of roster contents. TheAwakening carries them and is one
#           of the three books behind the 89.1% author-held-out result.
#   gold    MansfieldPark had 16 rows whose expected_speaker WAS `_GROUP` or
#           `_UNKNOWABLE`: unanswerable rows scored as ordinary ones.
#
# Matched by name rather than by prefix would have been the fragile choice -
# a 22nd novel may add a fourth marker - so the prefix is the rule and
# test_pdnc_fixture_excludes_pseudo_speakers pins both halves.
def is_pseudo_speaker(name):
    """True for PDNC's non-character markers (_group, _unknowable, _narr)."""
    return name.strip().startswith("_")


# PDNC ALSO LISTS NAMES THAT NEVER SPEAK, and a correct short answer can land on
# one. TheSignOfTheFour's Wooden-Legged Man (100 quotations, alias Jonathan
# Small) has a separate entry "Small" with zero quotations; a model answering
# SMALL for his confession named the right man and was scored as naming another
# character. On the nine-novel panel that one entry flipped 10 of 57 paired
# adapter verdicts (2026-09-28): it hid an adapter answering MORDECAI SMITH
# where the base said SMALL, and credited adapters that had learned to echo
# PDNC's main name "WOODEN-LEGGED MAN" rather than to attribute better.
#
# Hand-reviewed pairs only. An automatic rule ("a silent name that is part of
# the gold name") also accepts SMALL for "A Small, Dark, Brisk Man", who is a
# different person, and MISS ELLIOT for Anne, whom the novel never calls that.
# Rejected on review and deliberately absent: Persuasion MISS ELLIOT, Mansfield
# Park BERTRAM (five speaking Bertrams), AgeOfInnocence CATHERINE (undecided).
# build() refuses a pair whose short name has any quotation, so a corpus update
# that gives one a line cannot turn this into a wrong merge silently.
REVIEWED_SILENT_ALIASES = {
    "TheSignOfTheFour": {"Wooden-Legged Man": "Small"},
    "PrideAndPrejudice": {"Mr. Denney": "Denny"},
    "HardTimes": {"Josiah Bounderby": "Bounderby",
                  "Mr. James Harthouse": "Harthouse"},
    "AHandfulOfDust": {"Reggie St Cloud": "Reggie"},
}


def load_novel(folder, name):
    legacy = os.path.join(folder, f"{name}_quotes.csv")
    if os.path.exists(legacy):
        quote_path = legacy
        char_path = os.path.join(folder, f"{name}_chars.csv")
        text_path = os.path.join(folder, f"{name}.txt")
    else:
        novel_dir = os.path.join(folder, name)
        quote_path = os.path.join(novel_dir, "quotation_info.csv")
        char_path = os.path.join(novel_dir, "character_info.csv")
        text_path = os.path.join(novel_dir, "novel_text.txt")
    with open(quote_path, encoding="utf-8") as quotes_file:
        quotes = list(csv.DictReader(quotes_file))
    with open(char_path, encoding="utf-8") as chars_file:
        chars = list(csv.DictReader(chars_file))
    with open(text_path, encoding="utf-8") as text_file:
        text = text_file.read()
    return quotes, chars, text


def build(folder, name, context_chars=400):
    quotes, chars, text = load_novel(folder, name)
    aliases, category, roster = [], {}, []
    for c in chars:
        main = (c.get("Main Name") or "").strip()
        if not main or is_pseudo_speaker(main):
            continue
        roster.append(main.upper())
        category[main.upper()] = (c.get("Category") or "").strip()
        try:
            alt = ast.literal_eval(c.get("Aliases") or "set()")
        except Exception:
            alt = set()
        group = {main.upper()} | {str(a).upper() for a in alt if str(a).strip()}
        silent = REVIEWED_SILENT_ALIASES.get(name, {}).get(main)
        if silent:
            spoken = sum(1 for q in quotes
                         if (q.get("speaker") or "").strip() == silent)
            if spoken:
                raise ValueError(f"{name}: {silent!r} has {spoken} quotations; "
                                 f"it is a speaker, not an alias of {main!r}")
            group.add(silent.upper())
        if len(group) > 1:
            aliases.append(sorted(group))

    entries, skipped = [], collections.Counter()
    for n, q in enumerate(quotes):
        line = (q.get("quoteText") or "").strip()
        speaker = (q.get("speaker") or "").strip().upper()
        if not line or not speaker or speaker in SPECIAL:
            skipped["no speaker"] += 1
            continue
        if is_pseudo_speaker(speaker):
            skipped["pseudo speaker"] += 1
            continue
        # Context comes from the byte spans, so the model sees the same
        # surroundings a reader would - not a reconstruction.
        try:
            spans = ast.literal_eval(q.get("quoteByteSpans") or "[]")
            start = min(s[0] for s in spans)
            end = max(s[1] for s in spans)
        except Exception:
            skipped["no span"] += 1
            continue
        entries.append({
            "id": f"{name}-{n:05d}",
            "line": line,
            "expected_speaker": speaker,
            "quote_type": (q.get("quoteType") or "").strip(),
            "category": category.get(speaker, "unknown"),
            "prev_context": text[max(0, start - context_chars):start].strip(),
            "next_context": text[end:end + context_chars].strip(),
        })
    return {"book": name, "source": "PDNC", "entries": entries,
            "aliases": aliases, "roster": sorted(set(roster)),
            "skipped": dict(skipped)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--folder", required=True, help="directory of PDNC downloads")
    # CONTEXT WIDTH AS A FLAG, NOT A DEFAULT. It was reachable only by editing
    # build()'s signature, which is why exactly one width other than 400 was
    # ever produced. Widening 400 -> 3,200 was worth +12.7 points and the curve
    # had not flattened; the closest published analogue (Llama-3 8b on PDNC,
    # arXiv 2406.11380) chunks at 4096 tokens, roughly 16,000 characters.
    ap.add_argument("--context-chars", type=int, default=400,
                    help="characters of context kept either side of a line")
    ap.add_argument("--suffix", default=None,
                    help="fixture name suffix, e.g. w8000; defaults to none")
    ap.add_argument("--novels", nargs="+", required=True)
    ap.add_argument("--out_dir", default=REPO + "/app/fixtures")
    args = ap.parse_args()

    for name in args.novels:
        fx = build(args.folder, name, context_chars=args.context_chars)
        by_type = collections.Counter(e["quote_type"] for e in fx["entries"])
        by_cat = collections.Counter(e["category"] for e in fx["entries"])
        suffix = f"_{args.suffix}" if args.suffix else ""
        out = os.path.join(args.out_dir,
                           f"attribution_gold_pdnc_{name.lower()}{suffix}.json")
        json.dump(fx, open(out, "w"), ensure_ascii=False, indent=1)
        print(f"{name}: {len(fx['entries'])} quotations, "
              f"{len(fx['roster'])} characters, {len(fx['aliases'])} alias groups")
        print(f"   by type: {dict(by_type)}")
        print(f"   by category: {dict(by_cat)}")
        if fx["skipped"]:
            print(f"   skipped: {fx['skipped']}")
        print(f"   wrote {out}")


if __name__ == "__main__":
    main()
