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

AND IT IS THE ADDRESSEE, NOT MERELY THE LAST PERSON TO SPEAK. That was the
open question here until the quote ORDER was brought in. In a two-party
exchange the addressee is also the previous speaker, so both explanations
predict the same answer on 643 of the 852 wrong rows: 54.9% of those rows name
the previous speaker and 55.3% the next one, against 78.5% naming an addressee. The cells that separate
them do not:

    named the addressee and NOT the previous speaker   205
    named the previous speaker and NOT the addressee     4

Fifty-one to one. The model is not copying whoever spoke last; it is
tracking who is being SPOKEN TO and naming them. That is a stronger claim and
a more useful one, because it means the direction information is present and
being assigned backwards rather than absent.

WHAT IT STILL DOES NOT SAY: why. Whether the prompt invites it, whether the
turn structure dominates a weak local cue, or whether the model has no notion
of direction at all and this is a coincidence of proximity, is not settled by
counts. `--prompt-variant speaker_not_addressee` is the arm.
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
# Every fixture family shares this stem; the corpus name follows it. Stripping
# the shared part rather than a hardcoded corpus prefix is what lets the same
# analysis read RiQuA, whose fixtures are `attribution_gold_riqua_<text>.json`.
STEM = "attribution_gold_"
DEFAULT_GLOB = PREFIX + "*_w3200.json"
WINDOW = 10


def row_id(entry):
    """-> the id this corpus uses to place a quote in document order.

    PDNC rows carry `pdnc_quote_id` (Q0, Q1 ...). RiQuA rows do not, and their
    own `id` is already in document order. Reading only the PDNC field left
    `quote_id` None on every RiQuA row, the neighbour block never executed,
    and the separation printed 0/0/0 - which reads like "no evidence either
    way" and actually meant "did not run".
    """
    return entry.get("pdnc_quote_id") or entry.get("id")


def speaker_index(entries, raw_book):
    """-> {quote id: speaker} for a book, from whichever source has it."""
    if raw_book:
        return {q: (row or {}).get("speaker") for q, row in raw_book.items()}
    return {e.get("id"): e.get("expected_speaker")
            for e in entries if e.get("id")}


def corpus_key(stem):
    """-> the book key inside a fixture stem, whatever corpus it belongs to.

    `attribution_gold_pdnc_prideandprejudice_w3200` -> `prideandprejudice_w3200`
    `attribution_gold_riqua_austen_emma_1`          -> `austen_emma_1`

    Strips the shared stem AND the corpus name. Stripping only the shared part
    left `pdnc_` on the key and match_book stopped resolving anything, which
    took PDNC from 205 rows to zero - caught by re-running PDNC before
    trusting the change.
    """
    key = stem[len(STEM):] if stem.startswith(STEM) else stem
    for corpus in ("pdnc_", "riqua_", "wp2021_"):
        if key.startswith(corpus):
            return key[len(corpus):]
    return key


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


def turn_neighbours(order, index, speakers):
    """-> (previous speaker, next speaker) around a quotation.

    `speakers` is a flat {quote id: speaker} map so this works for any corpus:
    PDNC builds it from its raw rows, RiQuA from the fixture entries. It used
    to index the raw row shape directly, which is why passing a map raised
    `TypeError: string indices must be integers`.
    """
    prev = speakers.get(order[index - 1]) if index > 0 else None
    nxt = speakers.get(order[index + 1]) if index + 1 < len(order) else None
    return prev, nxt


def separate_addressee_from_persistence(predicted, addressed, prev_speaker,
                                        groups):
    """-> which hypothesis this row supports, or None when both agree.

    In a two-party exchange the addressee IS the previous speaker, so most rows
    cannot tell "tracks who is spoken to" from "copies whoever spoke last".
    Only the disagreements carry information, and they are counted apart.
    """
    is_addressee = any(same_speaker(predicted, a, groups) for a in addressed)
    is_previous = bool(prev_speaker) and same_speaker(predicted, prev_speaker,
                                                      groups)
    if is_addressee and not is_previous:
        return "addressee_not_previous_speaker"
    if is_previous and not is_addressee:
        return "previous_speaker_not_addressee"
    return None


def entry_addressees(entry, raw_book):
    """-> the addressee names for one row, from whichever source carries them.

    ONE dispatch point, not two analyses. PDNC fixtures do not carry
    addressees; the names live in the raw corpus keyed by `pdnc_quote_id`.
    RiQuA fixtures carry an `addressees` list inline, because its corpus marks
    the relation directly and the reader kept it. Anything else with the same
    field works without further change.

    The inline field wins when present: a fixture that states its own
    addressees is the more specific answer, and PDNC rows never have one.
    """
    inline = entry.get("addressees")
    if inline:
        return [str(a) for a in inline if a]
    source = (raw_book or {}).get(entry.get("pdnc_quote_id"))
    return addressees(source) if source else []


