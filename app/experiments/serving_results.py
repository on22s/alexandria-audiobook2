"""Check finalized paired serving records before a campaign resumes."""
import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.manifest import validate_stored_summary


def get_complete_serving_rows(document):
    """Return rows only after final metadata, paired coverage and counts agree."""
    if not isinstance(document, dict):
        raise ValueError("serving result must be an object")
    meta = document.get("meta")
    if not isinstance(meta, dict) or meta.get("validation") != "ok":
        raise ValueError("serving result lacks successful final validation")
    finished = meta.get("finished")
    if type(finished) not in (int, float) or finished <= 0 or (type(finished) is float and not math.isfinite(finished)):
        raise ValueError("serving result lacks a final completion time")
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("serving result has no scored rows")
    seen = {arm: set() for arm in ("base", "lora")}
    labels = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("arm"), str) or row["arm"] not in seen:
            raise ValueError("serving row must name base or lora")
        arm, identifier = row["arm"], row.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen[arm]:
            raise ValueError("serving row has a missing or duplicate id")
        if type(row.get("correct")) is not bool:
            raise ValueError("serving row correctness must be boolean")
        if "predicted" not in row or (row["predicted"] is not None and not isinstance(row["predicted"], str)):
            raise ValueError("serving row prediction must be text or null")
        if not isinstance(row.get("line"), str) or not isinstance(row.get("expected"), str) or not row["expected"]:
            raise ValueError("serving row requires gold line and expected speaker")
        label = (row["line"], row["expected"])
        if identifier in labels and labels[identifier] != label:
            raise ValueError("paired serving rows disagree on gold evidence")
        labels[identifier] = label
        seen[arm].add(identifier)
    if not seen["base"] or seen["base"] != seen["lora"]:
        raise ValueError("serving arms do not cover the same nonempty sample")
    summary = document.get("summary")
    if not isinstance(summary, dict) or set(summary) != set(seen):
        raise ValueError("serving summary must name both arms")
    for arm, bucket in summary.items():
        if not isinstance(bucket, dict) or any(type(bucket.get(key)) is not int for key in ("n", "correct")):
            raise ValueError(f"{arm}: summary counts must be integers")
    problems = validate_stored_summary(document)
    if problems:
        raise ValueError("; ".join(problems))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact")
    args = parser.parse_args()
    try:
        get_complete_serving_rows(json.loads(Path(args.artifact).read_text()))
    except (OSError, ValueError) as error:
        print(f"REFUSING incomplete serving result: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
