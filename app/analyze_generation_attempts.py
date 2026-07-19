"""Report-only calibration analyzer over `scripts/*.generation_quality.json`
manifests (see generate_script.py's `build_generation_quality_manifest` and
`record_attempt_context`/`call_llm_for_entries` for how the telemetry this
reads is produced).

Purpose: put real numbers under the quality-gate thresholds
(chunk_quality.py's 0.90 pass line, generate_script.py's 0.75
NEAR_MISS_RECALL_THRESHOLD) instead of guessing whether they sit at a real
valley. Prints tables only - no threshold changes here (Rule 9), no
auto-recommendations.

Manifest shape recap (verified against real manifests in scripts/):
- Every manifest has a "chunks" list of *accepted* chunks: each item has
  chunk_number, adaptively_split, and an "attempts" list of attempt records
  in call order (phase "full" first, then "split" attempts per split_part
  if the chunk was adaptively split).
- Attempt records have: attempt (1-based, RESETS per phase/part and for
  the one-shot bonus retry - see KNOWN_LIMITATIONS), outcome ("accepted",
  "quality_rejected", "response_rejected", "api_error"), failure_codes
  (rejected attempts) or recovery_codes (an accepted attempt that had
  earlier failure codes), and quality_metrics (only on "quality_rejected",
  includes source_token_recall).
- A manifest with status == "failed" additionally has a top-level
  "failed_chunk_attempts" list for the one chunk that exhausted the whole
  book's generation - that chunk is never in "chunks" (accepted_chunks
  only ever holds chunks that passed), so it's the only source of
  data on chunks that failed outright.
"""

import argparse
import json
import os
import sys
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import get_runtime_data_dir  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_scripts_dir() -> str:
    """Mirror core.py's SCRIPTS_DIR = get_runtime_data_dir(ROOT_DIR)/scripts."""
    return os.path.join(get_runtime_data_dir(ROOT_DIR), "scripts")


RECALL_BANDS = [
    ("<0.30", None, 0.30),
    ("0.30-0.60", 0.30, 0.60),
    ("0.60-0.75", 0.60, 0.75),
    ("0.75-0.90", 0.75, 0.90),
    (">=0.90", 0.90, None),
]

KNOWN_LIMITATIONS = [
    "Bonus attempts (the one-shot retry granted after a near-miss final "
    "attempt, see generate_script.py's process_chunk) are not "
    "distinguishable from regular attempts in the recorded telemetry: the "
    "'attempt' field resets to 1 for the bonus call same as any fresh "
    "phase/split-part attempt sequence. Counts below cannot separate them.",
]


def recall_band(recall: float) -> str:
    """Return which RECALL_BANDS bucket `recall` falls in. Band edges are
    inclusive on the lower bound, exclusive on the upper (0.75 falls in
    "0.75-0.90", 0.90 falls in ">=0.90")."""
    for label, lo, hi in RECALL_BANDS:
        if lo is not None and recall < lo:
            continue
        if hi is not None and recall >= hi:
            continue
        return label
    return RECALL_BANDS[-1][0]


# --- Manifest loading (I/O boundary; kept thin so aggregation below stays pure) --

