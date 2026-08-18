"""Which artifacts might back the goals that cite none?

THE STATE THIS ADDRESSES. 25 of 30 goals name no artifact, so
goal_evidence_audit - which joins goals to the structural audit by filename -
reports "cites none" for five sixths of the file. That does not mean the
evidence is missing; it means nobody wrote the filename down, and a reader
following a claim has nowhere to go.

WHAT THIS DOES AND DOES NOT DO. It SUGGESTS, by matching the words in a goal
against the words in artifact names and their recorded experiment families. A
name match is not evidence that an artifact supports a claim - only reading
both can establish that - so the output is a shortlist for a human, and the
tool refuses to write citations itself. Guessing here would be worse than the
silence it replaces: a wrong citation is read as verified.

Ranking prefers artifacts the structural audit rates highest, because a goal
should cite reproducible evidence when reproducible evidence exists.
"""
import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.goal_evidence_audit import parse_goals  # noqa: E402

AUDIT = os.path.join(REPO, "ab_test_runtime", "audit",
                     "artifact_structural_audit.json")
GOALS = os.path.join(REPO, "GOALS.md")

# Words that appear in nearly every goal and match nearly every artifact.
STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "is",
        "are", "be", "must", "not", "with", "that", "this", "it", "its", "at",
        "by", "as", "from", "goal", "book", "books", "test", "run", "runs",
        "one", "two", "no", "any", "all", "per", "than", "when", "each"}

RANK = {"supported_measurement": 0, "supported_structure": 1,
        "provisional": 2, "exploratory": 3, None: 4}


def tokens(text):
    return {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in STOP}


def goal_body(path):
    """-> {number: full text of that goal's section}."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    bodies, current = {}, None
    for line in lines:
        heading = re.match(r"^### (\d+\.\d+)\s+(.*)", line)
        if heading:
            current = heading.group(1)
            bodies[current] = heading.group(2) + "\n"
        elif current:
            bodies[current] += line + "\n"
    return bodies


def suggest(goals_path=GOALS, audit_path=AUDIT, per_goal=5):
    with open(audit_path, encoding="utf-8") as handle:
        artifacts = json.load(handle)["artifacts"]
    bodies = goal_body(goals_path)
    out = []
    for goal in parse_goals(goals_path):
        if goal["cited"]:
            continue
        want = tokens(bodies.get(goal["number"], goal["title"]))
        scored = []
        for row in artifacts:
            name = row["artifact"]
            overlap = want & tokens(name.replace("_", " ").replace(".json", ""))
            if len(overlap) < 2:            # one shared word is coincidence
                continue
            scored.append((-len(overlap), RANK.get(row.get("classification"), 4),
                           name, sorted(overlap), row.get("classification"),
                           row.get("completeness")))
        scored.sort()
        out.append({"goal": goal["number"], "title": goal["title"],
                    "candidates": [{"artifact": n, "shared_words": w,
                                    "classification": c, "completeness": comp}
                                   for _, _, n, w, c, comp in scored[:per_goal]]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="write the shortlist as JSON")
    ap.add_argument("--per-goal", type=int, default=5)
    args = ap.parse_args()
    rows = suggest(per_goal=args.per_goal)
    for row in rows:
        print(f"\n{row['goal']}  {row['title']}")
        if not row["candidates"]:
            print("    no candidate shares two words with this goal - either "
                  "the evidence is named differently, or it does not exist")
        for c in row["candidates"]:
            print(f"    {c['artifact']:55s} {c['classification'] or '-':22s} "
                  f"{', '.join(c['shared_words'])}")
    print(f"\n{len(rows)} goals cite nothing; "
          f"{sum(1 for r in rows if not r['candidates'])} have no candidate at all.")
    print("These are SUGGESTIONS from name overlap. Read the artifact before "
          "citing it: a wrong citation reads as verified.")
    if args.out:
        # Stamped like any other artifact: this file is a shortlist someone
        # may act on months later, and "which commit's audit produced these
        # suggestions" is exactly the question they will have.
        from experiments.provenance import provenance
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"suggestions": rows,
                       "provenance": provenance(__file__, args)},
                      fh, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
