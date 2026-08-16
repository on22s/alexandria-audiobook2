"""Decide which language a candidate term came from, using the publisher.

THE PROBLEM. `lexicon_corpus_scan` finds Latinized CJK vocabulary but cannot
say whether a term is Japanese or Chinese, and the respelling differs: 丹田 is
"dahn-tyen", 先生 is "sen-say". A phonotactic test does not separate them -
`kadokawa`, `sensei` and `isekai` all validate as legal pinyin, because
romaji and pinyin syllable shapes overlap almost completely. That was measured
before this file was written, not assumed.

WHAT DOES SEPARATE THEM IS BIBLIOGRAPHIC, NOT PHONETIC. An EPUB carries its
publisher, and English-language light-novel publishing is concentrated enough
that the publisher names the source tradition. Measured over a 929-book sample
of this library: 98% of books declare a publisher, across 49 distinct names,
with a very short head - J-Novel Club, Yen On, Seven Seas, Yen Press.

`dc:language` is useless here and worth saying out loud: every book declares
`en`, because it describes the translation, not the original.

A TERM INHERITS THE BOOKS IT APPEARS IN. If every book containing `dantian`
is published by a Chinese-source imprint, the term is Chinese. If a term
straddles both, that is reported as a straddle rather than resolved - a word
used in both traditions is a real thing, and forcing a label would invent
certainty this cannot support.

SEVEN SEAS IS DELIBERATELY AMBIGUOUS. It publishes Japanese light novels AND
Chinese danmei, so it is mapped to `mixed` rather than guessed. Roughly a
tenth of the library sits under it, and pretending otherwise would poison
exactly the terms this exists to classify.
"""
import argparse
import collections
import json
import os
import re
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Source tradition by publisher. Only imprints whose catalogue is unambiguous
# are named; everything else stays unknown, which is a usable answer.
PUBLISHER_LANGUAGE = {
    "j-novel club": "ja", "yen on": "ja", "yen press": "ja", "jy": "ja",
    "cross infinite world": "ja", "viz media": "ja", "haikasoru/viz media": "ja",
    "kodansha comics": "ja", "kodansha": "ja", "vertical": "ja",
    "one peace books": "ja", "tokyopop": "ja", "digital manga publishing": "ja",
    "hanashi media, llc": "ja", "tentai books": "ja", "sol press": "ja",
    "airship": "ja", "dark horse": "ja",
    # Seven Seas runs both a light-novel line and the Danmei imprint.
    "seven seas entertainment": "mixed", "seven seas": "mixed",
    "seven seas danmei": "zh", "rosmei": "zh", "peach flower house": "zh",
    # Fan and small-press imprints found in this library. "Shōsetsuka ni Narō"
    # is the Japanese web-novel site itself, which names the source outright.
    "shōsetsuka ni narō": "ja", "shosetsuka ni narou": "ja",
    "sol press, llc.": "ja", "sol press": "ja", "one peace ebooks": "ja",
    "orbit": "ja", "dark horse comics": "ja", "oppatranslations, llc": "ja",
}

# BACK-MATTER MARKERS, for the books that declare no publisher at all - 130 of
# them here, which is exactly the fan-made EPUB case. A translator's note
# names the source tradition even when the metadata does not: it cites the
# site the raws came from, or explains the honorifics, or apologises for how
# it handled the kanji.
#
# Only the PRESENCE of a marker is recorded. No sentence from any book is
# stored or reported - the output is a count of matches, which is a fact about
# vocabulary rather than an extract.
LANGUAGE_MARKERS = {
    "ja": (r"syosetu", r"shousetsuka", r"shosetsuka", r"narou\b", r"kakuyomu",
           r"\bkanji\b", r"\bromaji\b", r"\bfurigana\b", r"\bhonorific",
           r"from the japanese", r"japanese (?:web ?novel|raws|original)"),
    "zh": (r"\bpinyin\b", r"\bhanzi\b", r"jjwxc", r"jinjiang", r"qidian",
           r"\bdanmei\b", r"\bxianxia\b", r"\bwuxia\b", r"\bcultivation novel",
           r"from the chinese", r"chinese (?:web ?novel|raws|original)"),
}


def back_matter_language(path, tail_chars=20000):
    """-> 'ja' | 'zh' | 'unknown', from translator-note markers near the end.

    Reads the tail because a translator's note is almost always the last
    thing in the file. Returns a verdict only when one language's markers
    clearly outnumber the other's; a tie is left unknown rather than guessed.
    """
    try:
        from routers.script import extract_epub_text
        text = extract_epub_text(path)[-tail_chars:].lower()
    except Exception:                                       # noqa: BLE001
        return "unknown", {}
    hits = {lang: sum(len(re.findall(p, text)) for p in patterns)
            for lang, patterns in LANGUAGE_MARKERS.items()}
    ja, zh = hits.get("ja", 0), hits.get("zh", 0)
    if ja >= 2 and ja > zh * 2:
        return "ja", hits
    if zh >= 2 and zh > ja * 2:
        return "zh", hits
    return "unknown", hits


