"""Re-score a finished respelling run without regenerating a second of audio.

WHY THIS EXISTS RATHER THAN A RE-RUN. The scoring was wrong; the generation was
not. That distinction was checked before it was relied on, because re-running
costs ~14 GPU-hours and re-scoring costs seconds:

  - 12 of 12 stored WAVs re-transcribed to text byte-identical to what the run
    recorded, so the stored transcripts faithfully represent the audio and
    whisper is deterministic on them.
  - 0 of 12 plain/respelled WAV pairs were identical, so the respelling really
    did change what was spoken.
  - The rows where the recognizer produced a canned hallucination have normal
    speech energy and duration (RMS 0.083 / 3.08 s) versus the rest (0.080 /
    3.26 s), so nothing rendered silent.
  - 0 render or transcribe errors, 0 empty transcripts across 5,880 terms.

The audio is good. Only the question asked of it was wrong, and that question
lives entirely in the JSON.

WHAT CHANGES. `helps` used to mean "more of the word's characters appeared
somewhere", and now means "the word came out". See `scattered_overlap` in
measure_respellings for what that cost.

THE OLD FIELDS ARE PRESERVED under `legacy_*` rather than overwritten. The
comparison between the two scorings is the evidence that the correction
mattered, and an artifact that quietly changed its own numbers could not be
audited afterwards.
"""
import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import atomic_json_write                         # noqa: E402
from experiments.measure_respellings import score_recovery  # noqa: E402


def rescore_row(row):
    """-> the row with recovery scores, keeping the old ones as `legacy_*`."""
    if "plain_heard" not in row or "respelled_heard" not in row:
        return dict(row)                     # skipped or errored; leave as-is
    fresh = dict(row)
    for label in ("plain", "respelled"):
        old = fresh.pop(f"{label}_kana_overlap", None)
        if old is not None:
            fresh[f"legacy_{label}_scattered"] = old
        for field, value in score_recovery(row["kana"], row[f"{label}_heard"]).items():
            fresh[f"{label}_{field}"] = value
    fresh["legacy_helps"] = row.get("helps")
    fresh["helps"] = bool(fresh["respelled_recovers_word"]
                          and not fresh["plain_recovers_word"])
    fresh["hurts"] = bool(fresh["plain_recovers_word"]
                          and not fresh["respelled_recovers_word"])
    fresh["closeness_delta"] = round(
        fresh["respelled_closeness"] - fresh["plain_closeness"], 3)
    fresh.pop("delta", None)
    return fresh


def summarize(rows):
    """-> counts that let the two scorings be compared directly."""
    scored = [r for r in rows if "helps" in r and "plain_recovers_word" in r]
    if not scored:
        return {"scored": 0}
    helped = [r for r in scored if r["helps"]]
    hurt = [r for r in scored if r["hurts"]]
    legacy_helped = [r for r in scored if r.get("legacy_helps")]
    # How many of the old "helps" were the word never actually appearing?
    phantom = [r for r in legacy_helped if not r["respelled_recovers_word"]]
    return {
        "scored": len(scored),
        "plain_recovered": sum(1 for r in scored if r["plain_recovers_word"]),
        "respelled_recovered": sum(1 for r in scored if r["respelled_recovers_word"]),
        "helps": len(helped),
        "hurts": len(hurt),
        "help_rate": round(len(helped) / len(scored), 4),
        "legacy_helps": len(legacy_helped),
        "legacy_help_rate": round(len(legacy_helped) / len(scored), 4),
        "legacy_helps_where_word_never_appeared": len(phantom),
        "mean_closeness_plain": round(
            sum(r["plain_closeness"] for r in scored) / len(scored), 4),
        "mean_closeness_respelled": round(
            sum(r["respelled_closeness"] for r in scored) / len(scored), 4),
    }


def near_misses(rows, floor=0.5):
    """Terms a respelling brought CLOSE without landing it.

    These are the interesting ones for a human: the phonemes moved the right
    way and something specific is still wrong, which is a different problem
    from a word the model has no idea about.
    """
    out = []
    for row in rows:
        if row.get("respelled_recovers_word") or "respelled_closeness" not in row:
            continue
        if row["respelled_closeness"] >= floor:
            out.append({"term": row["term"], "kana": row["kana"],
                        "respelling": row.get("respelling"),
                        "closeness": row["respelled_closeness"],
                        "heard": row["respelled_heard"][:80]})
    return sorted(out, key=lambda r: -r["closeness"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True, help="a respelling result file")
    ap.add_argument("--out", required=True, help="where to write the rescored copy")
    ap.add_argument("--near-miss-floor", type=float, default=0.5)
    args = ap.parse_args()

    with open(args.artifact, encoding="utf-8") as handle:
        document = json.load(handle)
    rows = [rescore_row(r) for r in document["results"]]
    stats = summarize(rows)

    document["results"] = rows
    document["rescored_from"] = os.path.relpath(args.artifact, REPO)
    document["note"] = (
        "RESCORED. `helps` now means the whole word was recovered, on readings "
        "rather than characters. The previous per-character score is kept as "
        "`legacy_*` so the two can be compared; see scattered_overlap in "
        "measure_respellings.py for why it was retired.")
    document["summary"] = stats
    document["near_misses"] = near_misses(rows, args.near_miss_floor)[:60]
    # Provenance, so a number can name the code that produced it. 58 of 95
    # artifact-writing scripts here omitted this; the gate artifacts goal 2.7
    # rests on are the cost - 87 files that cannot say what made them.
    try:
        from experiments.provenance import provenance
        document["provenance"] = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        document["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    atomic_json_write(document, args.out)

    print(f"rescored {stats['scored']} terms from {os.path.basename(args.artifact)}\n")
    print(f"  the word came out, plain spelling      {stats['plain_recovered']:>6}")
    print(f"  the word came out, respelled           {stats['respelled_recovered']:>6}")
    print(f"  respelling RECOVERED a lost word       {stats['helps']:>6}"
          f"   ({stats['help_rate']:.1%})")
    print(f"  respelling LOST a word that worked     {stats['hurts']:>6}")
    print(f"\n  old score said helped                  {stats['legacy_helps']:>6}"
          f"   ({stats['legacy_help_rate']:.1%})")
    print(f"  of those, the word never appeared      "
          f"{stats['legacy_helps_where_word_never_appeared']:>6}")
    print(f"\n  near misses (>= {args.near_miss_floor} of the word, still wrong): "
          f"{len(document['near_misses'])} shown")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
