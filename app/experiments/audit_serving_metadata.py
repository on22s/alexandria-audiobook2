"""Audit legacy serving-evaluation metadata and compare base-only results."""
import argparse
import collections
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.provenance import provenance


FALSE_DECODING_FIELDS = {"base_quant", "lora"}


def get_sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def audit_artifact(path):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    rows = doc.get("rows") or []
    meta = doc.get("meta") or {}
    decoding = meta.get("decoding") or {}
    arms = sorted({row.get("arm") for row in rows})
    false_fields = sorted(FALSE_DECODING_FIELDS.intersection(decoding))
    paired_claim = "differ only by the adapter scale" in meta.get(
        "notes", "").lower()
    return {
        "artifact": os.path.basename(path),
        "artifact_sha256": get_sha256(path),
        "model": meta.get("model"),
        "rows": len(rows),
        "actual_arms": arms,
        "unsupported_decoding_fields": false_fields,
        "false_paired_claim": paired_claim and len(arms) == 1,
        "measurement_changed": False,
    }


def summarize_base(path, label=None):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    rows = [row for row in doc["rows"] if row["arm"] == "base"]
    by_book = collections.defaultdict(list)
    for row in rows:
        by_book[row["id"].split(":", 1)[0]].append(row)

    def summary(book_rows):
        return {"rows": len(book_rows),
                "correct": sum(bool(row["correct"]) for row in book_rows),
                "unanswered": sum(not row.get("predicted") for row in book_rows)}

    return {
        "label": label or doc["meta"]["model"],
        "artifact": os.path.basename(path),
        "artifact_sha256": get_sha256(path),
        "per_book": {book: summary(book_rows)
                     for book, book_rows in sorted(by_book.items())},
        "pooled": summary(rows),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    serving = [path for path in sorted(glob.glob(os.path.join(
        args.artifact_dir, "lora_serving_eval__*.json")))
               if ".CORRECTION." not in os.path.basename(path)
               and ".INVALID." not in os.path.basename(path)]
    audited = [audit_artifact(path) for path in serving]

    scout_path = os.path.join(
        args.artifact_dir,
        "lora_serving_eval__scout-q4km-corrected-gold-a100-20260830.json")
    comparisons = [summarize_base(scout_path, "Llama 4 Scout")]
    llama33_path = os.path.join(
        args.artifact_dir,
        "lora_serving_eval__llama33-q4km-corrected-gold-a100-20260830.json")
    if os.path.exists(llama33_path):
        comparisons.append(summarize_base(llama33_path, "Llama 3.3 70B"))
    seen_models = set()
    for path in sorted(glob.glob(os.path.join(
            args.artifact_dir, "distill_eval__*corrected-gold*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        if "meta" not in doc or "rows" not in doc:
            continue
        base_rows = [row for row in doc["rows"] if row.get("arm") == "base"]
        model = doc["meta"].get("model")
        if len(base_rows) != 383 or model in seen_models:
            continue
        seen_models.add(model)
        comparisons.append(summarize_base(path, model))

    report = {
        "scope": "Committed lora_serving_eval artifacts",
        "affected_artifacts": len([a for a in audited
                                   if a["unsupported_decoding_fields"]]),
        "base_only_false_adapter_claims": len([a for a in audited
                                                if a["false_paired_claim"]]),
        "artifacts": audited,
        "baseline_comparison": comparisons,
        "provenance": provenance(__file__, args),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