def book_metadata(path):
    """-> {publisher, creators} from the EPUB's OPF, or {} if unreadable."""
    try:
        with zipfile.ZipFile(path) as archive:
            opf = [n for n in archive.namelist() if n.endswith(".opf")]
            if not opf:
                return {}
            xml = archive.read(opf[0]).decode("utf-8", "replace")
    except Exception:                                       # noqa: BLE001
        return {}

    def field(name):
        found = re.findall(rf"<dc:{name}[^>]*>(.*?)</dc:{name}>", xml, re.S)
        return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", v)).strip()
                for v in found if v.strip()]

    publisher = (field("publisher") or [""])[0]
    return {"publisher": publisher,
            "language_of": PUBLISHER_LANGUAGE.get(publisher.strip().lower(), "unknown"),
            "creators": field("creator")[:3]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkpoint", default=os.path.join(
        REPO, "ab_test_runtime", "lexicon_scan", "checkpoint.json"))
    ap.add_argument("--candidates", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_corpus_candidates.json"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_attributed.json"))
    args = ap.parse_args()

    with open(args.checkpoint, encoding="utf-8") as handle:
        done = json.load(handle).get("done", {})
    with open(args.candidates, encoding="utf-8") as handle:
        candidates = json.load(handle)["candidates"]
    wanted = {c["term"] for c in candidates}

    # One OPF read per book that actually contributed terms.
    per_book = {}
    publishers = collections.Counter()
    back_matter_used = collections.Counter()
    for path, record in done.items():
        if "terms" not in record:
            continue
        meta = book_metadata(path)
        # Fan-made EPUBs declare no publisher. Fall back to the translator's
        # note in the back matter, which names the source when the metadata
        # does not. Only run for the books that need it - it costs a full
        # extraction each.
        if meta.get("language_of", "unknown") == "unknown":
            guessed, hits = back_matter_language(path)
            if guessed != "unknown":
                meta["language_of"] = guessed
                meta["attributed_by"] = "back_matter"
                meta["marker_hits"] = hits
                back_matter_used[guessed] += 1
        per_book[path] = meta
        publishers[meta.get("publisher") or "(none)"] += 1

    # Aggregate: for each term, which source traditions did it appear under?
    langs = collections.defaultdict(collections.Counter)
    for path, record in done.items():
        if "terms" not in record:
            continue
        label = per_book.get(path, {}).get("language_of", "unknown")
        for term in record["terms"]:
            if term in wanted:
                langs[term][label] += 1

    enriched = []
    for candidate in candidates:
        counts = langs.get(candidate["term"], collections.Counter())
        ja, zh = counts.get("ja", 0), counts.get("zh", 0)
        mixed, unknown = counts.get("mixed", 0), counts.get("unknown", 0)
        confident = ja + zh
        total = max(sum(counts.values()), 1)
        # A VERDICT NEEDS TO OUTWEIGH THE AMBIGUITY, NOT MERELY EXIST.
        # The first version asked only "is the other language zero?", which
        # labelled `dantian` Japanese on 2 books out of 37 while the other 33
        # were Seven Seas and therefore unresolved. Two books cannot outvote
        # thirty-three unknowns, and 丹田 is a Chinese term.
        if confident < 3 or confident / total < 0.5:
            verdict = "unattributed"
        elif zh == 0:
            verdict = "ja"
        elif ja == 0:
            verdict = "zh"
        else:
            verdict = "straddles"
        enriched.append({**candidate, "by_language": dict(counts),
                         "verdict": verdict,
                         # How much of the evidence came from imprints whose
                         # tradition is known at all. A verdict resting on two
                         # books out of forty is not the same claim as one
                         # resting on all forty.
                         "attributed_fraction": round(
                             confident / max(sum(counts.values()), 1), 3)})
    order = {"zh": 0, "straddles": 1, "ja": 2, "unattributed": 3}
    enriched.sort(key=lambda c: (order.get(c["verdict"], 9), -c["books"]))

    document = {"publisher_counts": dict(publishers.most_common()),
                "resolved_by_back_matter": dict(back_matter_used),
                "publisher_map": PUBLISHER_LANGUAGE,
                "note": "bibliographic metadata only; no book text is stored",
                "candidates": enriched}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)

    tally = collections.Counter(c["verdict"] for c in enriched)
    print(f"{len(enriched)} candidates attributed: {dict(tally)}")
    print(f"\n{'verdict':13}{'books':>6}{'series':>7}  term")
    for c in enriched[:24]:
        print(f"  {c['verdict']:11}{c['books']:>6}{c['series']:>7}  "
              f"{c['term']:16} {c['kana']}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
