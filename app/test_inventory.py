from pathlib import Path
import unittest


class TestInventoryTests(unittest.TestCase):
    def test_regression_case_inventory_is_stable(self):
        # Updated intentionally when tests are added/removed. Keeping this in a
        # separate module prevents a file-split mistake from silently passing.
        suite = unittest.defaultTestLoader.discover(
            str(Path(__file__).parent), pattern="test_*_regressions.py"
        )
        self.assertEqual(81, suite.countTestCases())


if __name__ == "__main__":
    unittest.main()
