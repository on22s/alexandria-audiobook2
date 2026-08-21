#!/usr/bin/env python3
"""Record a content digest for adapters whose provenance never bound one.

Of 254 `training_meta.json` files on this machine, 177 carried a
`checkpoint_sha256` and every one of those 177 matched the bytes beside it.
The other 76 recorded hyperparameters, a loss and a reference-audio path with
nothing tying any of it to a particular adapter.

WHAT THIS CAN AND CANNOT SAY. A digest written today attests to what the file
IS, not to what it was when it was trained. It closes the gap going forward -
a later edit or swap becomes detectable - and it does not retroactively verify
the claim. That distinction is the whole point, so it is written into the
artifact: a backfilled digest is marked `checkpoint_sha256_backfilled` with the
date, and readers that care about provenance strength can tell the two apart
instead of seeing 254 uniformly trustworthy records.

Never overwrites an existing digest. If one is present and does NOT match the
bytes, that is a finding, not something to paper over: it is reported and the
file is left exactly as it was.

    python tools/backfill_adapter_digests.py --root . [--apply]

Dry run by default.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "app"))

from utils import atomic_json_write  # noqa: E402

ADAPTER = "adapter_model.safetensors"


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_metas(root):
    """-> every training_meta.json under root, skipping virtualenvs."""
    out = []
    for base, dirs, names in os.walk(root):
        # `cache` is HF_HOME: third-party downloads whose snapshots are
        # symlinks into a blob store. One of them dangles, and attesting
        # somebody else's cache entry is not this tool's job - the installed
        # copy under builtin_lora is what we ship and what gets checked.
        dirs[:] = [d for d in dirs
                   if d not in {"env", "venv", ".venv", ".git", "node_modules",
                                "cache"}]
        if "training_meta.json" in names:
            out.append(os.path.join(base, "training_meta.json"))
    return sorted(out)


def classify(meta_path):
    """-> (verdict, detail) for one adapter, touching nothing."""
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError) as exc:
        return "unreadable", str(exc)
    if not isinstance(meta, dict):
        return "unreadable", "not a JSON object"
    adapter = os.path.join(os.path.dirname(meta_path), ADAPTER)
    if not os.path.isfile(adapter):
        return "no adapter file", adapter
    claimed = meta.get("checkpoint_sha256")
    actual = file_sha256(adapter)
    if not claimed:
        return "backfillable", actual
    if claimed == actual:
        return "already bound", claimed
    return "MISMATCH", "%s recorded, %s on disk" % (claimed[:12], actual[:12])


def backfill(meta_path, digest, today):
    with open(meta_path, encoding="utf-8") as handle:
        meta = json.load(handle)
    meta["checkpoint_sha256"] = digest
    meta["checkpoint_sha256_backfilled"] = today
    atomic_json_write(meta, meta_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=REPO)
    parser.add_argument("--apply", action="store_true",
                        help="write the digests; omit for a dry run")
    args = parser.parse_args()

    today = datetime.date.today().isoformat()
    counts = {}
    mismatches = []
    pending = []
    for meta_path in find_metas(args.root):
        verdict, detail = classify(meta_path)
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "MISMATCH":
            mismatches.append((meta_path, detail))
        elif verdict == "backfillable":
            pending.append((meta_path, detail))

    for verdict in sorted(counts):
        print("%-18s %d" % (verdict, counts[verdict]))
    for path, detail in mismatches:
        print("MISMATCH %s: %s" % (os.path.relpath(path, args.root), detail))

    if not args.apply:
        print("\ndry run; %d would be backfilled. Re-run with --apply." % len(pending))
        return 1 if mismatches else 0

    for path, digest in pending:
        backfill(path, digest, today)
    print("\nbackfilled %d, marked checkpoint_sha256_backfilled=%s" % (len(pending), today))
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
