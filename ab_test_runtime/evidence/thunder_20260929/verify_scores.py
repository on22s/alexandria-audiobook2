"""Verify the captured Thunder PDNC results offline; no inference requests.

Run with the project's Python interpreter. Raw artifacts remain unchanged.
"""
import hashlib
import gzip
import json
import math
import sys
from collections import Counter
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[2]
sys.path.insert(0, str(REPO / "app"))
from experiments.scoring import alias_groups, same_speaker
from experiments.manifest import strict_shared_summary, validate_stored_summary
from experiments.stats import paired


def get_file_bytes(path):
    if path.exists():
        return path.read_bytes()
    return gzip.decompress(path.with_name(path.name + ".gz").read_bytes())


def get_file_sha256(path):
    return hashlib.sha256(get_file_bytes(path)).hexdigest()


def main():
    # Validate the instrument on known successes AND known failures first.
    groups = alias_groups({"aliases": [["JAKE", "JAKE BARNES"]]})
    assert same_speaker("JAKE", "JAKE BARNES", groups)
    assert same_speaker("MR. KNIGHTLEY", "MR KNIGHTLEY")
    assert same_speaker("田中", "田中")
    assert not same_speaker("JAKE", "ROBERT COHN", groups)
    assert not same_speaker("JAKE", None, groups)
    assert not same_speaker("田中", "中田")
    assert not same_speaker("ALICE", "MALICE")

    # Independently fetched remote hashes accompany the new seed3 capture.
    for item in json.loads((EVIDENCE / "remote_manifest.json").read_text()):
        name = Path(item["path"]).name
        if name.startswith("lora_serving_eval__"):
            path = REPO / "ab_test_runtime/experiments" / name
        elif name.startswith("attribution_gold_"):
            path = EVIDENCE / "fixtures" / name
        else:
            path = EVIDENCE / name
        assert len(get_file_bytes(path)) == item["bytes"], path
        assert get_file_sha256(path) == item["sha256"], path

    output = []
    paths = sorted((REPO / "ab_test_runtime/experiments").glob(
        "lora_serving_eval__qwen3-8b-window25-michel2v2-*.json"))
    assert len(paths) >= 10
    for path in paths:
        doc = json.loads(path.read_text())
        assert doc["meta"]["validation"] == "ok" and doc["meta"]["finished"], path
        assert len(doc["rows"]) == 5310, path
        assert not validate_stored_summary(doc), path
        gold_by_book = {}
        for book, expected_hash in doc["meta"]["gold_files"].items():
            fixture_path = EVIDENCE / "fixtures" / ("attribution_gold_" + book + ".json")
            assert get_file_sha256(fixture_path) == expected_hash, (path.name, book)
            fixture = json.loads(fixture_path.read_text())
            gold_by_book[book] = (
                {entry["id"]: entry for entry in fixture["entries"]},
                alias_groups(fixture),
            )
        assert len(gold_by_book) == 9
        rows, seen = [], set()
        for row in doc["rows"]:
            key = (row["arm"], row["id"])
            assert key not in seen, (path.name, key)
            seen.add(key)
            book, ident = row["id"].split(":", 1)
            entries, groups = gold_by_book[book]
            gold = entries[ident]
            assert row["line"] == gold["line"], (path.name, key)
            assert row["expected"] == gold["expected_speaker"].upper(), (path.name, key)
            correct = same_speaker(gold["expected_speaker"], row["predicted"], groups)
            assert correct == row["correct"], (path.name, key)
            rows.append({**row, "correct": correct})
        arms = {arm: {r["id"]: r["correct"] for r in rows if r["arm"] == arm}
                for arm in ("base", "lora")}
        assert len(arms["base"]) == 2655
        assert set(arms["base"]) == set(arms["lora"])
        strict = strict_shared_summary(rows)
        for key in ("arms", "shared_ids", "dropped_ids_by_arm"):
            assert strict[key] == doc["strict"][key], (path.name, key)
        for key in ("first", "second", "improved", "regressed"):
            assert strict["paired"][key] == doc["strict"]["paired"][key], (path.name, key)
        assert math.isclose(strict["paired"]["p"], doc["strict"]["paired"]["p"],
                            rel_tol=1e-12, abs_tol=0), path
        p, regressed, improved, n = paired(arms["base"], arms["lora"])
        summary = {}
        for arm, answers in arms.items():
            correct = sum(answers.values())
            assert correct == doc["summary"][arm]["correct"], (path.name, arm)
            summary[arm] = {"correct": correct, "n": len(answers),
                            "accuracy_pct": 100 * correct / len(answers),
                            "unanswered": sum(not r["predicted"] for r in rows if r["arm"] == arm)}
        failures = Counter(r["candidate_provenance"] for r in rows
                           if "|batch_failed=" in r["candidate_provenance"])
        output.append({"artifact": path.name, "sha256": get_file_sha256(path),
                       "host": doc["meta"]["host"], "model": doc["meta"]["model"],
                       "summary": summary, "strict": strict["arms"],
                       "paired": {"n": n, "improved": improved, "regressed": regressed, "p": p},
                       "failure_rows": dict(failures),
                       "producer_dirty": doc["meta"]["git"]["dirty"]})
    print(json.dumps({"verified": len(output), "rows": sum(
        r["paired"]["n"] * 2 for r in output), "results": output}, indent=2))


if __name__ == "__main__":
    main()
