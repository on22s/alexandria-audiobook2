"""When the model is wrong it names the person being spoken TO.

PDNC annotates an ADDRESSEE for every quotation and this project had never
opened the column. It is in the file we already parse.

    of 857 wrong rows carrying an addressee, 669 named an addressee   78.1%

In a two-person scene that is trivial - the addressee IS the only other
person - so the number only means something where the model had a real choice.
Restricting to wrong rows where BOTH an addressee and some other present
character were available:

    chose the ADDRESSEE                621   77.8%
    chose another present character    110   13.8%
    chose someone absent                67    8.4%

with 2.3 addressees against 3.3 other present characters on offer. Choosing
among present people at random would land near 41%. It lands at 78%.

WHAT THIS EXPLAINS, and it is most of the attribution page:

  - the alternation constraint LOST 11.31 points (`constraint_refine.json`).
    The model already follows the turn structure; it follows it INVERTED, so a
    constraint that enforces alternation doubles the same error.
  - 498 wrong rows have gold and prediction both within ten mentions (#374).
    Speaker and addressee are both present by construction.
  - Explicit quotes fail at .645 with the name beside the line (#372, #382).
    Conversational direction is overriding the local cue, not missing it.
  - the same error mode appeared in Chinese (#390): every error of the naive
    frame rule was `向X道`, "said TO X".

That last one matters most. Two languages, two entirely different methods - a
14B model prompted in English, a regular-expression frame in Chinese - failing
the same way. This is a property of the task, not of either system.

WHAT IT DOES NOT SAY. It does not say the model is reasoning about direction
and inverting it; a model that simply named the most recently mentioned person
would land here too whenever that person is the addressee. Separating those
needs an arm, not an artifact.
"""
import argparse
import ast
import collections
import csv
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.annotator_evidence import load_raw, match_book  # noqa: E402
from experiments.provenance import provenance  # noqa: E402
from experiments.scene_narrowing import recent_mentions  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.two_stage_attribution import roster_lines  # noqa: E402

csv.field_size_limit(10 ** 7)
PREFIX = "attribution_gold_pdnc_"
WINDOW = 10


def addressees(row):
    """-> the annotated addressees, or [] when the column is unusable."""
    try:
        value = ast.literal_eval(row.get("addressees") or "[]")
    except (ValueError, SyntaxError):
        return []
    return [a for a in value if a] if isinstance(value, list) else []


def classify(predicted, expected, addressed, present, groups):
    """-> which kind of person the model named.

    `present` is the recently-mentioned cast, `addressed` the annotation. A row
    where no non-addressee was present is EXCLUDED by the caller, not counted
    here: in a two-hander the addressee is the only alternative and choosing it
    says nothing.
    """
    if any(same_speaker(predicted, a, groups) for a in addressed):
        return "addressee"
    others = [n for n in present
              if not same_speaker(expected, n, groups)
              and not any(same_speaker(a, n, groups) for a in addressed)]
    if any(same_speaker(predicted, n, groups) for n in others):
        return "other_present"
    return "absent"


def analyse(rows, fixtures, raw):
    gold, cast = {}, {}
    for stem, fixture in fixtures.items():
        cast[stem] = ([l.split(" [also")[0] for l in roster_lines(fixture)],
                      alias_groups(fixture))
        key = stem[len(PREFIX):]
        for suffix in ("_w3200",):
            if key.endswith(suffix):
                key = key[:-len(suffix)]
        book = match_book(key, raw)
        for entry in fixture.get("entries") or []:
            gold[(stem, entry["id"])] = (book, entry)

    naive = collections.Counter()
    strict = collections.Counter()
    options = collections.Counter()
    examples = []
    for row in rows:
        if row.get("correct"):
            continue
        stem, _, quote = (row.get("id") or "").partition(":")
        joined = gold.get((stem, quote))
        if not joined or stem not in cast:
            continue
        book, entry = joined
        source = (raw.get(book) or {}).get(entry.get("pdnc_quote_id"))
        if source is None:
            continue
        addressed = addressees(source)
        if not addressed:
            continue
        names, groups = cast[stem]
        naive["addressee" if any(
            same_speaker(row.get("predicted"), a, groups) for a in addressed)
            else "other"] += 1

        present = recent_mentions(entry.get("prev_context"), names, groups)[:WINDOW]
        others = [n for n in present
                  if not same_speaker(row.get("expected"), n, groups)
                  and not any(same_speaker(a, n, groups) for a in addressed)]
        if not others:
            continue                      # a two-hander decides nothing
        verdict = classify(row.get("predicted"), row.get("expected"),
                           addressed, present, groups)
        strict[verdict] += 1
        options["addressees"] += len(addressed)
        options["others"] += len(others)
        if verdict == "addressee" and len(examples) < 6:
            examples.append({"id": row.get("id"), "gold": row.get("expected"),
                             "model_said": row.get("predicted"),
                             "annotated_addressees": addressed})
    return naive, strict, options, examples


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    ap.add_argument("--pdnc", default=os.path.join(REPO, "ab_test_runtime", "pdnc", "data"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = load_raw(args.pdnc)
    fixtures = {}
    for path in sorted(glob.glob(os.path.join(args.fixtures, PREFIX + "*_w3200.json"))):
        with open(path, encoding="utf-8") as handle:
            fixtures[os.path.basename(path)[:-len(".json")]] = json.load(handle)
    if not fixtures or not raw:
        raise SystemExit("need both the _w3200 fixtures and the PDNC corpus")
    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)

    naive, strict, options, examples = analyse(
        artifact.get("rows") or [], fixtures, raw)
    n_strict = sum(strict.values())
    n_naive = sum(naive.values())
    chance = (options["addressees"] / (options["addressees"] + options["others"])
              if n_strict else None)
    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "which KIND of person the model names when it is wrong, using "
                 "PDNC's addressee annotation. No model was re-run",
        "all_wrong_rows_with_an_addressee": {
            "n": n_naive,
            "named_an_addressee": naive["addressee"],
            "share": round(naive["addressee"] / n_naive, 4) if n_naive else None,
            "caveat": "in a two-person scene the addressee is the only "
                      "alternative, so this number alone proves nothing"},
        "rows_where_another_present_person_was_available": {
            "n": n_strict,
            **{k: {"n": v, "share": round(v / n_strict, 4)}
               for k, v in strict.items()},
            "mean_addressee_options": round(options["addressees"] / n_strict, 2)
            if n_strict else None,
            "mean_other_present_options": round(options["others"] / n_strict, 2)
            if n_strict else None,
            "share_expected_if_choosing_at_random": round(chance, 4)
            if chance else None},
        "examples": examples,
        "verdict": ("when it is wrong the model names the person being spoken "
                    "TO, %.1f%% of the time against %.1f%% expected from "
                    "choosing among present people at random"
                    % (100 * strict["addressee"] / n_strict,
                       100 * chance) if n_strict else "no usable rows"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("all wrong rows with an addressee: %d, of which %d named one (%.1f%%)"
          % (n_naive, naive["addressee"], 100 * naive["addressee"] / n_naive))
    print("\nrows where another present person was also available: %d" % n_strict)
    for kind in ("addressee", "other_present", "absent"):
        if strict[kind]:
            print("  %-22s %5d  %.1f%%" % (kind, strict[kind],
                                           100 * strict[kind] / n_strict))
    print("  random would give %.1f%%" % (100 * chance))
    print("\n%s" % doc["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
