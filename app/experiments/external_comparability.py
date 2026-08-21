"""Published attribution scores are not comparable with ours, or each other.

This document and its chains cite external numbers as though they were targets:
"99.3% the field reports on Explicit", "the published formulation reaches 90.6%
on PDNC with an 8B model". Reading the papers behind them on 2026-08-21 shows
the numbers were produced under protocols that differ from ours and from each
other, sometimes by more than the gap being discussed.

The case that settles it: ZERO-SHOT GPT-3.5 ON THE SAME TASK FAMILY.

    SIG (arXiv 2312.14590), on PDNC       a strong baseline; SIG beats it by 9%
    NLPCC 2024 (arXiv 2408.09452), RiQua  10.90%
    NLPCC 2024, on JY-QuotePlus           70.07%

One model, one task, 10.9% to roughly 80%. The differences are protocol -
extractive span matching against generation, gold mentions against predicted,
per-quote accuracy against F1 - not capability.

This file holds the record rather than the argument. Each entry carries the
number, where it came from, the protocol that produced it, and whether it can
stand beside ours. `comparable` is allowed to be False with a reason; that is
the useful state, not a failure to research it.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402

REQUIRED = ("claim", "value", "source", "dataset", "protocol",
            "comparable_to_ours", "why")

RECORD = [
    {"claim": "Explicit-quote accuracy, current state of the art",
     "value": 0.993, "source": "Fast and Accurate Quotation Attribution "
                               "(arXiv 2608.02359)",
     "dataset": "PDNC", "protocol":
         "ModernBERT joint scoring with GOLD character mentions and the GOLD "
         "character list, at training and evaluation; 5-fold cross-validation "
         "at book level. The authors call the setting unrealistic and give a "
         "separate LitBank number with predicted coreference",
     "comparable_to_ours": False,
     "why": "we supply a roster but no gold mention spans, so their setting "
            "resolves for free what ours must infer"},
    {"claim": "Overall PDNC accuracy, same system",
     "value": 0.945, "source": "Fast and Accurate Quotation Attribution "
                               "(arXiv 2608.02359)",
     "dataset": "PDNC", "protocol": "as above, gold mentions",
     "comparable_to_ours": False, "why": "same oracle"},
    {"claim": "Explicit-quote accuracy, hand-written rules",
     "value": 0.99, "source": "Elson & McKeown, AAAI 2010",
     "dataset": "their own 6-author corpus",
     "protocol": "category prediction on Quote-Said-Person, no oracle, "
                 "reported on 22% of their corpus",
     "comparable_to_ours": True,
     "why": "we reproduced it directly - PR #372 measured .9899 on the same "
            "pattern in our own data, so this one transfers and did"},
    {"claim": "Zero-shot GPT-3.5, described as a strong baseline",
     "value": None, "source": "SIG (arXiv 2312.14590)", "dataset": "PDNC",
     "protocol": "generation, beaten by SIG by 9% overall",
     "comparable_to_ours": False,
     "why": "no absolute figure is quoted in a form that can be placed beside "
            "ours; only the margin over it"},
    {"claim": "Zero-shot GPT-3.5", "value": 0.109,
     "source": "Identifying Speakers and Addressees (arXiv 2408.09452, NLPCC 2024)",
     "dataset": "RiQua",
     "protocol": "scored as extractive reading comprehension against annotated "
                 "spans; a generative model is penalised for answering with a "
                 "name rather than a span",
     "comparable_to_ours": False,
     "why": "the number measures format agreement more than attribution, and "
            "is the clearest evidence that these figures cannot be ranked"},
    {"claim": "Zero-shot GPT-3.5", "value": 0.7007,
     "source": "arXiv 2408.09452", "dataset": "JY-QuotePlus (Chinese)",
     "protocol": "same paper, same model, same protocol, different corpus",
     "comparable_to_ours": False,
     "why": "60 points from the same model in the same paper; the corpus and "
            "its annotation decide the number as much as the method does"},
    {"claim": "Our own arm", "value": 0.656,
     "source": "two_stage_attribution_w3200.json", "dataset": "PDNC, 2494 rows",
     "protocol": "generation, roster supplied, no gold mentions, alias-aware "
                 "exact-name scoring",
     "comparable_to_ours": True, "why": "it is ours"},
]


def missing_fields(entry):
    """-> the required fields this entry does not carry."""
    return [k for k in REQUIRED if k not in entry]


def incomparable(record):
    return [e for e in record if not e.get("comparable_to_ours")]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    bad = {e.get("claim", "?"): missing_fields(e) for e in RECORD
           if missing_fields(e)}
    if bad:
        raise SystemExit("entries missing required fields: %s" % bad)

    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "a record of external attribution numbers this project cites, "
                 "with the protocol each was produced under. It settles whether "
                 "they can be placed beside ours; it does not re-measure them",
        "entries": RECORD,
        "comparable": len(RECORD) - len(incomparable(RECORD)),
        "not_comparable": len(incomparable(RECORD)),
        "verdict": "one external number transfers - Elson's .99, which we "
                   "reproduced ourselves at .9899. The rest were produced "
                   "under oracles or scoring formats we do not share and must "
                   "not be quoted as targets",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%-46s %8s %s" % ("claim / dataset", "value", "comparable"))
    for e in RECORD:
        print("  %-44s %8s %s"
              % (("%s / %s" % (e["claim"], e["dataset"]))[:44],
                 "n/a" if e["value"] is None else "%.3f" % e["value"],
                 "yes" if e["comparable_to_ours"] else "no"))
    print("\n%s" % doc["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
