"""Keep the architecture-pattern map tied to real project boundaries."""
import unittest
from pathlib import Path


REPO = Path(__file__).parent.parent.parent


class ArchitecturePatternDocumentationTests(unittest.TestCase):
    def test_pattern_map_names_existing_implementation_files(self):
        document = (REPO / "docs" / "ARCHITECTURE_PATTERNS.md").read_text(
            encoding="utf-8")
        for relative_path in (
            "app/llm_provider.py",
            "app/tts.py",
            "app/attribution_adapter.py",
            "app/attribution_prompt_variants.py",
            "app/generate_script.py",
            "app/review_script.py",
            "app/project.py",
            "app/core.py",
            "app/tests/test_attribution_adapter.py",
            "app/tests/test_profile_failover.py",
            "app/tests/test_llm_provider.py",
            "app/tests/test_multi_process_state.py",
            "app/tests/test_runtime_regressions.py",
        ):
            self.assertIn(relative_path, document)
            self.assertTrue((REPO / relative_path).is_file(), relative_path)

    def test_document_records_the_safety_invariants(self):
        document = (REPO / "docs" / "ARCHITECTURE_PATTERNS.md").read_text(
            encoding="utf-8")
        for phrase in (
            "claim_gpu_task",
            "unknown",
            "bounded and consistent",
            "Dynamic narrator",
            "flat-file storage",
        ):
            self.assertIn(phrase, document)


if __name__ == "__main__":
    unittest.main()
