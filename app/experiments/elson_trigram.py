"""The 2010 syntactic-trigram rule, as a pre-pass over a model's answers.

Elson & McKeown (AAAI 2010) sort each quote into a syntactic category BEFORE
applying any learning, and two of their categories imply a speaker outright.
Quote-Said-Person - <QUOTE> <EXPRESS VERB> <PERSON> - covers 22% of their
corpus and their rule alone answers it at .99.

Stratifying our own stored answers the same way is what motivated this file.
On the 2,494 PDNC rows of `two_stage_attribution_w3200.json`:

    Implicit    1228   .629
    Anaphoric    723   .712
    Explicit     543   .645      <- should be the free category
    overall     2494   .656

and, narrowing to where the gold speaker's name literally sits within 60
characters AFTER the quote - the Quote-Said-Person shape - we score .681 where
a decision tree with hand-written patterns scored .99.

WHAT THIS DOES NOT DO. Elson built a nominal chunker, so `said her father`
resolves for them and is declined here: we match proper names against the
book's own roster and nothing else. Pronouns are declined by design - that is
their Anaphora category, which does NOT imply a speaker. Declining is the
point; a rule that fires everywhere is a different, worse rule.

The rule reads only the quote text, its surrounding context and the roster -
the same material the model was given. It never sees the gold answer.
"""
import argparse
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402
from experiments.source_context import build_index, locate  # noqa: E402
from experiments.scoring import alias_groups, normalize, same_speaker  # noqa: E402

# Elson compiled ~6,000 surface forms from WordNet subtrees. This is the head
# of that distribution: the verbs that actually introduce speech in 19th-century
# prose, in the conjugations that appear next to a quote.
EXPRESS_VERBS = r"""
said says say saying answered answers answer answering replied replies reply
cried cries cry asked asks ask exclaimed exclaims added adds continued
observed remarked returned murmured whispered shouted called muttered sighed
repeated resumed inquired enquired demanded retorted declared protested urged
interrupted insisted responded began thought thinks think mused stammered
faltered rejoined pursued admitted confessed agreed objected persisted
"""
VERB = r"(?:%s)" % "|".join(sorted(EXPRESS_VERBS.split(), key=len, reverse=True))

# A capitalised run, optionally with an honorific. Not anchored to the roster
# here - the roster check happens after, so that "said the Colonel" and "said
# Netherfield" are rejected for the same reason: they are not a cast member.
NAME = r"(?:(?:Mr|Mrs|Miss|Dr|Sir|Lady|Lord|Captain|Colonel|Major)\.?\s+)?" \
       r"(?:[A-Z][\w'-]*)(?:\s+(?:de|van|von|of)?\s*[A-Z][\w'-]*){0,2}"

# Whatever separates the closing quote from the attribution: the quote mark
# itself, the comma or dash the author put inside it, whitespace, newlines.
GAP = r'["“”\'\s,.;:!?\-—–]*'

AFTER_VERB_NAME = re.compile(r"^%s(%s)\s+(%s)" % (GAP, VERB, NAME))
AFTER_NAME_VERB = re.compile(r"^%s(%s)\s+(%s)\b" % (GAP, NAME, VERB))
# The trailing gap must include the OPENING quote mark: `Mrs. Bennet cried, "`
# is where prev_context ends, and a trailing class without it matched nothing.
TAIL = r'[\s,:\-—–"“”\']*$'
BEFORE_NAME_VERB = re.compile(r"(%s)\s+(%s)\b%s" % (NAME, VERB, TAIL))
BEFORE_VERB_NAME = re.compile(r"\b(%s)\s+(%s)%s" % (VERB, NAME, TAIL))

