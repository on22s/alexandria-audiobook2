"""Build a non-destructive ledger for scores changed by a gold alias correction.

Historical artifacts remain immutable. This reads every tracked JSON document
with ExperimentRecord-shaped rows, recomputes correctness using current gold,
and records only artifacts whose stored score changes. Untracked downloads are
excluded because a shared audit must reproduce in a clean checkout.
"""
import argparse
import collections
import json
import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.provenance import provenance  # noqa: E402
from utils import atomic_json_write  # noqa: E402


def tracked_json_paths(repo):
    result = subprocess.run(["git", "ls-files", "-z", "ab_test_runtime/*.json",
                             "ab_test_runtime/**/*.json"], cwd=repo,
                            check=True, capture_output=True)
    return [os.path.join(repo, p.decode()) for p in result.stdout.split(b"\0") if p]


def rescore_document(path, groups, expected_name):
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    rows = document.get("rows") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return None
    by_arm = collections.defaultdict(lambda: {"rows": 0, "old_correct": 0,
                                              "new_correct": 0, "changed": 0})
    changed_ids = []
    for row in rows:
        if not isinstance(row, dict) or row.get("expected") != expected_name:
            continue
        arm = str(row.get("arm") or "<missing>")
        now = bool(row.get("predicted")) and same_speaker(
            row.get("expected"), row.get("predicted"), groups)
        values = by_arm[arm]
        values["rows"] += 1
        values["old_correct"] += bool(row.get("correct"))
        values["new_correct"] += now
        if now != bool(row.get("correct")):
            values["changed"] += 1
            changed_ids.append(row.get("id"))
    if not changed_ids:
        return None
    return {"artifact": os.path.relpath(path, REPO),
            "changed_row_evaluations": len(changed_ids),
            "changed_ids": changed_ids, "arms": dict(sorted(by_arm.items()))}


def build(repo, fixture_path, expected_name="TSUKIHI"):
    with open(fixture_path, encoding="utf-8") as handle:
        groups = alias_groups(json.load(handle))
    artifacts = [result for path in tracked_json_paths(repo)
                 if (result := rescore_document(path, groups, expected_name))]
    artifacts.sort(key=lambda row: row["artifact"])
    return {
        "scope": "tracked JSON artifacts only; historical files are not rewritten",
        "correction": ["TSUKIHI", "TSUKIHI ARARAGI"],
        "artifacts_changed": len(artifacts),
        "row_evaluations_changed": sum(r["changed_row_evaluations"] for r in artifacts),
        "artifacts": artifacts,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=os.path.join(
        APP, "fixtures", "attribution_gold_owarimonogatari3.json"))
    parser.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "audit", "tsukihi_alias_rescore.json"))
    args = parser.parse_args()
    result = build(REPO, args.fixture)
    result["provenance"] = provenance(__file__, args)
    atomic_json_write(result, args.out)
    print(f"{result['artifacts_changed']} artifacts; "
          f"{result['row_evaluations_changed']} row evaluations changed")


if __name__ == "__main__":
    main()
