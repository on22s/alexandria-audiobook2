"""Fail unless the sharded unit-test jobs together ran every test exactly once.

Each shard writes a verify_release.py JSON report whose unit_tests gate records
`tests_ran` and the identities observed when each test starts. Counts alone
cannot detect equal-count duplicates and omissions. Both counts and executed
identities are compared with the inventory, which update_test_inventory.py
already ties to discovery.

    python check_shard_reports.py DIR --shards 3

DIR holds the downloaded per-shard artifacts (any depth); every file named
release-report.json is read. In CI, --run-id/--run-attempt/--commit-sha select
one immutable artifact per shard: the highest attempt no later than the current
attempt, within the same run and exact checkout SHA. Successful shards retained
by a failed-job retry may come from earlier attempts. A newer failed or missing
report must never fall back to an older passing report. Artifact identity comes
from GitHub's workflow context, not report timestamps or artifact listing order.
"""

import argparse
from collections import Counter
import json
import re
import sys
from pathlib import Path

from unit_test_sharding import parse_shard_spec

INVENTORY_PATH = Path(__file__).parent / "tests" / "unit_test_inventory.json"


def get_inventory_total(path=INVENTORY_PATH):
    inventory = json.loads(Path(path).read_text(encoding="utf-8"))
    return sum(len(tests) for tests in inventory.values())


def get_unit_gate(report):
    return next((gate for gate in report.get("gates", []) if gate.get("name") == "unit_tests"), None)


def get_shard_report_errors(reports, shard_count, expected_tests, expected_test_ids=None):
    """Check shard counts and identities; compare exact inventory when supplied."""
    errors = []
    if len(reports) != shard_count:
        errors.append(f"expected {shard_count} shard reports, found {len(reports)}")
    seen, total, executed = [], 0, []
    for position, report in enumerate(reports, 1):
        spec = report.get("shard")
        try:
            index, count = parse_shard_spec(spec)
        except ValueError:
            errors.append(f"report {position} has no valid shard marker: {spec!r}")
            continue
        label = f"shard {index}/{count}"
        if count != shard_count:
            errors.append(f"{label} was run as one of {count}, expected {shard_count}")
        seen.append(index)
        if report.get("status") != "passed":
            errors.append(f"{label} did not pass (status {report.get('status')!r})")
        gate = get_unit_gate(report)
        ran = (gate or {}).get("result", {}).get("tests_ran")
        if gate is None or gate.get("status") != "passed":
            errors.append(f"{label} has no passing unit_tests gate")
        elif isinstance(ran, bool) or not isinstance(ran, int) or ran < 1:
            errors.append(f"{label} did not record a positive tests_ran (got {ran!r})")
        else:
            total += ran
        ids = (gate or {}).get("result", {}).get("test_ids")
        if (not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids)
                or len(ids) != ran):
            errors.append(f"{label} has no valid executed test identities matching tests_ran")
        else:
            executed.extend(ids)
    if sorted(seen) != list(range(1, shard_count + 1)):
        errors.append(f"shard numbers {sorted(seen)} are not exactly 1..{shard_count}, each once")
    if total != expected_tests:
        errors.append(f"shards ran {total} tests in total but the inventory lists {expected_tests}; "
                      "a module was dropped or run twice")
    duplicated = sorted(identifier for identifier, count in Counter(executed).items() if count > 1)
    if duplicated:
        errors.append(f"tests executed more than once: {duplicated}")
    if expected_test_ids is not None:
        expected = set(expected_test_ids)
        missing = sorted(expected - set(executed))
        unknown = sorted(set(executed) - expected)
        if missing:
            errors.append(f"inventory tests not executed: {missing}")
        if unknown:
            errors.append(f"executed tests outside inventory: {unknown}")
    return errors


_ARTIFACT_NAME = re.compile(
    r"release-verification-report-run-(\d+)-sha-([0-9a-f]{40})-attempt-([1-9]\d*)-shard-(\d+)"
)


def load_ci_reports(directory, shard_count, run_id, run_attempt, commit_sha):
    """Select attempts before reading reports, so bad latest reports fail closed.

    download-artifact keeps each artifact in its own immediate child directory.
    The suffix is the zero-based matrix job index; report shards are one-based.
    No Actions API access or additional token permissions are required.
    """
    selected = {}
    for folder in sorted(Path(directory).iterdir()):
        match = _ARTIFACT_NAME.fullmatch(folder.name)
        if not folder.is_dir() or match is None:
            raise ValueError(f"unexpected shard artifact: {folder.name}")
        artifact_run, artifact_sha, attempt, index = match.groups()
        attempt, index = int(attempt), int(index)
        if artifact_run != run_id or artifact_sha != commit_sha:
            raise ValueError(f"artifact does not match the expected run/commit: {folder.name}")
        if attempt > run_attempt or not 0 <= index < shard_count:
            raise ValueError(f"artifact has an invalid attempt/shard: {folder.name}")
        if index not in selected or attempt > selected[index][0]:
            selected[index] = (attempt, folder)
    reports = []
    for index, (attempt, folder) in sorted(selected.items()):
        paths = list(folder.rglob("release-report.json"))
        if len(paths) != 1:
            raise ValueError(f"{folder.name}: expected one release-report.json, found {len(paths)}")
        report = json.loads(paths[0].read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("shard") != f"{index + 1}/{shard_count}":
            raise ValueError(f"{folder.name}: report shard does not match artifact identity")
        print(f"Selected shard {index + 1}/{shard_count} from run attempt {attempt}")
        reports.append(report)
    return reports


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory")
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--inventory", default=str(INVENTORY_PATH))
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--commit-sha")
    args = parser.parse_args(argv)
    context = (args.run_id, args.run_attempt, args.commit_sha)
    if any(value is not None for value in context):
        if (any(value is None for value in context) or not args.run_id.isdigit()
                or args.run_attempt < 1 or not re.fullmatch(r"[0-9a-f]{40}", args.commit_sha)):
            parser.error("CI selection requires a run ID, positive attempt, and full commit SHA")
    try:
        if args.run_id is not None:
            reports = load_ci_reports(args.directory, args.shards, *context)
        else:
            paths = sorted(Path(args.directory).rglob("release-report.json"))
            reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    except (OSError, ValueError) as error:
        print(f"SHARD CHECK FAILED: {error}", file=sys.stderr)
        return 1
    expected = get_inventory_total(args.inventory)
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    identities = [identifier for tests in inventory.values() for identifier in tests]
    errors = get_shard_report_errors(reports, args.shards, expected, identities)
    for error in errors:
        print(f"SHARD CHECK FAILED: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"All {args.shards} shards passed and together ran {expected} tests, matching the inventory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
