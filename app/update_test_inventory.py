"""Regenerate or verify the checked-in unittest inventory."""

import argparse
import json
from pathlib import Path

from utils import atomic_json_write

from tests.test_inventory import (EXCLUDED_TEST_MODULES, INVENTORY_PATH,
                            InventoryUnavailable, _tracked_test_modules,
                            get_test_directory, get_unit_test_inventory)

CANNOT_VERIFY = 2


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


def write_inventory(path=INVENTORY_PATH, *, module_names=None):
    """Write the current inventory deterministically and return its path."""
    path = Path(path)
    if module_names is None:
        inventory = get_unit_test_inventory()
    else:
        # Maintenance only: CI still performs complete import/discovery. Never
        # fabricate the IDs of unavailable modules or publish a partial file.
        inventory = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(inventory, dict) or any(not isinstance(key, str)
                or not isinstance(ids, list) or any(not isinstance(item, str) for item in ids)
                for key, ids in inventory.items())):
            raise ValueError('Existing inventory is not a module-to-test-ID mapping')
        inventory.update(get_unit_test_inventory(module_names=module_names))
    atomic_json_write(dict(sorted(inventory.items())), str(path), newline="\n")
    return path


def report_unavailable(message, error):
    """Report every module that could not be imported, never a comparison."""
    print(message)
    for module_name, reason in error.failures:
        print(f"- {module_name}: {reason}")
    print("Nothing was compared against the committed inventory.")


def find_untracked_test_modules(test_directory=None):
    """Test files on disk that git does not track, so are not inventoried.

    The inventory deliberately covers only tracked files, because this tree is
    shared with other branches. The cost is an ordering trap: write a new test,
    regenerate, commit, and CI discovers tests the inventory never saw. Warning
    here turns a red build into a line of output.
    """
    directory = get_test_directory(test_directory)
    tracked = _tracked_test_modules(directory)
    if tracked is None:
        return []
    return sorted(
        path.name for path in directory.glob("test*.py")
        if path.stem not in EXCLUDED_TEST_MODULES and path.stem not in tracked)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report drift without modifying the inventory")
    parser.add_argument('--module', action='append', dest='module_names', metavar='TEST_MODULE',
                        help='update only this tracked module (repeatable); retains other IDs, not full verification')
    args = parser.parse_args(argv)
    if args.check and args.module_names:
        parser.error('--check always verifies all modules; --module is an explicit maintenance update only')
    untracked = find_untracked_test_modules()
    if untracked:
        print("\nWARNING: these test files are untracked and are NOT in the "
              "inventory.\nIf they belong to this change, 'git add' them and "
              "run this again, or CI\nwill discover tests the inventory does "
              "not list:")
        for name in untracked:
            print(f"  {name}")
    if args.check:
        try:
            differences = check_inventory()
        except InventoryUnavailable as error:
            report_unavailable("Unit test inventory cannot be verified.", error)
            return CANNOT_VERIFY
        if differences:
            print("Unit test inventory drift detected:")
            for difference in differences:
                print(f"- {difference}")
            return 1
        print("Unit test inventory matches discovery.")
        return 0
    try:
        path = write_inventory(module_names=args.module_names)
    except InventoryUnavailable as error:
        report_unavailable("Unit test inventory was not written.", error)
        return CANNOT_VERIFY
    except (OSError, ValueError) as error:
        print(f'Unit test inventory was not written: {error}')
        return CANNOT_VERIFY
    print(f"Updated {path}")
    if args.module_names:
        print('TARGETED UPDATE ONLY: unchanged modules were retained, not rediscovered. '
              'Run --check in the full test environment before claiming full verification.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
