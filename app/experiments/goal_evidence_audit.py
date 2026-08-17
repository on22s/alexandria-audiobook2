"""What is each goal's evidence actually worth? Read from the audit, per goal.

WHY. The structural audit classifies all 480 artifacts and nobody reads it. It
reports that only 108 carry embedded provenance and an identifiable harness;
198 carry no provenance or metadata at all; 202 cannot even say whether the
tree was dirty when they were written. RESULTS_INDEX.md states the consequence
in its own header - "the numbers are inspectable but the run is not
reproducible from its recorded commit" - and draws the distinction that
matters, between INSPECTABLE and CITABLE.

None of that reaches the place the numbers are actually quoted. GOALS.md names
artifacts and reports figures; the audit knows what those artifacts are worth;
the two have never been put side by side. So a goal can read as settled while
resting on a file that cannot say what produced it, and nothing says so.

This joins them. For every goal that names an artifact, it reports the
classification the audit already assigned, and flags the goals whose evidence
is weakest.

WHAT IT DOES NOT DO. Judge whether a conclusion is right. The audit's own scope
line is explicit - "structural audit only; classifications do not validate
scientific conclusions" - and this inherits that limit exactly. A goal resting
entirely on `supported_structure` artifacts can still be wrong; one resting on
`exploratory` ones can still be right. What this measures is whether the
evidence could be checked by someone else, which is a different question and
the one nobody was asking.

A GOAL NAMING NO ARTIFACT IS NOT SCORED, and that is itself reported: goal 2.7
quotes figures (0.404 -> 0.503) without naming the file they come from, so a
filename join cannot see it. Those are listed separately rather than counted as
having no evidence.
"""
import argparse
import collections
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Weakest first, so a goal's worst evidence is what gets reported.
RANK = ["exploratory", "provisional", "supported_structure",
        "supported_measurement"]


def load_audit(path):
    """-> {artifact basename: (classification, dirty, commit)}"""
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    return {row["artifact"]: (row.get("classification"), row.get("dirty"),
                              row.get("commit"))
            for row in document.get("artifacts", [])}


def parse_goals(path):
    """-> [(number, title, [artifact names cited])] in file order."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    goals, current = [], None
    for line in lines:
        heading = re.match(r"^### (\d+\.\d+)\s+(.*)", line)
        if heading:
            current = {"number": heading.group(1), "title": heading.group(2),
                       "cited": []}
            goals.append(current)
            continue
        if current is None:
            continue
        current["cited"] += re.findall(r"`([a-z0-9_]+\.json)`", line)
    for goal in goals:
        goal["cited"] = sorted(set(goal["cited"]))
    return goals


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--goals", default=os.path.join(REPO, "GOALS.md"))
    ap.add_argument("--audit", default=os.path.join(
        REPO, "ab_test_runtime", "audit", "artifact_structural_audit.json"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "goal_evidence_audit.json"))
    args = ap.parse_args()

    audit = load_audit(args.audit)
    goals = parse_goals(args.goals)

    rows, unscored = [], []
    for goal in goals:
        if not goal["cited"]:
            unscored.append(goal)
            continue
        found = [(name,) + audit[name] for name in goal["cited"] if name in audit]
        # A goal may name a fixture, a config or the lexicon rather than an
        # experiment artifact - goal 5.2 cites pronunciation.json, which is the
        # thing being built, not evidence about it. Those are not "missing from
        # the audit", they were never in its scope, and calling them missing
        # would manufacture a problem.
        missing = [name for name in goal["cited"]
                   if name not in audit
                   and os.path.exists(os.path.join(
                       REPO, "ab_test_runtime", "experiments", name))]
        out_of_scope = [name for name in goal["cited"]
                        if name not in audit
                        and not os.path.exists(os.path.join(
                            REPO, "ab_test_runtime", "experiments", name))]
        classes = [c for _, c, _, _ in found if c]
        worst = min(classes, key=lambda c: RANK.index(c)
                    if c in RANK else -1) if classes else None
        rows.append({
            "goal": goal["number"], "title": goal["title"],
            "cited": len(goal["cited"]),
            "not_in_audit": missing,
            "not_experiment_artifacts": out_of_scope,
            "weakest_evidence": worst,
            "classifications": dict(collections.Counter(classes)),
            "dirty_artifacts": sum(1 for _, _, d, _ in found if d is True),
            "no_commit": sum(1 for _, _, _, c in found if not c),
            "artifacts": [{"name": n, "classification": c, "dirty": d,
                           "commit": (c2 or "")[:8]} for n, c, d, c2 in found],
        })

    document = {
        "note": "structural only; inherits the audit's scope - classifications "
                "do not validate scientific conclusions. Measures whether a "
                "goal's evidence could be checked by someone else.",
        "goals_scored": len(rows),
        "goals_citing_no_artifact": [g["number"] for g in unscored],
        "rows": rows,
    }
    try:
        from experiments.provenance import provenance
        document["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        document["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)

    order = {c: i for i, c in enumerate(RANK)}
    rows.sort(key=lambda r: (order.get(r["weakest_evidence"], -1), r["goal"]))
    print(f"{len(rows)} goals cite an artifact; "
          f"{len(unscored)} cite none\n")
    print(f"  {'goal':6}{'weakest':22}{'cited':>6}{'dirty':>7}{'no commit':>11}  title")
    for r in rows:
        print(f"  {r['goal']:6}{str(r['weakest_evidence']):22}{r['cited']:>6}"
              f"{r['dirty_artifacts']:>7}{r['no_commit']:>11}  {r['title'][:38]}")
    if unscored:
        print(f"\n  cite no artifact by name (a filename join cannot see their "
              f"evidence): {', '.join(g['number'] for g in unscored)}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
