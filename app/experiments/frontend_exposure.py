"""How much of what we send the TTS is text it might read wrong?

Goal 5.1 asks that nothing UNSPEAKABLE reaches the engine, and that is met -
`verbalize_symbols` drops or speaks every character with no spoken form, 0 of
98,134 lines survive with one. This asks the different question the Chinese
TTS front-end test sets are built around: text that reaches the engine intact
and SPEAKABLE, but which the engine may render wrongly.

`1999` has a spoken form. It has two - "nineteen ninety-nine" and "one thousand
nine hundred and ninety-nine" - and nothing here checks which one comes out.
Same for `XIV`, `3rd`, `12-15`, `7:30`. The published Chinese front-end
datasets enumerate exactly these categories, plus polyphones, phonological
change and mixed-script text, because they are the places a front-end silently
differs from a reader.

WHAT THIS IS AND IS NOT. It counts EXPOSURE - how many lines contain each
category - and nothing else. Whether the engine gets them right needs synthesis
and an ASR read-back, which is GPU work. A category with zero exposure needs no
probe; a category with a thousand lines earns one. That is the whole purpose:
deciding what is worth listening to.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402

# Named so a reader can tell what each one is for. Ordered most to least
# likely to be read wrongly by a general engine.
CATEGORIES = (
    ("year", re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b"),
     "a four-digit year is read as a year or as a cardinal, never both"),
    ("roman_numeral", re.compile(
        r"\b(?=[MDCLXVI]{2,}\b)M*(?:C[MD]|D?C{0,3})(?:X[CL]|L?X{0,3})(?:I[XV]|V?I{0,3})\b"),
     "chapter numbering; also matches real words like MIX and DID"),
    ("grouped_number", re.compile(r"\b\d{1,3}(?:,\d{3})+\b"), "thousands separators"),
    ("ordinal", re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE), "3rd, 21st"),
    ("decimal", re.compile(r"\b\d+\.\d+\b"), "0.75"),
    ("digit_range", re.compile(r"\b\d+\s*[-–]\s*\d+\b"), "12-15, read as 'to' or 'minus'"),
    ("time_of_day", re.compile(r"\b\d{1,2}:\d{2}\b"), "7:30"),
    ("bare_digits", re.compile(r"\b\d+\b"), "any digit run; the superset"),
    ("cjk", re.compile(r"[぀-ヿ㐀-鿿]"),
     "Japanese or Chinese inside otherwise English narration"),
    ("non_ascii_latin", re.compile(r"[À-ɏ]"), "accented Latin"),
    ("emoji_or_symbol", re.compile(r"[☀-➿\U0001f300-\U0001faff]"),
     "should be zero: verbalize_symbols handles these"),
)


def scan(lines):
    """-> {category: line count}. A line counts once per category."""
    tally = collections.Counter()
    for text in lines:
        if not text:
            continue
        for name, pattern, _ in CATEGORIES:
            if pattern.search(text):
                tally[name] += 1
    return tally


def read_scripts(paths):
    """-> every line of speakable text across the given script files."""
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        entries = doc if isinstance(doc, list) else (doc.get("entries") or [])
        for entry in entries:
            if isinstance(entry, dict) and entry.get("text"):
                out.append(entry["text"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scripts", default=os.path.join(
        REPO, "ab_test_runtime", "retrofit_library"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.scripts, "*.json")))
    lines = read_scripts(paths)
    if not lines:
        raise SystemExit("no script lines under %s" % args.scripts)
    tally = scan(lines)

    rows = [{"category": name, "lines": tally[name],
             "share": round(tally[name] / len(lines), 5),
             "why_it_can_go_wrong": why}
            for name, _, why in CATEGORIES]
    numeric = sum(tally[n] for n in ("year", "roman_numeral", "grouped_number",
                                     "ordinal", "decimal", "digit_range",
                                     "time_of_day", "bare_digits"))
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "EXPOSURE only - how many lines contain each category. Whether "
                 "the engine renders them correctly needs synthesis and an ASR "
                 "read-back, and is not measured here",
        "scripts": len(paths),
        "lines": len(lines),
        "categories": rows,
        "lines_with_any_numeric_form": numeric,
        "note_cjk": ("zero CJK lines reach the engine, although five of eight "
                     "source books contain CJK between 23 and 779 times "
                     "(GOALS 5.1). Something upstream removes it; whether that "
                     "is normalisation working or content being lost is not "
                     "settled here"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%d scripts, %d speakable lines\n" % (len(paths), len(lines)))
    print("  %-18s %8s %8s" % ("category", "lines", "share"))
    for row in rows:
        if row["lines"]:
            print("  %-18s %8d %7.2f%%" % (row["category"], row["lines"],
                                           100 * row["share"]))
    zero = [r["category"] for r in rows if not r["lines"]]
    if zero:
        print("\n  no exposure: %s" % ", ".join(zero))
    return 0


if __name__ == "__main__":
    sys.exit(main())
