"""Read a gate verdict only from this attempt's isolated output."""
import argparse
import json
import math
import os
from pathlib import Path
import sys


def get_recheck_score(document, adapter, exit_code):
    if not isinstance(document, dict) or exit_code not in (0, 3):
        raise ValueError("no measured gate verdict")
    if document.get("adapter") != os.path.normpath(adapter):
        raise ValueError("gate artifact names another adapter")
    score = document.get("median_ecapa")
    if type(score) not in (int, float) or not -1 <= score <= 1 or not math.isfinite(score):
        raise ValueError("gate score is not a finite cosine similarity")
    if type(document.get("lines")) is not int or document["lines"] < 1:
        raise ValueError("gate has no measured lines")
    if type(document.get("passed")) is not bool or document["passed"] != (exit_code == 0):
        raise ValueError("gate verdict disagrees with process result")
    return score


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact")
    parser.add_argument("adapter")
    parser.add_argument("exit_code", type=int)
    args = parser.parse_args()
    try:
        score = get_recheck_score(json.loads(Path(args.artifact).read_text()), args.adapter, args.exit_code)
    except (OSError, ValueError) as error:
        print(f"NOT MEASURED: {error}", file=sys.stderr)
        return 1
    print(f"{score:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
