"""Score the PDNC intervention pilots as PAIRED comparisons, with a noise floor.

WHY THIS EXISTS. Each pilot artifact reports two headline accuracies, and the
gaps look small but real: +1.0, +2.3, +0.8 points. Those are BETWEEN-arm
percentages on the same 600 lines, which is the wrong comparison twice over -
it ignores that the rows are paired, and it offers nothing to judge the size
against.

THE NOISE FLOOR IS THE POINT. Two of these runs measured the IDENTICAL baseline
condition hours apart. Whatever they disagree about is churn, and it turns out
to be 33 of 600 rows - the same order as the discordant counts the
interventions are judged on. A +2.3 point arm sitting on a 5.5% floor is not
obviously a finding, and the only way to see that is to put the two numbers
side by side.

It reads committed artifacts and touches no GPU, so it can run beside the
queue.
"""
import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.stats import paired, paired_coverage          # noqa: E402
from experiments.manifest import completeness, read_inputs  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

EXPERIMENTS = os.path.join(REPO, "ab_test_runtime", "experiments")
PILOTS = ("pdnc_evidence", "pdnc_sequence", "pdnc_context_evidence")


def _load_pilot(path):
    """Read answer maps and explicit transport failures without mixing them."""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    arms = collections.defaultdict(dict)
    failures = collections.defaultdict(list)
    seen = set()
    for row in payload["rows"]:
        arm, row_id = row["arm"], row["id"]
        key = (arm, row_id)
        if key in seen:
            raise ValueError(f"Duplicate pilot row: {key}")
        seen.add(key)
        answers = arms[arm]
        if row.get("status") == "transport_failed":
            failures[arm].append(row_id)
        else:
            if type(row["correct"]) is not bool:
                raise ValueError(f"Pilot correctness must be boolean: {key}")
            answers[row_id] = row["correct"]
    return arms, {arm: failures[arm] for arm in arms}


def arms_of(path):
    """Return {arm: {row_id: correct}} for measured rows in one pilot."""
    return _load_pilot(path)[0]


def score(paths):
    per_pilot, baselines = [], {}
    for path in paths:
        arms, failures = _load_pilot(path)
        if "baseline" not in arms:
            raise SystemExit("%s has no baseline arm; arms=%s"
                             % (path, sorted(arms)))
        base = arms["baseline"]
        others = [a for a in arms if a != "baseline"]
        if len(others) != 1:
            raise SystemExit("%s has %d non-baseline arms (%s); this scorer "
                             "compares exactly one" % (path, len(others), others))
        arm = arms[others[0]]
        p, base_only, arm_only, n = paired(base, arm)
        per_pilot.append({
            "artifact": os.path.basename(path),
            "completeness": completeness(path),
            "arm": others[0],
            "shared_rows": n,
            "baseline_accuracy": sum(base.values()) / len(base) if base else None,
            "paired_cohort": paired_coverage(base, arm),
            "transport_failures": {name: len(ids) for name, ids in failures.items()},
            "transport_failure_ids": failures,
            "arm_accuracy": sum(arm.values()) / len(arm) if arm else None,
            "arm_only_wins": arm_only,
            "baseline_only_wins": base_only,
            "p_value": p,
        })
        baselines[os.path.basename(path)] = base

    # THE SAME CONDITION, TWICE. Anything these disagree about is run-to-run
    # churn and nothing else, which is the yardstick the table above needs.
    floor = []
    names = sorted(baselines)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = baselines[names[i]], baselines[names[j]]
            shared = sorted(set(a) & set(b))
            disagree = sum(1 for k in shared if a[k] != b[k])
            floor.append({
                "runs": [names[i], names[j]],
                "shared_rows": len(shared),
                "rows_disagreeing": disagree,
                "fraction": disagree / len(shared) if shared else None,
            })
    return {"pilots": per_pilot, "baseline_noise_floor": floor}


def render(summary):
    lines = ["=== PDNC pilots, paired ==="]
    def format_accuracy(value):
        return "n/a" if value is None else f"{value * 100:5.1f}%"
    for row in summary["pilots"]:
        cohort = row["paired_cohort"]
        first, second = cohort["first"], cohort["second"]
        failures = row["transport_failures"]
        lines.append(
            f"  {row['artifact'].replace('__pilot__local-llamacpp.json', ''):26} "
            f"{row['arm']:10} base {format_accuracy(first['shared_accuracy'])}  "
            f"arm {format_accuracy(second['shared_accuracy'])}  shared n={cohort['shared_rows']}  "
            f"unmatched {first['unmatched_rows']}/{second['unmatched_rows']}  "
            f"transport failures {failures['baseline']}/{failures[row['arm']]}  "
            f"discordant {row['arm_only_wins']}/{row['baseline_only_wins']}  p={row['p_value']:.3f}")
    lines.append("=== the same baseline condition, measured twice ===")
    for row in summary["baseline_noise_floor"]:
        fraction = row["fraction"]
        lines.append("  %s vs %s: %d/%d rows disagree (%s)"
                     % (row["runs"][0][:24], row["runs"][1][:24],
                        row["rows_disagreeing"], row["shared_rows"],
                        "n/a" if fraction is None else "%.1f%%" % (fraction * 100)))
    lines.append("  ^ read every p above against this. An arm whose discordant")
    lines.append("    count is the size of the floor has not shown anything.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilots", nargs="+", default=list(PILOTS),
                        help="pilot stems under ab_test_runtime/experiments")
    parser.add_argument("--out", default=os.path.join(
        EXPERIMENTS, "pdnc_pilots_paired.json"))
    args = parser.parse_args()

    paths = []
    for stem in args.pilots:
        path = stem if os.path.isabs(stem) else os.path.join(
            EXPERIMENTS, "%s__pilot__local-llamacpp.json" % stem)
        if not os.path.exists(path):
            raise SystemExit("no such pilot artifact: %s" % path)
        paths.append(path)

    summary = score(paths)
    print(render(summary))

    # WHICH artifacts, by content hash - the same provenance pair_e_row records.
    # A rescorer's number is only as good as the copies it read, and these
    # pilots are exactly the kind of file a replay stage rewrites underneath.
    payload = dict(summary, read_inputs=read_inputs(paths, REPO),
                   provenance=provenance(__file__, args))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
