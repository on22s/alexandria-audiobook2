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
import locale
import os
from pathlib import Path
import shutil
import tempfile
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
OUTPUT_TAIL_BYTES = 16384

# Dependency order. Producers first, consumer last.
INDEXES = [
    ("tools/audit/audit_experiment_artifacts.py", "structural audit",
     ("ab_test_runtime/audit/artifact_structural_audit.json",)),
    ("tools/audit/audit_legacy_attribution.py", "legacy attribution audit",
     ("ab_test_runtime/audit/legacy_attribution_audit.json",
      "LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md")),
    ("tools/audit/collect_results.py", "results index",
     ("RESULTS_INDEX.md", "results_index.csv")),
    # Downstream of both audits AND of GOALS.md: it reports what each goal's
    # evidence is worth. It was written today and left out of every refresh
    # path, so it silently aged the moment anything else changed - the exact
    # failure this file exists to prevent, reintroduced by its own author.
    ("app/experiments/goal_evidence_audit.py", "goal evidence audit",
     ("ab_test_runtime/audit/goal_evidence_audit.json",)),
]


def run(script, check, python):
    argv = [python, os.path.join(REPO, script)] + (["--check"] if check else [])
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        done = subprocess.run(argv, cwd=REPO, stdout=stdout, stderr=stderr)
        tails = []
        for stream in (stdout, stderr):
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - OUTPUT_TAIL_BYTES))
            # A byte tail can begin within a multibyte character.
            text = stream.read(OUTPUT_TAIL_BYTES).decode(
                locale.getpreferredencoding(False), errors="replace")
            tails.append(text.replace("\r\n", "\n").replace("\r", "\n"))
        return done.returncode == 0, "".join(tails).strip()


class RefreshFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Index refresh failed ({code})")


def restore_indexes(snapshots):
    """Restore every saved target; report all errors and retain failed backups."""
    failures = []
    for target, backup, existed in snapshots:
        try:
            if existed:
                os.replace(backup, target)
            else:
                target.unlink(missing_ok=True)
        except OSError as error:
            failures.append(f"{target}: {error}")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="report staleness without writing, as CI does")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    if args.check:
        stale = []
        for script, label, _outputs in INDEXES:
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

    backup_root = Path(tempfile.mkdtemp(prefix=".index-refresh-", dir=REPO))
    snapshots = []
    preserve_backups = False
    try:
        for _script, _label, outputs in INDEXES:
            for relative in outputs:
                target = Path(REPO, relative)
                backup = backup_root / str(len(snapshots))
                existed = target.exists()
                if target.is_symlink() or (existed and not target.is_file()):
                    raise ValueError(f"Index target must be a regular file: {target}")
                if existed:
                    shutil.copy2(target, backup)
                snapshots.append((target, backup, existed))
        try:
            for script, label, _outputs in INDEXES:
                ok, output = run(script, False, args.python)
                if not ok:
                    print(f"  FAILED {label}", file=sys.stderr)
                    print(output[-1500:], file=sys.stderr)
                    raise RefreshFailure(2)
                print(f"  regenerated {label}")

            # Prove it, rather than assume the order was sufficient.
            remaining = [label for script, label, _outputs in INDEXES
                         if not run(script, True, args.python)[0]]
            if remaining:
                print(f"\nSTILL STALE after a full pass: {', '.join(remaining)}",
                      file=sys.stderr)
                print("The dependency order in INDEXES no longer matches reality.",
                      file=sys.stderr)
                raise RefreshFailure(3)
        except BaseException as error:
            failures = restore_indexes(snapshots)
            if failures:
                preserve_backups = True
                print("Index rollback FAILED: " + "; ".join(failures), file=sys.stderr)
                print(f"Recovery backups retained at {backup_root}", file=sys.stderr)
                return 4
            print("Refresh aborted; prior index files restored.", file=sys.stderr)
            if isinstance(error, RefreshFailure):
                return error.code
            raise
        print("\nevery index agrees")
        return 0
    finally:
        if not preserve_backups:
            shutil.rmtree(backup_root)


if __name__ == "__main__":
    sys.exit(main())
