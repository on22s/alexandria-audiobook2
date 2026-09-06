#!/usr/bin/env python3
"""Do any held-out clips appear in a training set?

WHY THIS IS A MODULE AND NOT A HEREDOC. This check was written inline inside
`half_arm_rescue_20260906.sh`, where nothing could import it and no test could
reach it. It was validated by hand once, on three cases, and then trusted - and
its FIRST version could never have fired at all, because it compared basenames
and every split renumbers from zero, so `train_0000.wav` and `val_0000.wav` are
different names for what may be the same clip. It reported CLEAN on a file
compared against itself.

A guard that lives where no test can call it is a guard nobody can show
failing, which is what goal 6.6 is about. The chain now calls this.

THE IDENTITY OF A CLIP is its source volume and its transcript, not its
filename. Two copies of one clip in two splits carry different names; two
different clips can never carry the same (volume, text) pair in practice.
"""
import argparse
import json
import sys


def clip_keys(path):
    """-> {(source_volume, text)} for every row in a metadata.jsonl."""
    keys = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            text = (row.get("text") or "").strip()
            if text:
                keys.add((row.get("source_volume") or "", text))
    return keys


def leaked(train_path, val_path):
    """-> (count, total_val) of held-out clips present in training.

    Raises ValueError when either side is unreadable or empty: a comparison
    against nothing must not come back CLEAN.
    """
    train, val = clip_keys(train_path), clip_keys(val_path)
    if not train or not val:
        raise ValueError(f"unreadable or empty: {train_path} / {val_path}")
    return len(train & val), len(val)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("train_metadata")
    ap.add_argument("val_metadata")
    args = ap.parse_args()
    try:
        count, total = leaked(args.train_metadata, args.val_metadata)
    except (OSError, ValueError) as exc:
        print(f"UNREADABLE {exc}")
        return 2
    print(f"LEAK {count} of {total}" if count else "CLEAN")
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main())