def quote_order(book, entries, raw_book):
    """-> the ids of this book's quotes in document order.

    PDNC numbers its quotes Q0, Q1 ... and the raw corpus is the authority.
    A fixture without that (RiQuA) is already built in document order by its
    reader, so the entry sequence IS the order and its own ids identify rows.
    Returning the right KIND of id for each corpus keeps the neighbour
    analysis identical for both.
    """
    if raw_book:
        return sorted(raw_book, key=lambda q: int(q[1:]) if q[1:].isdigit() else 0)
    return [e.get("id") for e in entries if e.get("id")]


def analyse(rows, fixtures, raw):
    gold, cast = {}, {}
    for stem, fixture in fixtures.items():
        cast[stem] = ([l.split(" [also")[0] for l in roster_lines(fixture)],
                      alias_groups(fixture))
        key = corpus_key(stem)
        for suffix in ("_w3200",):
            if key.endswith(suffix):
                key = key[:-len(suffix)]
        book = match_book(key, raw) or stem
        for entry in fixture.get("entries") or []:
            gold[(stem, entry["id"])] = (book, entry)

    # Build the document order once per book, from whichever source has it.
    order, seen_books = {}, {}
    for stem, fixture in fixtures.items():
        key = corpus_key(stem)
        for suffix in ("_w3200",):
            if key.endswith(suffix):
                key = key[:-len(suffix)]
        book = match_book(key, raw) or stem
        seen_books.setdefault(book, fixture.get("entries") or [])
    speakers = {}
    for book, entries in seen_books.items():
        order[book] = quote_order(book, entries, raw.get(book))
        speakers[book] = speaker_index(entries, raw.get(book))
    naive = collections.Counter()
    strict = collections.Counter()
    options = collections.Counter()
    separation = collections.Counter()
    neighbours = collections.Counter()
    examples = []
    for row in rows:
        if row.get("correct"):
            continue
        stem, _, quote = (row.get("id") or "").partition(":")
        joined = gold.get((stem, quote))
        if not joined or stem not in cast:
            continue
        book, entry = joined
        addressed = entry_addressees(entry, raw.get(book))
        if not addressed:
            continue
        names, groups = cast[stem]
        naive["addressee" if any(
            same_speaker(row.get("predicted"), a, groups) for a in addressed)
            else "other"] += 1

        # Does the row distinguish "tracks the addressee" from "copies the
        # last speaker"? Most do not, and those are counted as agreeing.
        sequence = order.get(book) or []
        quote_id = row_id(entry)
        if quote_id in sequence:
            index = sequence.index(quote_id)
            previous, following = turn_neighbours(
                sequence, index, speakers.get(book) or {})
            for label, who in (("previous_speaker", previous),
                               ("next_speaker", following)):
                if who and same_speaker(row.get("predicted"), who, groups):
                    neighbours[label] += 1
            neighbours["rows_with_an_order"] += 1
            verdict = separate_addressee_from_persistence(
                row.get("predicted"), addressed, previous, groups)
            separation[verdict or "both_agree"] += 1

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
    return naive, strict, options, separation, neighbours, examples


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    ap.add_argument("--pdnc", default=os.path.join(REPO, "ab_test_runtime", "pdnc", "data"))
    ap.add_argument("--fixture-glob", default=DEFAULT_GLOB,
                    help="which fixture family to read. The default is PDNC's "
                         "wide-context set; pass attribution_gold_riqua_*.json "
                         "to run the same analysis on RiQuA, whose fixtures "
                         "carry their addressees inline.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = load_raw(args.pdnc)
    fixtures = {}
    for path in sorted(glob.glob(os.path.join(args.fixtures, args.fixture_glob))):
        with open(path, encoding="utf-8") as handle:
            fixtures[os.path.basename(path)[:-len(".json")]] = json.load(handle)
    if not fixtures or not raw:
        raise SystemExit("need both the _w3200 fixtures and the PDNC corpus")
    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)

    naive, strict, options, separation, neighbours, examples = analyse(
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
        "also_named_the_neighbouring_speaker": dict(neighbours),
        "addressee_or_persistence": {
            **{k: v for k, v in separation.items()},
            "note": "in a two-party exchange the addressee IS the previous "
                    "speaker, so only the disagreeing rows carry information"},
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
    a = separation.get("addressee_not_previous_speaker", 0)
    b = separation.get("previous_speaker_not_addressee", 0)
    print("\naddressee, or just whoever spoke last?")
    print("  named the addressee and NOT the previous speaker   %5d" % a)
    print("  named the previous speaker and NOT the addressee   %5d" % b)
    print("  rows where both explanations agree                 %5d"
          % separation.get("both_agree", 0))
    print("\n%s" % doc["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
