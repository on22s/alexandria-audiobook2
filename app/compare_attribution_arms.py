"""Compare two three-pass attribution arms and sample their disagreements.

Structural metrics cannot say which arm is correct, so the manual scoring pass
should look only at entries where the arms actually disagree.
"""

import argparse
import json
import random


def find_disagreements(arm_a, arm_b):
    """Return entries where two arms assigned different speakers."""
    if len(arm_a) != len(arm_b):
        raise ValueError(
            f"arms have different entry counts: {len(arm_a)} vs {len(arm_b)}")
    rows = []
    for index, (left, right) in enumerate(zip(arm_a, arm_b)):
        if not left or not right:
            continue
        if left.get("speaker") != right.get("speaker"):
            rows.append({"index": index, "arm_a": left.get("speaker"),
                         "arm_b": right.get("speaker"),
                         "text": left.get("text", "")[:300]})
    return rows


def sample_disagreements(rows, size=50, seed=7):
    """Draw a reproducible random sample for hand-scoring."""
    if len(rows) <= size:
        return list(rows)
    return random.Random(seed).sample(rows, size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm_a", help="checkpoint or result JSON for arm A")
    parser.add_argument("arm_b", help="checkpoint or result JSON for arm B")
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", default="disagreements.json")
    args = parser.parse_args()

    def load(path):
        data = json.load(open(path, encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("named") or data.get("entries") or []
        return data

    entries_a, entries_b = load(args.arm_a), load(args.arm_b)
    rows = find_disagreements(entries_a, entries_b)
    sample = sample_disagreements(rows, args.size, args.seed)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump({"total_entries_compared": len(entries_a),
                   "disagreement_count": len(rows), "sample": sample},
                  fh, indent=2, ensure_ascii=False)
    print(f"{len(rows)} disagreements out of {len(entries_a)} entries; "
          f"wrote {len(sample)} sampled to {args.output}")


if __name__ == "__main__":
    main()
