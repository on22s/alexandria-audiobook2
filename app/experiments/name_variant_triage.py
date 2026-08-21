"""Which of 5.2's "inconsistent" names are really inconsistent?

`name_consistency.py` reports character names spelled more than one way, and
its number is what goal 5.2's baseline rests on. Reading the four variant
groups it found across the shipped books shows two of them are not name
variants at all:

    subaru    Subaru(2793)  Su-ba-ru(3)      a name, stretched for effect
    rem       Rem(890)      R-e-m(3)         a name, stretched for effect
    ferris    Ferris(1224)  Ferri's(57)      "Ferri is", a NICKNAME + contraction
    knights   knights(86)   Knights(35)      a COMMON NOUN, not a character

`Ferri's real busy` is the character Ferris referring to himself as Ferri and
contracting "is"; stripping the apostrophe makes it collide with `Ferris`.
`Knights` is `Chapter 4 The Candidates for the Throne and Their Knights`.

THIS DOES NOT MAKE THE REAL CASES DEFECTS EITHER. `Su-ba-ru!` and `R-e-m` are
deliberate: the author is writing someone drawing the name out. An engine that
voices three separate syllables may be doing exactly what the page asks. Whether
that is right is a listening judgement, not a measurement, and belongs with 6.5.

So the lexicon's emptiness may be a much smaller gap than 5.07% suggests - four
groups, two of them artefacts, and the remaining two worth six occurrences that
might not want fixing.
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

APOSTROPHES = "'’ʼ"
# `\w+`, not `\w`: the first version required a single leading character and
# so matched `R-e-m` while missing `Su-ba-ru`, classifying one of a matched
# pair of identical cases as something else. A legitimately hyphenated name
# is not at risk because a stretch is always the MINORITY spelling - three
# occurrences against 2,793 - and the caller checks that.
STRETCHED = re.compile(r"^\w+(?:[-‐‑]\w+)+$")


def classify(forms, roster):
    """-> (verdict, why) for one group of spellings that normalise together.

    `roster` is the set of names the book's own cast list knows, lowercased.
    """
    variants = sorted(forms, key=lambda f: -forms[f])
    dominant = variants[0]
    others = variants[1:]
    if dominant.lower() not in roster:
        # NOT "not a character". The cast files cover the books that have been
        # cast, so this means only "the roster does not know this name" -
        # `knights` is a common noun and `Ferris` is a real character absent
        # from voice_config. Both are correctly excluded from the lexicon
        # question; only one of them is not a person, and this check cannot
        # tell which.
        return "not_in_cast_list", "%r is not in the supplied cast list" % dominant
    for other in others:
        if any(a in other for a in APOSTROPHES):
            # ANY apostrophe form is grammar, not spelling. The first version
            # only flagged one whose stem DIFFERED from the dominant, which
            # catches `Ferri’s` against `Ferris` but lets `Rem’s` against `Rem`
            # through as a spelling variant - and a possessive is exactly what
            # a lexicon must not be asked to fix.
            stem = re.split("[%s]" % APOSTROPHES, other)[0]
            same = stem.lower() == dominant.lower()
            return ("contraction_or_possessive",
                    "%r is %s an apostrophe form of %r"
                    % (other, "" if same else "a DIFFERENT stem plus", stem))
    stretched = [o for o in others
                 if STRETCHED.match(o) and forms[o] < forms[dominant]]
    if stretched:
        return ("deliberate_stretch",
                "%r spells the name out, %d times against %d; the engine "
                "voicing separate syllables may be correct"
                % (stretched[0], forms[stretched[0]], forms[dominant]))
    return "genuine_variant", "spellings differ with no obvious cause"


def load_roster(paths):
    """-> lowercased cast names from the given voice config and alias files.

    AN EMPTY ROSTER IS FATAL, not a default. These files are untracked, so a
    worktree does not have them - and with an empty set every group classifies
    as `not_a_character`, which is a confident, plausible, completely wrong
    answer. That is the failure mode this whole file exists to expose, so it
    must not be the failure mode of the file itself.
    """
    names = set()
    for full in paths:
        try:
            with open(full, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            for key, value in doc.items():
                names.add(str(key).lower())
                if isinstance(value, list):
                    names.update(str(v).lower() for v in value)
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--consistency", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "name_consistency.json"))
    ap.add_argument("--roster", nargs="+", default=[
        os.path.join(REPO, "voice_config.json"),
        os.path.join(REPO, "character_aliases.json")],
        help="cast lists; untracked, so a worktree must be told where they are")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.consistency, encoding="utf-8") as handle:
        doc = json.load(handle)
    groups = collections.OrderedDict()
    for book in (doc.get("per_book") or {}).values():
        for key, forms in (book.get("examples") or {}).items():
            groups.setdefault(key, collections.Counter()).update(forms)

    roster = load_roster(args.roster)
    if not roster:
        raise SystemExit(
            "the cast list is empty (looked in %s). Every group would classify "
            "as not_a_character, which is wrong and would look right. Point "
            "--roster at voice_config.json and character_aliases.json."
            % ", ".join(args.roster))
    rows = []
    for key, forms in sorted(groups.items(), key=lambda kv: -sum(kv[1].values())):
        verdict, why = classify(dict(forms), roster)
        rows.append({"key": key, "forms": dict(forms), "verdict": verdict,
                     "why": why,
                     "minority_occurrences": sum(sorted(forms.values())[:-1])})

    counts = collections.Counter(r["verdict"] for r in rows)
    actionable = [r for r in rows if r["verdict"] == "genuine_variant"]
    stretched = [r for r in rows if r["verdict"] == "deliberate_stretch"]
    out = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "triage of the variant groups behind goal 5.2's baseline; no "
                 "audio was measured and no lexicon entry is proposed",
        "groups": rows,
        "by_verdict": dict(counts),
        "occurrences_needing_a_decision": sum(r["minority_occurrences"]
                                              for r in stretched + actionable),
        "verdict": ("the baseline counts things the lexicon cannot help: %d of %d "
                    "groups are unknown to the cast list or are an apostrophe "
                    "form. "
                    "The rest are deliberate stretches, %d occurrences, and "
                    "whether an engine should smooth them is a listening "
                    "question rather than a measurement"
                    % (counts["not_in_cast_list"] + counts["contraction_or_possessive"],
                       len(rows),
                       sum(r["minority_occurrences"] for r in stretched))),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=1, ensure_ascii=False)

    print("%-10s %-26s %s" % ("group", "verdict", "forms"))
    for r in rows:
        print("  %-10s %-26s %s" % (r["key"], r["verdict"],
              "  ".join("%s(%d)" % kv for kv in
                        sorted(r["forms"].items(), key=lambda kv: -kv[1]))))
    print("\n%s" % out["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
