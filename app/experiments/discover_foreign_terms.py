"""Find Latinized Japanese words that are NOT names, for the lexicon.

WHAT WAS MISSING. `pronunciation.json` already carries the mechanism and a
discovered list of proper nouns, derived from character_aliases.json and
voice_config.json. That covers names - and names are the one class the
pipeline can already enumerate. It never looked for ordinary vocabulary,
which is where the audible damage actually is:

    arigatou   read as "Ara got to"      - parsed as three English words
    kawaii     read as "Kauai"           - the Hawaiian island
    barusu     read as "Berusu"

    senpai     read correctly            - needs no entry
    itadakimasu near-correct (イタドキマス) - a voicing slip, not a failure

Measured 2026-08-16 by generating each in an English sentence and
transcribing with both an English and a Japanese ASR.

WHY A DICTIONARY PAIR AND NOT PHONOTACTICS. A first attempt scored words by
Japanese mora structure and returned 25,949 "hits" across eight books, of
which essentially one was real: `time`, `were`, `before`, `been`, `more` all
decompose into valid Japanese morae. The working test is membership in TWO
dictionaries at once - present in Japanese, absent from English:

    same -> サメ  is a real Japanese word (shark), but `same` is English  -> reject
    made -> マデ  is the particle まで,       but `made` is English        -> reject
    kawaii        Japanese, not English                                   -> keep

SudachiDict is used rather than JMdict because it is Apache-2.0 rather than
CC BY-SA, which matters for a shipped app, and because its 2.9M entries
include proper nouns and spelling variants.

SHIPS THE FINDINGS EMPTY, following the convention pronunciation.json already
sets: a respelling is added because it was MEASURED to help, never because it
looked right. This writes the candidate and leaves the value blank.

AND IT WILL NOT CATCH EVERYTHING. A Japanese word that is also an English
word is invisible to this by construction - `tatami` and `manga` are in the
English lexicon and will never be proposed. That is the correct trade: those
are loanwords an English reader pronounces acceptably anyway.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

LEXICON = os.path.join(REPO, "pronunciation.json")
# Contractions split on the apostrophe and leave fragments that resolve as
# Japanese: `aren` (from aren't) scored 106 hits, `weren` 79. They are not
# words, and no lexicon should ever respell them.
CONTRACTION_TAILS = ("aren", "weren", "wasn", "hasn", "hadn", "didn", "doesn",
                     "don", "isn", "couldn", "wouldn", "shouldn", "haven", "won")


def build_detector():
    """-> f(word) -> bool, or None with a reason if the tools are missing."""
    try:
        import romkan
        from sudachipy import Dictionary
        from spellchecker import SpellChecker
    except ImportError as exc:
        return None, f"missing {exc.name}: pip install sudachipy sudachidict_core romkan pyspellchecker"
    tokenizer = Dictionary(dict="core").create()
    english = SpellChecker()

    def is_foreign(word):
        lowered = word.lower()
        if len(lowered) < 4 or lowered in CONTRACTION_TAILS:
            return False
        # Stylised English - "nooo", "sooo", "aaare" - is not foreign vocabulary.
        if re.search(r"(.)\1\1", lowered):
            return False
        if lowered in english:
            return False
        # DROPPED-G DIALECT IS ENGLISH. `makin`, `messin`, `havin`, `bein`,
        # `runnin` are "makin'" and friends with the apostrophe already
        # stripped; each resolves as Japanese and none is a word. Restoring
        # the g and re-asking the English lexicon settles it in one test.
        if lowered.endswith("in") and (lowered + "g") in english:
            return False
        # Laughter and stylised sound are not vocabulary: haha, hehe, hoho.
        if re.fullmatch(r"(ha|he|hi|ho|fu|wa){2,}", lowered):
            return False
        kana = romkan.to_katakana(lowered)
        if re.search(r"[A-Za-z]", kana):      # did not romanise cleanly
            return False
        morphemes = tokenizer.tokenize(kana)
        return len(morphemes) == 1 and not morphemes[0].is_oov()

    return is_foreign, None


def roster_forms(scripts):
    """Every written form the pipeline already treats as a name.

    Names are excluded because they have their own discovery path and their
    own entries; proposing them again would duplicate what pronunciation.json
    was built from.
    """
    forms = set()
    for path in (os.path.join(REPO, "character_aliases.json"),
                 os.path.join(REPO, "voice_config.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(item.keys()); stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str):
                forms.update(p.lower() for p in re.split(r"[^A-Za-z]+", item) if p)
    for path in scripts:
        try:
            with open(path, encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            speaker = str((entry or {}).get("speaker") or "")
            forms.update(p.lower() for p in re.split(r"[^A-Za-z]+", speaker) if p)
    return forms


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scripts", nargs="*", default=None,
                    help="saved script JSONs (default: everything in scripts/)")
    ap.add_argument("--min-occurrences", type=int, default=5)
    ap.add_argument("--write", action="store_true",
                    help="merge candidates into pronunciation.json with EMPTY "
                         "values; without this, only reports")
    ap.add_argument("--lexicon", default=LEXICON)
    args = ap.parse_args()

    scripts = args.scripts or [
        p for p in sorted(glob.glob(os.path.join(REPO, "scripts", "*.json")))
        if ".generation_quality" not in p and ".voice_config" not in p
        and ".meta" not in p and ".checkpoint" not in p]
    if not scripts:
        sys.exit("no scripts found")

    is_foreign, problem = build_detector()
    if problem:
        sys.exit(problem)

    names = roster_forms(scripts)
    counts = collections.Counter()
    for path in scripts:
        try:
            with open(path, encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for word in re.findall(r"[A-Za-z]{4,}", str((entry or {}).get("text") or "")):
                lowered = word.lower()
                if lowered in names:
                    continue
                # A fragment of a roster name is the name, not a new term:
                # `suba` is Subaru truncated, `baru` its tail.
                if any(lowered in form for form in names if len(form) > len(lowered)):
                    continue
                if is_foreign(word):
                    counts[lowered] += 1

    keep = {w: c for w, c in counts.items() if c >= args.min_occurrences}
    print(f"{len(scripts)} scripts, {len(names)} roster forms excluded")
    print(f"{len(keep)} candidate terms at >= {args.min_occurrences} occurrences\n")
    for word, count in sorted(keep.items(), key=lambda kv: -kv[1]):
        print(f"   {count:5}  {word}")
    if not args.write:
        print("\n(report only; pass --write to add them with empty values)")
        return 0

    with open(args.lexicon, encoding="utf-8") as handle:
        lexicon = json.load(handle)
    entries = lexicon.setdefault("names", {})
    added = [w for w in sorted(keep) if w not in entries]
    for word in added:
        entries[word] = ""                      # empty: measure before filling
    lexicon["_terms"] = {
        "discovered_by": "app/experiments/discover_foreign_terms.py",
        "rule": "in a Japanese dictionary (SudachiDict) and absent from an "
                "English one; roster names excluded, contraction fragments "
                "and stylised repeats rejected",
        "counts": {w: keep[w] for w in sorted(keep)},
        "note": "values ship EMPTY. Add a respelling only when it has been "
                "measured to help - judge by listening, not by WER.",
    }
    with open(args.lexicon, "w", encoding="utf-8") as handle:
        json.dump(lexicon, handle, indent=1, ensure_ascii=False)
    print(f"\nadded {len(added)} empty entries to {args.lexicon}: "
          f"{', '.join(added) if added else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
