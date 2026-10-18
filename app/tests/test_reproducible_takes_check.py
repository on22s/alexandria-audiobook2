"""The #1329 live check's verdicts: each must be able to come out false."""
import unittest

from experiments.reproducible_takes_check import get_verdicts

PASS = {"on_take0_a": "a", "on_take0_b": "a", "on_take1_a": "b", "on_take1_b": "b",
        "off_a": "c", "off_b": "d"}


class VerdictTests(unittest.TestCase):
    def test_the_expected_pattern_passes_every_check(self):
        self.assertTrue(all(get_verdicts(PASS).values()))

    def test_each_failure_is_caught_by_its_own_check(self):
        cases = {"repeat_identical": {"on_take0_b": "z"},
                 "different_take_differs": {"on_take1_a": "a", "on_take1_b": "a"},
                 "take1_repeat_identical": {"on_take1_b": "z"},
                 "control_off_differs": {"off_b": "c"}}
        for name, change in cases.items():
            verdicts = get_verdicts({**PASS, **change})
            self.assertFalse(verdicts[name], name)
            self.assertEqual([name], [k for k, v in verdicts.items() if not v], name)


if __name__ == "__main__":
    unittest.main()
