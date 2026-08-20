#!/usr/bin/env python3
"""Regenerate the structural experiment-artifact audit."""
import argparse
import collections
import glob
import hashlib
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
from experiments.manifest import completeness  # noqa: E402
EXPERIMENT_DIR = os.path.join(REPO, "ab_test_runtime", "experiments")
DEFAULT_OUT = os.path.join(
    REPO, "ab_test_runtime", "audit", "artifact_structural_audit.json")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_artifact(path):
    row = {"artifact": os.path.basename(path), "bytes": os.path.getsize(path),
           "sha256": file_sha256(path)}
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        # "unknown", not null: an unreadable file is one whose completeness
        # cannot be determined, which is exactly what unknown means. A null
        # here would be a fourth state that no reader handles.
        row.update({"identity_contract": None, "seed": None, "status": None,
                    "has_rows": False, "classification": "exploratory",
                    "completeness": "unknown",
                    "reason": f"unreadable JSON: {type(exc).__name__}"})
        return row
    if not isinstance(doc, dict):
        row.update({"classification": "exploratory",
                    "completeness": "unknown",
                    "reason": "top-level list, no embedded identity"})
        return row

    # WAS THIS RUN FINISHED? The index is where someone looks before citing a
    # number, and until now a run killed at 1129 of 1200 terms was
    # indistinguishable here from one that completed. Same question as the
    # chains and the scorers ask, one definition (Rule 15).
    row["completeness"] = completeness(doc)

    provenance = doc.get("provenance")
    meta = doc.get("meta")
    # has_rows USED TO MEAN "there is a rows key of list type", which is true
    # of an EMPTY list - so an artifact recording no answers at all reported
    # has_rows=true. It now means what it says, and it looks under BOTH names
    # the artifacts in this directory use: the experiment contract calls the
    # list `rows`, the measurement scripts call it `results`, and checking only
    # one would call 1,129 measured terms "empty".
    records = doc.get("rows")
    if not isinstance(records, list):
        records = doc.get("results")
    row.update({"seed": None, "status": doc.get("status"),
                "row_count": len(records) if isinstance(records, list) else None,
                "has_rows": bool(isinstance(records, list) and records)})
    if isinstance(provenance, dict):
        git = provenance.get("git") or {}
        args = provenance.get("args") or {}
        row.update({"identity_contract": "tts_provenance",
                    "seed": provenance.get("seed", args.get("seed")),
                    # WHEN the run happened, from the same block that already
                    # supplies seed, commit and dirty. Its absence was not a
                    # design choice: "when was this measured" could only be
                    # answered by opening each artifact, and the 374 without a
                    # provenance block have no answer at all - which is worth
                    # seeing in the audit rather than discovering per file.
                    "written": provenance.get("written"),
                    "commit": git.get("commit"), "dirty": git.get("dirty")})
        if git.get("commit") and len(git.get("harness_sha256", "")) == 64:
            row.update({"classification": "supported_structure",
                        "reason": "embedded provenance and identifiable harness"})
        else:
            row.update({"classification": "exploratory",
                        "reason": "embedded provenance is incomplete"})
    elif isinstance(meta, dict):
        git = meta.get("git") or {}
        row.update({"identity_contract": "experiment_meta",
                    "seed": meta.get("seed"), "commit": git.get("commit"),
                    # Same field, other contract: experiment metadata calls it
                    # `written` too, so both paths report it identically.
                    "written": meta.get("written"),
                    "dirty": git.get("dirty")})
        if git.get("commit"):
            row.update({"classification": "provisional",
                        "reason": "older metadata contract; semantic/manual review required"})
        else:
            row.update({"classification": "exploratory",
                        "reason": "metadata present without commit identity"})
    else:
        row.update({"identity_contract": "none",
                    "classification": "exploratory",
                    "reason": "no embedded provenance or experiment metadata"})

    return row


