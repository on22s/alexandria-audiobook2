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
        row.update({"identity_contract": None, "seed": None, "status": None,
                    "has_rows": False, "classification": "exploratory",
                    "reason": f"unreadable JSON: {type(exc).__name__}"})
        return row
    if not isinstance(doc, dict):
        row.update({"classification": "exploratory",
                    "reason": "top-level list, no embedded identity"})
        return row

    provenance = doc.get("provenance")
    meta = doc.get("meta")
    row.update({"seed": None, "status": doc.get("status"),
                "has_rows": isinstance(doc.get("rows"), list)})
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


def indexable_artifacts(experiment_dir=EXPERIMENT_DIR):
    """Artifact paths that belong in a CHECKED-IN index: the tracked ones.

    A committed index is a claim about what evidence exists for everyone. An
    artifact sitting untracked on one machine is precisely the evidence nobody
    else can see, so indexing it makes the index unreproducible: CI checks out
    the committed files, regenerates, and disagrees. That is not hypothetical -
    it failed PR #340 with six local artifacts in the index, and it would have
    failed again after every run of the overnight queue.

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
