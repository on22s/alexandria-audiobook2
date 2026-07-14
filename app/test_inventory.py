import importlib
import json
from pathlib import Path
import unittest


INVENTORY_PATH = Path(__file__).with_name("unit_test_inventory.json")
EXCLUDED_TEST_MODULES = {"test_api"}  # Script-style live API suite, not unittest.


def _iter_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _iter_tests(test)
        else:
            yield test


def get_unit_test_inventory():
    """Return every discoverable unittest module and its stable test IDs."""
    modules = sorted(
        path.stem for path in Path(__file__).parent.glob("test*.py")
        if path.stem not in EXCLUDED_TEST_MODULES
    )
    inventory = {}
    for module_name in modules:
        module = importlib.import_module(module_name)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        inventory[module_name] = sorted(test.id() for test in _iter_tests(suite))
    return inventory


class TestInventoryTests(unittest.TestCase):
    def test_unit_test_inventory_is_stable(self):
        from update_test_inventory import get_inventory_differences

        expected = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        actual = get_unit_test_inventory()

        self.assertNotIn("test_api", actual)
        differences = get_inventory_differences(expected, actual)
        self.assertEqual([], differences, "Unit test inventory drift:\n" + "\n".join(differences))

    def test_inventory_differences_are_compact_and_actionable(self):
        from update_test_inventory import get_inventory_differences

        differences = get_inventory_differences(
            {"test_example": ["test_example.Example.test_removed"]},
            {"test_example": ["test_example.Example.test_added"]},
        )
        self.assertEqual([
            "Missing from inventory: test_example.Example.test_added",
            "No longer discovered: test_example.Example.test_removed",
        ], differences)

    def test_inventory_writer_is_deterministic_and_check_is_read_only(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater

        inventory = {"test_z": ["z"], "test_a": ["a"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "inventory.json")
            with patch.object(updater, "get_unit_test_inventory", return_value=inventory):
                updater.write_inventory(path)
                first = path.read_bytes()
                self.assertLess(first.index(b'"test_a"'), first.index(b'"test_z"'))
                updater.write_inventory(path)
                self.assertEqual(first, path.read_bytes())
                self.assertEqual([], updater.check_inventory(path))
                self.assertEqual(first, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
