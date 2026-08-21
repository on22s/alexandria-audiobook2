"""Elson's method in Chinese: same idea, different frame, ten times the reach.

Every method result in this project rests on four Japanese light novels in
English translation and three English classics. JY-QuotePlus (NLPCC 2024) is
8,144 quotations from 射雕英雄传 annotated with speaker, addressee, speaking
mode and a LINGUISTIC CUE - the Chinese counterpart of the PDNC column that
produced #382 and then #384.

English puts the attribution after the quote: `"..." said Mr. Darcy`. Chinese
puts it before: `黄蓉道：「...」`. So Elson's Quote-Said-Person cannot fire on
Chinese at all, and the question is whether the METHOD survives when the
pattern is rewritten - a named mention plus a speech cue immediately before the
quotation.

    the frame is adjacent on            92.9% of quotations
    the rule fires on                   41.5%
    accuracy where it fires             .9828
    share of ALL quotations it answers  40.8%

Our English trigram fires on 4.0% of PDNC at .9899 (#372). So the method
transfers and reaches ten times as far, because Chinese narrative marks
attribution far more regularly than English does.

THE ADDRESSEE CORRECTION IS THE INTERESTING PART. A naive rule scores .9249,
and every error inspected was `向X道` or `對X道` - "said TO X" - taking the
addressee for the speaker. Excluding names preceded by a directional
preposition lifts accuracy to .9828 while the share of quotations answered
correctly barely moves, 40.9% to 40.8%: it converts wrong answers into
declines, which is the right trade for a pre-pass and the wrong one for a
benchmark.

THE CORPUS IS NOT VENDORED. It is fetched locally and only derived statistics
are recorded here. JY-QuotePlus carries no licence, and it annotates a novel
that remains in copyright; measuring on it locally and publishing counts is a
different act from redistributing it.
"""
import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402

# Longest first: 说道 must be tried before 道 or the head keeps a stray 说.
CUES = ("接口道", "说道", "叫道", "问道", "喝道", "笑道", "答道",
        "道", "说", "问", "叫")
# Directional prepositions marking who was spoken TO.
ADDRESSEE_MARKERS = ("向", "对", "對", "与", "與", "跟", "朝")


def alias_map(rows):
    """-> {surface mention: canonical entity}, the corpus's own alias table."""
    out = {}
    for row in rows:
        labels = row.get("labels") or {}
        mention = (labels.get("说话人-mention") or "").strip()
        entity = (labels.get("说话人-entity") or "").strip()
        if mention and entity:
            out.setdefault(mention, entity)
    return out


def last_pre_sentence(row):
    """-> the sentence immediately before the quotation, colon stripped."""
    pre = [s for s in (row.get("context_pre") or []) if s and s.strip()]
    return pre[-1].strip().rstrip("：:") if pre else ""


def attribute(sentence, surfaces, alias, skip_addressee=True):
    """-> (entity, reason). entity None means the frame did not resolve.

    The frame is <MENTION><CUE> at the very end of the preceding sentence. A
    mention preceded by a directional preposition is the ADDRESSEE and is
    declined rather than guessed - the naive form takes it and scores .9249
    where declining scores .9828.
    """
    for cue in CUES:
        if not sentence.endswith(cue):
            continue
        head = sentence[:-len(cue)]
        for surface in surfaces:
            if surface and head.endswith(surface):
                before = head[:-len(surface)]
                if skip_addressee and before and before[-1] in ADDRESSEE_MARKERS:
                    return None, "mention is the addressee (%s)" % before[-1]
                return alias[surface], "frame"
        return None, "cue present, no known mention before it"
    return None, "no cue at the sentence boundary"


def evaluate(rows, skip_addressee):
    alias = alias_map(rows)
    surfaces = sorted(alias, key=len, reverse=True)
    tally = collections.Counter()
    for row in rows:
        gold = ((row.get("labels") or {}).get("说话人-entity") or "").strip()
        guess, _ = attribute(last_pre_sentence(row), surfaces, alias,
                             skip_addressee)
        if guess is None:
            tally["declines"] += 1
        elif guess == gold:
            tally["correct"] += 1
        else:
            tally["wrong"] += 1
    fired = tally["correct"] + tally["wrong"]
    total = sum(tally.values())
    return {"fired": fired,
            "fire_rate": round(fired / total, 4) if total else None,
            "accuracy_where_fired": round(tally["correct"] / fired, 4) if fired else None,
            "share_of_all_answered_correctly": round(tally["correct"] / total, 4)
            if total else None,
            "declines": tally["declines"], "correct": tally["correct"],
            "wrong": tally["wrong"], "surface_forms": len(alias),
            "entities": len(set(alias.values()))}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", required=True,
                    help="JY-QuotePlus.json, fetched locally; not vendored here")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if not os.path.exists(args.corpus):
        raise SystemExit(
            "no corpus at %s. JY-QuotePlus is not vendored in this repository - "
            "it carries no licence and annotates a novel still in copyright. "
            "Fetch it locally and point --corpus at the json." % args.corpus)
    with open(args.corpus, encoding="utf-8") as handle:
        rows = json.load(handle)

    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "Elson's syntactic-frame method rewritten for Chinese word "
                 "order, measured on a locally fetched corpus. Derived counts "
                 "only; no corpus text is recorded",
        "corpus": "JY-QuotePlus (NLPCC 2024), 射雕英雄传",
        "quotations": len(rows),
        "naive": evaluate(rows, skip_addressee=False),
        "addressee_aware": evaluate(rows, skip_addressee=True),
        "english_comparison": {
            "source": "PR #372, our Elson trigram on PDNC",
            "fire_rate": 0.040, "accuracy_where_fired": 0.9899},
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%d quotations, %d surface forms over %d entities\n"
          % (len(rows), doc["addressee_aware"]["surface_forms"],
             doc["addressee_aware"]["entities"]))
    print("  %-16s %8s %10s %12s" % ("rule", "fires", "accuracy", "answers all"))
    for name in ("naive", "addressee_aware"):
        r = doc[name]
        print("  %-16s %7.1f%% %10.4f %11.1f%%"
              % (name, 100 * r["fire_rate"], r["accuracy_where_fired"],
                 100 * r["share_of_all_answered_correctly"]))
    print("  %-16s %7.1f%% %10.4f" % ("english (#372)", 4.0, 0.9899))
    return 0


if __name__ == "__main__":
    sys.exit(main())