def load_manifests(scripts_dir: str) -> Tuple[List[dict], List[str]]:
    """Load every *.generation_quality.json under `scripts_dir`.

    Returns (manifests, warnings) - a manifest that isn't a JSON object, or
    is missing the "chunks" key, is skipped with a warning rather than
    raising, so one corrupt/malformed file doesn't abort the whole report.
    """
    manifests = []
    warnings = []
    if not os.path.isdir(scripts_dir):
        return manifests, [f"scripts dir not found: {scripts_dir}"]
    for name in sorted(os.listdir(scripts_dir)):
        if not name.endswith(".generation_quality.json"):
            continue
        path = os.path.join(scripts_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            warnings.append(f"skipped {name}: unreadable/invalid JSON ({e})")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
            warnings.append(f"skipped {name}: not a manifest object (missing 'chunks')")
            continue
        manifests.append(data)
    return manifests, warnings


def extract_chunk_records(manifest: dict) -> List[dict]:
    """Flatten one manifest into per-chunk records:
    {chunk_number, accepted, adaptively_split, attempts}.

    Chunks in manifest["chunks"] are always accepted (accepted_chunks only
    ever holds chunks that passed). A "failed" manifest's single
    failed_chunk_attempts list becomes one additional accepted=False record;
    its adaptively_split is derived from whether any recorded attempt has
    phase == "split", since the failed path never stores that flag itself.
    """
    records = []
    for item in manifest.get("chunks", []):
        records.append({
            "chunk_number": item.get("chunk_number"),
            "accepted": True,
            "adaptively_split": bool(item.get("adaptively_split", False)),
            "attempts": item.get("attempts", []) or [],
        })
    failed_attempts = manifest.get("failed_chunk_attempts")
    if manifest.get("status") == "failed" and isinstance(failed_attempts, list):
        records.append({
            "chunk_number": manifest.get("failed_chunk"),
            "accepted": False,
            "adaptively_split": any(a.get("phase") == "split" for a in failed_attempts),
            "attempts": failed_attempts,
        })
    return records


def all_chunk_records(manifests: Iterable[dict]) -> List[dict]:
    records = []
    for manifest in manifests:
        records.extend(extract_chunk_records(manifest))
    return records


# --- Pure aggregations (unit-tested with synthetic manifests) --------------

def recall_histogram(records: List[dict]) -> Dict[str, int]:
    """Aggregation 1: histogram of source_token_recall across
    quality-rejected attempts, bucketed by RECALL_BANDS."""
    counts = {label: 0 for label, _, _ in RECALL_BANDS}
    for record in records:
        for attempt in record["attempts"]:
            if attempt.get("outcome") != "quality_rejected":
                continue
            recall = (attempt.get("quality_metrics") or {}).get("source_token_recall")
            if recall is None:
                continue
            counts[recall_band(recall)] += 1
    return counts


def recovery_by_band(records: List[dict]) -> Dict[str, Dict[str, int]]:
    """Aggregation 2: for each quality-rejected attempt with a recall value,
    did any LATER attempt in the same chunk record end up "accepted"?
    Returns {band: {"rejected": n, "recovered": n}}."""
    result = {label: {"rejected": 0, "recovered": 0} for label, _, _ in RECALL_BANDS}
    for record in records:
        attempts = record["attempts"]
        for i, attempt in enumerate(attempts):
            if attempt.get("outcome") != "quality_rejected":
                continue
            recall = (attempt.get("quality_metrics") or {}).get("source_token_recall")
            if recall is None:
                continue
            band = recall_band(recall)
            result[band]["rejected"] += 1
            if any(a.get("outcome") == "accepted" for a in attempts[i + 1:]):
                result[band]["recovered"] += 1
    return result


def attempt_count_distribution(records: List[dict]) -> Dict[str, Dict[int, int]]:
    """Aggregation 3: {"accepted": {n_attempts: chunk_count}, "failed": {...}}
    - how many attempts each chunk record took, split by whether the chunk
    was eventually accepted or ultimately failed the whole book."""
    dist = {"accepted": {}, "failed": {}}
    for record in records:
        key = "accepted" if record["accepted"] else "failed"
        n = len(record["attempts"])
        dist[key][n] = dist[key].get(n, 0) + 1
    return dist


def split_outcomes(records: List[dict]) -> Dict[str, int]:
    """Aggregation 4: among chunk records with adaptively_split True, how
    many ended up accepted vs failed."""
    counts = {"accepted": 0, "failed": 0}
    for record in records:
        if not record["adaptively_split"]:
            continue
        counts["accepted" if record["accepted"] else "failed"] += 1
    return counts


# --- Reporting --------------------------------------------------------------

def format_report(records: List[dict], warnings: List[str]) -> str:
    lines = []
    lines.append(f"Loaded {len(records)} chunk record(s) "
                  f"({sum(1 for r in records if r['accepted'])} accepted, "
                  f"{sum(1 for r in records if not r['accepted'])} failed).")
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  - {w}")

    lines.append("")
    lines.append("=== 1. Recall histogram (quality-rejected attempts) ===")
    hist = recall_histogram(records)
    for label, _, _ in RECALL_BANDS:
        lines.append(f"  {label:>10}: {hist[label]}")

    lines.append("")
    lines.append("=== 2. Recovery rate by recall band ===")
    recovery = recovery_by_band(records)
    for label, _, _ in RECALL_BANDS:
        stats = recovery[label]
        rejected = stats["rejected"]
        rate = f"{100 * stats['recovered'] / rejected:.0f}%" if rejected else "n/a"
        lines.append(f"  {label:>10}: rejected={rejected:<5} recovered={stats['recovered']:<5} rate={rate}")

    lines.append("")
    lines.append("=== 3. Attempt-count distribution ===")
    dist = attempt_count_distribution(records)
    for key in ("accepted", "failed"):
        lines.append(f"  {key}:")
        for n in sorted(dist[key]):
            lines.append(f"    {n} attempt(s): {dist[key][n]} chunk(s)")

    lines.append("")
    lines.append("=== 4. Split outcomes (adaptively_split chunks) ===")
    split = split_outcomes(records)
    lines.append(f"  accepted: {split['accepted']}")
    lines.append(f"  failed:   {split['failed']}")

    lines.append("")
    lines.append("=== Reading ===")
    lines.append("Numbers only, no auto-recommendations. Known limitation(s):")
    for note in KNOWN_LIMITATIONS:
        lines.append(f"  - {note}")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts-dir", default=None,
                         help="Directory of *.generation_quality.json manifests "
                              "(default: mirrors app's SCRIPTS_DIR resolution).")
    parser.add_argument("--json", action="store_true",
                         help="Print raw aggregation data as JSON instead of tables.")
    args = parser.parse_args(argv)

    scripts_dir = args.scripts_dir or default_scripts_dir()
    manifests, warnings = load_manifests(scripts_dir)
    records = all_chunk_records(manifests)

    if args.json:
        print(json.dumps({
            "scripts_dir": scripts_dir,
            "warnings": warnings,
            "manifest_count": len(manifests),
            "recall_histogram": recall_histogram(records),
            "recovery_by_band": recovery_by_band(records),
            "attempt_count_distribution": attempt_count_distribution(records),
            "split_outcomes": split_outcomes(records),
            "known_limitations": KNOWN_LIMITATIONS,
        }, indent=2))
    else:
        print(f"scripts dir: {scripts_dir}")
        print(format_report(records, warnings))

    return 0


if __name__ == "__main__":
    sys.exit(main())
