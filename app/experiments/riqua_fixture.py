"""Build attribution fixtures from RiQuA, the other English corpus with addressees.

WHY A SECOND CORPUS. `addressee_confusion.py` found that when our arm is wrong
it names the person being spoken TO 77.8% of the time, and that this is not
mere persistence (205 rows name the addressee and not the previous speaker
against 4 the other way). Every one of those rows is PDNC, annotated by one
group under one scheme. A finding that large resting on one annotation scheme
is exactly the kind that turns out to be a property of the scheme. RiQuA is
independent: different annotators, different guidelines, 11 works of 19th
century public-domain fiction, and it marks speaker, addressee and cue.

WHAT RiQuA IS NOT. Its Speaker and Addressee relations point at MENTION SPANS,
not at resolved characters, and there is no coreference layer: 39.4% of speaker
mentions and 35.6% of addressee mentions are bare pronouns. PDNC gives
canonical names plus alias groups. So this is not a drop-in second copy of the
PDNC task and must not be reported as one. Requiring both sides to be named
leaves 1,537 of 5,963 quotations, and that reduced set is what this writes.
The counts of what was dropped, and why, go in the fixture rather than a log,
because a fixture that silently represents a quarter of a corpus is how a
number ends up meaning something other than it says.

NOT VENDORED. RiQuA carries no stated licence, so the corpus is fetched and
converted locally and the fixtures it produces are gitignored. Only this
script and the counts it reports are committed.

    https://www.ims.uni-stuttgart.de/documents/ressourcen/korpora/riqua/riqua.tar.gz
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

# Bare pronouns cannot name a character, so a row whose gold is one cannot be
# scored against a roster of names. `who`/`that` appear as relative-clause
# subjects standing in for a speaker and are the same problem.
PRONOUNS = frozenset("""
    i me my myself mine we us our ourselves ours you your yourself yourselves
    yours he him his himself she her hers herself it its itself they them their
    themselves theirs who whom that which
