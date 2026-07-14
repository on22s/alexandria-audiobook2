"""Regenerate or verify the checked-in unittest inventory."""

import argparse
import json
from pathlib import Path

from test_inventory import INVENTORY_PATH, get_unit_test_inventory


def format_inventory(inventory):
    """Return the deterministic on-disk representation of an inventory."""
    return json.dumps(inventory, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def get_inventory_differences(expected, actual):
    """Return compact, actionable differences between two inventories."""
    differences = []
    for module_name in sorted(set(expected) | set(actual)):
        expected_tests = set(expected.get(module_name, []))
        actual_tests = set(actual.get(module_name, []))
        for test_id in sorted(actual_tests - expected_tests):
            differences.append(f"Missing from inventory: {test_id}")
        for test_id in sorted(expected_tests - actual_tests):
            differences.append(f"No longer discovered: {test_id}")
    return differences


def check_inventory(path=INVENTORY_PATH):
    """Compare the checked-in inventory with current test discovery."""
    expected = json.loads(Path(path).read_text(encoding="utf-8"))
    return get_inventory_differences(expected, get_unit_test_inventory())


def write_inventory(path=INVENTORY_PATH):
    """Write the current inventory deterministically and return its path."""
    path = Path(path)
    path.write_text(format_inventory(get_unit_test_inventory()), encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report drift without modifying the inventory")
    args = parser.parse_args(argv)
    if args.check:
        differences = check_inventory()
        if differences:
            print("Unit test inventory drift detected:")
            for difference in differences:
                print(f"- {difference}")
            return 1
        print("Unit test inventory matches discovery.")
        return 0
    path = write_inventory()
    print(f"Updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