# WHICH CATEGORIES ARE WORTH APPLYING, measured on the 2,494 stored rows
# rather than assumed. Firing every category the classifier recognises is a
# LOSS against firing only the verb-first orders:
#
#   quote_said_person                          98 fired  +1.20 pts  fixed 30 broke 0
#   + said_person_quote                        99 fired  +1.24 pts  fixed 31 broke 0
#   + person_said_quote                       102 fired  +1.20 pts  fixed 31 broke 1
#   + quote_person_said (all four)            115 fired  +0.96 pts  fixed 32 broke 8
#
# The name-first order <QUOTE> <PERSON> <VERB> scores 3 of 13 - worse than the
# model it would override. Inspecting those rows, the pattern also matches
# narration ABOUT a character next to someone else's quote ("Mr. Bennet made no
# answer"), which is not an attribution at all; that reading is an inference,
# the 3-of-13 is a measurement. It is classified and reported, never applied.
APPLY_BY_DEFAULT = ("quote_said_person", "said_person_quote")

PRONOUNS = {"HE", "SHE", "THEY", "I", "IT", "WE", "YOU", "HIS", "HER",
            "THEIR", "THAT", "THIS", "THERE", "AND", "BUT", "THE", "A", "AN"}


def resolve(surface, roster, groups):
    """-> the roster name `surface` denotes, or None.

    A surface form that is not a cast member is declined, not guessed. This is
    what keeps `said the housekeeper` and `said Netherfield` out.
    """
    if not surface:
        return None
    key = normalize(surface)
    if not key or key in PRONOUNS or key.split()[0] in PRONOUNS:
        return None
    for name in roster:
        if same_speaker(name, surface, groups):
            return name
    # A bare surname or given name standing for a longer roster entry:
    # "Darcy" for "MR. DARCY". Only accepted when exactly one entry matches,
    # so "BENNET" - which four cast members share - stays declined.
    hits = [n for n in roster if key in normalize(n).split()]
    return hits[0] if len(hits) == 1 else None


def classify(line, prev_context, next_context, roster, groups):
    """-> (category, implied_speaker or None).

    Categories that do not imply a speaker return None and leave the model's
    answer untouched.
    """
    after = next_context or ""
    before = prev_context or ""
    for pattern, order, category in (
            (AFTER_VERB_NAME, "vn", "quote_said_person"),
            (AFTER_NAME_VERB, "nv", "quote_person_said"),
            (BEFORE_NAME_VERB, "nv", "person_said_quote"),
            (BEFORE_VERB_NAME, "vn", "said_person_quote")):
        text = after if category.startswith("quote_") else before
        match = pattern.search(text)
        if not match:
            continue
        surface = match.group(2) if order == "vn" else match.group(1)
        speaker = resolve(surface, roster, groups)
        if speaker:
            return category, speaker
    return "no_pattern", None


