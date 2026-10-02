"""Build one shared hand-scoring sheet across every model in a matrix run.

Pairwise arm comparison cannot answer the quality question: two identical runs
disagree on 37.4% of speaker assignments, which is larger than the difference
between any two configurations. So instead of comparing models to each other,
this samples a fixed set of lines once and shows what every model said about
those same lines, leaving one blank column for the correct answer.

Sampling is by text rather than entry index because models segment differently
- two runs of one book produced 1,995 and 2,038 entries.
"""

import argparse
import glob
import json
import os
import random

from utils import atomic_json_write, is_path_inside
from attribution_accuracy import normalize_speaker

from compare_attribution_arms import normalize, load_attribution_entries


def load_named(checkpoint_path):
    return [entry for entry in load_attribution_entries(checkpoint_path) if entry]


def find_model_runs(results_dir, book_tag):
    """Return {model_name: entries} for every model that finished this book."""
    if (not isinstance(book_tag, str) or not book_tag or book_tag in (".", "..")
            or any(char in book_tag for char in ("/", "\\", "\x00"))):
        raise ValueError("book_tag must be a single directory name")
    runs = {}
    pattern = os.path.join(glob.escape(os.fspath(results_dir)), "*", glob.escape(book_tag),
                           "result.json.threepass_checkpoint.json")
    for path in sorted(glob.glob(pattern)):
        if not is_path_inside(path, results_dir):
            raise ValueError(f"checkpoint is outside the results directory: {path}")
        model = path.split(os.sep)[-3]
        entries = load_named(path)
        if entries:
            runs[model] = entries
    return runs


def neighbour_context(entries, position, window=3):
    """Return text-only neighbours from the explicitly selected sequence.

    Model sequences remain model-specific unless their contexts agree. An
    authoritative source sequence must have one unique target occurrence.
    Speakers are omitted so model guesses cannot bias the scoring decision.
    """
    before = []
    for entry in entries[max(0, position - window):position]:
        text = " ".join(str(entry.get("text") or "").split())
        if text:
            before.append(text)
    after = []
    for entry in entries[position + 1:position + 1 + window]:
        text = " ".join(str(entry.get("text") or "").split())
        if text:
            after.append(text)
    return before, after


def build_sheet(runs, size=50, seed=7, window=3, source_entries=None):
    """Sample spoken lines and collect every model's answer for each.

    Lines are keyed on normalized text, so a line only enters the sheet if it
    survived segmentation identically across every model - otherwise the models
    would be scored on different text and the comparison would be meaningless.
    """
    if not runs:
        return []
    models = sorted(runs)
    indexed, positions = {}, {}
    for model in models:
        by_text, by_position, seen_twice = {}, {}, set()
        for position, entry in enumerate(runs[model]):
            key = normalize(entry.get("text"))
            if not key:
                continue
            if key in by_text:
                # Keeping the first occurrence silently compares unrelated
                # instances of a repeated line - "Sorry." and "Cough..." recur
                # constantly - and shows the wrong surrounding context for the
                # one being judged. An ambiguous key is dropped instead: a
                # smaller sheet of sound rows beats a larger one of shaky rows.
                seen_twice.add(key)
                continue
            by_text[key] = entry
            by_position[key] = position
        for key in seen_twice:
            by_text.pop(key, None)
            by_position.pop(key, None)
        indexed[model] = by_text
        positions[model] = by_position

    shared = set(indexed[models[0]])
    for model in models[1:]:
        shared &= set(indexed[model])

    # Only lines somebody attributed to a character: NARRATOR-only lines carry
    # no judgement to score.
    candidates = [
        key for key in shared
        if any((indexed[model][key].get("speaker") or "").upper()
               not in ("NARRATOR", "", "UNKNOWN") for model in models)
    ]
    candidates.sort()
    chosen = (candidates if len(candidates) <= size
              else random.Random(seed).sample(candidates, size))

    source_positions = {}
    if source_entries is not None:
        if not isinstance(source_entries, list) or any(not isinstance(entry, dict) for entry in source_entries):
            raise ValueError("Source entries must be a list of objects")
        for position, entry in enumerate(source_entries):
            source_positions.setdefault(normalize(entry.get("text")), []).append(position)

    spine = models[0]
    rows = []
    for key in chosen:
        answers = {model: indexed[model][key].get("speaker") for model in models}
        contexts = {}
        for model in models:
            before, after = neighbour_context(runs[model], positions[model][key], window)
            contexts[model] = {"before": before, "after": after}
        conflict = any(context != contexts[models[0]] for context in contexts.values())
        if source_entries is not None:
            matches = source_positions.get(key, [])
            if len(matches) != 1:
                raise ValueError(f"Target does not have one unique source occurrence: {key[:80]}")
            before, after = neighbour_context(source_entries, matches[0], window)
            origin = "source_entries"
        elif conflict:
            before, after = [], []
            origin = "model_specific"
        else:
            before, after = contexts[models[0]]["before"], contexts[models[0]]["after"]
            origin = "model_consensus"
        rows.append({
            "text": indexed[spine][key].get("text", "")[:400],
            "context_before": before,
            "context_after": after,
            "context_origin": origin,
            "context_conflict": conflict,
            "context_by_model": contexts,
            "answers": answers,
            # Normalized, so "RUDI" and "rudi " are not a disagreement. The
            # raw values stay in "answers" for display.
            "models_agree": len({normalize_speaker(v)
                                 for v in answers.values()}) == 1,
            "correct_speaker": "",          # <- you fill this in
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir")
    parser.add_argument("book_tag")
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", default="scoring_sheet.json")
    parser.add_argument("--window", type=int, default=3,
                        help="Neighbouring lines of context each side (default 3). "
                             "A line alone is often unanswerable; the narration "
                             "around it names the speaker.")
    parser.add_argument("--source-entries", help="Authoritative ordered source entries JSON for context")
    args = parser.parse_args()
    if args.size < 0:
        parser.error("--size must be nonnegative")
    if args.window < 0:
        parser.error("--window must be nonnegative")

    runs = find_model_runs(args.results_dir, args.book_tag)
    if not runs:
        print(f"no completed runs for {args.book_tag} under {args.results_dir}")
        return
    source_entries = load_named(args.source_entries) if args.source_entries else None
    rows = build_sheet(runs, args.size, args.seed, args.window, source_entries=source_entries)
    disputed = [r for r in rows if not r["models_agree"]]
    atomic_json_write({"book": args.book_tag, "models": sorted(runs),
                       "entries_per_model": {m: len(e) for m, e in runs.items()},
                       "sampled": len(rows), "disputed": len(disputed),
                       "rows": rows}, args.output)
    print(f"models: {', '.join(sorted(runs))}")
    print(f"entries per model: {[len(e) for e in runs.values()]}")
    print(f"sampled {len(rows)} shared lines; {len(disputed)} disputed "
          f"({len(disputed)/max(len(rows),1):.0%})")
    print(f"\nFill in correct_speaker for each row in {args.output}.")
    print("Scoring the same lines for every model avoids pairwise comparison, "
          "which the 37.4% identical-run disagreement makes unusable.")


if __name__ == "__main__":
    main()
