"""Compare two three-pass attribution arms and sample their disagreements.

Structural metrics cannot say which arm is correct, so the manual scoring pass
should look only at entries where the arms actually disagree.
"""

import argparse
import difflib
import json
import random
from collections import Counter

from utils import atomic_json_write
from generation_checkpoint_deltas import load_generation_checkpoint_document

from attribution_accuracy import normalize_speaker, is_attribution_comparison_eligible



def load_attribution_entries(path):
    """Load checkpoint or result entries with one validated shape policy."""
    data = load_generation_checkpoint_document(path)
    if isinstance(data, dict):
        entries = data.get("named")
        if entries is not None and not isinstance(entries, list):
            raise ValueError(f"Checkpoint named entries must be a list: {path}")
        if not entries:
            entries = data.get("entries")
        if entries is None:
            entries = []
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"Checkpoint must be an object or entry list: {path}")
    if not isinstance(entries, list) or any(
            entry is not None and not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"Checkpoint entries must be objects or null: {path}")
    return entries

def normalize(text):
    """Collapse whitespace and case so alignment is not defeated by formatting."""
    return " ".join(str(text or "").split()).casefold()


def align_arms(arm_a, arm_b):
    """Pair entries between two arms by text, returning (pairs, coverage).

    Index-based pairing cannot work: segmentation is not deterministic, and two
    identical runs of the same book produced 1,995 and 2,036 entries. Matching
    on text lets the comparison skip over the entries one arm split differently
    instead of silently comparing misaligned lines - or refusing to run at all.

    coverage is the share of the LARGER arm's entries that found a partner. It
    used to divide by arm_a alone, so ten entries all matching against a
    thousand reported 100% coverage while 99% of arm_b went uncompared - and
    the number flipped depending on argument order.
    """
    # Keep the original positions: the index is what a human uses to find the
    # entry in the checkpoint when scoring the sample.
    left_indexed = [(i, e) for i, e in enumerate(arm_a) if e]
    right = [e for e in arm_b if e]
    left = [e for _, e in left_indexed]
    left_keys = [normalize(e.get("text")) for e in left]
    right_keys = [normalize(e.get("text")) for e in right]
    left_counts, right_counts = Counter(left_keys), Counter(right_keys)
    left_positions = [i for i, key in enumerate(left_keys)
                      if key and left_counts[key] == right_counts[key] == 1]
    right_positions = [i for i, key in enumerate(right_keys)
                       if key and left_counts[key] == right_counts[key] == 1]
    matcher = difflib.SequenceMatcher(
        a=[left_keys[i] for i in left_positions],
        b=[right_keys[i] for i in right_positions], autojunk=False)
    pairs = []
    for a_start, b_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            position = left_positions[a_start + offset]
            pairs.append((left_indexed[position][0], left[position],
                          right[right_positions[b_start + offset]]))
    coverage = len(pairs) / max(len(left), len(right), 1)
    return pairs, coverage


def find_disagreements(arm_a, arm_b, pairs=None):
    """Return entries where two arms assigned different speakers.

    Accepts already-computed pairs so a caller that has aligned the arms does
    not pay for the SequenceMatcher pass twice.
    """
    if pairs is None:
        pairs, _coverage = align_arms(arm_a, arm_b)
    rows = []
    for index, left, right in pairs:
        # Normalized, so case and stray whitespace are not a disagreement;
        # the raw values are still reported for display.
        if normalize_speaker(left.get("speaker")) != normalize_speaker(
                right.get("speaker")):
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
    if args.size < 0:
        parser.error("--size must be nonnegative")

    entries_a, entries_b = load_attribution_entries(args.arm_a), load_attribution_entries(args.arm_b)
    pairs, coverage = align_arms(entries_a, entries_b)
    rows = find_disagreements(entries_a, entries_b, pairs=pairs)
    sample = sample_disagreements(rows, args.size, args.seed)
    eligible = is_attribution_comparison_eligible(coverage)
    atomic_json_write(
        {"entries_arm_a": len(entries_a), "entries_arm_b": len(entries_b),
         "aligned": len(pairs), "alignment_coverage": round(coverage, 4),
         "comparison_eligible": eligible,
         "disagreement_count": len(rows), "sample": sample}, args.output)
    print(f"aligned {len(pairs)} of {len(entries_a)}/{len(entries_b)} entries "
          f"({coverage:.1%} coverage)")
    print(f"{len(rows)} disagreements among aligned entries "
          f"({len(rows)/max(len(pairs),1):.1%}); wrote {len(sample)} sampled "
          f"to {args.output}")
    if not eligible:
        print("WARNING: low alignment coverage - the arms segmented very "
              "differently, so this comparison covers only part of the book.")


if __name__ == "__main__":
    main()