def load_sources(pairs):
    """--source BOOK=FILE ... -> {book: (source, normalised, offsets)}."""
    out = {}
    for pair in pairs or ():
        book, _, path = pair.partition("=")
        if not path:
            raise SystemExit("--source needs BOOK=FILE, got %r" % pair)
        with open(path, encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        normalised, offsets = build_index(source)
        out[book] = (source, normalised, offsets)
    return out


def contexts_for(entry, book, sources):
    """-> (prev, next, status).

    The fixture's own contexts win when it has them - they are the annotation,
    not a reconstruction. Only the books that lack them are aligned.
    """
    if entry.get("prev_context") or entry.get("next_context"):
        return entry.get("prev_context"), entry.get("next_context"), "fixture"
    if book not in sources:
        return None, None, "no_source"
    source, normalised, offsets = sources[book]
    return locate(entry.get("line"), normalised, offsets, source)


def split_id(row_id):
    """-> (book, quote id) for both artifact id conventions.

    PDNC arms write "<fixture>:<quote id>"; the light-novel arms write a bare
    "index18-00017". Reading the second as the first made the book name
    "index18-00017", every gold lookup missed, and the run reported zero rows
    with no error - the shape of failure this file exists to avoid.
    """
    book, sep, quote = (row_id or "").partition(":")
    if sep:
        return book, quote
    return re.sub(r"-\d+$", "", book), book


def load_gold(fixtures_dir):
    gold, rosters, groups = {}, {}, {}
    for path in sorted(glob.glob(os.path.join(
            fixtures_dir, "attribution_gold_*.json"))):
        if "provisional" in os.path.basename(path):
            continue
        book = os.path.basename(path)[:-len(".json")]
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        rosters[book] = list(fixture.get("roster") or [])
        groups[book] = alias_groups(fixture)
        for entry in fixture.get("entries") or []:
            gold[(book, entry["id"])] = entry
            # Also by bare quote id, for artifacts that do not prefix the book.
            gold.setdefault(entry["id"], entry)
            short = book[len("attribution_gold_"):]
            rosters.setdefault(short, rosters[book])
            groups.setdefault(short, groups[book])
    return gold, rosters, groups


def mcnemar_exact(fixed, broke):
    """Two-sided exact binomial p for a paired win/loss count."""
    n = fixed + broke
    if not n:
        return 1.0
    from math import comb
    k = min(fixed, broke)
    tail = sum(comb(n, i) for i in range(k + 1))
    return min(1.0, 2.0 * tail / (2.0 ** n))


def subsets(rows):
    """Every cumulative subset's delta, so the applied set is auditable."""
    order = ["quote_said_person", "said_person_quote", "person_said_quote",
             "quote_person_said"]
    base = sum(1 for r in rows if r["model_correct"])
    out = {}
    for i in range(1, len(order) + 1):
        keep = set(order[:i])
        hit = [r for r in rows if r["fired"] and r["category"] in keep]
        comb = sum(1 for r in rows
                   if (r["rule_correct"] if (r["fired"] and r["category"] in keep)
                       else r["model_correct"]))
        out["+".join(order[:i])] = {
            "fired": len(hit),
            "delta_points": round(100 * (comb - base) / len(rows), 2) if rows else None,
            "fixed": sum(1 for r in hit if r["rule_correct"] and not r["model_correct"]),
            "broke": sum(1 for r in hit if r["model_correct"] and not r["rule_correct"]),
        }
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True,
                        help="a two_stage_attribution artifact with per-row answers")
    parser.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--source", nargs="*", default=[],
                        metavar="BOOK=FILE",
                        help="recover contexts by locating each line in this "
                             "book's source; for fixtures with no context")
    parser.add_argument("--roster-from-artifact", action="store_true",
                        help="take the cast from the candidates the model was "
                             "shown, not from the fixture")
    parser.add_argument("--arm",
                        help="artifacts that hold several arms repeat every "
                             "gold row once per arm; scoring them together "
                             "quadruples n and makes the rows dependent")
    parser.add_argument("--apply", nargs="*", default=list(APPLY_BY_DEFAULT),
                        help="categories whose implied speaker overrides the "
                             "model; the rest are classified and reported only")
    args = parser.parse_args()
    applied = set(args.apply)

    with open(args.artifact, encoding="utf-8") as handle:
        artifact = json.load(handle)
    gold, rosters, groups = load_gold(args.fixtures)
    sources = load_sources(args.source)
    if args.roster_from_artifact:
        seen = {}
        for row in artifact.get("rows") or []:
            book = split_id(row.get("id"))[0]
            seen.setdefault(book, set()).update(row.get("candidates") or ())
        for book, names in seen.items():
            if names:
                rosters[book] = sorted(names)

    artifact_rows = list(artifact.get("rows") or [])
    arms = {r.get("arm") for r in artifact_rows if r.get("arm")}
    if args.arm:
        artifact_rows = [r for r in artifact_rows if r.get("arm") == args.arm]
        if not artifact_rows:
            raise SystemExit("no rows for --arm %s; artifact has %s"
                             % (args.arm, sorted(arms)))
    elif len(arms) > 1:
        raise SystemExit(
            "this artifact holds %d arms (%s) and repeats every gold row once "
            "per arm. Pass --arm to pick one; scoring them together would "
            "report %d rows for %d quotes."
            % (len(arms), ", ".join(sorted(arms)), len(artifact_rows),
               len(artifact_rows) // len(arms)))

    rows, missing = [], 0
    context_status = {}
    for row in artifact_rows:
        book, quote_id = split_id(row.get("id"))
        entry = gold.get((book, quote_id)) or gold.get(quote_id)
        if entry is None:
            missing += 1
            continue
        prev, nxt, status = contexts_for(entry, book, sources)
        context_status[status] = context_status.get(status, 0) + 1
        category, implied = classify(
            entry.get("line"), prev, nxt,
            rosters.get(book, ()), groups.get(book, ()))
        expected = row.get("expected")
        model = row.get("predicted")
        g = groups.get(book, ())
        rows.append({
            "id": row.get("id"),
            "quote_type": entry.get("quote_type"),
            "context": status,
            "category": category,
            "expected": expected,
            "model": model,
            "rule": implied,
            "model_correct": bool(same_speaker(expected, model, g)),
            "rule_correct": bool(implied) and bool(same_speaker(expected, implied, g)),
            "fired": bool(implied),
        })

    for r in rows:
        r["applied"] = r["fired"] and r["category"] in applied
    fired = [r for r in rows if r["applied"]]
    fixed = sum(1 for r in fired if r["rule_correct"] and not r["model_correct"])
    broke = sum(1 for r in fired if r["model_correct"] and not r["rule_correct"])
    model_correct = sum(1 for r in rows if r["model_correct"])
    combined = sum(1 for r in rows
                   if (r["rule_correct"] if r["applied"] else r["model_correct"]))
    by_type = {}
    for r in rows:
        b = by_type.setdefault(r["quote_type"] or "?",
                               {"n": 0, "fired": 0, "model": 0, "combined": 0})
        b["n"] += 1
        b["fired"] += r["applied"]
        b["model"] += r["model_correct"]
        b["combined"] += (r["rule_correct"] if r["applied"] else r["model_correct"])

    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "Elson & McKeown (AAAI 2010) syntactic trigram as a pre-pass "
                 "over stored model answers; the rule never sees the gold",
        "source_artifact": os.path.basename(args.artifact),
        "rows_scored": len(rows),
        "rows_unmatched_to_gold": missing,
        "model_accuracy": round(model_correct / len(rows), 4) if rows else None,
        "combined_accuracy": round(combined / len(rows), 4) if rows else None,
        "delta_points": round(100 * (combined - model_correct) / len(rows), 2)
        if rows else None,
        "rule": {
            "fired": len(fired),
            "fire_rate": round(len(fired) / len(rows), 4) if rows else None,
            "accuracy_where_it_fired": round(
                sum(r["rule_correct"] for r in fired) / len(fired), 4) if fired else None,
            "model_accuracy_on_those_rows": round(
                sum(r["model_correct"] for r in fired) / len(fired), 4) if fired else None,
            "fixed": fixed, "broke": broke,
            "mcnemar_p": mcnemar_exact(fixed, broke),
        },
        "applied_categories": sorted(applied),
        "context_source": context_status,
        "subsets": subsets(rows),
        "by_quote_type": by_type,
        "by_category": {},
        "rows": rows,
    }
    for r in rows:
        c = doc["by_category"].setdefault(
            r["category"], {"n": 0, "rule_correct": 0, "model_correct": 0})
        c["n"] += 1
        c["rule_correct"] += r["rule_correct"]
        c["model_correct"] += r["model_correct"]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)
    if not rows:
        raise SystemExit(
            "no rows matched the gold; %d artifact rows were unmatched. Check "
            "that --fixtures holds the book's fixture." % missing)
    print("context:", doc["context_source"])
    print("rows %d | model %.4f -> combined %.4f (%+.2f pts)" % (
        len(rows), doc["model_accuracy"], doc["combined_accuracy"], doc["delta_points"]))
    print("rule fired %d (%.1f%%), correct %.4f there, model %.4f there; "
          "fixed %d broke %d p=%.3g" % (
              len(fired), 100 * doc["rule"]["fire_rate"],
              doc["rule"]["accuracy_where_it_fired"] or 0,
              doc["rule"]["model_accuracy_on_those_rows"] or 0,
              fixed, broke, doc["rule"]["mcnemar_p"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
