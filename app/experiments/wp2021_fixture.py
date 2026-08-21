"""Build attribution fixtures from WP2021, a Chinese corpus that names CHARACTERS.

WHY THIS CORPUS. Goal 1.3 is this document's largest known overstatement, and
every method result we hold comes from four Japanese light novels IN
TRANSLATION - nothing has been validated on non-English prose. WP2021 is 2,548
annotated quotes from `World of Plainness`, and unlike RiQuA its gold is a
CHARACTER, not whichever noun phrase sat near the quote:

    bare-pronoun golds                       0.0%   (RiQuA: 39.4%)
    gold resolves to a canonical character  100.0%   (RiQuA: no coreference)
    gender per character                      yes    (PDNC does not carry this)

WHAT IT IS NOT. WP2021 has NO ADDRESSEE FIELD, so it cannot test
addressee_confusion.py's result; that needs PDNC, RiQuA, or the Japanese BCCWJ
annotation, which does carry 聴者. This corpus answers a different and
higher-priority question: does attribution generalise beyond English at all.

THE FORMAT, AND HOW IT WAS WRONG ONCE. A first version of this reader used the
SQuAD-shaped re-release in yudiandoris/csi, taking its `question` as the
quote's introduction and its `answer` as the speaker. That reader ran clean,
passed 14 tests and reported 99.8% resolved. It was wrong: where the question
named exactly one character the gold agreed only 11.6% of the time, which is
how it was caught. The tests were fine - they tested the code against the
format it assumed. Nothing about a passing test asks whether the assumption
holds, so `consistency_report` below now asks, on every build.

The ORIGINAL release is used instead, and is exactly what the paper describes
(Chen, Ling & Dai, Interspeech 2019, section 3.3): an instance is 21
sentences - the centre quote plus ten either side - then a bracketed list of
CANDIDATE character indices into name_and_alias.txt, then the index of the
gold speaker. Three properties hold on 180 of 180 dev instances and are
asserted, not trusted: the centre line is a quote, the gold is among the
candidates, and the gold character is named somewhere in the window.

NOT VENDORED. No stated licence, so the corpus is fetched and converted
locally, fixtures are gitignored, and only this script and its counts ship.

    https://github.com/chenjiaxiang/Chinese-dataset-for-speaker-identification
"""
import argparse
import collections
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

GENDER = {"0": "female", "1": "male"}
WINDOW_SENTENCES = 21
CENTRE = WINDOW_SENTENCES // 2          # the quote being attributed
CANDIDATES = re.compile(r"^\[[\d,\s]*\]$")
OPEN_QUOTE = ("“", "「", "『", "‘")


def read_characters(path):
    """-> [(canonical, [aliases], gender)] in FILE ORDER.

    Order is the contract: the corpus refers to characters by their index in
    this file, so sorting or de-duplicating it would silently relabel every
    quote in the dataset.
    """
    characters = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            parts = line.split()
            if not parts:
                continue
            if len(parts) < 2 or parts[0] not in GENDER:
                raise ValueError("%s:%d is not `gender name alias...`: %r"
                                 % (path, lineno, line.rstrip()))
            characters.append((parts[1], parts[1:], GENDER[parts[0]]))
    if not characters:
        raise ValueError("%s named no characters" % path)
    return characters


