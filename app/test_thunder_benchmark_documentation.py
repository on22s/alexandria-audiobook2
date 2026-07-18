import json
import os
import unittest

from benchmark_core import get_stage_registry


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY_PATH = os.path.join(ROOT_DIR, "docs", "benchmarks", "thunder_2026-07-18.json")
GUIDE_PATH = os.path.join(ROOT_DIR, "THUNDER_COMPUTE.md")


class ThunderBenchmarkDocumentationTests(unittest.TestCase):
    def setUp(self):
        with open(SUMMARY_PATH, encoding="utf-8") as source:
            self.summary = json.load(source)
        with open(GUIDE_PATH, encoding="utf-8") as source:
            self.guide = source.read()

    def test_summary_covers_every_registered_stage(self):
        self.assertEqual(set(get_stage_registry()), set(self.summary["results"]))
        for result in self.summary["results"].values():
            self.assertTrue(result["measured_scope"])
            self.assertIn("local", result)
            self.assertIn("thunder", result)
            self.assertTrue(result["source_prs"])

    def test_guide_links_canonical_summary_and_preserves_scope_limits(self):
        self.assertIn("docs/benchmarks/thunder_2026-07-18.json", self.guide)
        self.assertIn("ASR phase only", self.summary["results"]["voicelab_preparer"]["measured_scope"])
        self.assertNotIn("Voice Lab — measured end to end", self.guide)

    def test_dataset_builder_is_not_silently_equated_to_voice_design(self):
        self.assertIn("batch orchestration", self.summary["results"]["dataset_builder"]["measured_scope"])
        self.assertIn("Dataset Builder", self.guide)


if __name__ == "__main__":
    unittest.main()
