"""Refuse a rewrite that turns a measurement into an empty one.

WHAT HAPPENED. `dataset_ref_audit.json` held 101 measured rows on 2026-08-07.
The replay stage rewrote it on 2026-08-18 with `results: []` and no explanation
of any kind, and that empty version was committed and carried on main for two
days. The only surviving copy of the 101 rows was on two old feature branches
that were one `git push --delete` away from being pruned as stale.

THE DISTINCTION THIS DRAWS, and it is the whole point. Two other artifacts were
emptied by the same replay - three_pass_vs_single_fallback and
three_pass_vs_single_index18, both 1 row to 0 - and those are FINE. They carry
`"failures": [{"book": ..., "arm": ..., "rc": 1}]`, which is a run saying "I
could not reproduce this, and here is why". That is the honest shape and must
keep working; restoring their old numbers would reinstate figures the replay
could not verify.

So the rule is not "artifacts may never shrink". It is: an artifact may only
shrink if the new version SAYS WHY. Silence is the failure.

Run it after a stage that rewrites artifacts and before committing them.
"""
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPERIMENTS = os.path.join("ab_test_runtime", "experiments")
# Keys a run uses to record that it could not measure. Any non-empty one makes
# a shrink explained rather than silent.
EXPLANATION_KEYS = ("failures", "errors", "error", "skipped", "refusals",
                    "status_reason", "note")


def row_count(doc):
    """-> number of measured rows, or None when the shape has no row list.

    Both contracts in this directory: the experiment records call the list
    `rows`, the measurement scripts call it `results`. A document with neither
    returns None and is not judged - a comparison artifact keeps its data under
    its own names and shrinking is not defined for it.
    """
    if not isinstance(doc, dict):
        return None
    for key in ("rows", "results"):
        value = doc.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def explained(doc):
    """-> True when the document itself says why it is short."""
    for key in EXPLANATION_KEYS:
        value = doc.get(key)
        if isinstance(value, (list, dict)) and len(value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def committed_version(path, ref, repo):
    """-> the parsed artifact at `ref`, or None if it is not there / unreadable."""
    result = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (ref, path)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def scan(repo, ref):
    """-> (silent_shrinks, explained_shrinks) as lists of dicts."""
    listing = subprocess.run(
        ["git", "-C", repo, "ls-files", os.path.join(EXPERIMENTS, "*.json")],
        capture_output=True, text=True, check=True).stdout.split()
    silent, explained_rows = [], []
    for path in listing:
        full = os.path.join(repo, path)
        if not os.path.exists(full):
            continue
        try:
            with open(full, encoding="utf-8") as handle:
                current = json.load(handle)
        except (OSError, ValueError):
            continue
        before = committed_version(path, ref, repo)
        if before is None:
            continue
        old, new = row_count(before), row_count(current)
        if old is None or new is None or new >= old:
            continue
        record = {"artifact": os.path.basename(path), "was": old, "now": new,
                  "explained": explained(current)}
        (explained_rows if record["explained"] else silent).append(record)
    return silent, explained_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ref", default="HEAD",
                        help="what to compare against (default: HEAD)")
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args()

    silent, explained_rows = scan(args.repo, args.ref)
    for row in explained_rows:
        print("  ok   %s: %d -> %d rows, and it says why"
              % (row["artifact"], row["was"], row["now"]))
    if not silent:
        print("no artifact lost rows without explaining itself")
        return
    print("\nARTIFACTS LOST ROWS WITH NO EXPLANATION:")
    for row in silent:
        print("  %s: %d -> %d rows" % (row["artifact"], row["was"], row["now"]))
    raise SystemExit(
        "\nA run that measured less than the one before it either failed or "
        "changed what it measures, and the artifact has to say which.\nRecord "
        "the reason in a `failures` list, or do not commit the rewrite.\n"
        "dataset_ref_audit.json went 101 rows to 0 this way and the loss was "
        "invisible for two days.")


if __name__ == "__main__":
    main()
