#!/usr/bin/env python3
"""Report whether attribution artifacts can support roster-conditioned claims.

An ``available`` summary of zero is ambiguous when every row carries
``in_candidates=null``: it means the evaluator did not record its roster, not
that the expected speaker was absent.  This audit separates those states and
groups them by experiment and producing commit.
"""
import argparse
import collections
import glob
import json
import os


def classify_rows(rows):
    """Return the roster-membership instrumentation state for artifact rows."""
    if not rows:
        return "no_rows"
    present = ["in_candidates" in row for row in rows]
    values = [row.get("in_candidates") for row in rows]
    if not any(present):
        return "field_absent"
    if all(value is None for value in values):
        return "all_null"
    if all(value is not None for value in values):
        return "populated"
    return "mixed"


def audit_paths(paths):
    groups = collections.Counter()
    artifacts = []
    for path in sorted(paths):
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
            continue
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        experiment = meta.get("experiment") or doc.get("experiment") or "unknown"
        state = classify_rows(doc["rows"])
        commit = git.get("commit")
        groups[(experiment, commit, state)] += 1
        artifacts.append({
            "artifact": os.path.basename(path),
            "experiment": experiment,
            "commit": commit,
            "rows": len(doc["rows"]),
            "candidate_membership": state,
        })
    return {
        "groups": [
            {"experiment": experiment, "commit": commit,
             "candidate_membership": state, "artifacts": count}
            for (experiment, commit, state), count in sorted(
                groups.items(), key=lambda item: tuple(str(x) for x in item[0]))
        ],
        "artifacts": artifacts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="+", help="artifact files or globs")
    parser.add_argument("--out")
    args = parser.parse_args()
    paths = []
    for pattern in args.paths:
        matches = glob.glob(pattern, recursive=True)
        paths.extend(matches or [pattern])
    result = audit_paths(paths)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
