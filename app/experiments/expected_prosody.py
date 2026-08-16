"""What the pitch SHOULD do, from the text alone.

WHY THIS IS DIFFERENT FROM prosody_fidelity. That script compares a generated
take against a human reading the same line, which is the right measurement and
has one hard limit: it needs a human recording of that exact sentence. Eval
sets have those. Actual audiobooks do not - which means the two languages
nobody here can audition are also the two we can only check on material we
happen to have a reference for.

These tools remove the reference. Japanese and Chinese both carry meaning in
pitch, and in both the intended pitch is RECOVERABLE FROM THE TEXT:

    JAPANESE  pyopenjtalk (OpenJTalk) returns full-context labels whose A:
              field gives each mora's position relative to its accent
              nucleus. That is the accent pattern - where the pitch must
              fall - for any sentence, no recording needed.

              VERIFIED ON MINIMAL PAIRS, which is the test that matters:
              箸 (chopsticks) comes back 2 morae accent 1 and 橋 (bridge)
              2 morae accent 2, the textbook distinction that is invisible
              to any metric comparing transcribed characters. 雨 likewise.
              KNOWN LIMIT: phrase segmentation still emits an occasional
              spurious one-mora phrase at the start of a longer string
              (日本語 splits 1 + 4). The accent VALUES are right, the phrase
              boundaries are not always, so count accents rather than
              phrases until that is fixed.

    CHINESE   pypinyin returns the tone of every syllable. Tone 1 is high and
              level, 2 rises, 3 dips, 4 falls. A syllable produced with the
              wrong contour is a different word.

So a wrong accent or a wrong tone becomes checkable on any generated line of
any book, against what the language says it should have been rather than
against a recording we may not own.

FOUND BY SEARCHING GITHUB IN JAPANESE AND CHINESE. Neither surfaced in the
English searches for TTS evaluation tooling, because neither is a TTS
evaluation tool - they are text-processing components of Japanese and Chinese
speech stacks, and only obviously applicable if you already know pitch carries
meaning in those languages.

WHAT THIS FILE DOES AND DOES NOT DO. It extracts the expectation. Comparing it
against measured f0 is the next step and is deliberately not bolted on here -
the extraction is verifiable on its own (a known accent pattern, a known tone
sequence), and a scorer that fused both would be much harder to trust when it
disagreed with an ear.
"""
import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

# Tone 5 is pypinyin's neutral tone; it carries no contour of its own and must
# not be scored as if it did.
NEUTRAL_TONE = "5"


def japanese_accent(text):
    """-> accent phrases, each {moras, accent} where accent is the nucleus.

    The A: field counts morae relative to the accent nucleus, so it runs
    negative before it, hits 0 at it, and positive after. A phrase whose
    count never reaches 0 is heiban - unaccented - which is itself a pattern
    and not a missing value.
    """
    try:
        import pyopenjtalk
    except ImportError:
        return None
    labels = pyopenjtalk.extract_fullcontext(text)
    # ONE ENTRY PER MORA, NOT PER PHONE. The A: field is carried by every
    # phone, so `hashi` yields four rows for two morae - counting rows makes
    # 箸 look four morae long. A mora is its vowel (or a moraic n/N), so
    # count those and ignore the consonants riding on them, and drop the
    # sil/pau boundary phones, which carry no A: value at all.
    moras = []
    for label in labels:
        phone = re.search(r"\-([^+]+)\+", label)
        found = re.search(r"/A:([+-]?\d+)", label)
        if not (phone and found):
            continue
        symbol = phone.group(1)
        if symbol in ("sil", "pau"):
            continue
        if symbol.lower() not in ("a", "i", "u", "e", "o", "n", "n:", "cl"):
            continue
        moras.append(int(found.group(1)))

    phrases, current = [], []
    for position in moras:
        # Within a phrase the count rises by one per mora. A value that does
        # not continue the run starts the next accent phrase.
        if current and position != current[-1] + 1:
            phrases.append(current)
            current = []
        current.append(position)
    if current:
        phrases.append(current)
    return [{"moras": len(p),
             "accent": (p.index(0) + 1) if 0 in p else 0}   # 0 == heiban
            for p in phrases if p]


def chinese_tones(text):
    """-> [(syllable, tone)] with tone in 1-4, or 5 for neutral."""
    try:
        from pypinyin import pinyin, Style
    except ImportError:
        return None
    out = []
    for group in pinyin(text, style=Style.TONE3, neutral_tone_with_five=True):
        syllable = group[0]
        if not syllable:
            continue
        tone = syllable[-1] if syllable[-1].isdigit() else NEUTRAL_TONE
        out.append((syllable[:-1] if syllable[-1].isdigit() else syllable, tone))
    return out


def describe(text, language):
    if language == "ja":
        phrases = japanese_accent(text)
        if phrases is None:
            return {"error": "pyopenjtalk not installed"}
        return {"accent_phrases": phrases,
                "phrase_count": len(phrases),
                "accented": sum(1 for p in phrases if p["accent"]),
                "heiban": sum(1 for p in phrases if not p["accent"])}
    if language == "zh":
        tones = chinese_tones(text)
        if tones is None:
            return {"error": "pypinyin not installed"}
        contoured = [t for _s, t in tones if t != NEUTRAL_TONE]
        return {"syllables": len(tones),
                "tone_sequence": "".join(t for _s, t in tones),
                "contoured": len(contoured),
                "neutral": len(tones) - len(contoured)}
    return {"error": f"no pitch expectation defined for {language!r}; this is "
                     f"for languages where pitch carries lexical meaning"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--generated", required=True,
                    help="an *_generate.json artifact, for its text and language")
    ap.add_argument("--language", default=None, help="ja or zh (default: from artifact)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "expected_prosody.json"))
    args = ap.parse_args()

    with open(args.generated, encoding="utf-8") as handle:
        doc = json.load(handle)
    language = args.language or doc.get("language")
    rows = (doc.get("rows") or [])[:args.limit]
    if not rows:
        sys.exit("artifact has no rows")

    results = []
    for row in rows:
        text = row.get("text") or ""
        if not text:
            continue
        results.append({"id": row.get("id"), "text": text,
                        **describe(text, language)})
    if not results:
        sys.exit("no text found in that artifact")
    if results[0].get("error"):
        sys.exit(results[0]["error"])

    print(f"{len(results)} lines, language={language}\n")
    for row in results[:4]:
        summary = {k: v for k, v in row.items()
                   if k not in ("id", "text", "accent_phrases")}
        print(f"  {row['text'][:34]}")
        print(f"     {summary}")
    document = {"source": os.path.relpath(args.generated, REPO),
                "language": language, "lines": len(results), "results": results}
    try:
        from experiments.provenance import provenance
        document["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        document["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    from utils import atomic_json_write
    atomic_json_write(document, args.out)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