""".split())

RELATION = re.compile(r"(\w+) Arg1:(\w+) Arg2:(\w+)")


def is_name(mention):
    """-> True when a mention can identify a character in a roster.

    A capital letter is required as well as a non-pronoun: RiQuA marks spans
    like "the old man" as entities, and those name a person in the prose but
    cannot be matched against a cast list of proper names.
    """
    mention = (mention or "").strip()
    if not mention or mention.lower() in PRONOUNS:
        return False
    return re.search(r"[A-Z]", mention) is not None


def parse_ann(path):
    """-> (spans, relations) from one brat .ann file.

    spans: id -> (type, start, end, text). relations: list of (type, a1, a2).
    Argument ORDER is not assumed: it was measured across all 15,780 relations
    in the corpus as Arg1=Entity/Cue, Arg2=Quotation, and `link_quotes` asserts
    it rather than trusting this comment.
    """
    spans, relations = {}, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            if parts[0].startswith("T"):
                head = parts[1].split()
                # Brat allows discontinuous spans ("start end;start end"); the
                # outer envelope is what we need for ordering and context.
                offsets = [int(n) for n in re.findall(r"\d+", parts[1])]
                if not offsets:
                    continue
                spans[parts[0]] = (head[0], min(offsets), max(offsets),
                                   parts[2] if len(parts) > 2 else "")
            elif parts[0].startswith("R"):
                match = RELATION.match(parts[1])
                if match:
                    relations.append(match.groups())
    return spans, relations


def link_quotes(spans, relations):
    """-> quote id -> {"speakers": [...], "addressees": [...], "cues": [...]}.

    Fails loud on an unexpected argument order instead of silently producing a
    fixture whose speakers are quotations.
    """
    linked = collections.defaultdict(
        lambda: {"speakers": [], "addressees": [], "cues": []})
    field = {"Speaker": "speakers", "Addressee": "addressees",
             "Cueing": "cues"}
    for rel, arg1, arg2 in relations:
        if rel not in field or arg1 not in spans or arg2 not in spans:
            continue
        if spans[arg2][0] != "Quotation":
            raise ValueError(
                "RiQuA relation %s points Arg2 at a %s, not a Quotation; the "
                "argument order this reader assumes does not hold in %r"
                % (rel, spans[arg2][0], (rel, arg1, arg2)))
        linked[arg2][field[rel]].append(spans[arg1][3].strip())
    return linked


def context(text, start, end, window):
    return text[max(0, start - window):start], text[end:end + window]


def build_text(ann_path, txt_path, window):
    """-> (entries, dropped counter, roster) for one RiQuA text."""
    spans, relations = parse_ann(ann_path)
    linked = link_quotes(spans, relations)
    with open(txt_path, encoding="utf-8") as fh:
        text = fh.read()

    stem = os.path.basename(txt_path)[:-4]
    quotes = sorted(((sid, s) for sid, s in spans.items()
                     if s[0] == "Quotation"), key=lambda kv: kv[1][1])
    entries, dropped, roster = [], collections.Counter(), collections.Counter()
    for index, (sid, span) in enumerate(quotes):
        _type, start, end, quote = span
        rel = linked.get(sid)
        if not rel or not rel["speakers"] or not rel["addressees"]:
            dropped["missing one side"] += 1
            continue
        speakers = [m for m in rel["speakers"] if is_name(m)]
        addressees = [m for m in rel["addressees"] if is_name(m)]
        if not speakers and not addressees:
            dropped["both pronoun"] += 1
            continue
        if not speakers:
            dropped["speaker pronoun only"] += 1
            continue
        if not addressees:
            dropped["addressee pronoun only"] += 1
            continue
        before, after = context(text, start, end, window)
        roster.update(speakers)
        roster.update(addressees)
        entries.append({
            "id": "%s-%05d" % (stem, index),
            "line": quote,
            "expected_speaker": speakers[0],
            "all_speaker_mentions": speakers,
            "addressees": addressees,
            "cues": rel["cues"],
            "quote_type": "Cued" if rel["cues"] else "Uncued",
            "category": "riqua",
            "prev_context": before,
            "next_context": after,
            "riqua_span": [start, end],
        })
    return entries, dropped, roster


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--riqua", required=True,
                    help="path to the unpacked riqua/merged directory")
    ap.add_argument("--out", required=True, help="fixture directory to write")
    ap.add_argument("--window", type=int, default=3200,
                    help="context characters either side, matching the PDNC "
                         "w3200 fixtures so the two are comparable")
    args = ap.parse_args(argv)

    anns = sorted(f for f in os.listdir(args.riqua) if f.endswith(".ann"))
    if not anns:
        # An empty corpus directory would otherwise write 15 empty fixtures
        # and report a clean run.
        raise SystemExit("no .ann files in %s - is that the merged/ dir?"
                         % args.riqua)
    os.makedirs(args.out, exist_ok=True)

    totals, written, all_dropped = 0, [], collections.Counter()
    for ann in anns:
        stem = ann[:-4]
        txt = os.path.join(args.riqua, stem + ".txt")
        if not os.path.exists(txt):
            raise SystemExit("%s has no matching .txt" % ann)
        entries, dropped, roster = build_text(
            os.path.join(args.riqua, ann), txt, args.window)
        all_dropped.update(dropped)
        totals += len(entries)
        path = os.path.join(args.out, "attribution_gold_riqua_%s.json" % stem)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "book": stem,
                "source": "RiQuA (Papay & Pado, LREC 2020), converted by "
                          "riqua_fixture.py",
                "provenance": provenance(__file__),
                "entries": entries,
                "roster": sorted(roster),
                # Alias grouping is NOT provided. RiQuA has no coreference
                # layer, so "Emma" and "Miss Woodhouse" are separate roster
                # entries and a scorer must treat them as distinct or supply
                # its own grouping. Saying so here beats a caller assuming the
                # PDNC contract holds.
                "aliases": [],
                "aliases_note": "RiQuA ships no coreference; none inferred",
                "dropped": dict(dropped),
                "kept": len(entries),
            }, fh, indent=1, ensure_ascii=False)
        written.append((stem, len(entries)))

    print("%-24s %6s" % ("text", "kept"))
    for stem, n in sorted(written, key=lambda kv: -kv[1]):
        print("  %-22s %6d" % (stem, n))
    print("\n  %-22s %6d" % ("TOTAL kept", totals))
    print("\ndropped, and why:")
    for reason, n in all_dropped.most_common():
        print("  %-24s %6d" % (reason, n))
    print("\n  kept %d of %d quotations (%.1f%%). The rest cannot be scored "
          "against\n  a roster of names: RiQuA's golds are mention spans and "
          "carry no coreference."
          % (totals, totals + sum(all_dropped.values()),
             100 * totals / max(1, totals + sum(all_dropped.values()))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