def tracked_state(path, repo=None):
    """-> "tracked" | "untracked" | "unknown". The one place that answers this.

    `repo` is which repository to ask, and callers in other modules must pass
    their own: audit_legacy_attribution resolves gold paths against ITS REPO,
    which tests patch. Silently asking this module's repo instead answered a
    question nobody had asked, and reported a temp-dir fixture as untracked.

    Any checked-in index that consults an untracked input encodes local state
    and disagrees with CI the moment it is regenerated - which is how both
    index gates broke on PR #340, in two different files, for the same reason
    (Rule 15).

    "UNKNOWN IS A THIRD ANSWER" and must not be spelled "untracked", the same
    distinction gpu_job.sh's tree_state makes for the same reason. A temp
    directory, a source export or a container without git cannot answer the
    question, and collapsing that into "untracked" would make an audit run
    outside a repository silently ignore every gold fixture it was handed -
    reporting nothing wrong while checking nothing at all.
    """
    try:
        result = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path],
                                cwd=repo or REPO, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode == 0:
        return "tracked"
    stderr = result.stderr.decode("utf-8", "replace").lower()
    # git distinguishes these itself: "did not match any file(s) known to git"
    # is an answer; "not a git repository" is a refusal to answer.
    if "not a git repository" in stderr or "outside repository" in stderr:
        return "unknown"
    return "untracked"


def indexable_artifacts(experiment_dir=EXPERIMENT_DIR):
    """Artifact paths that belong in a CHECKED-IN index: the tracked ones.

    A committed index is a claim about what evidence exists for everyone. An
    artifact sitting untracked on one machine is precisely the evidence nobody
    else can see, so indexing it makes the index unreproducible: CI checks out
    the committed files, regenerates, and disagrees. That is not hypothetical -
    it failed PR #340 with six local artifacts in the index, and it would have
    failed again after every run of the overnight queue.

    The same separation DVC makes structurally: a stage declares `deps`
    (inputs, the script included) and `outs`, and dvc.lock hashes the two
    separately, so a rewritten output can never be mistaken for a changed
    input. We have no dvc.lock, so "is it in the repository" stands in for
    "can anyone but this machine see it" - which is the question a committed
    index is actually asking.

    This mirrors tests/test_inventory.py::_tracked_test_modules, which exists
    for the identical reason after breaking the build three times in one day,
    including its fallback: when git cannot answer (a source export, a
    container without git), fall back to the filesystem rather than reporting
    an empty index.
    """
    on_disk = sorted(glob.glob(os.path.join(experiment_dir, "*.json")))
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", experiment_dir],
            cwd=REPO, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return on_disk, []
    if result.returncode != 0:
        return on_disk, []
    tracked = {os.path.basename(name)
               for name in result.stdout.decode("utf-8").split("\0") if name}
    keep = [p for p in on_disk if os.path.basename(p) in tracked]
    skipped = [os.path.basename(p) for p in on_disk
               if os.path.basename(p) not in tracked]
    return keep, skipped


def build_audit(experiment_dir=EXPERIMENT_DIR):
    paths, _ = indexable_artifacts(experiment_dir)
    artifacts = [classify_artifact(path) for path in paths]
    summary = dict(collections.Counter(
        row["classification"] for row in artifacts))
    return {
        "scope": "structural audit only; classifications do not validate scientific conclusions",
        "summary": summary, "artifacts": artifacts}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    audit = build_audit()
    _, untracked = indexable_artifacts()
    if untracked:
        # Reported, never indexed. Silence here would mean an artifact could
        # be produced, cited in conversation, and never noticed as missing
        # from the record.
        print(f"note: {len(untracked)} artifact(s) on disk are untracked and "
              f"therefore not indexed; commit them to have them counted:",
              file=sys.stderr)
        for name in untracked[:10]:
            print(f"  {name}", file=sys.stderr)
    if args.check:
        try:
            with open(args.out, encoding="utf-8") as handle:
                current = json.load(handle)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"structural audit is unreadable: {exc}") from exc
        if current != audit:
            raise SystemExit("structural audit is stale; regenerate it")
        print(f"structural audit is current ({len(audit['artifacts'])} artifacts)")
        return
    from utils import atomic_json_write
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    atomic_json_write(audit, args.out)
    print(f"wrote {len(audit['artifacts'])} artifacts to {args.out}")


if __name__ == "__main__":
    main()
