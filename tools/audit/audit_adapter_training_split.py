#!/usr/bin/env python3
"""Audit which shipped voice adapters were trained on their own validation split (GOALS.md 2.7).

Every dataset zip splits 180 train / 20 val, so an adapter whose training_meta.json says it saw 200
samples was trained on its own val clips and its identity-gate number is an upper bound. The
manifest's sample_count is only a copy and can go stale after a retrain, so this reads each adapter's
OWN training_meta.json, and with --verify-hash confirms that file describes the weights on disk
(checkpoint_sha256 == sha256 of adapter_model.safetensors).

An adapter with no readable metadata is reported as its own class. It is never counted as train-only.
"""
import argparse
import collections
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRAIN_ONLY = 180      # 180 train / 20 val
ALL_CLIPS = 200       # train + val: the adapter saw its own validation clips
WEIGHTS = "adapter_model.safetensors"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def classify(models_dir, entry, verify_hash=False):
    """One manifest entry -> a record of what the adapter's own metadata says."""
    name = entry["id"]
    record = {"id": name, "manifest_sample_count": entry.get("sample_count")}
    meta_path = os.path.join(models_dir, name, "training_meta.json")
    if not os.path.isfile(meta_path):
        return dict(record, klass="no-metadata", num_samples=None)
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        num = meta["num_samples"]
        if isinstance(num, bool) or not isinstance(num, int):
            raise ValueError("num_samples is not an integer")
    except (OSError, ValueError, KeyError, TypeError):
        return dict(record, klass="unreadable", num_samples=None)
    klass = {TRAIN_ONLY: "train-only", ALL_CLIPS: "all-clips"}.get(num, "other-count")
    record.update(klass=klass, num_samples=num,
                  manifest_disagrees=entry.get("sample_count") != num)
    if verify_hash:
        weights = os.path.join(models_dir, name, WEIGHTS)
        expected = meta.get("checkpoint_sha256")
        if not expected or not os.path.isfile(weights):
            record["weights_match_meta"] = None
        else:
            record["weights_match_meta"] = sha256_file(weights) == expected
    return record


def audit(models_dir, verify_hash=False):
    with open(os.path.join(models_dir, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    records = sorted((classify(models_dir, entry, verify_hash) for entry in manifest),
                     key=lambda r: r["id"])
    by_class = collections.defaultdict(list)
    for record in records:
        by_class[record["klass"]].append(record["id"])
    summary = {
        "adapters": len(records),
        "counts": {klass: len(ids) for klass, ids in sorted(by_class.items())},
        "all_clips": by_class.get("all-clips", []),
        "other_count": {r["id"]: r["num_samples"] for r in records if r["klass"] == "other-count"},
        "unclassified": by_class.get("no-metadata", []) + by_class.get("unreadable", []),
        "manifest_claims_all_clips": sum(1 for r in records if r["manifest_sample_count"] == ALL_CLIPS),
        "manifest_disagrees": [r["id"] for r in records if r.get("manifest_disagrees")],
    }
    if verify_hash:
        summary["weights_do_not_match_meta"] = [r["id"] for r in records if r.get("weights_match_meta") is False]
        summary["weights_unverifiable"] = [r["id"] for r in records if r.get("weights_match_meta") is None
                                           and r["klass"] not in ("no-metadata", "unreadable")]
    return {"summary": summary, "adapters": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models-dir", default=os.path.join(REPO, "lora_models"))
    parser.add_argument("--verify-hash", action="store_true",
                        help="also check each training_meta.json describes the weights on disk")
    parser.add_argument("--full", action="store_true", help="print every adapter, not just the summary")
    args = parser.parse_args()
    result = audit(args.models_dir, args.verify_hash)
    print(json.dumps(result if args.full else result["summary"], indent=1, sort_keys=True))
    summary = result["summary"]
    if summary["unclassified"] or summary.get("weights_do_not_match_meta"):
        sys.exit(2)     # the audit could not vouch for every adapter: fail loudly, do not report a clean count


if __name__ == "__main__":
    main()
