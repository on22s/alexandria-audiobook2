"""Was the two-stage model ever given the answer? Yes - every single time.

WHY THIS EXISTS. two_stage_attribution_full.json records `in_candidates: false`
on 2,250 of its 2,494 rows, which reads as "the correct speaker was not in the
cast the model was shown" and would make 54.5% accuracy a story about roster
recall. It is an artefact of a bug: the run passed the roster DISPLAY lines
("MRS. BENNET [also: BENNET]") where ExperimentRecord expects names, and the
field is an exact membership test.

Recomputed by expanding each line into the names it stands for, the expected
speaker was available in 2,494 of 2,494 rows. Every wrong answer was a wrong
CHOICE from a list containing the right name.

Reads the committed artifact and calls no model, so it costs nothing to re-run.
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
from experiments.two_stage_attribution import roster_names  # noqa: E402


def gold_index():
    """-> {row id: gold entry} across every PDNC fixture."""
    index = {}
    for path in glob.glob(os.path.join(REPO, "app", "fixtures",
                                       "attribution_gold_pdnc_*.json")):
        stem = os.path.basename(path)[:-5]
        with open(path, encoding="utf-8") as handle:
            for entry in json.load(handle)["entries"]:
                index["%s:%s" % (stem, entry["id"])] = entry
    return index


def analyse(artifact):
    with open(artifact, encoding="utf-8") as handle:
        rows = json.load(handle)["rows"]
    gold = gold_index()
    per_type = collections.defaultdict(
        lambda: {"n": 0, "wrong": 0, "answer_available": 0,
                 "wrong_but_available": 0})
    stated_absent = 0
    for row in rows:
        available = row["expected"] in set(roster_names(row["candidates"]))
        stated_absent += row.get("in_candidates") is False
        entry = gold.get(row["id"]) or {}
        bucket = per_type[entry.get("quote_type", "unknown")]
        bucket["n"] += 1
        bucket["answer_available"] += available
        if not row["correct"]:
            bucket["wrong"] += 1
            bucket["wrong_but_available"] += available
    total = len(rows)
    available = sum(b["answer_available"] for b in per_type.values())
    return {
        "artifact": os.path.basename(artifact),
        "rows": total,
        "field_said_absent": stated_absent,
        "actually_available": available,
        "selection_failures": sum(b["wrong_but_available"]
                                  for b in per_type.values()),
        "by_quote_type": {k: dict(v) for k, v in sorted(per_type.items())},
    }


def render(summary):
    lines = ["=== %s ===" % summary["artifact"],
             "  rows                          %d" % summary["rows"],
             "  in_candidates said absent     %d  (the bug)"
             % summary["field_said_absent"],
             "  answer actually available     %d" % summary["actually_available"],
             "  wrong WITH the answer present %d  <- every failure is a choice"
             % summary["selection_failures"], ""]
    lines.append("  %-11s %6s %6s %8s" % ("quote type", "n", "wrong", "wrong%"))
    for name, bucket in summary["by_quote_type"].items():
        lines.append("  %-11s %6d %6d %7.1f%%"
                     % (name, bucket["n"], bucket["wrong"],
                        100.0 * bucket["wrong"] / max(1, bucket["n"])))
    lines.append("")
    lines.append("  Explicit quotes NAME the speaker in the text, and are the")
    lines.append("  second-worst bucket. That is the finding worth chasing.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--artifact", default=os.path.join(
        REPO, "ab_test_runtime", "experiments",
        "two_stage_attribution_full.json"))
    parser.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "two_stage_selection_gap.json"))
    args = parser.parse_args()

    summary = analyse(args.artifact)
    print(render(summary))
    payload = dict(summary, provenance=provenance(__file__, args))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
