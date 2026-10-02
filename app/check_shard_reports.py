"""Fail unless the sharded unit-test jobs together ran every test exactly once.

Each shard writes a verify_release.py JSON report whose unit_tests gate records
`tests_ran`. A shard that passes proves only that ITS tests passed; a dropped or
double-assigned module would still be green. So the totals are compared with the
checked-in inventory, which `update_test_inventory.py --check` already ties to
discovery.

    python check_shard_reports.py DIR --shards 3

DIR holds the downloaded per-shard artifacts (any depth); every file named
release-report.json is read.
"""

import argparse
import json
import sys
from pathlib import Path

from unit_test_sharding import parse_shard_spec

INVENTORY_PATH = Path(__file__).parent / "tests" / "unit_test_inventory.json"


def get_inventory_total(path=INVENTORY_PATH):
    inventory = json.loads(Path(path).read_text(encoding="utf-8"))
    return sum(len(tests) for tests in inventory.values())


def get_unit_gate(report):
    return next((gate for gate in report.get("gates", []) if gate.get("name") == "unit_tests"), None)


def get_shard_report_errors(reports, shard_count, expected_tests):
    """Return a list of problems; empty means the shards cover the suite exactly once."""
    errors = []
    if len(reports) != shard_count:
        errors.append(f"expected {shard_count} shard reports, found {len(reports)}")
    seen, total = [], 0
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
    if sorted(seen) != list(range(1, shard_count + 1)):
        errors.append(f"shard numbers {sorted(seen)} are not exactly 1..{shard_count}, each once")
    if total != expected_tests:
        errors.append(f"shards ran {total} tests in total but the inventory lists {expected_tests}; "
                      "a module was dropped or run twice")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory")
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--inventory", default=str(INVENTORY_PATH))
    args = parser.parse_args(argv)
    paths = sorted(Path(args.directory).rglob("release-report.json"))
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    expected = get_inventory_total(args.inventory)
    errors = get_shard_report_errors(reports, args.shards, expected)
    for error in errors:
        print(f"SHARD CHECK FAILED: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"All {args.shards} shards passed and together ran {expected} tests, matching the inventory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
