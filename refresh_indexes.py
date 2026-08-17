"""Regenerate the three evidence indexes in dependency order, and prove it took.

WHY THIS EXISTS. There are three index scripts and no single way to run them,
so everyone - me included, four times today - regenerates one, sees it pass,
and commits a tree where another is stale. CI checks all three separately and
fails on whichever was missed.

They form a DAG, verified rather than assumed:

    audit_experiment_artifacts.py  ->  artifact_structural_audit.json   (reads nothing)
    audit_legacy_attribution.py    ->  legacy_attribution_audit.json    (reads nothing)
    collect_results.py             ->  RESULTS_INDEX.md, results_index.csv
                                       (READS both audits)

So the audits must run before the index, and one pass is enough. This still
re-checks all three afterwards instead of trusting that reasoning, because a
dependency added later would silently break the order and the failure would
look like flaky CI.

RUN IT FROM A CLEAN CHECKOUT when the output is going to be committed. These
scripts scan the filesystem, not git, so regenerating in a working tree that
holds uncommitted artifacts produces an index describing files CI cannot see -
which is stale from the moment it lands. That mistake was made twice today and
caught both times only by checking afterwards.
"""
import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))

# Dependency order. Producers first, consumer last.
INDEXES = [
    ("audit_experiment_artifacts.py", "structural audit"),
    ("audit_legacy_attribution.py", "legacy attribution audit"),
    ("collect_results.py", "results index"),
]


def run(script, check, python):
    argv = [python, os.path.join(REPO, script)] + (["--check"] if check else [])
    done = subprocess.run(argv, cwd=REPO, capture_output=True, text=True)
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="report staleness without writing, as CI does")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    if args.check:
        stale = []
        for script, label in INDEXES:
            ok, output = run(script, True, args.python)
            print(f"  {'PASS' if ok else 'STALE'}  {label}")
            if not ok:
                stale.append((label, output.splitlines()[-1] if output else ""))
        if stale:
            print("\nstale:", file=sys.stderr)
            for label, why in stale:
                print(f"  {label}: {why}", file=sys.stderr)
            print("\nregenerate with: python refresh_indexes.py", file=sys.stderr)
            return 1
        return 0

    for script, label in INDEXES:
        ok, output = run(script, False, args.python)
        if not ok:
            print(f"  FAILED {label}", file=sys.stderr)
            print(output[-1500:], file=sys.stderr)
            # An undeclared artifact family aborts the legacy audit by design.
            # Say so rather than leaving a half-refreshed set behind.
            print("\nrefresh aborted; the remaining indexes were NOT regenerated "
                  "so the tree is not left half-updated.", file=sys.stderr)
            return 2
        print(f"  regenerated {label}")

    # Prove it, rather than assume the order was sufficient.
    remaining = [label for script, label in INDEXES
                 if not run(script, True, args.python)[0]]
    if remaining:
        print(f"\nSTILL STALE after a full pass: {', '.join(remaining)}",
              file=sys.stderr)
        print("The dependency order in INDEXES no longer matches reality.",
              file=sys.stderr)
        return 3
    print("\nall three indexes agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