def parse_instances(path):
    """-> [(context_lines, candidate_indices, gold_index)].

    Blocks are located by the bracket line rather than by counting from the
    top: a single stray blank line would otherwise shift every instance after
    it and silently attribute every quote to the wrong character.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    instances = []
    for i, line in enumerate(lines):
        if not CANDIDATES.match(line.strip()):
            continue
        if i < WINDOW_SENTENCES or i + 1 >= len(lines):
            raise ValueError("%s: block at line %d has no room for a window "
                             "or a gold index" % (path, i))
        context = lines[i - WINDOW_SENTENCES:i]
        candidates = [int(n) for n in re.findall(r"\d+", line)]
        gold = lines[i + 1].strip()
        if not gold.isdigit():
            raise ValueError("%s:%d gold index is not a number: %r"
                             % (path, i + 2, gold))
        instances.append((context, candidates, int(gold)))
    if not instances:
        raise ValueError("%s contained no instances" % path)
    return instances


def consistency_report(instances, characters):
    """-> counts for the three properties the format guarantees.

    This is the check that the first version of this reader lacked. It is
    cheap, it runs on every build, and it is what distinguishes a decoded
    format from a plausible guess about one.
    """
    report = collections.Counter()
    for context, candidates, gold in instances:
        report["instances"] += 1
        if len(context) == WINDOW_SENTENCES:
            report["window_is_21_sentences"] += 1
        if context[CENTRE].strip().startswith(OPEN_QUOTE):
            report["centre_line_is_a_quote"] += 1
        if gold in candidates:
            report["gold_is_among_candidates"] += 1
        if gold < len(characters):
            report["gold_index_in_range"] += 1
            if any(a in "".join(context) for a in characters[gold][1]):
                report["gold_named_in_window"] += 1
    return report


def build(instances, characters):
    """-> (entries, skipped counter). One entry per attributable quote."""
    entries, skipped = [], collections.Counter()
    for index, (context, candidates, gold) in enumerate(instances):
        if gold >= len(characters):
            skipped["gold index out of range"] += 1
            continue
        canonical, aliases, gender = characters[gold]
        entries.append({
            "id": "wp2021-%05d" % index,
            "line": context[CENTRE].strip(),
            "expected_speaker": canonical,
            "gender": gender,
            "aliases": aliases,
            # The corpus supplies its own candidate list per instance, derived
            # by matching aliases against the window. Kept because it is a
            # roster the model can be shown, and because roster_order_bias.py
            # needs a per-row cast rather than a whole-book one.
            "candidates": [characters[c][0] for c in candidates
                           if c < len(characters)],
            "category": "wp2021",
            "prev_context": "".join(context[:CENTRE]),
            "next_context": "".join(context[CENTRE + 1:]),
        })
    return entries, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", required=True,
                    help="directory with {train,dev,test}.txt and "
                         "name_and_alias.txt")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    characters = read_characters(
        os.path.join(args.corpus, "name_and_alias.txt"))
    os.makedirs(args.out, exist_ok=True)
    print("characters: %d" % len(characters))

    totals, combined = 0, collections.Counter()
    for split in ("train", "dev", "test"):
        path = os.path.join(args.corpus, "%s.txt" % split)
        if not os.path.exists(path):
            raise SystemExit("missing %s" % path)
        instances = parse_instances(path)
        report = consistency_report(instances, characters)
        combined.update(report)
        entries, skipped = build(instances, characters)
        totals += len(entries)
        speakers = sorted({e["expected_speaker"] for e in entries})
        with open(os.path.join(args.out,
                               "attribution_gold_wp2021_%s.json" % split),
                  "w", encoding="utf-8") as fh:
            json.dump({
                "book": "wp2021_%s" % split,
                "source": "WP2021 / World of Plainness (Chen, Ling & Dai, "
                          "Interspeech 2019), original release",
                "language": "zh",
                "provenance": provenance(__file__),
                "entries": entries,
                "roster": speakers,
                "aliases": [characters[i][1] for i in range(len(characters))
                            if characters[i][0] in set(speakers)],
                "gender": {c: g for c, _, g in characters if c in set(speakers)},
                "format_consistency": dict(report),
                "skipped": dict(skipped),
                "kept": len(entries),
            }, fh, indent=1, ensure_ascii=False)
        print("  %-6s %5d quotes, %3d speakers" % (split, len(entries),
                                                   len(speakers)))

    n = combined["instances"]
    print("\nformat consistency over all %d instances:" % n)
    for key in ("window_is_21_sentences", "centre_line_is_a_quote",
                "gold_is_among_candidates", "gold_index_in_range",
                "gold_named_in_window"):
        got = combined[key]
        flag = "" if got == n else "   <-- NOT UNIVERSAL, read before trusting"
        print("  %-26s %5d/%d  %5.1f%%%s" % (key, got, n, 100 * got / n, flag))
    print("\n  TOTAL %d quotes across three splits" % totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
