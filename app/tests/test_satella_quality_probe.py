"""The Satella quality probe must isolate seed and adapter effects."""
import json
import os
import tempfile
import unittest

from experiments.satella_quality_probe import (TEXTS, build_arms, build_public,
                                                make_html)


class SatellaQualityProbeTests(unittest.TestCase):
    def test_expanded_probe_has_short_and_long_lines(self):
        self.assertGreaterEqual(len(TEXTS), 10)
        self.assertLess(min(map(len, TEXTS)), 15)
        self.assertGreater(max(map(len, TEXTS)), 140)

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

    def test_public_manifest_does_not_expose_arm_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            key = os.path.join(folder, "key.json")
            with open(key, "w", encoding="utf-8") as handle:
                json.dump({"arm_configuration": {"secret_adapter": "secret"}},
                          handle)
            public = build_public([], key, 7)
        encoded = json.dumps(public)
        self.assertNotIn("secret_adapter", encoded)
        self.assertNotIn("arm_configuration", encoded)
        self.assertEqual(64, len(public["concealed_key_sha256"]))

    def test_page_defines_quality_scale_and_requires_complete_sets(self):
        page = make_html({"sets": [], "source_sha256": "x"})
        self.assertIn("1 = badly broken or unpleasant", page)
        self.assertIn("rate all three clips", page)


if __name__ == "__main__":
    unittest.main()
