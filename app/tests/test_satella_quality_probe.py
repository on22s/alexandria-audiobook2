"""The Satella quality probe must isolate seed and adapter effects."""
import unittest

from experiments.satella_quality_probe import build_arms, make_html


class SatellaQualityProbeTests(unittest.TestCase):
    def test_arms_change_one_variable_at_a_time(self):
        arms = build_arms("satella", "control", 11, 22)
        shipped = arms["shipped_adapter_shipped_seed"]
        alternate = arms["shipped_adapter_alternate_seed"]
        control = arms["control_adapter_shipped_seed"]
        self.assertEqual(shipped["adapter_path"], alternate["adapter_path"])
        self.assertNotEqual(shipped["seed"], alternate["seed"])
        self.assertEqual(shipped["seed"], control["seed"])
        self.assertNotEqual(shipped["adapter_path"], control["adapter_path"])

    def test_public_page_does_not_name_concealed_arms(self):
        page = make_html({"sets": [], "source_sha256": "x"})
        for label in ("shipped_adapter", "control_adapter", "alternate_seed"):
            self.assertNotIn(label, page)

    def test_page_defines_quality_scale_and_requires_complete_sets(self):
        page = make_html({"sets": [], "source_sha256": "x"})
        self.assertIn("1 = badly broken or unpleasant", page)
        self.assertIn("rate all three clips", page)


if __name__ == "__main__":
    unittest.main()
