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
        expected = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        actual = get_unit_test_inventory()

        self.assertNotIn("test_api", actual)
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
